"""Session-scoped statistics.

This module is the single source of truth for every number that the
Twitch bot, the REST API, and the overlay will ever display. All
queries are scoped to the *active* StreamSession, so a new stream
always starts with a clean slate.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.match import MatchRecord
from app.models.session import StreamSession


@dataclass(frozen=True)
class SessionTotals:
    matches: int
    kills: int
    deaths: int
    assists: int
    first_bloods: int
    first_deaths: int
    clutches_won: int
    aces: int
    avg_acs: float


async def get_active_session(db: AsyncSession) -> StreamSession | None:
    result = await db.execute(
        select(StreamSession).where(StreamSession.is_active.is_(True)).limit(1)
    )
    return result.scalar_one_or_none()


async def start_session(db: AsyncSession) -> StreamSession:
    """Begin a new stream session. Ends any dangling active one first."""
    existing = await get_active_session(db)
    if existing is not None:
        existing.is_active = False
        existing.end_time = datetime.now(timezone.utc)
    session = StreamSession()
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def end_session(db: AsyncSession) -> StreamSession | None:
    session = await get_active_session(db)
    if session is None:
        return None
    session.is_active = False
    session.end_time = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(session)
    return session


async def get_session_totals(db: AsyncSession, session_id: int) -> SessionTotals:
    """Aggregate every MatchRecord belonging to one session."""
    result = await db.execute(
        select(
            func.count(MatchRecord.id),
            func.coalesce(func.sum(MatchRecord.kills), 0),
            func.coalesce(func.sum(MatchRecord.deaths), 0),
            func.coalesce(func.sum(MatchRecord.assists), 0),
            func.coalesce(func.sum(MatchRecord.first_bloods), 0),
            func.coalesce(func.sum(MatchRecord.first_deaths), 0),
            func.coalesce(func.sum(MatchRecord.clutches_won), 0),
            func.coalesce(func.sum(MatchRecord.aces), 0),
            func.coalesce(func.avg(MatchRecord.score), 0.0),
        ).where(MatchRecord.session_id == session_id)
    )
    row = result.one()
    return SessionTotals(
        matches=row[0],
        kills=row[1],
        deaths=row[2],
        assists=row[3],
        first_bloods=row[4],
        first_deaths=row[5],
        clutches_won=row[6],
        aces=row[7],
        avg_acs=round(float(row[8]), 1),
    )


def entry_ratio(totals: SessionTotals) -> float:
    """First Blood : First Death ratio — the core Duelist entry stat.

    Convention used by the Valorant community: if you have first bloods
    but zero first deaths, the ratio is just your first blood count.
    """
    if totals.first_deaths == 0:
        return float(totals.first_bloods)
    return round(totals.first_bloods / totals.first_deaths, 2)


def impact_score(totals: SessionTotals) -> float:
    """Custom impact metric.

    Formula v1 (tune freely — keep it documented):
        impact = (FB * 2 + clutches * 3 + aces * 5) / matches + ACS / 100

    Rationale: entries, clutches, and aces are the round-swinging plays
    a Duelist is paid to deliver; ACS is the baseline contribution.
    Normalizing by match count keeps streams of different lengths
    comparable.
    """
    if totals.matches == 0:
        return 0.0
    playmaking = (
        totals.first_bloods * 2 + totals.clutches_won * 3 + totals.aces * 5
    ) / totals.matches
    return round(playmaking + totals.avg_acs / 100, 2)
