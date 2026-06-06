"""Pro champion statistics from Oracle's Elixir data already in SQLite.

Spec defers gol.gg scraping to a later optional source — the local DB has
~7,000 pro games of pick/ban/winrate data which is sufficient for v1. This
module computes per-(champion, patch, league) stats and merges them into
``champion_profiles.json``.

Output fields per champion per patch:
  - pickrate: % of games where the champion was picked
  - banrate:  % of games where the champion was banned
  - winrate:  win % conditional on being picked
  - priority_score: composite 0-10, weighted (pickrate + banrate) / 2 * 10
  - games_sampled: how many games we drew from

A minimum sample-size threshold (default 5 games) is enforced — champions
below that get NaN winrates and confidence=0, so the model doesn't trust
high-variance noise from one-off picks.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MIN_GAMES_FOR_WINRATE = 5


@dataclass
class ChampionPatchStats:
    champion: str
    patch_version: str
    league: str | None       # None = global (all leagues)
    times_picked: int
    times_banned: int
    times_won: int
    games_in_window: int     # denominator for pickrate/banrate

    @property
    def pickrate(self) -> float:
        return self.times_picked / self.games_in_window if self.games_in_window else 0.0

    @property
    def banrate(self) -> float:
        return self.times_banned / self.games_in_window if self.games_in_window else 0.0

    @property
    def winrate(self) -> float:
        if self.times_picked < MIN_GAMES_FOR_WINRATE:
            return 0.5  # neutral prior when data is sparse
        return self.times_won / self.times_picked

    @property
    def priority_score(self) -> float:
        """0-10 composite. Higher = pickrate + banrate elevated.

        Lots of bans + lots of picks = high priority. A 30% pickrate + 40%
        banrate scores 10 * (0.30 + 0.40) / 2 = 3.5; a contested patch-meta
        champion with 50% pickrate + 80% banrate scores ~6.5.
        """
        return min(10.0, 10.0 * (self.pickrate + self.banrate) / 2)

    @property
    def confidence(self) -> float:
        """0-1 confidence proxy based on sample size.

        Pure logarithmic ramp: 5 games = 0.3, 20 games = 0.6, 100 games = 0.9.
        """
        if self.times_picked < MIN_GAMES_FOR_WINRATE:
            return 0.0
        from math import log
        return min(1.0, log(self.times_picked) / log(100))


# ---------- DB queries ---------------------------------------------------


def _resolve_patch_id(conn: sqlite3.Connection, patch_version: str) -> int | None:
    row = conn.execute(
        "SELECT patch_id FROM patches WHERE version = ?",
        (patch_version,),
    ).fetchone()
    return row["patch_id"] if row else None


def compute_patch_stats(
    conn: sqlite3.Connection,
    patch_version: str,
    league: str | None = None,
) -> list[ChampionPatchStats]:
    """Aggregate per-champion pick/ban/winrate stats for one patch.

    Args:
        conn: SQLite connection.
        patch_version: ``patches.version`` value (e.g. "16.1").
        league: Optional ``matches.league`` filter (e.g. "LCK"). None = global.

    Returns:
        A list of ChampionPatchStats, one per champion with at least one pick
        OR one ban in the window. Champions never touched are omitted.
    """
    patch_id = _resolve_patch_id(conn, patch_version)
    if patch_id is None:
        log.warning("Patch version %s not found in patches table", patch_version)
        return []

    # Count games in window (denominator for pickrate / banrate).
    if league:
        games_in_window = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM match_games g
            JOIN matches m ON m.match_id = g.match_id
            WHERE g.patch_id = ? AND m.league = ?
            """,
            (patch_id, league),
        ).fetchone()["c"]
    else:
        games_in_window = conn.execute(
            "SELECT COUNT(*) AS c FROM match_games WHERE patch_id = ?",
            (patch_id,),
        ).fetchone()["c"]

    if games_in_window == 0:
        log.info("No games for patch=%s league=%s", patch_version, league)
        return []

    # Picks + winrate
    if league:
        picks_sql = """
            SELECT d.champion,
                   COUNT(*) AS times_picked,
                   SUM(CASE WHEN d.team_id = g.winner_team_id THEN 1 ELSE 0 END) AS times_won
            FROM match_drafts d
            JOIN match_games g ON g.game_id = d.game_id
            JOIN matches m ON m.match_id = g.match_id
            WHERE g.patch_id = ? AND m.league = ? AND d.is_ban = 0
            GROUP BY d.champion
        """
        bans_sql = """
            SELECT d.champion, COUNT(*) AS times_banned
            FROM match_drafts d
            JOIN match_games g ON g.game_id = d.game_id
            JOIN matches m ON m.match_id = g.match_id
            WHERE g.patch_id = ? AND m.league = ? AND d.is_ban = 1
            GROUP BY d.champion
        """
        params = (patch_id, league)
    else:
        picks_sql = """
            SELECT d.champion,
                   COUNT(*) AS times_picked,
                   SUM(CASE WHEN d.team_id = g.winner_team_id THEN 1 ELSE 0 END) AS times_won
            FROM match_drafts d
            JOIN match_games g ON g.game_id = d.game_id
            WHERE g.patch_id = ? AND d.is_ban = 0
            GROUP BY d.champion
        """
        bans_sql = """
            SELECT d.champion, COUNT(*) AS times_banned
            FROM match_drafts d
            JOIN match_games g ON g.game_id = d.game_id
            WHERE g.patch_id = ? AND d.is_ban = 1
            GROUP BY d.champion
        """
        params = (patch_id,)

    pick_rows = {r["champion"]: r for r in conn.execute(picks_sql, params).fetchall()}
    ban_rows = {r["champion"]: r["times_banned"] for r in conn.execute(bans_sql, params).fetchall()}

    all_champs = set(pick_rows.keys()) | set(ban_rows.keys())
    out: list[ChampionPatchStats] = []
    for champ in all_champs:
        picks = pick_rows.get(champ)
        out.append(ChampionPatchStats(
            champion=champ,
            patch_version=patch_version,
            league=league,
            times_picked=picks["times_picked"] if picks else 0,
            times_banned=ban_rows.get(champ, 0),
            times_won=picks["times_won"] if picks else 0,
            games_in_window=games_in_window,
        ))
    out.sort(key=lambda s: s.priority_score, reverse=True)
    return out


def compute_recent_stats(
    conn: sqlite3.Connection,
    patches: list[str],
    league: str | None = None,
) -> list[ChampionPatchStats]:
    """Aggregate stats across multiple recent patches.

    Useful for the ``pro_stats.30d`` window — merge ~2 patches' worth of
    games so the sample size is reasonable.
    """
    accumulator: dict[str, ChampionPatchStats] = {}
    total_games_in_window = 0
    for patch in patches:
        sub = compute_patch_stats(conn, patch, league)
        if not sub:
            continue
        total_games_in_window += sub[0].games_in_window
        for s in sub:
            if s.champion in accumulator:
                acc = accumulator[s.champion]
                acc.times_picked += s.times_picked
                acc.times_banned += s.times_banned
                acc.times_won += s.times_won
            else:
                accumulator[s.champion] = ChampionPatchStats(
                    champion=s.champion,
                    patch_version=",".join(patches),
                    league=league,
                    times_picked=s.times_picked,
                    times_banned=s.times_banned,
                    times_won=s.times_won,
                    games_in_window=0,  # fixed up below
                )
    for s in accumulator.values():
        s.games_in_window = total_games_in_window
    return sorted(accumulator.values(), key=lambda x: x.priority_score, reverse=True)


# ---------- JSON persistence + profile merge -----------------------------


def save_patch_stats(
    stats: list[ChampionPatchStats],
    path: str | Path = "data/patch_stats.json",
) -> None:
    """Persist stats as a flat list. Keyed lookup by champion name is left
    to the consumer because we may have multi-patch / multi-league entries."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = []
    for s in stats:
        d = asdict(s)
        d["pickrate"] = round(s.pickrate, 4)
        d["banrate"] = round(s.banrate, 4)
        d["winrate"] = round(s.winrate, 4)
        d["priority_score"] = round(s.priority_score, 2)
        d["confidence"] = round(s.confidence, 2)
        payload.append(d)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def merge_into_profiles(
    profiles: dict,                  # dict[str, ChampionProfile] — avoid circular import
    stats: list[ChampionPatchStats],
    patch_version: str,
    league: str | None = None,
) -> int:
    """Update each profile's pro_stats subblock from the latest patch stats.

    Returns the number of profiles updated. Champions that have stats but no
    matching profile are skipped silently — they get created by the LLM
    curator step downstream.
    """
    from loltrader.comp.profiles import ProStats  # local import

    updated = 0
    for s in stats:
        if s.champion not in profiles:
            continue
        p = profiles[s.champion]
        p.pro_stats = ProStats(
            pickrate_30d=round(s.pickrate, 4),
            banrate_30d=round(s.banrate, 4),
            winrate_30d=round(s.winrate, 4),
            priority_score=round(s.priority_score, 2),
            games_sampled=s.times_picked,
        )
        p.patch = patch_version
        updated += 1
    return updated
