"""Stream online/offline watcher — the local-friendly EventSub alternative.

WHY POLLING INSTEAD OF EVENTSUB:
  - EventSub HTTP webhooks need a public callback URL (ngrok/tunnel) —
    that breaks the zero-config localhost-alongside-OBS deployment.
  - EventSub over WebSocket is only cleanly supported in twitchio 3.x;
    we are pinned to 2.10 for its stable commands.Bot API.
  - Helix `Get Streams` + a client-credentials app token is one small
    GET per minute on httpx (already in the stack). No new deps, no
    tunnels, no extra sockets.

WHAT IT DOES:
  Polls Twitch every TWITCH_POLL_INTERVAL_SECONDS. On a confirmed
  offline -> live transition it starts a StreamSession (and publishes
  SESSION_STARTED); on live -> offline it ends it (SESSION_ENDED).

ROBUSTNESS DETAILS:
  - App access tokens are cached and auto-refreshed on 401 or expiry.
  - Transitions require TWO consecutive identical readings (debounce):
    a single flaky API response, or Twitch's brief "reconnect
    protection" window after a dropped stream, can never start/stop a
    session spuriously. Cost: up to one extra poll of detection latency.
  - On startup it reconciles: if the app restarted mid-stream, the first
    poll realigns the DB session with reality.
"""

import asyncio
import logging
import time

import httpx

from app.core.config import get_settings
from app.core.database import async_session_factory
from app.core.redis import get_redis
from app.services import stats
from app.services.events import EventType, StreamEvent, publish_event

logger = logging.getLogger(__name__)
settings = get_settings()

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"


class TwitchAppAuth:
    """Client-credentials (app access) token with automatic refresh.

    App tokens live for ~60 days, but we never trust the clock blindly:
    a 401 from Helix forces exactly one refresh-and-retry before the
    error is allowed to propagate to the watcher's cycle-level handler.
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: float = 0.0

    async def get_token(self, client: httpx.AsyncClient, *, force_refresh: bool = False) -> str:
        if self._token and not force_refresh and time.time() < self._expires_at - 60:
            return self._token
        response = await client.post(
            TOKEN_URL,
            params={
                "client_id": settings.twitch_client_id,
                "client_secret": settings.twitch_client_secret,
                "grant_type": "client_credentials",
            },
        )
        response.raise_for_status()
        body = response.json()
        self._token = body["access_token"]
        self._expires_at = time.time() + body.get("expires_in", 3600)
        logger.info("Twitch app access token refreshed (valid for %ss)", body.get("expires_in"))
        return self._token


async def is_channel_live(client: httpx.AsyncClient, auth: TwitchAppAuth) -> bool:
    """True if the configured channel is currently broadcasting."""
    for attempt in range(2):  # initial try + one token-refresh retry
        token = await auth.get_token(client, force_refresh=attempt > 0)
        response = await client.get(
            STREAMS_URL,
            params={"user_login": settings.twitch_channel},
            headers={"Client-Id": settings.twitch_client_id, "Authorization": f"Bearer {token}"},
        )
        if response.status_code == 401 and attempt == 0:
            logger.warning("Helix rejected app token — forcing refresh and retrying once")
            continue
        response.raise_for_status()
        return bool(response.json().get("data"))
    return False  # unreachable in practice; satisfies the type checker


async def _apply_state(is_live: bool) -> None:
    """Reconcile the DB session with the observed stream state."""
    redis = get_redis()
    async with async_session_factory() as db:
        if is_live:
            session = await stats.start_session(db)
            await publish_event(redis, StreamEvent(
                type=EventType.SESSION_STARTED, payload={"session_id": session.id},
            ))
            logger.info("Stream is LIVE — session %s started", session.id)
        else:
            session = await stats.end_session(db)
            if session is not None:
                await publish_event(redis, StreamEvent(
                    type=EventType.SESSION_ENDED, payload={"session_id": session.id},
                ))
                logger.info("Stream is OFFLINE — session %s ended", session.id)


async def run_stream_watcher() -> None:
    """Continuous watch loop. Runs forever as a background task."""
    logger.info(
        "Stream watcher started: #%s every %ss (2-reading debounce)",
        settings.twitch_channel, settings.twitch_poll_interval_seconds,
    )
    auth = TwitchAppAuth()
    confirmed_state: bool | None = None  # what the DB currently reflects
    pending_state: bool | None = None    # first sighting of a different state

    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            try:
                live = await is_channel_live(client, auth)

                if confirmed_state is None:
                    # First observation after boot — reconcile immediately:
                    # starts a session if live, closes a dangling one if not.
                    await _apply_state(live)
                    confirmed_state = live
                    pending_state = None
                elif live == confirmed_state:
                    pending_state = None  # steady state; cancel any pending transition
                elif live == pending_state:
                    # Second consecutive differing reading -> confirmed transition.
                    await _apply_state(live)
                    confirmed_state = live
                    pending_state = None
                else:
                    pending_state = live  # first sighting; wait for confirmation

            except httpx.HTTPError as exc:
                logger.warning("Stream watcher HTTP error: %s", exc)
            except Exception:
                logger.exception("Stream watcher cycle failed unexpectedly")

            await asyncio.sleep(settings.twitch_poll_interval_seconds)
