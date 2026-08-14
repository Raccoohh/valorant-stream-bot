"""Contract tests for the HenrikDev v4 parser (app.ingestion.poller).

We do NOT control the upstream API. These tests pin the payload shapes
we depend on and document the parser's behavior at its known edges:

  - Sage resurrect: an ACE requires 5+ DISTINCT victims.
  - Remade/cancelled matches: missing round/kill-feed data must
    degrade to zeros, never crash.
  - Post-plant death: if you become the last player alive, die, and
    your team still wins the round (post-plant defuse), the current
    model COUNTS it as a clutch. That is an accepted approximation,
    documented and locked in by test_post_plant_death_boundary.

The fixtures default to the .env.example identity (raccoohh#EUW),
matching the Settings defaults — no monkeypatching required.
"""

from typing import Any

from app.ingestion.poller import _extract_player_stats

MY_NAME = "raccoohh"
MY_TAG = "EUW"
MY_TEAM = "Red"
ENEMY_TEAM = "Blue"


# ----------------------------------------------------------------------
# Fixture builders — construct minimal but realistic v4 payloads
# ----------------------------------------------------------------------
def make_player(
    name: str,
    tag: str,
    team: str,
    agent: str = "Jett",
    kills: int = 0,
    deaths: int = 0,
    assists: int = 0,
    score: int = 0,
) -> dict[str, Any]:
    return {
        "name": name,
        "tag": tag,
        "team_id": team,
        "agent": {"name": agent},
        "stats": {"kills": kills, "deaths": deaths, "assists": assists, "score": score},
    }


def make_kill(
    round_num: int,
    t_ms: int,
    killer: dict[str, Any],
    victim: dict[str, Any],
) -> dict[str, Any]:
    return {
        "round": round_num,
        "time_in_round_in_ms": t_ms,
        "killer": {"name": killer["name"], "tag": killer["tag"], "team": killer["team_id"]},
        "victim": {"name": victim["name"], "tag": victim["tag"], "team": victim["team_id"]},
        "assistants": [],
    }


def make_rounds(winners: list[str]) -> list[dict[str, Any]]:
    """Round outcomes, 1-based alignment with kill events' `round` field."""
    return [{"winning_team": w} for w in winners]


def make_match(
    players: list[dict[str, Any]],
    rounds: Any = None,
    kills: Any = None,
    match_id: str = "match-001",
    map_name: str = "Ascent",
) -> dict[str, Any]:
    match: dict[str, Any] = {
        "metadata": {"match_id": match_id, "map": {"name": map_name}},
        "players": players,
    }
    if rounds is not None:
        match["rounds"] = rounds
    if kills is not None:
        match["kills"] = kills
    return match


def standard_lobby(me_stats: dict[str, int] | None = None) -> tuple[dict, list[dict], list[dict]]:
    """Returns (me, teammates, enemies) — a full 5v5."""
    me = make_player(MY_NAME, MY_TAG, MY_TEAM, **(me_stats or {}))
    teammates = [make_player(f"Mate{i}", f"M{i}", MY_TEAM) for i in range(1, 5)]
    enemies = [make_player(f"Enemy{i}", f"E{i}", ENEMY_TEAM) for i in range(1, 6)]
    return me, teammates, enemies


def extract(match: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    result = _extract_player_stats(match)
    assert result is not None, "parser unexpectedly rejected the fixture"
    return result


# ----------------------------------------------------------------------
# Baseline extraction
# ----------------------------------------------------------------------
def test_basic_stats_and_acs() -> None:
    me, mates, enemies = standard_lobby(
        {"kills": 20, "deaths": 10, "assists": 5, "score": 2600}
    )
    match = make_match(
        players=[me, *mates, *enemies],
        rounds=make_rounds([MY_TEAM] * 13),  # 13 rounds -> ACS = 2600/13 = 200
        kills=[],
    )
    fields, _ = extract(match)
    assert fields["match_id"] == "match-001"
    assert fields["map_name"] == "Ascent"
    assert fields["agent"] == "Jett"
    assert fields["kills"] == 20
    assert fields["deaths"] == 10
    assert fields["assists"] == 5
    assert fields["score"] == 200


def test_player_matching_is_case_insensitive() -> None:
    me = make_player("RaCcOoHh", "euw", MY_TEAM, kills=7, score=700)
    _, mates, enemies = standard_lobby()
    match = make_match(
        players=[me, *mates, *enemies],
        rounds=make_rounds([MY_TEAM] * 10),
        kills=[],
    )
    fields, _ = extract(match)
    assert fields["kills"] == 7


def test_match_without_me_returns_none() -> None:
    _, mates, enemies = standard_lobby()
    match = make_match(players=[*mates, *enemies], rounds=[], kills=[])
    assert _extract_player_stats(match) is None


# ----------------------------------------------------------------------
# First bloods / first deaths — earliest kill of the round decides
# ----------------------------------------------------------------------
def test_first_blood_and_first_death_use_earliest_kill() -> None:
    me, mates, enemies = standard_lobby()
    kills = [
        # Round 1: I open at t=100 -> first blood. A later kill must not matter.
        make_kill(1, 100, me, enemies[0]),
        make_kill(1, 5000, mates[0], enemies[1]),
        # Round 2: I die first at t=50 -> first death.
        make_kill(2, 50, enemies[0], me),
        make_kill(2, 4000, mates[0], enemies[0]),
    ]
    match = make_match(
        players=[me, *mates, *enemies],
        rounds=make_rounds([MY_TEAM, ENEMY_TEAM]),
        kills=kills,
    )
    fields, _ = extract(match)
    assert fields["first_bloods"] == 1
    assert fields["first_deaths"] == 1


# ----------------------------------------------------------------------
# ACE + Sage resurrect edge case — distinct victims only
# ----------------------------------------------------------------------
def test_ace_counts_with_sage_resurrect() -> None:
    """6 kills in a round, but one victim appears twice (resurrected).

    5 DISTINCT enemies died -> exactly one ace. This pins the rule that
    a res can never inflate an ace into a '6-kill round' myth, nor deny
    a legitimate ace just because someone died twice.
    """
    me, mates, enemies = standard_lobby()
    kills = [
        make_kill(1, 1000, me, enemies[0]),
        make_kill(1, 2000, me, enemies[1]),
        make_kill(1, 3000, me, enemies[2]),
        make_kill(1, 4000, me, enemies[3]),
        make_kill(1, 5000, me, enemies[4]),
        make_kill(1, 6000, me, enemies[0]),  # Sage res -> same victim again
    ]
    match = make_match(
        players=[me, *mates, *enemies],
        rounds=make_rounds([MY_TEAM]),
        kills=kills,
    )
    fields, _ = extract(match)
    assert fields["aces"] == 1


def test_no_ace_when_res_makes_only_four_distinct_victims() -> None:
    """5 kill events but only 4 distinct victims -> NOT an ace."""
    me, mates, enemies = standard_lobby()
    kills = [
        make_kill(1, 1000, me, enemies[0]),
        make_kill(1, 2000, me, enemies[1]),
        make_kill(1, 3000, me, enemies[2]),
        make_kill(1, 4000, me, enemies[3]),
        make_kill(1, 5000, me, enemies[0]),  # res'd and killed again
    ]
    match = make_match(
        players=[me, *mates, *enemies],
        rounds=make_rounds([MY_TEAM]),
        kills=kills,
    )
    fields, _ = extract(match)
    assert fields["aces"] == 0


# ----------------------------------------------------------------------
# Clutches
# ----------------------------------------------------------------------
def test_clutch_1v3_won_triggers_big_clutch_alert_metadata() -> None:
    """All four teammates die while 3 enemies remain; I clean up and win.

    Expect: clutches_won=1, big_clutches=1, best situation "1v3".
    """
    me, mates, enemies = standard_lobby()
    kills = [
        make_kill(1, 1000, me, enemies[0]),   # I open: 5v4
        make_kill(1, 2000, me, enemies[1]),   # 5v3
        make_kill(1, 3000, enemies[2], mates[0]),
        make_kill(1, 4000, enemies[2], mates[1]),
        make_kill(1, 5000, enemies[3], mates[2]),
        make_kill(1, 6000, enemies[3], mates[3]),  # now last alive, 1v3
        make_kill(1, 7000, me, enemies[2]),
        make_kill(1, 8000, me, enemies[3]),
        make_kill(1, 9000, me, enemies[4]),
    ]
    match = make_match(
        players=[me, *mates, *enemies],
        rounds=make_rounds([MY_TEAM]),
        kills=kills,
    )
    fields, alerts = extract(match)
    assert fields["clutches_won"] == 1
    assert alerts["big_clutches"] == 1
    assert alerts["best_clutch_situation"] == "1v3"


def test_clutch_1v1_counts_for_stats_but_not_for_alerts() -> None:
    """A 1v1 win is a clutch for !clutch, but below the 1v3 alert bar."""
    me, mates, enemies = standard_lobby()
    kills = [
        *[make_kill(1, 1000 + i * 100, me, enemies[i]) for i in range(4)],  # 5v1
        make_kill(1, 2000, enemies[4], mates[0]),
        make_kill(1, 2100, enemies[4], mates[1]),
        make_kill(1, 2200, enemies[4], mates[2]),
        make_kill(1, 2300, enemies[4], mates[3]),  # last alive, 1v1
        make_kill(1, 3000, me, enemies[4]),
    ]
    match = make_match(
        players=[me, *mates, *enemies],
        rounds=make_rounds([MY_TEAM]),
        kills=kills,
    )
    fields, alerts = extract(match)
    assert fields["clutches_won"] == 1
    assert alerts["big_clutches"] == 0
    assert alerts["best_clutch_situation"] is None


def test_lost_last_alive_round_is_not_a_clutch() -> None:
    """Becoming the last one alive and LOSING is not a clutch."""
    me, mates, enemies = standard_lobby()
    kills = [
        *[make_kill(1, 1000 + i * 100, enemies[0], mates[i]) for i in range(4)],
        make_kill(1, 2000, enemies[0], me),  # I die too; round lost
    ]
    match = make_match(
        players=[me, *mates, *enemies],
        rounds=make_rounds([ENEMY_TEAM]),
        kills=kills,
    )
    fields, _ = extract(match)
    assert fields["clutches_won"] == 0


# ----------------------------------------------------------------------
# DOCUMENTED BOUNDARY — post-plant death
# ----------------------------------------------------------------------
def test_post_plant_death_boundary() -> None:
    """BOUNDARY BEHAVIOR — pinned intentionally, not by accident.

    Scenario: all four teammates die (I become last alive, 1v5), I get
    two kills, then I die — but my team still wins the round on the
    post-plant defuse.

    The current model ONLY tracks 'became last alive' + 'team won'; it
    does not require me to survive. So this round COUNTS as a clutch
    (and a 1v5 alert) even though I died. We accept this approximation
    because it is rare, hype-worthy anyway, and eliminating it would
    require spike-state tracking the kill feed alone cannot provide.

    If this test ever fails after a parser change, the boundary moved —
    update this docstring and the poller's module docs together.
    """
    me, mates, enemies = standard_lobby()
    kills = [
        *[make_kill(1, 1000 + i * 100, enemies[0], mates[i]) for i in range(4)],
        make_kill(1, 2000, me, enemies[0]),
        make_kill(1, 2100, me, enemies[1]),
        make_kill(1, 3000, enemies[2], me),  # I die...
    ]
    match = make_match(
        players=[me, *mates, *enemies],
        rounds=make_rounds([MY_TEAM]),  # ...but my team wins the defuse
        kills=kills,
    )
    fields, alerts = extract(match)
    assert fields["clutches_won"] == 1       # <- the documented approximation
    assert alerts["big_clutches"] == 1
    assert alerts["best_clutch_situation"] == "1v5"


# ----------------------------------------------------------------------
# Malformed / remade matches — degrade, never crash
# ----------------------------------------------------------------------
def test_remade_match_missing_rounds_and_killfeed() -> None:
    """Cancelled/remade matches often ship with NO rounds/kills arrays.

    Base stats must still extract (using stats.rounds_played fallback
    for ACS), and every round-derived stat degrades to 0.
    """
    me = make_player(MY_NAME, MY_TAG, MY_TEAM, kills=10, deaths=2, assists=1, score=1500)
    me["stats"]["rounds_played"] = 10
    _, mates, enemies = standard_lobby()
    match = make_match(players=[me, *mates, *enemies])  # no rounds, no kills
    fields, alerts = extract(match)
    assert fields["kills"] == 10
    assert fields["score"] == 150  # 1500 / 10 via the fallback path
    assert fields["first_bloods"] == 0
    assert fields["first_deaths"] == 0
    assert fields["aces"] == 0
    assert fields["clutches_won"] == 0
    assert alerts["big_clutches"] == 0


def test_garbage_kill_events_are_skipped_individually() -> None:
    """A kill feed full of junk must not crash or corrupt counts."""
    me, mates, enemies = standard_lobby()
    kills = [
        None,
        "not-a-dict",
        {"round": "not-an-int"},
        {"round": 1},  # valid round, but no killer/victim
        42,
    ]
    match = make_match(
        players=[me, *mates, *enemies],
        rounds=make_rounds([MY_TEAM]),
        kills=kills,
    )
    fields, _ = extract(match)
    assert fields["first_bloods"] == 0
    assert fields["first_deaths"] == 0
    assert fields["aces"] == 0
    assert fields["clutches_won"] == 0


def test_structurally_broken_payloads_return_none() -> None:
    """The parser's contract: unusable payloads -> None, never an exception."""
    assert _extract_player_stats({"players": "garbage"}) is None
    assert _extract_player_stats({"players": []}) is None                      # no players
    assert _extract_player_stats({"players": [], "metadata": None}) is None  # no metadata
    assert _extract_player_stats(
        {"players": [make_player(MY_NAME, MY_TAG, MY_TEAM)], "metadata": {}}
    ) is None  # no match_id
