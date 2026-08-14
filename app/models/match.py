from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


class MatchRecord(SQLModel, table=True):
    """Per-match stats for YOUR player only, linked to a stream session.

    `match_id` is the HenrikDev match UUID — it doubles as our
    idempotency key so the poller can never insert the same match twice.
    """

    __tablename__ = "match_records"

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="stream_sessions.id", index=True)
    match_id: str = Field(unique=True, index=True)

    map_name: str
    agent: str = ""
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    first_bloods: int = 0      # Duelist entry metric — times YOU got the round's first kill
    first_deaths: int = 0      # times YOU were the round's first death
    clutches_won: int = 0      # 1vX situations won (X >= 1)
    aces: int = 0
    score: int = 0             # ACS — average combat score, for !impact

    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
