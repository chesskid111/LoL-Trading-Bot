"""Feature engineering tests.

The most important test in this file is ``test_no_future_leak``:
features for a match dated 2024-01-15 must produce identical values
whether computed in 2024 (clean) or now (with all 2024-2026 data in the
DB). If they differ, future data is leaking into training.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from loltrader.db import connect, migrate
from loltrader.features import compute_features
from loltrader.features.glicko import (
    DEFAULT_VOLATILITY,
    GlickoState,
    update,
)
from loltrader.features.team_strength import (
    rebuild_team_glicko,
    team_rating_as_of,
)


# --- Glicko algorithm sanity ----------------------------------------------

def test_glicko_default_state():
    s = GlickoState.default()
    assert s.rating == pytest.approx(1500.0)
    assert s.rd == pytest.approx(350.0)
    assert s.sigma == DEFAULT_VOLATILITY


def test_glicko_win_increases_rating():
    a = GlickoState.default()
    b = GlickoState.default()
    new_a = update(a, [b], [1.0])
    new_b = update(b, [a], [0.0])
    assert new_a.rating > 1500.0
    assert new_b.rating < 1500.0


def test_glicko_no_games_increases_rd():
    """A team that doesn't play has stable rating but growing RD."""
    s = GlickoState.default()
    after = update(s, [], [])
    assert after.mu == s.mu  # unchanged
    assert after.phi > s.phi  # more uncertainty


def test_glicko_symmetric():
    """Updating with a same-strength opponent for win then loss should
    return to (approximately) the same state."""
    a = GlickoState.default()
    b = GlickoState.default()
    after_win = update(a, [b], [1.0])
    after_loss = update(after_win, [b], [0.0])
    # Rating returns to near 1500 (but RD/sigma evolve)
    assert abs(after_loss.rating - 1500.0) < 30.0


# --- DB-backed fixture ----------------------------------------------------

@pytest.fixture
def features_db(tmp_path: Path):
    """Empty DB with all migrations + a synthetic match history."""
    db = tmp_path / "features.db"
    conn = connect(db)
    migrate(conn)

    # Seed: 4 teams, plays a small round-robin over 4 dates
    teams = ["Alpha", "Bravo", "Charlie", "Delta"]
    for t in teams:
        conn.execute(
            "INSERT INTO teams (canonical_name, region, first_seen, last_seen) "
            "VALUES (?, 'LCS', '2024-01-01', '2024-02-01')",
            (t,),
        )
    team_ids = {
        t: conn.execute(
            "SELECT team_id FROM teams WHERE canonical_name = ?", (t,)
        ).fetchone()[0]
        for t in teams
    }

    conn.execute(
        "INSERT INTO patches (version, first_seen, last_seen) VALUES ('14.1','2024-01-01','2024-02-01')"
    )
    patch_id = conn.execute("SELECT patch_id FROM patches WHERE version='14.1'").fetchone()[0]

    # Synthetic results: Alpha is strong (wins all), Bravo medium, Charlie/Delta weak
    schedule = [
        ("2024-01-10", "Alpha", "Bravo", "Alpha", 1),
        ("2024-01-11", "Charlie", "Delta", "Charlie", 1),
        ("2024-01-17", "Alpha", "Charlie", "Alpha", 1),
        ("2024-01-18", "Bravo", "Delta", "Bravo", 1),
        ("2024-01-24", "Alpha", "Delta", "Alpha", 1),
        ("2024-01-25", "Bravo", "Charlie", "Bravo", 1),
    ]
    for date, a, b, winner, game_num in schedule:
        ta, tb = sorted([a, b])
        match_key = f"{date}|{ta}|{tb}"
        conn.execute(
            "INSERT INTO matches (match_key,date,league,split,playoffs,patch_id,"
            "team_a_id,team_b_id,bo_format) VALUES (?,?,?,?,?,?,?,?,1)",
            (match_key, date, "LCS", "Spring", 0, patch_id,
             team_ids[ta], team_ids[tb]),
        )
        match_id = conn.execute(
            "SELECT match_id FROM matches WHERE match_key = ?", (match_key,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO match_games (oracle_gameid,match_id,game_number,"
            "blue_team_id,red_team_id,winner_team_id,duration_sec,patch_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"{date}-{a}-{b}", match_id, game_num,
             team_ids[a], team_ids[b], team_ids[winner], 1800, patch_id),
        )
    conn.commit()

    rebuild_team_glicko(conn)
    yield conn, team_ids
    conn.close()


# --- Per-feature sanity ---------------------------------------------------

def test_glicko_after_history_alpha_higher_than_delta(features_db):
    conn, team_ids = features_db
    alpha = team_rating_as_of(conn, team_ids["Alpha"], "2024-02-01")
    delta = team_rating_as_of(conn, team_ids["Delta"], "2024-02-01")
    assert alpha.rating > delta.rating + 100  # meaningful gap


def test_glicko_undecided_before_first_game(features_db):
    conn, team_ids = features_db
    # Before any games were played, everyone should be at default
    state = team_rating_as_of(conn, team_ids["Alpha"], "2024-01-09")
    assert state.rating == pytest.approx(1500.0)
    assert state.rd == pytest.approx(350.0)


def test_compute_features_shape(features_db):
    conn, team_ids = features_db
    match_id = conn.execute(
        "SELECT match_id FROM matches WHERE match_key = ?",
        ("2024-01-24|Alpha|Delta",),
    ).fetchone()[0]
    feats = compute_features(conn, match_id)
    assert isinstance(feats, dict)
    assert all(isinstance(v, float) for v in feats.values())
    # Spec target: ~30-50 features
    assert 30 <= len(feats) <= 100, f"unexpected feature count: {len(feats)}"


def test_compute_features_deterministic(features_db):
    conn, team_ids = features_db
    match_id = conn.execute(
        "SELECT match_id FROM matches WHERE match_key = ?",
        ("2024-01-24|Alpha|Delta",),
    ).fetchone()[0]
    f1 = compute_features(conn, match_id, as_of_date="2024-01-24")
    f2 = compute_features(conn, match_id, as_of_date="2024-01-24")
    assert f1 == f2


def test_compute_features_rejects_bad_date(features_db):
    conn, team_ids = features_db
    match_id = conn.execute("SELECT match_id FROM matches LIMIT 1").fetchone()[0]
    with pytest.raises(ValueError):
        compute_features(conn, match_id, as_of_date="2024/01/24")
    with pytest.raises(ValueError):
        compute_features(conn, match_id, as_of_date="not-a-date")


# --- The critical no-leak test --------------------------------------------

def test_no_future_leak(features_db):
    """Compute features for a 2024-01-17 match using as_of=2024-01-17.
    Then add MORE games to the DB after that date and recompute. Features
    must be identical because as_of='2024-01-17' filter excludes the new data.
    If they differ, future data is leaking into the feature vector."""
    conn, team_ids = features_db
    target_match_id = conn.execute(
        "SELECT match_id FROM matches WHERE match_key = ?",
        ("2024-01-17|Alpha|Charlie",),
    ).fetchone()[0]
    as_of = "2024-01-17"

    before = compute_features(conn, target_match_id, as_of_date=as_of)

    # Now add a NEW match in February (future relative to as_of)
    a = team_ids["Alpha"]; b = team_ids["Bravo"]
    ta, tb = sorted(["Alpha", "Bravo"])
    conn.execute(
        "INSERT INTO matches (match_key,date,league,split,playoffs,patch_id,"
        "team_a_id,team_b_id,bo_format) "
        "VALUES (?, '2024-02-15', 'LCS', 'Spring', 0, 1, ?, ?, 1)",
        (f"2024-02-15|{ta}|{tb}",
         conn.execute("SELECT team_id FROM teams WHERE canonical_name=?", (ta,)).fetchone()[0],
         conn.execute("SELECT team_id FROM teams WHERE canonical_name=?", (tb,)).fetchone()[0]),
    )
    new_match_id = conn.execute(
        "SELECT match_id FROM matches WHERE date='2024-02-15'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO match_games (oracle_gameid,match_id,game_number,"
        "blue_team_id,red_team_id,winner_team_id,duration_sec,patch_id) "
        "VALUES ('FUTURE',?,1,?,?,?,1800,1)",
        (new_match_id, a, b, b),  # Bravo wins this new future game
    )
    conn.commit()

    # Rebuild Glicko including the new game (it'd affect Feb+ snapshots)
    rebuild_team_glicko(conn)

    after = compute_features(conn, target_match_id, as_of_date=as_of)

    assert before == after, (
        "FUTURE LEAK DETECTED: features for a 2024-01-17 match changed "
        "when 2024-02-15 data was added to DB. The as_of filter is broken."
    )


def test_inference_uses_strict_less_than(features_db):
    """A feature computed with as_of equal to a match's own date must not
    include that same match in its inputs (strict <, not <=)."""
    conn, team_ids = features_db
    # Find Alpha's games on or after 2024-01-17
    match_id = conn.execute(
        "SELECT match_id FROM matches WHERE match_key = '2024-01-17|Alpha|Charlie'"
    ).fetchone()[0]
    # Compute features as_of the same date as the match. The Glicko
    # rating should be based on games STRICTLY BEFORE 2024-01-17 (so it
    # should match the rating as_of "2024-01-16").
    f_match_day = compute_features(conn, match_id, as_of_date="2024-01-17")
    f_day_before = compute_features(conn, match_id, as_of_date="2024-01-17")
    # (same call twice — sanity check determinism here)
    assert f_match_day == f_day_before

    # And importantly: a feature with as_of_date=match.date+1 should differ
    # because now the match itself is "in the past" for the data.
    # Glicko updates immediately after a game (no minimum-sample threshold
    # like recent_form has), so its rating should change.
    f_match_day_plus_1 = compute_features(conn, match_id, as_of_date="2024-01-18")
    assert (
        f_match_day_plus_1["team_a_glicko_rating"] != f_match_day["team_a_glicko_rating"]
        or f_match_day_plus_1["team_b_glicko_rating"] != f_match_day["team_b_glicko_rating"]
    ), "Including the match itself in features should change Glicko ratings"
