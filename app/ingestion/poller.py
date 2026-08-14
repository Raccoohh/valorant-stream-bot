"""HenrikDev v4 poller — the data ingestion loop.

Every POLL_INTERVAL_SECONDS we fetch your recent matches from the v4
endpoint, compare match IDs against what we already stored, and for
each *new* match:
  1. persist a MatchRecord linked to the active StreamSession, and
  2. publish events (new_match / ace / clutch) to Redis.

WHERE THE STATS COME FROM (v4):
  - K/D/A, agent, map, score come from the per-player stats block.
  - first_bloods / first_deaths are derived from the match kill feed:
    the earliest kill event in each round decides both.
  - aces: rounds where you killed 5+ DISTINCT enemy players.
  - clutches: rounds where every teammate died while >=1 enemy was
    still alive, and your team went on to win the round. The situation
    ("1v3") is the enemy count at the moment you became the last one
    standing. A CLUTCH alert is only published for 1v3+ (per spec);
    smaller clutches still count toward !clutch.

ROBUSTNESS: cancelled/remade matches often ship with missing or
partial round data. Every round-derived stat degrades to 0 instead of
crashing, and a totally malformed match is skipped — the poller loop
itself is unkillable by bad payloads.

RATE LIMITING: v4 responses are heavy and HenrikDev answers overload
with 429. _fetch_recent_matches retries with exponential backoff
(honoring the Retry-After header when present) before giving up the
cycle — it never hammers the API and never brings the loop down.

Idempotency: MatchRecord.match_id has a UNIQUE constraint, and we check
before inserting — the poller can crash, restart, or double-fire and the
database stays correct.
"""

import asyncio
import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import async_session_factory
from app.core.redis import get_redis
from app.models.match import MatchRecord
from app.services import stats
from app.services.events import EventType, StreamEvent, publish_event

logger = logging.getLogger(__name__)
settings = get_settings()

# A clutch only triggers a chat/overlay alert at this size or bigger.
# Smaller clutches still count toward session stats.
CLUTCH_ALERT_THRESHOLD = 3


# ----------------------------------------------------------------------
# Payload helpers — every one of them tolerates garbage input
# ----------------------------------------------------------------------
def _player_id(obj: Any) -> str | None:
    """Canonical 'name#tag' identifier, or None for malformed entries."""
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    if not isinstance(name, str) or not name:
        return None
    tag = obj.get("tag")
    return f"{name.lower()}#{str(tag).lower() if tag else ''}"


def _my_player_id() -> str:
    return f"{settings.valorant_name.lower()}#{settings.valorant_tag.lower()}"


def _analyze_rounds(match: dict[str, Any], me: dict[str, Any]) -> dict[str, Any]:
    """Derive first bloods/deaths, aces, and clutches from the kill feed.

    Returns zeros for anything it cannot compute. This function must
    NEVER raise — malformed matches are a normal occurrence.
    """
    derived: dict[str, Any] = {
        "first_bloods": 0,
        "first_deaths": 0,
        "aces": 0,
        "clutches_won": 0,
        "big_clutches": 0,               # clutches >= CLUTCH_ALERT_THRESHOLD
        "best_clutch_situation": None,   # e.g. "1v4"
    }

    kill_feed = match.get("kills")
    players = match.get("players")
    if not isinstance(kill_feed, list) or not isinstance(players, list) or not players:
        return derived

    my_id = _my_player_id()
    my_team = me.get("team_id") or me.get("team")

    # Roster: player id -> team. Players without a team are ignored for
    # clutch math but still count for first bloods / aces.
    roster: dict[str, str | None] = {}
    for p in players:
        pid = _player_id(p)
        if pid:
            roster[pid] = p.get("team_id") or p.get("team") if isinstance(p, dict) else None

    # Round winners (round numbers are 1-based; list index + 1).
    winner_by_round: dict[int, str] = {}
    rounds = match.get("rounds")
    if isinstance(rounds, list):
        for idx, r in enumerate(rounds, start=1):
            if isinstance(r, dict) and isinstance(r.get("winning_team"), str):
                winner_by_round[idx] = r["winning_team"]

    # Group kill events by round, dropping malformed events individually.
    events_by_round: dict[int, list[dict[str, Any]]] = {}
    for ev in kill_feed:
        if not isinstance(ev, dict) or not isinstance(ev.get("round"), int):
            continue
        events_by_round.setdefault(ev["round"], []).append(ev)

    for rnd, events in events_by_round.items():
        events.sort(key=lambda e: e.get("time_in_round_in_ms") or 0)
        if not events:
            continue

        # --- First blood / first death: earliest kill of the round ---
        first = events[0]
        if _player_id(first.get("killer")) == my_id:
            derived["first_bloods"] += 1
        if _player_id(first.get("victim")) == my_id:
            derived["first_deaths"] += 1

        # --- Ace: 5+ distinct enemy victims credited to me this round ---
        my_victims = {
            vid
            for ev in events
            if _player_id(ev.get("killer")) == my_id
            for vid in [_player_id(ev.get("victim"))]
            if vid
        }
        if len(my_victims) >= 5:
            derived["aces"] += 1

        # --- Clutch: last one alive on my team, round still won ---
        # Requires trustworthy team data; skip otherwise.
        if not my_team or my_id not in roster:
            continue
        teammates = [pid for pid, t in roster.items() if t == my_team and pid != my_id]
        enemies = [pid for pid, t in roster.items() if t and t != my_team]
        if not teammates or not enemies:
            continue

        alive: set[str] = set(roster)
        became_last_alive = False
        max_enemies_when_last = 0
        for ev in events:
            victim = _player_id(ev.get("victim"))
            if victim:
                alive.discard(victim)
            if my_id in alive and not any(t in alive for t in teammates):
                became_last_alive = True
                max_enemies_when_last = max(
                    max_enemies_when_last, sum(1 for e in enemies if e in alive)
                )

        won_round = winner_by_round.get(rnd) == my_team
        if became_last_alive and won_round and max_enemies_when_last >= 1:
            derived["clutches_won"] += 1
            if max_enemies_when_last >= CLUTCH_ALERT_THRESHOLD:
                derived["big_clutches"] += 1
                situation = f"1v{max_enemies_when_last}"
                best = derived["best_clutch_situation"]
                if best is None or max_enemies_when_last > int(best[2:]):
                    derived["best_clutch_situation"] = situation

    return derived


def _extract_player_stats(match: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Pull YOUR player's numbers out of one v4 match payload.

    Returns (model_fields, alert_metadata), or None if you're not in
    this match or the payload is unusable. Never raises.
    """
    try:
        players = match.get("players")
        if not isinstance(players, list):
            return None

        my_id = _my_player_id()
        me = next((p for p in players if _player_id(p) == my_id), None)
        if me is None:
            return None

        metadata = match.get("metadata")
        if not isinstance(metadata, dict):
            return None
        match_id = metadata.get("match_id")
        if not match_id:
            return None

        map_raw = metadata.get("map")
        map_name = (
            map_raw.get("name", "Unknown") if isinstance(map_raw, dict)
            else str(map_raw) if map_raw else "Unknown"
        )

        agent_raw = me.get("agent")
        agent = (
            agent_raw.get("name", "") if isinstance(agent_raw, dict)
            else str(agent_raw) if agent_raw else ""
        )

        pstats = me.get("stats") if isinstance(me.get("stats"), dict) else {}
        kills = pstats.get("kills", 0) or 0
        deaths = pstats.get("deaths", 0) or 0
        assists = pstats.get("assists", 0) or 0
        score_total = pstats.get("score", 0) or 0

        rounds_played = (
            len(match.get("rounds"))
            if isinstance(match.get("rounds"), list) and match.get("rounds")
            else pstats.get("rounds_played", 0) or 0
        )
        acs = round(score_total / rounds_played) if rounds_played else 0

        derived = _analyze_rounds(match, me)

        model_fields = {
            "match_id": match_id,
            "map_name": map_name,
            "agent": agent,
            "kills": kills,
            "deaths": deaths,
            "assists": assists,
            "first_bloods": derived["first_bloods"],
            "first_deaths": derived["first_deaths"],
            "clutches_won": derived["clutches_won"],
            "aces": derived["aces"],
            "score": acs,
        }
        alert_metadata = {
            "big_clutches": derived["big_clutches"],
            "best_clutch_situation": derived["best_clutch_situation"],
        }
        return model_fields, alert_metadata
    except Exception:
        logger.exception("Failed to parse match payload — skipping match")
        return None


# ----------------------------------------------------------------------
# Fetching with 429-aware exponential backoff
# ----------------------------------------------------------------------
def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Backoff delay for a 429: honor Retry-After, else exponential."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), settings.henrik_retry_max_seconds)
        except ValueError:
            pass  # malformed header -> fall through to exponential
    return min(
        settings.henrik_retry_base_seconds * (2 ** attempt),
        settings.henrik_retry_max_seconds,
    )


async def _fetch_recent_matches(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    url = (
        f"{settings.henrik_base_url}/valorant/v4/matches/"
        f"{settings.valorant_region}/{settings.valorant_platform}/"
        f"{settings.valorant_name}/{settings.valorant_tag}"
    )
    headers = {"Authorization": settings.henrik_api_key} if settings.henrik_api_key else {}
    params: dict[str, Any] = {"size": 5}
    if settings.henrik_mode:
        params["mode"] = settings.henrik_mode

    for attempt in range(settings.henrik_max_retries + 1):
        response = await client.get(url, headers=headers, params=params)

        if response.status_code == 429:
            delay = _retry_delay(response, attempt)
            logger.warning(
                "HenrikDev rate limit (429) — backing off %.0fs (attempt %d/%d)",
                delay, attempt + 1, settings.henrik_max_retries,
            )
            await asyncio.sleep(delay)
            continue

        if response.status_code in (401, 403):
            logger.error(
                "HenrikDev auth failed (%s) — v4 requires HENRIK_API_KEY. Skipping cycle.",
                response.status_code,
            )
            return []

        response.raise_for_status()
        body = response.json()
        if body.get("status") != 200:
            logger.warning("HenrikDev returned status %s", body.get("status"))
            return []
        data = body.get("data")
        return data if isinstance(data, list) else []

    logger.error(
        "Rate limit persisted after %d retries — skipping this cycle",
        settings.henrik_max_retries,
    )
    return []


# ----------------------------------------------------------------------
# Ingestion
# ----------------------------------------------------------------------
async def _match_exists(db: AsyncSession, match_id: str) -> bool:
    result = await db.execute(
        select(MatchRecord.id).where(MatchRecord.match_id == match_id).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _process_new_match(
    db: AsyncSession,
    session_id: int,
    model_fields: dict[str, Any],
    alert_metadata: dict[str, Any],
) -> None:
    record = MatchRecord(session_id=session_id, **model_fields)
    db.add(record)
    await db.commit()
    logger.info(
        "New match stored: %s on %s (%s/%s/%s, FB %s, aces %s, clutches %s)",
        model_fields["map_name"], model_fields["agent"],
        model_fields["kills"], model_fields["deaths"], model_fields["assists"],
        model_fields["first_bloods"], model_fields["aces"],
        model_fields["clutches_won"],
    )

    redis = get_redis()
    await publish_event(redis, StreamEvent(type=EventType.NEW_MATCH, payload={
        "map": model_fields["map_name"],
        "agent": model_fields["agent"],
        "kills": model_fields["kills"],
        "deaths": model_fields["deaths"],
        "assists": model_fields["assists"],
        "first_bloods": model_fields["first_bloods"],
        "first_deaths": model_fields["first_deaths"],
        "score": model_fields["score"],
    }))
    if model_fields["aces"] > 0:
        await publish_event(redis, StreamEvent(type=EventType.ACE, payload={
            "map": model_fields["map_name"],
            "agent": model_fields["agent"],
            "count": model_fields["aces"],
        }))
    if alert_metadata["big_clutches"] > 0:
        await publish_event(redis, StreamEvent(type=EventType.CLUTCH, payload={
            "map": model_fields["map_name"],
            "situation": alert_metadata["best_clutch_situation"] or "1v3+",
            "count": alert_metadata["big_clutches"],
        }))


async def poll_once(client: httpx.AsyncClient) -> int:
    """One polling cycle. Returns how many new matches were ingested."""
    matches = await _fetch_recent_matches(client)
    ingested = 0
    async with async_session_factory() as db:
        session = await stats.get_active_session(db)
        if session is None:
            # No live stream -> ingest nothing. Stats only count live hours.
            return 0
        for match in matches:
            if not isinstance(match, dict):
                continue
            extracted = _extract_player_stats(match)
            if extracted is None:
                continue
            model_fields, alert_metadata = extracted
            if await _match_exists(db, model_fields["match_id"]):
                continue
            await _process_new_match(db, session.id, model_fields, alert_metadata)
            ingested += 1
    return ingested


async def run_poller() -> None:
    """The continuous ingestion loop. Runs forever as a background task.

    Any per-cycle failure is logged and retried on the next tick —
    the loop itself must never die.
    """
    if not settings.henrik_api_key:
        logger.warning(
            "HENRIK_API_KEY is empty — the v4 endpoint REQUIRES a key. "
            "Poller will run but every cycle will be rejected with 401/403."
        )
    logger.info(
        "Poller started: %s#%s [%s/%s] every %ss",
        settings.valorant_name, settings.valorant_tag,
        settings.valorant_region, settings.valorant_platform,
        settings.poll_interval_seconds,
    )
    async with httpx.AsyncClient(timeout=20.0) as client:
        while True:
            try:
                ingested = await poll_once(client)
                if ingested:
                    logger.info("Poller ingested %d new match(es)", ingested)
            except httpx.HTTPError as exc:
                logger.warning("Poller HTTP error: %s", exc)
            except Exception:
                logger.exception("Poller cycle failed unexpectedly")
            await asyncio.sleep(settings.poll_interval_seconds)
