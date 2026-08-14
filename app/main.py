"""Application entry point.

One process, one asyncio event loop, four long-lived residents:

    1. Uvicorn serving FastAPI (HTTP + WebSocket)
    2. The twitchio bot          (started as a background task)
    3. The HenrikDev poller      (started as a background task)
    4. The Redis -> overlay fan-out (started as a background task)

FastAPI's lifespan context manager is the orchestration point: startup
runs before the server accepts traffic, shutdown runs after it stops.
Everything is cooperative async — no threads, no subprocesses, nothing
blocks the loop.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI

from app.api.routes import health, sessions, websocket
from app.core.config import get_settings
from app.core.database import dispose_engine, init_db
from app.core.redis import close_redis, get_redis, init_redis
from app.ingestion.poller import run_poller
from app.services.events import subscribe_events

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()


async def _ws_fanout() -> None:
    """Forward every Redis event to all connected OBS overlays."""
    redis = get_redis()
    async for event in subscribe_events(redis):
        await websocket.manager.broadcast(event)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # ---- Startup ----
    await init_db()
    await init_redis()

    background_tasks: list[asyncio.Task] = [
        asyncio.create_task(run_poller(), name="poller"),
        asyncio.create_task(_ws_fanout(), name="ws-fanout"),
    ]

    bot = None
    if settings.twitch_token:
        # Imported lazily so the app can boot without twitchio credentials.
        from app.bot.twitch_bot import ValorantBot

        bot = ValorantBot()
        # bot.start() is a coroutine that blocks until the bot disconnects —
        # wrapping it in create_task lets it share the loop with Uvicorn.
        background_tasks.append(
            asyncio.create_task(bot.start(), name="twitch-bot")
        )
        logger.info("Twitch bot task started for #%s", settings.twitch_channel)
    else:
        logger.warning("TWITCH_TOKEN not set — bot disabled, API/overlay only")

    if settings.twitch_client_id and settings.twitch_client_secret:
        # Auto start/stop StreamSessions when the channel goes live/offline.
        from app.ingestion.stream_watcher import run_stream_watcher

        background_tasks.append(
            asyncio.create_task(run_stream_watcher(), name="stream-watcher")
        )
        logger.info("Stream watcher task started for #%s", settings.twitch_channel)
    else:
        logger.warning(
            "TWITCH_CLIENT_ID/SECRET not set — sessions must be managed "
            "manually via POST /api/session/start|end"
        )

    yield  # <--- the application serves traffic here

    # ---- Shutdown ----
    for task in background_tasks:
        task.cancel()
    for task in background_tasks:
        with suppress(asyncio.CancelledError):
            await task
    if bot is not None:
        await bot.close()
    await close_redis()
    await dispose_engine()
    logger.info("Shutdown complete")


app = FastAPI(title="Valorant Live Stream Analytics Engine", lifespan=lifespan)
app.include_router(health.router)
app.include_router(sessions.router)
app.include_router(websocket.router)

# Dev-only simulator — mounted only when explicitly enabled, and every
# endpoint re-checks the flag at request time (defense in depth).
if settings.enable_dev_routes:
    from app.api.routes import dev

    app.include_router(dev.router)
    logger.warning("DEV ROUTES ENABLED at /dev — disable ENABLE_DEV_ROUTES in production")

# Serve the OBS overlay as static files. Browser Source URL:
#   http://localhost:8000/overlay/   (or http://<host-ip>:8000/overlay/)
from pathlib import Path  # noqa: E402

from fastapi.staticfiles import StaticFiles  # noqa: E402

OVERLAY_DIR = Path(__file__).resolve().parent.parent / "overlay"
app.mount("/overlay", StaticFiles(directory=OVERLAY_DIR, html=True), name="overlay")


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )
