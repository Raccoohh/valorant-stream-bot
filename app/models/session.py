from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


class StreamSession(SQLModel, table=True):
    """One row per live stream.

    `is_active` is the isolation boundary: every stats command scopes
    its queries to the session where is_active == True, so !entry and
    !clutch only ever reflect the current broadcast.
    """

    __tablename__ = "stream_sessions"

    id: int | None = Field(default=None, primary_key=True)
    start_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: datetime | None = Field(default=None)
    is_active: bool = Field(default=True, index=True)
