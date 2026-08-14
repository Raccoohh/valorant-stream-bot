"""Twitch chat bot (twitchio 2.x).

The bot does two independent jobs:

1. Reacts to chat — the !entry / !clutch / !impact commands query the
   database directly (commands are on-demand reads; no event bus needed).
2. Reacts to the game — a background task subscribes to the Redis event
   channel and posts automatic alerts (aces, clutches, match results).

Note on DI: FastAPI's Depends() only works inside request handlers. A
chat command is not an HTTP request, so here we use the session factory
directly — same pattern, just invoked manually.
"""

import asyncio
import logging

from twitchio.ext import commands

from app.core.config import get_settings
from app.core.database import async_session_factory
from app.core.redis import get_redis
from app.services import stats
from app.services.events import EventType, StreamEvent, subscribe_events

logger = logging.getLogger(__name__)
settings = get_settings()


class ValorantBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            token=settings.twitch_token,
            prefix=settings.twitch_prefix,
            initial_channels=[settings.twitch_channel],
        )
        self._redis_listener_task: asyncio.Task | None = None

    async def event_ready(self) -> None:
        logger.info("Twitch bot connected as %s", self.nick)
        if self._redis_listener_task is None:
            self._redis_listener_task = asyncio.create_task(self._listen_for_events())

    # ------------------------------------------------------------------
    # Redis -> chat alerts
    # ------------------------------------------------------------------
    async def _listen_for_events(self) -> None:
        redis = get_redis()
        async for event in subscribe_events(redis):
            try:
                await self._announce(event)
            except Exception:
                logger.exception("Failed to announce event %s", event.type)

    async def _announce(self, event: StreamEvent) -> None:
        channel = self.get_channel(settings.twitch_channel)
        if channel is None:
            return  # not in channel yet; event is still delivered to the overlay

        match event.type:
            case EventType.ACE:
                agent = event.payload.get("agent", "")
                await channel.send(
                    f"💥 ACE ALERT! {settings.twitch_channel} just wiped the whole "
                    f"enemy team on {event.payload.get('map', '')}"
                    f"{f' playing {agent}' if agent else ''}! no talent? cap."
                )
            case EventType.CLUTCH:
                situation = event.payload.get("situation", "1vX")
                await channel.send(
                    f"🔥 CLUTCH OR KICK! {settings.twitch_channel} just won a "
                    f"{situation} on {event.payload.get('map', '')}!"
                )
            case EventType.NEW_MATCH:
                p = event.payload
                await channel.send(
                    f"📊 Match on {p.get('map', '?')} finished: "
                    f"{p.get('kills', 0)}/{p.get('deaths', 0)}/{p.get('assists', 0)} "
                    f"on {p.get('agent', '?')} | "
                    f"FB: {p.get('first_bloods', 0)} | ACS: {p.get('score', 0)}"
                )
            case _:
                pass  # SESSION_* etc. are overlay-only for now

    # ------------------------------------------------------------------
    # Chat commands (session-scoped DB reads)
    # ------------------------------------------------------------------
    @commands.command(name="entry")
    async def entry_cmd(self, ctx: commands.Context) -> None:
        async with async_session_factory() as db:
            session = await stats.get_active_session(db)
            if session is None:
                await ctx.send("No active stream session — stats start when the stream does! 📴")
                return
            totals = await stats.get_session_totals(db, session.id)

        ratio = stats.entry_ratio(totals)
        await ctx.send(
            f"🎯 Entry stats this stream: {totals.first_bloods} first bloods / "
            f"{totals.first_deaths} first deaths (ratio {ratio}) "
            f"across {totals.matches} match(es)"
        )

    @commands.command(name="clutch")
    async def clutch_cmd(self, ctx: commands.Context) -> None:
        async with async_session_factory() as db:
            session = await stats.get_active_session(db)
            if session is None:
                await ctx.send("No active stream session — stats start when the stream does! 📴")
                return
            totals = await stats.get_session_totals(db, session.id)

        await ctx.send(
            f"🧊 {settings.twitch_channel} has won {totals.clutches_won} "
            f"clutch(es) this stream. Ice in the veins."
        )

    @commands.command(name="impact")
    async def impact_cmd(self, ctx: commands.Context) -> None:
        async with async_session_factory() as db:
            session = await stats.get_active_session(db)
            if session is None:
                await ctx.send("No active stream session — stats start when the stream does! 📴")
                return
            totals = await stats.get_session_totals(db, session.id)

        score = stats.impact_score(totals)
        await ctx.send(
            f"⚡ Impact rating this stream: {score} "
            f"(FB {totals.first_bloods} | clutches {totals.clutches_won} | "
            f"aces {totals.aces} | avg ACS {totals.avg_acs})"
        )
