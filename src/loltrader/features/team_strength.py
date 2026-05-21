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

from loltrader.features.glicko import GlickoState, update

log = logging.getLogger(__name__)


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

def rebuild_team_glicko(conn: sqlite3.Connection) -> int:
    """Walk every game in chronological order, updating each team's Glicko
    state, and write a snapshot after each game.

    Wipes existing snapshots and recomputes from scratch (this is fast for
    v1 corpus sizes). Returns the number of snapshots written.
    """
    log.info("Rebuilding team Glicko snapshots from scratch")
    start = time.time()
    conn.execute("DELETE FROM team_glicko_snapshots")

    # In-memory current state per team
    states: dict[int, GlickoState] = {}

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

        blue_state = states.get(blue_id) or GlickoState.default()
        red_state = states.get(red_id) or GlickoState.default()

        blue_score = 1.0 if winner == blue_id else 0.0
        red_score = 1.0 if winner == red_id else 0.0

        # Update each team based on this single game.
        # Glicko-2 ratings are updated based on opponent's PRE-game state.
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
    log.info("rebuild_team_glicko: %d snapshots in %.1fs", len(snapshots), elapsed)
    return len(snapshots)


# --- features extractor ---------------------------------------------------

def team_strength_features(
    conn: sqlite3.Connection,
    team_a_id: int,
    team_b_id: int,
    as_of_date: str,
) -> dict[str, float]:
    """Compute strength features for one match as of a date."""
    a = team_rating_as_of(conn, team_a_id, as_of_date)
    b = team_rating_as_of(conn, team_b_id, as_of_date)
    return {
        "team_a_glicko_rating": a.rating,
        "team_a_glicko_rd": a.rd,
        "team_a_glicko_sigma": a.sigma,
        "team_b_glicko_rating": b.rating,
        "team_b_glicko_rd": b.rd,
        "team_b_glicko_sigma": b.sigma,
        "glicko_rating_diff": a.rating - b.rating,
    }
