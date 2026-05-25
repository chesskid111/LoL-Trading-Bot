"""Standalone demo: poll Riot's livestats API for live pro game data.

Equivalent to what andydanger/live-lol-esports does, in Python. Run this
when LCK / LPL / LEC games are live to verify:
  1. What the actual delay is for the league you care about
  2. What the data looks like
  3. How frequently to poll

Usage:
    python research/live_lol_demo.py            # auto-find live games
    python research/live_lol_demo.py <gameId>   # poll a specific game
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

import requests

API_KEY = "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z"
PERSISTED = "https://esports-api.lolesports.com/persisted/gw"
LIVE = "https://feed.lolesports.com/livestats/v1"


def find_live_games() -> list[dict]:
    """Return all currently-live pro LoL games worldwide."""
    r = requests.get(f"{PERSISTED}/getLive?hl=en-US",
                     headers={"x-api-key": API_KEY}, timeout=10)
    events = r.json()["data"]["schedule"]["events"]
    out = []
    for e in events:
        ed = requests.get(
            f"{PERSISTED}/getEventDetails",
            params={"hl": "en-US", "id": e["id"]},
            headers={"x-api-key": API_KEY}, timeout=10,
        ).json()
        for g in ed["data"]["event"]["match"].get("games", []):
            if g.get("state") == "inProgress":
                out.append({
                    "gameId": g["id"],
                    "league": e["league"]["name"],
                    "teams": [t["name"] for t in e["match"]["teams"]],
                    "gameNumber": g.get("number"),
                })
    return out


def probe_minimum_delay(game_id: str) -> int | None:
    """Binary-search the minimum allowed delay (in seconds) for this game.
    Returns smallest delay that works, or None if even 600s fails."""
    for delay in [30, 45, 60, 75, 90, 120, 180, 300, 600]:
        d = datetime.now(timezone.utc) - timedelta(seconds=delay)
        d = d.replace(microsecond=0, second=d.second - (d.second % 10))
        ts = d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        try:
            r = requests.get(f"{LIVE}/window/{game_id}",
                             params={"startingTime": ts}, timeout=5)
            if r.status_code == 200 and r.json().get("frames"):
                return delay
        except Exception:
            pass
    return None


def get_frame(game_id: str, delay_sec: int) -> dict | None:
    """Get the most recent available frame at the given delay."""
    d = datetime.now(timezone.utc) - timedelta(seconds=delay_sec)
    d = d.replace(microsecond=0, second=d.second - (d.second % 10))
    ts = d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    r = requests.get(f"{LIVE}/window/{game_id}",
                     params={"startingTime": ts}, timeout=5)
    if r.status_code != 200:
        return None
    frames = r.json().get("frames") or []
    return frames[-1] if frames else None


def get_team_sides(game_id: str) -> tuple[str, str]:
    """Return (blue_team_code, red_team_code) for the given gameId.

    Reads gameMetadata.{blueTeamMetadata,redTeamMetadata} and derives the
    team code from the first participant's summonerName prefix (e.g. 'TLAW Morgan').
    Falls back to esportsTeamId if no participants are present.
    """
    d = datetime.now(timezone.utc) - timedelta(seconds=60)
    d = d.replace(microsecond=0, second=d.second - (d.second % 10))
    ts = d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    try:
        r = requests.get(f"{LIVE}/window/{game_id}",
                         params={"startingTime": ts}, timeout=5)
        if r.status_code != 200:
            return ("BLUE", "RED")
        gm = r.json().get("gameMetadata", {})

        def _code(side_key: str) -> str:
            side = gm.get(side_key, {})
            parts = side.get("participantMetadata", []) or []
            if parts:
                name = parts[0].get("summonerName", "")
                if " " in name:
                    return name.split(" ", 1)[0]
            return side.get("esportsTeamId", side_key[:-12].upper())

        return (_code("blueTeamMetadata"), _code("redTeamMetadata"))
    except Exception:
        return ("BLUE", "RED")


def find_game_start_ts(game_id: str) -> datetime | None:
    """Binary-search backwards to find the first in_game frame's wall-clock ts.

    The window endpoint only serves recent slices, so we query a range of
    candidate startingTimes (now-2h to now-30s) and find the earliest one
    that returns a frame whose gameState is 'in_game'. ~7 API calls.
    Returns None if game start can't be located (game ended/older than serving window).
    """
    now = datetime.now(timezone.utc)
    # Candidates: 2h, 90m, 60m, 45m, 30m, 20m, 10m, 5m, 2m, 1m ago
    candidates_min = [120, 90, 60, 45, 30, 20, 10, 5, 2, 1]
    earliest_in_game: datetime | None = None
    for mins in candidates_min:
        d = now - timedelta(minutes=mins)
        d = d.replace(microsecond=0, second=d.second - (d.second % 10))
        ts = d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        try:
            r = requests.get(f"{LIVE}/window/{game_id}",
                             params={"startingTime": ts}, timeout=5)
            if r.status_code != 200 or not r.text.strip():
                continue
            frames = r.json().get("frames") or []
            for f in frames:
                if f.get("gameState") == "in_game":
                    ft = datetime.strptime(f["rfc460Timestamp"][:19],
                                           "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                    if earliest_in_game is None or ft < earliest_in_game:
                        earliest_in_game = ft
                    break
        except Exception:
            continue
    return earliest_in_game


def fmt_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def print_frame(f: dict, game_start: datetime | None = None,
                blue_name: str = "BLUE", red_name: str = "RED") -> None:
    """Pretty-print the latest game state, with in-game clock if available."""
    b, red = f["blueTeam"], f["redTeam"]
    ts_str = f['rfc460Timestamp']
    in_game_clock = ""
    if game_start is not None:
        ft = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        elapsed = (ft - game_start).total_seconds()
        if elapsed >= 0:
            in_game_clock = f"  game={fmt_elapsed(elapsed)}"
    blabel = f"{blue_name} (blue)"
    rlabel = f"{red_name} (red)"
    w = max(len(blabel), len(rlabel))
    print(f"  Time:  {ts_str}  state={f['gameState']}{in_game_clock}")
    print(f"  {blabel:<{w}}: gold={b['totalGold']:>7,}  K/T/I = {b['totalKills']}/"
          f"{b['towers']}/{b['inhibitors']}  drakes={b['dragons']}  barons={b['barons']}")
    print(f"  {rlabel:<{w}}: gold={red['totalGold']:>7,}  K/T/I = {red['totalKills']}/"
          f"{red['towers']}/{red['inhibitors']}  drakes={red['dragons']}  barons={red['barons']}")
    diff = b["totalGold"] - red["totalGold"]
    leader = blue_name if diff > 0 else red_name
    print(f"  Lead:  {abs(diff):>+7,}g for {leader}")


def main() -> int:
    if len(sys.argv) > 1:
        # Specific gameId mode
        gid = sys.argv[1]
        print(f"Probing min delay for game {gid}...")
        delay = probe_minimum_delay(gid)
        if delay is None:
            print("  No data available even at 600s delay. Game might not be live.")
            return 1
        print(f"  Minimum working delay: {delay}s")
        print("Locating game start (binary-searching window endpoint)...")
        game_start = find_game_start_ts(gid)
        if game_start:
            print(f"  Game started at: {game_start.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        else:
            print("  Could not locate game start; in-game clock will be unavailable.")
        blue_name, red_name = get_team_sides(gid)
        print(f"  Sides: {blue_name} (blue) vs {red_name} (red)")
        print()
        print("Polling every 5 seconds. Ctrl+C to stop.")
        try:
            while True:
                f = get_frame(gid, delay)
                if f:
                    print()
                    print(f"=== {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC ===")
                    print_frame(f, game_start, blue_name, red_name)
                time.sleep(5)
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    # Auto-discover mode
    print("Finding live LoL pro games...")
    games = find_live_games()
    if not games:
        print("No live games right now. Try again during pro game hours.")
        print("LCK plays at 1-3 AM PDT, LEC at 9-15 PDT, LCS evenings, etc.")
        return 0

    print(f"Found {len(games)} live game(s):")
    print()
    for g in games:
        print(f"  [{g['league']}] {g['teams'][0]} vs {g['teams'][1]} (game {g['gameNumber']})")
        print(f"    gameId: {g['gameId']}")
        delay = probe_minimum_delay(g['gameId'])
        if delay is None:
            print(f"    ERROR: no data available within 600s delay")
        else:
            print(f"    >>> MIN DELAY: {delay}s for this league <<<")
            game_start = find_game_start_ts(g['gameId'])
            if game_start:
                print(f"    Game started: {game_start.strftime('%H:%M:%SZ')}")
            blue_name, red_name = get_team_sides(g['gameId'])
            print(f"    Sides: {blue_name} (blue) vs {red_name} (red)")
            f = get_frame(g['gameId'], delay)
            if f:
                print_frame(f, game_start, blue_name, red_name)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
