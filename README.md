# Valorant Live Stream Analytics Engine

Real-time Twitch bot + OBS overlay system for the channel **raccoohh** (Duelist main, captain of **no talent**).

The engine watches your Valorant matches while you stream, tracks session-scoped stats (first bloods, clutches, aces, ACS), posts automatic chat alerts, and plays animated overlays in OBS — all fully automatic once configured. Go live → stats start. Go offline → stats freeze.

---

## What It Does

| Feature | Trigger | Result |
|---|---|---|
| Session tracking | You go live / offline on Twitch | Stats automatically scoped to the current stream only |
| Match ingestion | You finish a Valorant match | Parsed from HenrikDev v4, stored in PostgreSQL |
| 💥 Ace alerts | 5+ distinct kills in one round | Chat message + animated OBS popup |
| 🔥 Clutch alerts | You win a 1v3 or bigger | Chat message + animated OBS popup |
| 📊 Match summaries | Every completed match | K/D/A, agent, ACS, first bloods posted to chat + overlay |
| `!entry` | Chat command | First Blood : First Death ratio for the current stream |
| `!clutch` | Chat command | Total clutches won this stream |
| `!impact` | Chat command | Custom playmaking metric (FB × 2 + clutches × 3 + aces × 5) / matches + ACS/100 |

---

## Architecture

```
                        ┌────────────────────────── ONE PYTHON PROCESS (asyncio) ──────────────────────────┐
                        │                                                                                  │
  Twitch "Get Streams"  │   stream_watcher ──> SESSION_STARTED / SESSION_ENDED                             │
        (60s poll) ────>│                                                                                  │
                        │   poller ──> PostgreSQL (MatchRecord, scoped to active StreamSession)            │
  HenrikDev v4 API      │     │                                                                            │
      (60s poll) ──────>│     └──> NEW_MATCH / ACE / CLUTCH ──> Redis Pub/Sub ──┬──> twitchio bot ──> chat │
                        │                                                       │                          │
                        │                                                       └──> FastAPI WebSocket     │
                        │                                                            (/ws/overlay)         │
                        └────────────────────────────────────────────────────────┬─────────────────────────┘
                                                                                 │
                                                              OBS Browser Source (overlay/)
                                                              reconnecting WS client + GSAP queue
```

- **One event loop**: Uvicorn, the Twitch bot, the poller, and the stream watcher all run as asyncio tasks in a single process. No threads, no tunnels, no public URL required.
- **Redis Pub/Sub** decouples producers (poller, watcher) from consumers (chat bot, overlay). New consumers can be added without touching existing code.
- **Idempotent ingestion**: `match_id` is UNIQUE in the database — restarts and double-polls can never duplicate a match.
- **Degraded, never dead**: malformed match payloads parse to zeros; 429 rate limits trigger exponential backoff; every background loop survives its own errors.

---

## Quickstart (the 10-minute deploy)

Prerequisites: [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running. That's it.

```bash
# 1. Clone / open this folder, then:
copy .env.example .env        # Windows
cp .env.example .env          # macOS / Linux

# 2. Fill in the four secrets in .env (see "Secrets Setup" below — 5 minutes)

# 3. Build and start everything:
docker compose up --build -d

# 4. Verify it's alive:
curl http://localhost:8000/health        # -> {"status":"ok"}
docker compose logs -f app               # watch the startup logs (Ctrl+C to exit)

# 5. Add the overlay to OBS (see "OBS Setup" below)

# 6. Go live. Everything else is automatic.
```

Useful shortcuts (requires `make`; optional):

```bash
make up        # docker compose up --build -d
make logs      # tail the app logs
make down      # stop everything
make test      # run the test suite in Docker
```

---

## Secrets Setup (foolproof)

All four secrets go in the `.env` file you created in step 1. Never commit `.env` to git.

### 1. `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` — Twitch app credentials

*Used by the stream watcher to detect when you go live. One-time setup, ~2 minutes.*

1. Go to the **Twitch Developer Console**: <https://dev.twitch.tv/console>
2. Log in with your Twitch account, then click **Applications** → **Register Your Application**.
3. Fill in:
   - **Name**: anything unique, e.g. `raccoohh-stream-analytics`
   - **OAuth Redirect URLs**: `http://localhost` (not actually used, but the field is mandatory)
   - **Category**: `Other`
4. Click **Create**, then **Manage** on your new application.
5. Copy the **Client ID** into `TWITCH_CLIENT_ID`.
6. Click **New Secret**, copy it into `TWITCH_CLIENT_SECRET`. *(Twitch shows the secret only once — if you lose it, just generate a new one.)*

### 2. `TWITCH_TOKEN` — bot account OAuth token

*Used by the chat bot to read commands and post alerts.*

1. Decide which account the bot speaks as — your own account or a dedicated bot account (a separate account named e.g. `notalent_bot` looks cleaner in chat). Log in to Twitch **as that account**.
2. Go to <https://twitchtokengenerator.com/>
3. Choose **Bot Chat Token** and authorize.
4. Copy the token — it starts with `oauth:` — into `TWITCH_TOKEN`, keeping the prefix:
   ```
   TWITCH_TOKEN=oauth:abc123yourtokenhere
   ```

### 3. `HENRIK_API_KEY` — HenrikDev Valorant API key

*Used to fetch your match data. The v4 endpoint REQUIRES this key — the poller will log 401/403 errors without it.*

1. Go to the HenrikDev portal: <https://docs.henrikdev.xyz/>
2. Follow the **"API Key" / "Get Started"** link to request a free key (it may redirect to their Discord or a dashboard — the key is issued there).
3. Copy the key into `HENRIK_API_KEY`.

### 4. Your Riot ID

Set these so the poller finds *your* player inside each match (case-insensitive):

```
VALORANT_NAME=raccoohh      # your Riot ID name (the part before the #)
VALORANT_TAG=EUW            # your tagline (the part after the #)
VALORANT_REGION=eu          # eu / na / ap / kr / br / latam
```

### Full `.env` example (with secrets filled in)

```ini
TWITCH_TOKEN=oauth:x7f2...
TWITCH_CLIENT_ID=gp762nuo...
TWITCH_CLIENT_SECRET=9h3k...
HENRIK_API_KEY=HDEV-...
VALORANT_NAME=raccoohh
VALORANT_TAG=EUW
```

Everything else in `.env.example` has sane defaults — leave it alone on first deploy.

---

## OBS Setup

1. In OBS, add a source: **Sources → + → Browser**.
2. Configure:
   - **URL**: `http://localhost:8000/overlay/`
   - **Width**: `1920`, **Height**: `1080` (match your canvas)
   - Leave **Shutdown source when not visible** **unchecked** (keeps the WebSocket alive).
3. The overlay background is fully transparent — it will look empty until an event fires. That is correct.
4. Look at the bottom-right corner of the source: a tiny **green dot** = connected to the engine, **red dot** = reconnecting.

### Test the overlay without playing a match

The engine ships with a dev simulator that publishes fake events through the exact same pipeline as real ones:

```bash
# Simulate a session start (stats bar slides in):
curl -X POST http://localhost:8000/api/session/start

# Simulate an ace:
curl -X POST http://localhost:8000/dev/simulate/ace

# Simulate a clutch:
curl -X POST http://localhost:8000/dev/simulate/clutch

# Stress-test the animation queue — 3 simultaneous aces must play SEQUENTIALLY:
curl -X POST "http://localhost:8000/dev/simulate/burst?event=ace&count=3&gap_ms=0"

# Custom payload:
curl -X POST http://localhost:8000/dev/simulate/match \
  -H "Content-Type: application/json" \
  -d "{\"map\": \"Lotus\", \"agent\": \"Raze\", \"kills\": 34}"
```

*(On Windows, `curl.exe` works in PowerShell; if quoting is painful, use Git Bash.)*

> **Before any serious deployment**: set `ENABLE_DEV_ROUTES=false` in `.env`. The simulator is dev tooling and loudly logs a warning at startup while enabled.

---

## Chat Commands

| Command | Response |
|---|---|
| `!entry` | `🎯 Entry stats this stream: 8 first bloods / 5 first deaths (ratio 1.6) across 4 match(es)` |
| `!clutch` | `🧊 raccoohh has won 3 clutch(es) this stream. Ice in the veins.` |
| `!impact` | `⚡ Impact rating this stream: 6.42 (FB 8 | clutches 3 | aces 1 | avg ACS 243.5)` |

All commands are scoped to the **current stream session** — going offline resets the slate.

---

## HTTP API Reference

| Method & Path | Purpose |
|---|---|
| `GET /health` | Liveness check |
| `POST /api/session/start` | Manually start a session (automatic when the watcher is configured) |
| `POST /api/session/end` | Manually end the active session |
| `GET /api/session/current` | Live session aggregates (totals, entry ratio, impact) as JSON |
| `GET /overlay/` | The OBS Browser Source page |
| `WS /ws/overlay` | Event stream for the overlay (JSON `{"type", "payload"}`) |
| `POST /dev/simulate/*` | Dev-only event simulator (see above) |

---

## Running the Tests

The parser contract suite pins every assumption we make about the HenrikDev v4 payload — including the Sage-resurrect ace rule, malformed/remade matches, and the documented post-plant clutch boundary.

```bash
make test          # inside Docker (no db/redis needed for these tests)
make test-local    # on the host (needs: pip install -r requirements.txt)
```

Expected: **13 passed**.

---

## Configuration Reference

Every value is an environment variable (or `.env` entry); all are read by `app/core/config.py`.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | local Postgres | SQLAlchemy async connection string |
| `REDIS_URL` | local Redis | Pub/Sub broker address |
| `TWITCH_TOKEN` | — | Bot chat OAuth token (**required for chat features**) |
| `TWITCH_CHANNEL` | `raccoohh` | Channel to join and watch |
| `TWITCH_PREFIX` | `!` | Chat command prefix |
| `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` | — | App credentials (**required for auto sessions**) |
| `TWITCH_POLL_INTERVAL_SECONDS` | `60` | Stream live/offline check cadence |
| `HENRIK_API_KEY` | — | HenrikDev key (**required for match data**) |
| `VALORANT_NAME` / `VALORANT_TAG` / `VALORANT_REGION` / `VALORANT_PLATFORM` | `raccoohh` / `EUW` / `eu` / `pc` | Your Riot ID |
| `HENRIK_MODE` | *(empty = all)* | Optional queue filter, e.g. `competitive` |
| `POLL_INTERVAL_SECONDS` | `60` | Match polling cadence |
| `HENRIK_MAX_RETRIES` / `HENRIK_RETRY_BASE_SECONDS` / `HENRIK_RETRY_MAX_SECONDS` | `5` / `5` / `120` | 429 backoff policy |
| `ENABLE_DEV_ROUTES` | `true` | Mounts `/dev/simulate/*` — **set `false` for real deployments** |

---

## Project Structure

```
├── docker-compose.yml / Dockerfile / Makefile / requirements.txt
├── .env.example                 # copy to .env and fill in secrets
├── conftest.py                  # pytest path setup
├── overlay/                     # OBS Browser Source (static files served at /overlay/)
│   ├── index.html  overlay.css  overlay.js
├── tests/
│   └── test_poller_parsing.py   # 13 contract tests for the v4 parser
└── app/
    ├── main.py                  # lifespan: boots Redis, DB, poller, watcher, bot, WS fan-out
    ├── core/
    │   ├── config.py            # Pydantic Settings — all env config
    │   ├── database.py          # async engine + session factory
    │   └── redis.py             # shared Redis client lifecycle
    ├── models/
    │   ├── session.py           # StreamSession (is_active = stats boundary)
    │   └── match.py             # MatchRecord (match_id UNIQUE = idempotency)
    ├── services/
    │   ├── events.py            # StreamEvent contract + pub/sub primitives
    │   └── stats.py             # all stat math (entry ratio, impact score)
    ├── ingestion/
    │   ├── poller.py            # HenrikDev v4 polling + round analytics + 429 backoff
    │   └── stream_watcher.py    # Twitch live/offline polling + session auto start/stop
    ├── bot/
    │   └── twitch_bot.py        # !entry / !clutch / !impact + automatic alerts
    └── api/routes/
        ├── health.py  sessions.py  websocket.py  dev.py
```

---

## Troubleshooting

| Symptom | Likely cause & fix |
|---|---|
| Logs show `HenrikDev auth failed (401/403)` | `HENRIK_API_KEY` missing or wrong — v4 requires it. |
| Logs show `Poller 429` warnings | Rate limited. The backoff handles it; if persistent, raise `POLL_INTERVAL_SECONDS` to `120`. |
| Bot never joins chat | `TWITCH_TOKEN` wrong/expired, or missing the `oauth:` prefix. Regenerate at twitchtokengenerator.com. |
| Sessions don't auto-start | `TWITCH_CLIENT_ID`/`SECRET` missing — check the startup warning log; you can still use `POST /api/session/start` manually. |
| Stats are empty after a match | Matches only count while a session is active, and the poller sees the last 5 matches — check `docker compose logs app` for "New match stored". |
| Overlay dot is red | Engine not running or wrong URL. Check `curl http://localhost:8000/health`. The overlay auto-reconnects with backoff — no OBS refresh needed. |
| Nothing in chat when testing | The dev simulator only publishes events; the bot must be connected (valid `TWITCH_TOKEN`) to speak. |

---

## Known Model Boundaries (documented, tested, intentional)

- **Post-plant clutch approximation**: if you become the last player alive, die, and your team still wins the round on a defuse, it counts as a clutch. Pinned by `test_post_plant_death_boundary`.
- **Aces require 5 distinct victims**: a Sage resurrect can neither manufacture nor deny an ace.
- **Clutch alerts fire at 1v3+**; smaller clutches still count toward `!clutch` (see `CLUTCH_ALERT_THRESHOLD` in `app/ingestion/poller.py`).
- **Stream transitions are debounced**: going live/offline takes up to ~2 polls (~2 min) to register, by design, so brief stream drops never wipe your session stats.

---

*Built for raccoohh.* 🦝
