"""Tests for draft features."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from loltrader.db import connect, migrate
from loltrader.features.draft import (
    _composition_tags,
    _team_picks_for_match,
    draft_features,
)


@pytest.fixture
def draft_db(tmp_path: Path):
    """DB with 2 teams, a match with a Game 1 + drafts, and champion metadata."""
    db = tmp_path / "draft.db"
    conn = connect(db)
    migrate(conn)

    # Seed champions
    now = int(time.time())
    champs = [
        # (name, fighter, mage, assassin, marksman, tank, support)
        ("Aatrox",    1, 0, 0, 0, 0, 0),
        ("Ahri",      0, 1, 1, 0, 0, 0),
        ("Kaisa",     0, 0, 0, 1, 0, 0),
        ("LeBlanc",   0, 1, 1, 0, 0, 0),
        ("Lulu",      0, 1, 0, 0, 0, 1),
        ("Maokai",    1, 0, 0, 0, 1, 0),
        ("Ornn",      0, 0, 0, 0, 1, 0),
        ("Senna",     0, 0, 0, 1, 0, 1),
        ("Sett",      1, 0, 0, 0, 0, 1),
        ("Yasuo",     1, 0, 1, 0, 0, 0),
    ]
    for c in champs:
        conn.execute(
            "INSERT INTO champions (champion_name, riot_key, tags, "
            "has_fighter, has_mage, has_assassin, has_marksman, has_tank, has_support, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (c[0], "0", "", c[1], c[2], c[3], c[4], c[5], c[6], now),
        )

    # Seed two teams + a patch + a match
    conn.execute("INSERT INTO teams (canonical_name, region, first_seen, last_seen) "
                 "VALUES ('TeamA', 'LCS', '2024-01-01', '2024-12-31')")
    conn.execute("INSERT INTO teams (canonical_name, region, first_seen, last_seen) "
                 "VALUES ('TeamB', 'LCS', '2024-01-01', '2024-12-31')")
    ta = conn.execute("SELECT team_id FROM teams WHERE canonical_name='TeamA'").fetchone()[0]
    tb = conn.execute("SELECT team_id FROM teams WHERE canonical_name='TeamB'").fetchone()[0]

    conn.execute("INSERT INTO patches (version, first_seen, last_seen) VALUES ('14.10', '2024-01-01', '2024-12-31')")
    patch_id = conn.execute("SELECT patch_id FROM patches WHERE version='14.10'").fetchone()[0]

    a, b = sorted(["TeamA", "TeamB"])
    conn.execute(
        "INSERT INTO matches (match_key, date, league, split, playoffs, patch_id, team_a_id, team_b_id, bo_format) "
        "VALUES (?, '2024-06-01', 'LCS', 'Summer', 0, ?, ?, ?, 1)",
        (f"2024-06-01|{a}|{b}", patch_id, ta, tb),
    )
    match_id = conn.execute("SELECT match_id FROM matches WHERE match_key=?",
                            (f"2024-06-01|{a}|{b}",)).fetchone()[0]
    conn.execute(
        "INSERT INTO match_games (oracle_gameid, match_id, game_number, blue_team_id, red_team_id, "
        "winner_team_id, duration_sec, patch_id) VALUES ('G1', ?, 1, ?, ?, ?, 1800, ?)",
        (match_id, ta, tb, ta, patch_id),
    )
    game_id = conn.execute("SELECT game_id FROM match_games WHERE oracle_gameid='G1'").fetchone()[0]

    # Team A's draft: Aatrox, Ahri, Kaisa, Senna, Maokai
    # Team B's draft: Yasuo, LeBlanc, Lulu, Sett, Ornn
    a_picks = ["Aatrox", "Ahri", "Kaisa", "Senna", "Maokai"]
    b_picks = ["Yasuo", "LeBlanc", "Lulu", "Sett", "Ornn"]
    for i, c in enumerate(a_picks, 1):
        conn.execute("INSERT INTO match_drafts (game_id, team_id, is_ban, pick_order, champion) "
                     "VALUES (?, ?, 0, ?, ?)", (game_id, ta, i, c))
    for i, c in enumerate(b_picks, 1):
        conn.execute("INSERT INTO match_drafts (game_id, team_id, is_ban, pick_order, champion) "
                     "VALUES (?, ?, 0, ?, ?)", (game_id, tb, i, c))

    conn.commit()
    yield conn, match_id, ta, tb, patch_id
    conn.close()


def test_picks_retrieved(draft_db):
    conn, match_id, ta, tb, _ = draft_db
    a_picks = _team_picks_for_match(conn, match_id, ta)
    b_picks = _team_picks_for_match(conn, match_id, tb)
    assert a_picks == ["Aatrox", "Ahri", "Kaisa", "Senna", "Maokai"]
    assert b_picks == ["Yasuo", "LeBlanc", "Lulu", "Sett", "Ornn"]


def test_composition_tag_counts(draft_db):
    conn, match_id, ta, tb, _ = draft_db
    a_picks = _team_picks_for_match(conn, match_id, ta)
    counts = _composition_tags(conn, a_picks)
    # Team A: Aatrox(F), Ahri(M+A), Kaisa(Mk), Senna(Mk+S), Maokai(F+T)
    # F=2, M=1, A=1, Mk=2, T=1, S=1
    assert counts["has_fighter"] == 2
    assert counts["has_mage"] == 1
    assert counts["has_assassin"] == 1
    assert counts["has_marksman"] == 2
    assert counts["has_tank"] == 1
    assert counts["has_support"] == 1


def test_draft_features_full_shape(draft_db):
    conn, match_id, ta, tb, patch_id = draft_db
    feats = draft_features(
        conn, match_id=match_id, team_a_id=ta, team_b_id=tb,
        patch_id=patch_id, as_of_date="2024-06-01",
    )
    # Expect at least: 18 tag features + 3 winrate + 2 n_picks + 12 archetype
    assert len(feats) >= 35
    # Spot-check keys
    assert "team_a_has_fighter" in feats
    assert "team_b_has_mage" in feats
    assert "tag_diff_has_marksman" in feats
    assert "team_a_avg_pick_winrate_patch" in feats
    assert "pick_winrate_diff" in feats
    # New v1.6 archetype features
    assert "team_a_arch_scaling" in feats
    assert "team_b_arch_teamfight" in feats
    assert "arch_diff_pick" in feats


def test_draft_features_empty_match(draft_db, tmp_path: Path):
    """A match with no drafts should return all zeros for tag counts."""
    conn, _, ta, tb, patch_id = draft_db
    # Create a fresh match with no drafts
    conn.execute(
        "INSERT INTO matches (match_key, date, league, patch_id, team_a_id, team_b_id, bo_format) "
        "VALUES ('empty-key', '2024-07-01', 'LCS', ?, ?, ?, 1)",
        (patch_id, ta, tb),
    )
    empty_match = conn.execute(
        "SELECT match_id FROM matches WHERE match_key='empty-key'"
    ).fetchone()[0]
    feats = draft_features(
        conn, match_id=empty_match, team_a_id=ta, team_b_id=tb,
        patch_id=patch_id, as_of_date="2024-07-01",
    )
    assert feats["team_a_has_fighter"] == 0.0
    assert feats["team_a_n_picks"] == 0.0
    # When no picks, winrate falls back to 0.5
    assert feats["team_a_avg_pick_winrate_patch"] == 0.5


def test_compute_features_includes_drafts(draft_db):
    """Top-level compute_features should include all the v1.6 features."""
    conn, match_id, _, _, _ = draft_db
    from loltrader.features import compute_features
    from loltrader.features.team_strength import rebuild_team_glicko
    rebuild_team_glicko(conn)
    feats = compute_features(conn, match_id)
    # v1: 43, +drafts: 66, +archetype: 78, +player-champ: 86, +lane: 95+
    assert len(feats) >= 90
    # Spot-check across categories
    assert "team_a_has_fighter" in feats               # draft tags
    assert "team_a_arch_scaling" in feats              # archetype (D)
    assert "team_a_pcwr_alltime" in feats              # player-on-champion (C)
    assert "lane_total_advantage" in feats             # lane matchup (B)
    assert "team_a_recent_roster_change" in feats      # roster reset (I)
