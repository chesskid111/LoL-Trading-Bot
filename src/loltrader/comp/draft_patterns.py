"""Per-champion pro draft patterns derived from match_drafts + match_games.

These are FACTS from pro games, not opinions. We feed them into the LLM
curator so the model's qualitative scoring is grounded in actual pro
behavior, not training-data bias (which is heavily solo-queue weighted).

What we compute per champion, filterable by patch + league:
  - Top partners: champions most often on the same team
  - Top opposing matchups: champions picked across in the same role,
    plus winrate against this champion
  - Role distribution: actual roles played, weighted by frequency
  - Player comfort: top players who play this champion, with winrates
  - Pick order pattern: median pick order (first pick vs counter pick)

The rolling window is configurable — typical use is last 3-5 patches or
last 90 days so the sample size per matchup is reasonable. Pro LoL has
relatively few games per patch, so single-patch data is often too sparse
to trust.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class PartnerStat:
    champion: str
    games_together: int
    wins_together: int

    @property
    def winrate(self) -> float:
        return self.wins_together / self.games_together if self.games_together else 0.0


@dataclass
class CounterStat:
    champion: str
    games_against: int
    losses_to_them: int  # how often the focus champion lost when this opposed

    @property
    def lose_rate(self) -> float:
        return self.losses_to_them / self.games_against if self.games_against else 0.0


@dataclass
class PlayerComfort:
    player_id: int
    player_name: str
    games: int
    wins: int

    @property
    def winrate(self) -> float:
        return self.wins / self.games if self.games else 0.0


@dataclass
class DraftPatterns:
    """Everything we know about how a champion is played in pro right now."""
    champion: str
    patches: list[str]
    league: str | None
    top_partners: list[PartnerStat]
    top_counters: list[CounterStat]   # opposing the focus champion in same role
    role_distribution: dict[str, int]  # role → games picked there
    top_players: list[PlayerComfort]
    median_pick_order: float | None
    times_first_pick: int
    times_counter_pick: int            # last pick or 4th/5th
    total_games_picked: int


# ---------- helpers -----------------------------------------------------


def _patch_ids_for(conn: sqlite3.Connection, patches: list[str]) -> list[int]:
    if not patches:
        return []
    qs = ",".join("?" * len(patches))
    return [
        r["patch_id"]
        for r in conn.execute(
            f"SELECT patch_id FROM patches WHERE version IN ({qs})", patches
        ).fetchall()
    ]


def _league_clause(league: str | None) -> tuple[str, tuple]:
    """Return ('AND m.league = ?', (league,)) or ('', ()) if no filter."""
    if league:
        return " AND m.league = ?", (league,)
    return "", ()


# ---------- queries -----------------------------------------------------


def top_partners(
    conn: sqlite3.Connection,
    champion: str,
    patches: list[str],
    league: str | None = None,
    limit: int = 10,
) -> list[PartnerStat]:
    """Champions most often drafted on the same team as the focus champion."""
    patch_ids = _patch_ids_for(conn, patches)
    if not patch_ids:
        return []
    league_sql, league_params = _league_clause(league)
    pid_qs = ",".join("?" * len(patch_ids))
    rows = conn.execute(
        f"""
        SELECT partner.champion AS partner,
               COUNT(*) AS games_together,
               SUM(CASE WHEN focus.team_id = g.winner_team_id THEN 1 ELSE 0 END) AS wins_together
        FROM match_drafts focus
        JOIN match_drafts partner
            ON focus.game_id = partner.game_id
           AND focus.team_id = partner.team_id
           AND focus.champion != partner.champion
        JOIN match_games g ON g.game_id = focus.game_id
        JOIN matches m ON m.match_id = g.match_id
        WHERE focus.is_ban = 0 AND partner.is_ban = 0
          AND focus.champion = ?
          AND g.patch_id IN ({pid_qs})
          {league_sql}
        GROUP BY partner.champion
        HAVING games_together >= 2
        ORDER BY games_together DESC, wins_together DESC
        LIMIT ?
        """,
        (champion, *patch_ids, *league_params, limit),
    ).fetchall()
    return [
        PartnerStat(
            champion=r["partner"],
            games_together=r["games_together"],
            wins_together=r["wins_together"],
        )
        for r in rows
    ]


def top_counters(
    conn: sqlite3.Connection,
    champion: str,
    patches: list[str],
    league: str | None = None,
    limit: int = 10,
) -> list[CounterStat]:
    """Champions picked across in the same role, with how often the focus loses."""
    patch_ids = _patch_ids_for(conn, patches)
    if not patch_ids:
        return []
    league_sql, league_params = _league_clause(league)
    pid_qs = ",".join("?" * len(patch_ids))
    # NOTE: match_drafts.role is NULL in Oracle's Elixir, so we route through
    # match_player_stats which has the role field populated. Same join logic
    # otherwise — same role on opposing teams.
    rows = conn.execute(
        f"""
        SELECT enemy.champion AS enemy,
               COUNT(*) AS games_against,
               SUM(CASE WHEN focus.team_id != g.winner_team_id THEN 1 ELSE 0 END) AS losses
        FROM match_player_stats focus
        JOIN match_player_stats enemy
            ON focus.game_id = enemy.game_id
           AND focus.team_id != enemy.team_id
           AND focus.role = enemy.role
        JOIN match_games g ON g.game_id = focus.game_id
        JOIN matches m ON m.match_id = g.match_id
        WHERE focus.champion = ?
          AND g.patch_id IN ({pid_qs})
          {league_sql}
        GROUP BY enemy.champion
        HAVING games_against >= 2
        ORDER BY losses DESC, games_against DESC
        LIMIT ?
        """,
        (champion, *patch_ids, *league_params, limit),
    ).fetchall()
    return [
        CounterStat(
            champion=r["enemy"],
            games_against=r["games_against"],
            losses_to_them=r["losses"],
        )
        for r in rows
    ]


def role_distribution(
    conn: sqlite3.Connection,
    champion: str,
    patches: list[str],
    league: str | None = None,
) -> dict[str, int]:
    """How often the champion is played in each role."""
    patch_ids = _patch_ids_for(conn, patches)
    if not patch_ids:
        return {}
    league_sql, league_params = _league_clause(league)
    pid_qs = ",".join("?" * len(patch_ids))
    # NOTE: match_drafts.role is NULL in this DB — use match_player_stats which
    # has role populated for each (game, player, champion) row.
    rows = conn.execute(
        f"""
        SELECT mps.role, COUNT(*) AS picks
        FROM match_player_stats mps
        JOIN match_games g ON g.game_id = mps.game_id
        JOIN matches m ON m.match_id = g.match_id
        WHERE mps.champion = ?
          AND g.patch_id IN ({pid_qs})
          {league_sql}
        GROUP BY mps.role
        ORDER BY picks DESC
        """,
        (champion, *patch_ids, *league_params),
    ).fetchall()
    return {r["role"]: r["picks"] for r in rows if r["role"]}


def top_players(
    conn: sqlite3.Connection,
    champion: str,
    patches: list[str],
    league: str | None = None,
    min_games: int = 3,
    limit: int = 5,
) -> list[PlayerComfort]:
    """Players who have played this champion the most, with winrates."""
    patch_ids = _patch_ids_for(conn, patches)
    if not patch_ids:
        return []
    league_sql, league_params = _league_clause(league)
    pid_qs = ",".join("?" * len(patch_ids))
    rows = conn.execute(
        f"""
        SELECT mp.player_id,
               COALESCE(p.ign, 'unknown') AS name,
               COUNT(*) AS games,
               SUM(CASE WHEN mp.team_id = g.winner_team_id THEN 1 ELSE 0 END) AS wins
        FROM match_player_stats mp
        JOIN match_games g ON g.game_id = mp.game_id
        JOIN matches m ON m.match_id = g.match_id
        LEFT JOIN players p ON p.player_id = mp.player_id
        WHERE mp.champion = ?
          AND g.patch_id IN ({pid_qs})
          {league_sql}
        GROUP BY mp.player_id
        HAVING games >= ?
        ORDER BY wins DESC, games DESC
        LIMIT ?
        """,
        (champion, *patch_ids, *league_params, min_games, limit),
    ).fetchall()
    return [
        PlayerComfort(
            player_id=r["player_id"],
            player_name=r["name"],
            games=r["games"],
            wins=r["wins"],
        )
        for r in rows
    ]


def pick_order_stats(
    conn: sqlite3.Connection,
    champion: str,
    patches: list[str],
    league: str | None = None,
) -> tuple[float | None, int, int, int]:
    """Returns (median_order, first_picks, counter_picks, total_picks).

    A counter pick = pick_order >= 4 (5th, 6th, 7th, 8th, 9th, 10th overall pick).
    A first pick = pick_order == 1 (first pick on either team in draft phase).
    """
    patch_ids = _patch_ids_for(conn, patches)
    if not patch_ids:
        return (None, 0, 0, 0)
    league_sql, league_params = _league_clause(league)
    pid_qs = ",".join("?" * len(patch_ids))
    rows = conn.execute(
        f"""
        SELECT d.pick_order
        FROM match_drafts d
        JOIN match_games g ON g.game_id = d.game_id
        JOIN matches m ON m.match_id = g.match_id
        WHERE d.is_ban = 0 AND d.champion = ?
          AND g.patch_id IN ({pid_qs})
          {league_sql}
        """,
        (champion, *patch_ids, *league_params),
    ).fetchall()
    orders = [r["pick_order"] for r in rows if r["pick_order"] is not None]
    if not orders:
        return (None, 0, 0, 0)
    orders.sort()
    median = orders[len(orders) // 2]
    first_picks = sum(1 for o in orders if o == 1)
    counter_picks = sum(1 for o in orders if o >= 4)
    return (median, first_picks, counter_picks, len(orders))


# ---------- main entry point --------------------------------------------


def get_draft_patterns(
    conn: sqlite3.Connection,
    champion: str,
    patches: list[str],
    league: str | None = None,
    partners_limit: int = 10,
    counters_limit: int = 10,
    players_limit: int = 5,
) -> DraftPatterns:
    """Aggregate all draft patterns for one champion in a single call.

    This is what the LLM curator calls before building its prompt — one
    SQL-heavy invocation per champion is fine, queries are well-indexed.
    """
    median, first_picks, counter_picks, total = pick_order_stats(
        conn, champion, patches, league
    )
    return DraftPatterns(
        champion=champion,
        patches=patches,
        league=league,
        top_partners=top_partners(conn, champion, patches, league, partners_limit),
        top_counters=top_counters(conn, champion, patches, league, counters_limit),
        role_distribution=role_distribution(conn, champion, patches, league),
        top_players=top_players(conn, champion, patches, league, limit=players_limit),
        median_pick_order=median,
        times_first_pick=first_picks,
        times_counter_pick=counter_picks,
        total_games_picked=total,
    )


def format_patterns_for_prompt(p: DraftPatterns) -> str:
    """Render DraftPatterns as a compact human-readable block for prompt injection.

    Trade-off: enough detail for the LLM to ground its scoring, but compact
    so each prompt stays under ~1-2k tokens. We sort partners/counters by
    game count (most evidence first) and show winrate context.
    """
    if p.total_games_picked == 0:
        return f"No pro games found for {p.champion} in window. Use general knowledge + web search."

    lines: list[str] = []
    lines.append(f"## Pro draft patterns for {p.champion}")
    lines.append(
        f"Window: patches {','.join(p.patches)}"
        + (f" league={p.league}" if p.league else " all leagues")
    )
    lines.append(f"Total pro games picked: {p.total_games_picked}")

    if p.role_distribution:
        roles = ", ".join(
            f"{r}={n}" for r, n in
            sorted(p.role_distribution.items(), key=lambda x: -x[1])
        )
        lines.append(f"Roles played: {roles}")

    if p.median_pick_order is not None:
        lines.append(
            f"Pick order: median={p.median_pick_order:.0f}, "
            f"first-picked={p.times_first_pick}x, counter-picked={p.times_counter_pick}x"
        )

    if p.top_partners:
        lines.append("Top teammates (same team, sorted by games together):")
        for partner in p.top_partners:
            lines.append(
                f"  - {partner.champion}: {partner.games_together} games, "
                f"{partner.winrate*100:.0f}% wins"
            )

    if p.top_counters:
        lines.append("Top opposing matchups (same role, sorted by losses to them):")
        for c in p.top_counters:
            lines.append(
                f"  - {c.champion}: {c.games_against} games, "
                f"{p.champion} lost {c.lose_rate*100:.0f}% of those"
            )

    if p.top_players:
        lines.append("Top players on this champion (sorted by wins):")
        for pl in p.top_players:
            lines.append(
                f"  - {pl.player_name}: {pl.games} games, {pl.winrate*100:.0f}% wr"
            )

    return "\n".join(lines)
