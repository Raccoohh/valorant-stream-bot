"""Event bus primitives — the single contract between all subsystems.

Everything that wants to announce something (poller, HTTP routes,
future webhook handlers) publishes a StreamEvent to Redis. Everything
that wants to react (Twitch bot, WebSocket fanout) subscribes. The
two sides never import each other.
"""

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.core.config import get_settings

settings = get_settings()


class EventType(StrEnum):
    NEW_MATCH = "new_match"
    ACE = "ace"
    CLUTCH = "clutch"          # payload should include "situation" e.g. "1v3"
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    COMMAND_IMPACT = "command_impact"  # so the overlay can react to !impact


class StreamEvent(BaseModel):
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)


async def publish_event(redis: Redis, event: StreamEvent) -> None:
    """Serialize and publish an event to the shared channel."""
    await redis.publish(settings.redis_event_channel, event.model_dump_json())


async def subscribe_events(redis: Redis):
    """Async generator yielding StreamEvents as they arrive.

    Uses a dedicated pubsub connection so the shared client's
    connection pool is never blocked by a long-lived subscription.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe(settings.redis_event_channel)
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                yield StreamEvent.model_validate(json.loads(message["data"]))
            except (json.JSONDecodeError, ValueError):
                # A malformed event must never kill the listener loop.
                continue
    finally:
        await pubsub.unsubscribe(settings.redis_event_channel)
        await pubsub.aclose()
