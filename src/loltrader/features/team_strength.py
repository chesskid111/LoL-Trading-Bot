"""Team-strength features via Glicko-2.

Two responsibilities:
  1. ``rebuild_team_glicko``: walks every game in chronological order,
     updating each team's Glicko state, and persists a snapshot after
     each game it plays. Idempotent.
  2. ``team_rating_as_of``: look up a team's rating snapshot before a
     given date (strict ``<``, no leak).
"""
from __future__ import annotations

import logging
import sqlite3
import time

from loltrader.features.glicko import SCALE, GlickoState, update

log = logging.getLogger(__name__)

# Roster-change detection: if a team retains fewer than this many of
# their previous game's 5 players, treat as a roster change and widen
# Glicko phi to acknowledge increased uncertainty about their true skill.
ROSTER_RETENTION_THRESHOLD = 3       # 5 - this = number of new players triggering reset
ROSTER_CHANGE_PHI_FLOOR = 150 / SCALE  # widen phi to at least ~150 RD on detected change


# --- snapshot lookup ------------------------------------------------------

def team_rating_as_of(
    conn: sqlite3.Connection,
    team_id: int,
    as_of_date: str,
) -> GlickoState:
    """Return the team's Glicko state as of (strictly before) as_of_date.

    If the team has no snapshots before that date (i.e., they hadn't
    played a game yet at that time), returns the default state.
    """
    row = conn.execute(
        """
        SELECT mu, phi, sigma FROM team_glicko_snapshots
        WHERE team_id = ? AND after_date < ?
        ORDER BY after_date DESC, after_game_id DESC
        LIMIT 1
        """,
        (team_id, as_of_date),
    ).fetchone()
    if row is None:
        return GlickoState.default()
    return GlickoState(mu=row["mu"], phi=row["phi"], sigma=row["sigma"])


# --- snapshot rebuild -----------------------------------------------------

def _team_players_for_game(
    conn: sqlite3.Connection, game_id: int, team_id: int
) -> frozenset[int]:
    """Get the set of player_ids that played for this team in this game."""
    rows = conn.execute(
        "SELECT player_id FROM match_player_stats "
        "WHERE game_id = ? AND team_id = ?",
        (game_id, team_id),
    ).fetchall()
    return frozenset(r["player_id"] for r in rows)


def rebuild_team_glicko(conn: sqlite3.Connection) -> int:
    """Walk every game in chronological order, updating each team's Glicko
    state, and write a snapshot after each game.

    Includes roster-change detection (Phase 1.6+): when a team retains
    fewer than ROSTER_RETENTION_THRESHOLD players from their previous
    game's roster, widen their Glicko phi (RD) to ROSTER_CHANGE_PHI_FLOOR
    before applying the new game's update. This acknowledges that we're
    less certain about the team's true skill after a significant
    composition change.

    Wipes existing snapshots and recomputes from scratch (fast for v1
    corpus sizes). Returns the number of snapshots written.
    """
    log.info("Rebuilding team Glicko snapshots from scratch (with roster reset)")
    start = time.time()
    conn.execute("DELETE FROM team_glicko_snapshots")

    states: dict[int, GlickoState] = {}
    last_rosters: dict[int, frozenset[int]] = {}
    roster_resets_count = 0

    rows = conn.execute(
        """
        SELECT mg.game_id, mg.blue_team_id, mg.red_team_id, mg.winner_team_id,
               m.date
        FROM match_games mg
        JOIN matches m ON m.match_id = mg.match_id
        WHERE mg.winner_team_id IS NOT NULL
        ORDER BY m.date ASC, mg.game_id ASC
        """
    ).fetchall()

    log.info("Walking %d games", len(rows))
    snapshots: list[tuple] = []
    for r in rows:
        game_id = r["game_id"]
        blue_id = r["blue_team_id"]
        red_id = r["red_team_id"]
        winner = r["winner_team_id"]
        date = r["date"]

        # Detect roster changes and widen phi BEFORE applying the game.
        # Effect: this game's rating update happens against an opponent
        # whose phi is wider, so updates are larger / faster-adapting.
        for tid in (blue_id, red_id):
            curr_roster = _team_players_for_game(conn, game_id, tid)
            prev_roster = last_rosters.get(tid)
            if prev_roster is not None and curr_roster:
                overlap = len(prev_roster & curr_roster)
                if overlap < ROSTER_RETENTION_THRESHOLD:
                    s = states.get(tid)
                    if s is not None and s.phi < ROSTER_CHANGE_PHI_FLOOR:
                        states[tid] = GlickoState(
                            mu=s.mu,
                            phi=ROSTER_CHANGE_PHI_FLOOR,
                            sigma=s.sigma,
                        )
                        roster_resets_count += 1
            if curr_roster:
                last_rosters[tid] = curr_roster

        blue_state = states.get(blue_id) or GlickoState.default()
        red_state = states.get(red_id) or GlickoState.default()

        blue_score = 1.0 if winner == blue_id else 0.0
        red_score = 1.0 if winner == red_id else 0.0

        new_blue = update(blue_state, [red_state], [blue_score])
        new_red = update(red_state, [blue_state], [red_score])

        states[blue_id] = new_blue
        states[red_id] = new_red

        snapshots.append((
            blue_id, game_id, date,
            new_blue.mu, new_blue.phi, new_blue.sigma,
            new_blue.rating, new_blue.rd,
        ))
        snapshots.append((
            red_id, game_id, date,
            new_red.mu, new_red.phi, new_red.sigma,
            new_red.rating, new_red.rd,
        ))

    log.info("Inserting %d snapshots", len(snapshots))
    conn.executemany(
        """
        INSERT INTO team_glicko_snapshots
            (team_id, after_game_id, after_date, mu, phi, sigma, rating, rd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        snapshots,
    )
    conn.commit()
    elapsed = time.time() - start
    log.info("rebuild_team_glicko: %d snapshots in %.1fs (%d roster resets)",
             len(snapshots), elapsed, roster_resets_count)
    return len(snapshots)


# --- features extractor ---------------------------------------------------

def _had_recent_roster_change(
    conn: sqlite3.Connection,
    team_id: int,
    as_of_date: str,
    window_days: int = 30,
) -> int:
    """Return 1 if the team has had a roster-change-grade composition
    change in the last ``window_days``, else 0.

    Detection: compare the team's most recent game's roster to the
    roster ``window_days`` ago. If overlap < ROSTER_RETENTION_THRESHOLD,
    flag a change.
    """
    rows = conn.execute(
        """
        SELECT mg.game_id, m.date
        FROM match_games mg
        JOIN matches m ON m.match_id = mg.match_id
        WHERE m.date < ?
          AND (mg.blue_team_id = ? OR mg.red_team_id = ?)
        ORDER BY m.date DESC
        LIMIT 30
        """,
        (as_of_date, team_id, team_id),
    ).fetchall()
    if len(rows) < 2:
        return 0
    # Most recent game
    recent = _team_players_for_game(conn, rows[0]["game_id"], team_id)
    # Game from ~window_days ago (or earliest in window if we don't have that many)
    from datetime import datetime, timedelta
    try:
        cutoff = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=window_days)).strftime("%Y-%m-%d")
    except ValueError:
        return 0
    older_row = next((r for r in rows if r["date"] <= cutoff), rows[-1])
    older = _team_players_for_game(conn, older_row["game_id"], team_id)
    if not recent or not older:
        return 0
    overlap = len(recent & older)
    return 1 if overlap < ROSTER_RETENTION_THRESHOLD else 0


def team_strength_features(
    conn: sqlite3.Connection,
    team_a_id: int,
    team_b_id: int,
    as_of_date: str,
) -> dict[str, float]:
    """Compute strength features for one match as of a date."""
    a = team_rating_as_of(conn, team_a_id, as_of_date)
    b = team_rating_as_of(conn, team_b_id, as_of_date)
    a_roster_change = _had_recent_roster_change(conn, team_a_id, as_of_date)
    b_roster_change = _had_recent_roster_change(conn, team_b_id, as_of_date)
    return {
        "team_a_glicko_rating": a.rating,
        "team_a_glicko_rd": a.rd,
        "team_a_glicko_sigma": a.sigma,
        "team_b_glicko_rating": b.rating,
        "team_b_glicko_rd": b.rd,
        "team_b_glicko_sigma": b.sigma,
        "glicko_rating_diff": a.rating - b.rating,
        "team_a_recent_roster_change": float(a_roster_change),
        "team_b_recent_roster_change": float(b_roster_change),
    }
