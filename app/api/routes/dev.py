"""Dev-only event simulator.

Publishes synthetic events to Redis with payloads that EXACTLY mirror
what ingestion/poller.py publishes for real matches, so the Twitch bot
and the OBS overlay cannot tell the difference. This lets you test the
full pipeline (Redis -> chat alert -> WebSocket -> GSAP animation)
without playing a single match.

Safety: this router is only mounted when ENABLE_DEV_ROUTES=true (see
main.py). If it is somehow mounted anyway, every endpoint refuses to
run when the flag is off — defense in depth.
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.redis import get_redis
from app.services.events import EventType, StreamEvent, publish_event

settings = get_settings()
router = APIRouter(prefix="/dev", tags=["dev"])


class MatchSimulation(BaseModel):
    """Field names match _extract_player_stats() output -> NEW_MATCH payload."""

    map: str = "Ascent"
    agent: str = "Jett"
    kills: int = Field(default=27, ge=0)
    deaths: int = Field(default=14, ge=0)
    assists: int = Field(default=6, ge=0)
    first_bloods: int = Field(default=5, ge=0)
    first_deaths: int = Field(default=3, ge=0)
    score: int = Field(default=312, ge=0)  # ACS


class AceSimulation(BaseModel):
    """Mirrors the ACE payload published in _process_new_match()."""

    map: str = "Ascent"
    agent: str = "Jett"
    count: int = Field(default=1, ge=1)


class ClutchSimulation(BaseModel):
    """Mirrors the CLUTCH payload published in _process_new_match()."""

    map: str = "Ascent"
    situation: str = "1v3"
    count: int = Field(default=1, ge=1)


def _guard() -> None:
    if not settings.enable_dev_routes:
        raise HTTPException(status_code=404, detail="Not found.")


@router.post("/simulate/match")
async def simulate_match(body: MatchSimulation | None = None) -> dict:
    """Publish one synthetic NEW_MATCH event (payload identical to the poller's)."""
    _guard()
    sim = body or MatchSimulation()
    await publish_event(
        get_redis(),
        StreamEvent(type=EventType.NEW_MATCH, payload=sim.model_dump()),
    )
    return {"published": "new_match", "payload": sim.model_dump()}


@router.post("/simulate/ace")
async def simulate_ace(body: AceSimulation | None = None) -> dict:
    """Publish one synthetic ACE event (payload identical to the poller's)."""
    _guard()
    sim = body or AceSimulation()
    await publish_event(
        get_redis(),
        StreamEvent(type=EventType.ACE, payload=sim.model_dump()),
    )
    return {"published": "ace", "payload": sim.model_dump()}


@router.post("/simulate/clutch")
async def simulate_clutch(body: ClutchSimulation | None = None) -> dict:
    """Publish one synthetic CLUTCH event (payload identical to the poller's)."""
    _guard()
    sim = body or ClutchSimulation()
    await publish_event(
        get_redis(),
        StreamEvent(type=EventType.CLUTCH, payload=sim.model_dump()),
    )
    return {"published": "clutch", "payload": sim.model_dump()}


@router.post("/simulate/burst")
async def simulate_burst(
    event: Literal["ace", "clutch", "match"] = "ace",
    count: int = 3,
    gap_ms: int = 0,
) -> dict:
    """Fire `count` events back-to-back to stress-test the overlay queue.

    With gap_ms=0 the events hit the WebSocket practically simultaneously —
    the overlay must still play them strictly sequentially. Use this to
    verify the event queue never overlaps GSAP animations.
    """
    _guard()
    if not 1 <= count <= 10:
        raise HTTPException(status_code=422, detail="count must be between 1 and 10.")

    redis = get_redis()
    for i in range(count):
        match event:
            case "ace":
                payload = AceSimulation(count=i + 1).model_dump()
                etype = EventType.ACE
            case "clutch":
                payload = ClutchSimulation(situation=f"1v{3 + i}").model_dump()
                etype = EventType.CLUTCH
            case "match":
                payload = MatchSimulation(kills=20 + i, score=250 + i * 10).model_dump()
                etype = EventType.NEW_MATCH
        await publish_event(redis, StreamEvent(type=etype, payload=payload))
        if gap_ms:
            await asyncio.sleep(gap_ms / 1000)

    return {"published": f"{event} x{count}", "gap_ms": gap_ms}
