"""Stream session lifecycle endpoints.

These let you (and later, a Twitch "stream online" webhook) mark when
a broadcast starts and ends. Everything the bot reports is scoped to
the active session.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.core.redis import get_redis
from app.services.events import EventType, StreamEvent, publish_event
from app.services import stats

router = APIRouter(prefix="/api/session", tags=["session"])


@router.post("/start")
async def start_stream(db: AsyncSession = Depends(get_db_session)) -> dict:
    session = await stats.start_session(db)
    await publish_event(
        get_redis(),
        StreamEvent(type=EventType.SESSION_STARTED, payload={"session_id": session.id}),
    )
    return {"session_id": session.id, "start_time": session.start_time}


@router.post("/end")
async def end_stream(db: AsyncSession = Depends(get_db_session)) -> dict:
    session = await stats.end_session(db)
    if session is None:
        raise HTTPException(status_code=404, detail="No active stream session.")
    await publish_event(
        get_redis(),
        StreamEvent(type=EventType.SESSION_ENDED, payload={"session_id": session.id}),
    )
    return {"session_id": session.id, "end_time": session.end_time}


@router.get("/current")
async def current_stream(db: AsyncSession = Depends(get_db_session)) -> dict:
    session = await stats.get_active_session(db)
    if session is None:
        return {"active": False}
    totals = await stats.get_session_totals(db, session.id)
    return {
        "active": True,
        "session_id": session.id,
        "start_time": session.start_time,
        "totals": totals.__dict__,
        "entry_ratio": stats.entry_ratio(totals),
        "impact": stats.impact_score(totals),
    }
