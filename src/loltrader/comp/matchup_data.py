"""Lane matchup data — pre-computes per-(role, champion_a, champion_b)
winrates from the local Oracle's Elixir match_player_stats data, with
Bayesian shrinkage for sparse matchups.

The spec calls for scraping gol.gg's matchup tables. v1 takes a shortcut:
the same data is computable from our DB, which avoids scraping fragility.
gol.gg can be added later as a cross-source if needed.

Output schema (data/lane_matchups.json):

    {
      "{role}|{champA}|{champB}": {
        "wins_a": int,        # games where champ_a beat champ_b
        "games": int,         # total games this matchup occurred
        "raw_winrate_a": float,
        "shrunk_winrate_a": float  # Bayesian blend with neutral 0.5
      },
      ...
    }

Bayesian shrinkage prevents tiny-sample noise from poisoning the model:
a 3-game 100% matchup gets pulled toward 0.5 with weight 5, so the effective
winrate is (3 + 0.5*5) / (3 + 5) = 0.69 instead of 1.0.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Prior strength for Bayesian shrinkage. A matchup with `games` real
# observations gets blended with PRIOR_WEIGHT "fake" observations at 0.5.
# Higher values = stronger pull toward neutral for small samples.
PRIOR_WEIGHT = 5.0
PRIOR_WINRATE = 0.5


@dataclass
class LaneMatchup:
    role: str
    champion_a: str
    champion_b: str
    wins_a: int
    games: int

    @property
    def raw_winrate_a(self) -> float:
        return self.wins_a / self.games if self.games else 0.5

    @property
    def shrunk_winrate_a(self) -> float:
        """Bayesian-shrunk winrate: blends raw rate with neutral prior."""
        num = self.wins_a + PRIOR_WINRATE * PRIOR_WEIGHT
        den = self.games + PRIOR_WEIGHT
        return num / den


def _key(role: str, champ_a: str, champ_b: str) -> str:
    return f"{role}|{champ_a}|{champ_b}"


# ---------- queries ------------------------------------------------------


def compute_matchups(
    conn: sqlite3.Connection,
    patches: list[str],
    league: str | None = None,
    min_games: int = 1,
) -> list[LaneMatchup]:
    """Per-(role, champion_a, champion_b) winrate from same-role opposing
    pairings in the given patch window.

    Args:
        conn: SQLite connection.
        patches: List of patch versions to aggregate (e.g. ['16.1', '16.08']).
        league: Optional league filter (e.g. 'LCK').
        min_games: Minimum games for a matchup to be returned. Defaults to 1
            so we have entries for every observed pair; downstream consumers
            can apply stricter thresholds.

    Notes:
        - Each game produces TWO entries (champion_a vs champion_b AND
          champion_b vs champion_a). They have the same `games` but
          symmetric `wins_a`.
        - Mirror matches (same champion both sides) are excluded.
    """
    # Resolve patch_ids
    if not patches:
        return []
    qs = ",".join("?" * len(patches))
    patch_ids = [
        r["patch_id"]
        for r in conn.execute(
            f"SELECT patch_id FROM patches WHERE version IN ({qs})", patches
        ).fetchall()
    ]
    if not patch_ids:
        log.warning("No patches found matching %s", patches)
        return []

    pid_qs = ",".join("?" * len(patch_ids))
    league_clause, league_params = ("", ())
    if league:
        league_clause = " AND m.league = ?"
        league_params = (league,)

    # Inner query: every (focus, enemy, role) tuple with wins where focus
    # was on the winning team.
    sql = f"""
        SELECT focus.role AS role,
               focus.champion AS champ_a,
               enemy.champion AS champ_b,
               COUNT(*) AS games,
               SUM(CASE WHEN focus.team_id = g.winner_team_id THEN 1 ELSE 0 END) AS wins_a
        FROM match_player_stats focus
        JOIN match_player_stats enemy
            ON enemy.game_id = focus.game_id
           AND enemy.team_id != focus.team_id
           AND enemy.role = focus.role
        JOIN match_games g ON g.game_id = focus.game_id
        JOIN matches m ON m.match_id = g.match_id
        WHERE g.patch_id IN ({pid_qs})
          AND focus.champion != enemy.champion
          {league_clause}
        GROUP BY focus.role, focus.champion, enemy.champion
        HAVING games >= ?
    """
    rows = conn.execute(sql, (*patch_ids, *league_params, min_games)).fetchall()
    return [
        LaneMatchup(
            role=r["role"],
            champion_a=r["champ_a"],
            champion_b=r["champ_b"],
            wins_a=int(r["wins_a"] or 0),
            games=int(r["games"]),
        )
        for r in rows
    ]


# ---------- persistence --------------------------------------------------


def save_matchups(
    matchups: list[LaneMatchup],
    path: str | Path = "data/lane_matchups.json",
) -> None:
    """Persist matchup data as a flat dict keyed by `role|champA|champB`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, dict] = {}
    for mu in matchups:
        d = asdict(mu)
        d["raw_winrate_a"] = round(mu.raw_winrate_a, 4)
        d["shrunk_winrate_a"] = round(mu.shrunk_winrate_a, 4)
        payload[_key(mu.role, mu.champion_a, mu.champion_b)] = d
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def load_matchups(
    path: str | Path = "data/lane_matchups.json",
) -> dict[str, dict]:
    """Load matchup data from JSON. Returns empty dict if file missing."""
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def lookup_matchup(
    matchups: dict[str, dict],
    role: str,
    champ_a: str,
    champ_b: str,
) -> tuple[float, int]:
    """Return (shrunk_winrate_a, games) for a specific matchup.

    Returns (0.5, 0) when no entry exists — meaning "neutral, no evidence".
    """
    entry = matchups.get(_key(role, champ_a, champ_b))
    if entry:
        return float(entry["shrunk_winrate_a"]), int(entry["games"])
    return 0.5, 0
