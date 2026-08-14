/* ============================================================
 * Valorant Live Overlay — client logic
 *
 * Three guarantees implemented here:
 *  1. AUTO-RECONNECT — exponential backoff with jitter, capped,
 *     attempt counter reset on every successful connection.
 *  2. EVENT QUEUE — every incoming event is enqueued and played
 *     strictly one-at-a-time; GSAP timelines never overlap.
 *  3. TRANSPARENCY — handled entirely in overlay.css; this script
 *     never sets a background anywhere.
 * ============================================================ */

"use strict";

/* ------------------------------------------------------------
 * Session stats state (updated in place, animated via GSAP)
 * ---------------------------------------------------------- */
const sessionStats = {
  matches: 0,
  kills: 0,
  deaths: 0,
  assists: 0,
  firstBloods: 0,
  firstDeaths: 0,
  clutches: 0,
  aces: 0,
  acsSum: 0, // running sum of per-match ACS for averaging
};

function entryRatio() {
  if (sessionStats.firstDeaths === 0) return sessionStats.firstBloods;
  return sessionStats.firstBloods / sessionStats.firstDeaths;
}

function impactScore() {
  if (sessionStats.matches === 0) return 0;
  const playmaking =
    (sessionStats.firstBloods * 2 +
      sessionStats.clutches * 3 +
      sessionStats.aces * 5) /
    sessionStats.matches;
  const avgAcs = sessionStats.acsSum / sessionStats.matches;
  return playmaking + avgAcs / 100;
}

function resetStats() {
  Object.keys(sessionStats).forEach((k) => (sessionStats[k] = 0));
}

/* ------------------------------------------------------------
 * DOM helpers
 * ---------------------------------------------------------- */
const els = {
  statsBar: document.getElementById("stats-bar"),
  kda: document.getElementById("stat-kda"),
  entry: document.getElementById("stat-entry"),
  clutches: document.getElementById("stat-clutches"),
  aces: document.getElementById("stat-aces"),
  impact: document.getElementById("stat-impact"),
  alertCard: document.getElementById("alert-card"),
  alertTitle: document.getElementById("alert-title"),
  alertSubtitle: document.getElementById("alert-subtitle"),
  connDot: document.getElementById("conn-dot"),
};

function renderStats() {
  els.kda.textContent = `${sessionStats.kills} / ${sessionStats.deaths} / ${sessionStats.assists}`;
  els.entry.textContent = `${sessionStats.firstBloods} : ${sessionStats.firstDeaths}`;
  els.clutches.textContent = String(sessionStats.clutches);
  els.aces.textContent = String(sessionStats.aces);
  els.impact.textContent = impactScore().toFixed(2);
}

/* ------------------------------------------------------------
 * REQUIREMENT 2 — The event queue.
 *
 * enqueue() is the ONLY entry point for events. A single consumer
 * loop drains the queue sequentially: it awaits each animation's
 * full completion before touching the next event. GSAP timelines
 * therefore can never overlap, no matter how fast events arrive.
 * ---------------------------------------------------------- */
const eventQueue = [];
let queueRunning = false;

function enqueue(event) {
  eventQueue.push(event);
  if (!queueRunning) {
    queueRunning = true;
    drainQueue();
  }
}

async function drainQueue() {
  while (eventQueue.length > 0) {
    const event = eventQueue.shift();
    try {
      await playEvent(event);
    } catch (err) {
      // A broken animation must never stall the queue.
      console.error("Overlay animation failed:", err);
    }
  }
  queueRunning = false;
}

/* Dispatches an event to its animation; resolves only when the
 * animation (including its "hold on screen" time) has finished. */
function playEvent(event) {
  switch (event.type) {
    case "ace":
      sessionStats.aces += event.payload?.count || 1;
      renderStats();
      return playAlert(
        "ACE!",
        `${event.payload?.map || ""} ${event.payload?.agent ? "· " + event.payload.agent : ""}`.trim(),
        "theme-ace",
        3.2
      );
    case "clutch":
      sessionStats.clutches += event.payload?.count || 1;
      renderStats();
      return playAlert(
        "CLUTCH OR KICK",
        `${event.payload?.situation || "1vX"} on ${event.payload?.map || "?"}`,
        "theme-clutch",
        3.0
      );
    case "new_match":
      applyMatchToStats(event.payload || {});
      return playMatchSummary(event.payload || {});
    case "session_started":
      resetStats();
      renderStats();
      return revealStatsBar();
    case "session_ended":
      return hideStatsBar();
    default:
      return Promise.resolve();
  }
}

function applyMatchToStats(p) {
  sessionStats.matches += 1;
  sessionStats.kills += p.kills || 0;
  sessionStats.deaths += p.deaths || 0;
  sessionStats.assists += p.assists || 0;
  sessionStats.firstBloods += p.first_bloods || 0;
  sessionStats.firstDeaths += p.first_deaths || 0;
  sessionStats.acsSum += p.score || 0;
  renderStats();
}

/* ------------------------------------------------------------
 * GSAP animations — every function returns a Promise that
 * resolves when the timeline is COMPLETELY done.
 * ---------------------------------------------------------- */
function playAlert(title, subtitle, themeClass, holdSeconds) {
  return new Promise((resolve) => {
    els.alertTitle.textContent = title;
    els.alertSubtitle.textContent = subtitle;
    els.alertCard.className = themeClass; // swap theme, drop .hidden

    const tl = gsap.timeline({ onComplete: resolve });
    tl.fromTo(
      els.alertCard,
      { scale: 0.3, opacity: 0, y: -40 },
      { scale: 1, opacity: 1, y: 0, duration: 0.5, ease: "back.out(1.8)" }
    )
      .to(els.alertCard, {
        scale: 1.05,
        duration: 0.25,
        yoyo: true,
        repeat: 1,
        ease: "power1.inOut",
      })
      .to({}, { duration: holdSeconds }) // hold on screen
      .to(els.alertCard, {
        opacity: 0,
        y: -30,
        duration: 0.4,
        ease: "power2.in",
      })
      .set(els.alertCard, { visibility: "hidden", y: 0, scale: 1 });
  });
}

function playMatchSummary(p) {
  return new Promise((resolve) => {
    els.alertTitle.textContent = `${p.map || "Match"} — ${p.kills || 0}/${p.deaths || 0}/${p.assists || 0}`;
    els.alertSubtitle.textContent = `${p.agent || ""} · ACS ${p.score || 0} · First Bloods ${p.first_bloods || 0}`;
    els.alertCard.className = "theme-match";

    const tl = gsap.timeline({ onComplete: resolve });
    tl.fromTo(
      els.alertCard,
      { x: "-110vw", opacity: 0 },
      { x: 0, opacity: 1, duration: 0.6, ease: "power3.out" }
    )
      .to({}, { duration: 2.6 })
      .to(els.alertCard, { x: "110vw", opacity: 0, duration: 0.5, ease: "power3.in" })
      .set(els.alertCard, { visibility: "hidden", x: 0 });
  });
}

function revealStatsBar() {
  return new Promise((resolve) => {
    els.statsBar.classList.remove("hidden");
    gsap.fromTo(
      els.statsBar,
      { opacity: 0, y: -20 },
      { opacity: 1, y: 0, duration: 0.6, ease: "power2.out", onComplete: resolve }
    );
  });
}

function hideStatsBar() {
  return new Promise((resolve) => {
    gsap.to(els.statsBar, {
      opacity: 0,
      y: -20,
      duration: 0.5,
      ease: "power2.in",
      onComplete: () => {
        els.statsBar.classList.add("hidden");
        gsap.set(els.statsBar, { y: 0 });
        resolve();
      },
    });
  });
}

/* ------------------------------------------------------------
 * REQUIREMENT 1 — Auto-reconnecting WebSocket.
 *
 * Backoff schedule: base * 2^attempt, capped, ±20% jitter.
 *   attempt 0 → ~1s, 1 → ~2s, 2 → ~4s, ... capped at 30s.
 * The attempt counter resets to 0 on every successful "open",
 * so a long-lived healthy connection that drops once retries
 * fast, while a genuinely dead server backs off politely.
 * ---------------------------------------------------------- */
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;
let reconnectAttempts = 0;
let socket = null;

function backoffDelayMs() {
  const exp = Math.min(RECONNECT_BASE_MS * 2 ** reconnectAttempts, RECONNECT_MAX_MS);
  const jitter = exp * 0.2 * (Math.random() * 2 - 1); // ±20%
  return Math.round(exp + jitter);
}

function setConnected(connected) {
  els.connDot.className = connected ? "connected" : "disconnected";
}

function connect() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${protocol}//${location.host}/ws/overlay`;

  socket = new WebSocket(url);

  socket.onopen = () => {
    reconnectAttempts = 0; // healthy connection -> reset backoff
    setConnected(true);
  };

  socket.onmessage = (msg) => {
    let event;
    try {
      event = JSON.parse(msg.data);
    } catch {
      return; // malformed payload — ignore, never crash the overlay
    }
    enqueue(event); // <- requirement 2: never animate directly here
  };

  socket.onclose = () => {
    setConnected(false);
    const delay = backoffDelayMs();
    reconnectAttempts += 1;
    setTimeout(connect, delay);
  };

  socket.onerror = () => {
    // Force close so onclose fires and the reconnect logic stays
    // in exactly one place.
    socket.close();
  };
}

/* Boot */
renderStats();
connect();
