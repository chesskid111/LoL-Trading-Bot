"""Historical livestats extraction for backtesting.

Empirically verified (2026-05-25):
    - Riot retains livestats frames for at least 45 days
    - LCK split 2 2026 (~65 matches × ~2.5 games each) is fully accessible
    - The window endpoint returns ~10 frames per call, covering a few seconds
    - To extract a full ~35-min game we need to walk window-by-window through
      the game's timeline (~200 calls per game)

This module is a one-shot dataset builder for Phase 6-equivalent training data:
no CV, just livestats. Used by the backtest pipeline before committing to
the rest of v2.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from loltrader.livestats.discovery import LIVE, PERSISTED, RIOT_API_KEY

log = logging.getLogger(__name__)

# LCK league id (from getLeagues probe 2026-05-25)
LCK_LEAGUE_ID = "98767991310872058"
LCS_LEAGUE_ID = "98767991299243165"
LEC_LEAGUE_ID = "98767991302996019"
LPL_LEAGUE_ID = "98767991314006698"

# Window-walking step. The window endpoint returns frames spanning a few
# seconds; we step by 10s to cover the game timeline without missing chunks.
WALK_STEP_SEC = 10

# Match scheduled-start to first-game-start offset. LCK broadcasts open with
# ~30 min of pre-game show then game 1 starts. To find data for game 1, probe
# starting at scheduled_start + this offset.
MATCH_TO_GAME1_OFFSET_SEC = 30 * 60

# How far past the latest fetched frame we keep walking before declaring the
# game ended. (Games can have pauses; this guards against early exit.)
GAME_END_QUIET_SEC = 5 * 60


@dataclass(frozen=True)
class HistoricalMatch:
    """One LCK series with its constituent games."""

    match_id: str
    scheduled_start_utc: datetime
    team_a_name: str
    team_b_name: str
    league_slug: str
    game_ids: list[str]              # in series order
    game_states: list[str]            # 'completed' / 'unneeded'
    winner_team_id: str | None = None  # if known from match metadata
    game_winners: list[str | None] | None = None  # per-game winner team ids


def _api_get(url: str, params: dict | None = None, timeout: float = 10) -> dict | None:
    """GET wrapper that returns parsed JSON or None on 204 / empty."""
    r = requests.get(url, params=params, headers={"x-api-key": RIOT_API_KEY},
                     timeout=timeout)
    if r.status_code == 204 or not r.text.strip():
        return None
    r.raise_for_status()
    return r.json()


def get_completed_matches(league_id: str, league_slug: str) -> list[HistoricalMatch]:
    """Return all completed matches for a league, sorted newest-first.

    The getSchedule endpoint returns the league's current season schedule
    (up to ~80 events). For older seasons we'd need to walk tournaments,
    which is out of scope for v2.0 backtest.
    """
    data = _api_get(f"{PERSISTED}/getSchedule",
                     params={"hl": "en-US", "leagueId": league_id})
    if data is None:
        return []
    events = data.get("data", {}).get("schedule", {}).get("events", []) or []
    completed_events = [e for e in events if e.get("state") == "completed"]

    out: list[HistoricalMatch] = []
    for ev in sorted(completed_events, key=lambda e: e.get("startTime", ""), reverse=True):
        match = ev.get("match", {})
        teams = match.get("teams", [])
        if len(teams) < 2:
            continue
        match_id = match.get("id")
        if not match_id:
            continue
        # Pull game ids from event details (the schedule itself doesn't have them)
        detail = _api_get(f"{PERSISTED}/getEventDetails",
                           params={"hl": "en-US", "id": match_id})
        if detail is None:
            continue
        d_match = detail.get("data", {}).get("event", {}).get("match", {})
        games = d_match.get("games", []) or []
        game_ids = [str(g["id"]) for g in games if g.get("id")]
        game_states = [g.get("state", "unknown") for g in games]

        # Determine series winner from teams' result fields
        d_teams = d_match.get("teams", [])
        winner_id = None
        for t in d_teams:
            if t.get("result", {}).get("outcome") == "win":
                winner_id = t.get("id")
                break

        # Per-game winners (each game has a 'state' but the winner is recorded
        # in match-level data; for now just track which games actually counted)
        out.append(HistoricalMatch(
            match_id=str(match_id),
            scheduled_start_utc=datetime.fromisoformat(
                ev["startTime"].replace("Z", "+00:00")
            ),
            team_a_name=teams[0].get("name", "?"),
            team_b_name=teams[1].get("name", "?"),
            league_slug=league_slug,
            game_ids=game_ids,
            game_states=game_states,
            winner_team_id=winner_id,
        ))
    return out


def _floor_to_step(d: datetime, step_sec: int) -> datetime:
    """Floor a UTC datetime down to the nearest step boundary."""
    epoch = int(d.timestamp())
    floored = (epoch // step_sec) * step_sec
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def fetch_game_frames(
    game_id: str,
    probe_start_utc: datetime,
    max_calls: int = 400,
) -> list[dict]:
    """Walk the window endpoint to collect all frames for a completed game.

    Adaptive step size:
        - Before any data is found: step by COARSE_STEP_SEC (60s) to skip
          pre-game / between-game empty zones quickly
        - Once data is seen: step by WALK_STEP_SEC (10s) for full coverage

    Args:
        game_id: Riot esports gameId.
        probe_start_utc: A timestamp BEFORE the game's window. Walker handles
            arbitrarily long empty pre-game stretches via coarse stepping.
        max_calls: Safety cap on number of API requests per game.

    Returns:
        List of frame dicts, dedup'd by rfc460Timestamp, sorted ascending.
    """
    COARSE_STEP_SEC = 60   # used while searching for game's data window

    seen: dict[str, dict] = {}
    cursor = _floor_to_step(probe_start_utc, WALK_STEP_SEC)
    quiet_since: datetime | None = None
    last_observed: datetime | None = None

    for _ in range(max_calls):
        ts = cursor.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        try:
            data = _api_get(f"{LIVE}/window/{game_id}",
                             params={"startingTime": ts}, timeout=5)
        except requests.RequestException as e:
            log.warning("transport error for %s at %s: %s", game_id, ts, e)
            step = COARSE_STEP_SEC if last_observed is None else WALK_STEP_SEC
            cursor += timedelta(seconds=step)
            continue
        if data is None:
            # No data at this cursor. If we haven't found the game yet, take
            # big steps. Once we've seen data, keep stepping at fine resolution
            # so we don't skip past the actual game-end.
            if last_observed is None:
                cursor += timedelta(seconds=COARSE_STEP_SEC)
            else:
                cursor += timedelta(seconds=WALK_STEP_SEC)
                if (cursor - last_observed).total_seconds() > GAME_END_QUIET_SEC:
                    break
            continue

        frames = data.get("frames", []) or []
        new_this_call = 0
        for f in frames:
            ts_str = f.get("rfc460Timestamp")
            if not ts_str or ts_str in seen:
                continue
            seen[ts_str] = f
            new_this_call += 1
            try:
                ft = datetime.strptime(ts_str[:19],
                                        "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                if last_observed is None or ft > last_observed:
                    last_observed = ft
            except ValueError:
                continue

        # If this call returned only finished frames, we're past game end
        if frames and all(f.get("gameState") == "finished" for f in frames):
            if quiet_since is None:
                quiet_since = cursor
            elif (cursor - quiet_since).total_seconds() > 30:
                break

        # Once we've found the game, walk in fine steps to capture all of it
        cursor += timedelta(seconds=WALK_STEP_SEC)

        # Brief throttle so we don't hammer the API
        time.sleep(0.02)

    return sorted(seen.values(), key=lambda f: f.get("rfc460Timestamp", ""))


def fetch_game_details(
    game_id: str,
    frames: list[dict],
) -> list[dict]:
    """Walk the /details endpoint over the time range covered by ``frames``.

    We re-use the timestamps from a prior ``fetch_game_frames`` window pass
    so we know exactly when the game ran — no coarse-search needed. Each
    detail frame is independent and time-keyed by rfc460Timestamp, so we
    can request directly at each known frame timestamp.

    Returns: list of detail frames (dedup'd by rfc460Timestamp).
    """
    if not frames:
        return []
    # Extract the game's time range from the window frames
    try:
        first_ts = datetime.strptime(frames[0].get("rfc460Timestamp", "")[:19],
                                     "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        last_ts = datetime.strptime(frames[-1].get("rfc460Timestamp", "")[:19],
                                    "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, KeyError):
        log.warning("could not parse frame timestamps for details fetch of %s", game_id)
        return []

    # Walk the same range at the same step. /details typically returns ~10s
    # of frames per call, same as /window.
    seen: dict[str, dict] = {}
    cursor = _floor_to_step(first_ts, WALK_STEP_SEC)
    end = last_ts + timedelta(seconds=30)
    max_calls = 500  # generous safety cap

    calls = 0
    while cursor <= end and calls < max_calls:
        ts = cursor.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        try:
            data = _api_get(f"{LIVE}/details/{game_id}",
                             params={"startingTime": ts}, timeout=5)
        except requests.RequestException as e:
            log.debug("transport error for details %s at %s: %s", game_id, ts, e)
            cursor += timedelta(seconds=WALK_STEP_SEC)
            calls += 1
            continue
        if data:
            for f in data.get("frames", []) or []:
                ts_str = f.get("rfc460Timestamp")
                if ts_str and ts_str not in seen:
                    seen[ts_str] = f
        cursor += timedelta(seconds=WALK_STEP_SEC)
        calls += 1
        time.sleep(0.02)

    return sorted(seen.values(), key=lambda f: f.get("rfc460Timestamp", ""))


def fetch_match_frames(match: HistoricalMatch) -> dict[str, list[dict]]:
    """Fetch frames for every completed game in a match.

    Returns {game_id: [frames...]} for games that have data.

    Each game's probe starts 5 min BEFORE the match's scheduled start. The
    walker handles the empty pre-game stretch correctly — it doesn't trigger
    QUIET-exit until it has seen at least one in_game frame, so it walks
    forward through silence to find each game's actual start. Each gameId
    only has data for its own game (verified empirically), so probing from
    scheduled_start naturally locates the right game whether it's game 1,
    2, or 3 of the series.
    """
    out: dict[str, list[dict]] = {}
    # Probe from match-scheduled-start minus 5 min to absorb any early start
    probe_start = match.scheduled_start_utc - timedelta(minutes=5)
    for i, (gid, state) in enumerate(zip(match.game_ids, match.game_states)):
        if state == "unneeded":
            continue
        log.info("fetching frames for %s (%s game %d/%d) probe_start=%s",
                 gid, match.match_id, i + 1, len(match.game_ids),
                 probe_start.strftime("%H:%M"))
        frames = fetch_game_frames(gid, probe_start)
        if not frames:
            log.warning("no frames retrieved for %s", gid)
            continue
        out[gid] = frames
    return out


# ---------- storage helpers (backtest dataset) ----------


def infer_winner_side(frames: list[dict]) -> str | None:
    """Determine which side won from the final game state.

    Heuristic: look at the last frame and pick the side with more inhibitors
    destroyed (= more of opponent's inhibitors taken). Falls back to towers,
    then kills, then None if tied.
    """
    if not frames:
        return None
    last = frames[-1]
    blue = last.get("blueTeam", {}) or {}
    red = last.get("redTeam", {}) or {}
    # Inhibitors destroyed by THIS team (opponent's inhibitors that fell).
    # The team with MORE inhibitors destroyed = winner (they pushed harder).
    b_inh = int(blue.get("inhibitors") or 0)
    r_inh = int(red.get("inhibitors") or 0)
    if b_inh != r_inh:
        return "blue" if b_inh > r_inh else "red"
    b_tow = int(blue.get("towers") or 0)
    r_tow = int(red.get("towers") or 0)
    if b_tow != r_tow:
        return "blue" if b_tow > r_tow else "red"
    b_k = int(blue.get("totalKills") or 0)
    r_k = int(red.get("totalKills") or 0)
    if b_k != r_k:
        return "blue" if b_k > r_k else "red"
    return None


def parse_team_codes_from_frames(frames: list[dict]) -> tuple[str | None, str | None]:
    """Extract (blue_code, red_code) from a frame's gameMetadata if present.

    Some window-endpoint frames include gameMetadata at the top level; we
    don't have it here on the frame itself. Returns (None, None) if we
    can't derive it from these frames alone. Callers can fall back to
    fetching gameMetadata via discovery.get_team_sides().
    """
    return None, None  # gameMetadata isn't per-frame; needs a separate API call


def store_historical_match(
    conn,
    match: HistoricalMatch,
    frames_by_game: dict[str, list[dict]],
    downsample_step_sec: int = 10,
) -> dict[str, int]:
    """Write extracted historical frames into live_frames + games_live.

    Args:
        conn: sqlite3.Connection
        match: HistoricalMatch metadata
        frames_by_game: {game_id: [frames...]}
        downsample_step_sec: keep at most one frame per N seconds of game time.
            Reduces data size 30x without losing model-relevant signal.

    Returns:
        Stats dict: {game_id: inserted_frame_count}
    """
    from loltrader.livestats.storage import (
        write_frame,
        register_game_first_seen,
        set_game_start_if_unset,
        parse_rfc460_to_unix,
    )

    stats: dict[str, int] = {}
    for i, gid in enumerate(match.game_ids):
        if gid not in frames_by_game:
            continue
        frames = frames_by_game[gid]
        if not frames:
            continue

        winner = infer_winner_side(frames)
        # Try to look up team codes (separate API call)
        try:
            from loltrader.livestats.discovery import get_team_sides
            sides = get_team_sides(gid)
        except Exception:
            sides = None

        # Register the game (idempotent)
        register_game_first_seen(
            conn, gid, match.league_slug,
            blue_team_code=sides.blue_team_code if sides else None,
            red_team_code=sides.red_team_code if sides else None,
            blue_esports_team_id=sides.blue_esports_team_id if sides else None,
            red_esports_team_id=sides.red_esports_team_id if sides else None,
        )
        # Update backtest-specific fields
        conn.execute(
            """
            UPDATE games_live
            SET winner_side = ?, esports_match_id = ?, game_number = ?, source = 'historical_backtest'
            WHERE game_id = ?
            """,
            (winner, match.match_id, i + 1, gid),
        )

        # Downsample frames: keep one per N-second bucket of game time
        bucket_ts: set[int] = set()
        inserted = 0
        for f in frames:
            ts_str = f.get("rfc460Timestamp", "")
            try:
                ts_unix = parse_rfc460_to_unix(ts_str)
            except ValueError:
                continue
            bucket = ts_unix - (ts_unix % downsample_step_sec)
            if bucket in bucket_ts:
                continue
            bucket_ts.add(bucket)
            if write_frame(conn, gid, f):
                inserted += 1

        # Set game start from earliest in_game frame (if not already set)
        first_in_game = next(
            (f for f in frames if f.get("gameState") == "in_game"), None
        )
        if first_in_game:
            try:
                gs_unix = parse_rfc460_to_unix(first_in_game["rfc460Timestamp"])
                set_game_start_if_unset(conn, gid, gs_unix)
            except ValueError:
                pass

        stats[gid] = inserted
    conn.commit()
    return stats

