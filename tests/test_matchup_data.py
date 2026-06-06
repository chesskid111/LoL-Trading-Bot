"""Tests for lane matchup data computation."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from loltrader.comp.matchup_data import (
    LaneMatchup,
    PRIOR_WEIGHT,
    PRIOR_WINRATE,
    compute_matchups,
    load_matchups,
    lookup_matchup,
    save_matchups,
)
from loltrader.db import connect, migrate


@pytest.fixture
def seeded_db(tmp_path: Path):
    """6-game fixture with deterministic matchup outcomes.

    Caitlyn vs Senna in bot lane:
      games 1-4: Caitlyn's team won (Caitlyn 4-0 vs Senna)
      games 5-6: Senna's team won (Caitlyn 0-2 vs Senna)
      total: Caitlyn 4 wins / 6 games = 66.7%

    Jhin vs Ezreal in bot lane:
      games 1-2: Jhin's team won (Jhin 2-0 vs Ezreal)
      games 3-4: Ezreal's team won (Jhin 0-2 vs Ezreal)
      total: Jhin 2/4 = 50%
    """
    db = tmp_path / "matchups.db"
    conn = connect(db)
    migrate(conn)
    now = int(time.time())

    conn.execute("INSERT INTO patches(version, first_seen, last_seen) VALUES ('16.1','2026-05-01','2026-05-14')")
    patch_id = conn.execute("SELECT patch_id FROM patches WHERE version='16.1'").fetchone()[0]

    conn.execute("INSERT INTO teams(team_id, oracle_teamid, canonical_name, region, first_seen, last_seen) VALUES (1,'a','TeamA','LCK',?,?)", (now, now))
    conn.execute("INSERT INTO teams(team_id, oracle_teamid, canonical_name, region, first_seen, last_seen) VALUES (2,'b','TeamB','LCK',?,?)", (now, now))

    # Players
    for pid in range(1, 5):
        conn.execute("INSERT INTO players(player_id, oracle_playerid, ign, role, first_seen, last_seen) VALUES (?,?,?,?,?,?)",
                     (pid, f"oe:p:{pid}", f"player{pid}", "bot", "2026-01-01", "2026-05-14"))

    # 6 matches with deterministic winners. Each game has Caitlyn on team A vs
    # Senna on team B, AND Jhin on team A vs Ezreal on team B (same player_stats rows).
    # We'll vary the winner. Note: bot lane = ADC for these purposes.
    winners = [1, 1, 1, 1, 2, 2]  # team 1 wins games 1-4, team 2 wins 5-6

    for game_idx in range(6):
        match_id = game_idx + 1
        winner = winners[game_idx]
        conn.execute(
            """INSERT INTO matches(match_id, match_key, date, league, split, playoffs, patch_id, team_a_id, team_b_id, series_winner_id, bo_format)
               VALUES (?, ?, ?, 'LCK', 'Spring', 0, ?, 1, 2, ?, 1)""",
            (match_id, f"m{match_id}", "2026-05-01", patch_id, winner),
        )
        conn.execute(
            """INSERT INTO match_games(game_id, oracle_gameid, match_id, game_number, blue_team_id, red_team_id, winner_team_id, duration_sec, patch_id)
               VALUES (?, ?, ?, 1, 1, 2, ?, 1800, ?)""",
            (match_id, f"og{match_id}", match_id, winner, patch_id),
        )

    # match_player_stats: only one game (game 1) gets Caitlyn-vs-Senna AND Jhin-vs-Ezreal
    # — we'll add Caitlyn vs Senna to all 6, and Jhin vs Ezreal only to games 1-4
    def add_stat(game_id, player_id, team_id, role, champion):
        conn.execute(
            """INSERT INTO match_player_stats(game_id, player_id, team_id, role, champion, kills, deaths, assists, cs, gold, damage_to_champs, vision_score)
               VALUES (?, ?, ?, ?, ?, 5, 3, 8, 200, 12000, 18000, 40)""",
            (game_id, player_id, team_id, role, champion),
        )

    for g in range(1, 7):
        add_stat(g, 1, 1, "bot", "Caitlyn")
        add_stat(g, 2, 2, "bot", "Senna")

    for g in range(1, 5):
        add_stat(g, 3, 1, "bot", "Jhin")   # Wait — duplicate role. Let's use 'mid' for Jhin/Ezreal
        # Actually we'd need to use a different role. Let me put Jhin/Ezreal in mid for the test.

    # Reset Jhin/Ezreal in mid for games 1-4
    conn.execute("DELETE FROM match_player_stats WHERE champion IN ('Jhin','Ezreal')")
    for g in range(1, 5):
        add_stat(g, 3, 1, "mid", "Jhin")
        add_stat(g, 4, 2, "mid", "Ezreal")

    conn.commit()
    yield conn
    conn.close()


def test_compute_matchups_basic(seeded_db):
    """Caitlyn vs Senna in bot lane: 4 wins / 6 games."""
    mus = compute_matchups(seeded_db, ["16.1"], league="LCK")
    by_pair = {(m.role, m.champion_a, m.champion_b): m for m in mus}

    cait_v_senna = by_pair[("bot", "Caitlyn", "Senna")]
    assert cait_v_senna.games == 6
    assert cait_v_senna.wins_a == 4
    assert cait_v_senna.raw_winrate_a == pytest.approx(4 / 6)

    # Symmetric entry: Senna vs Caitlyn should be 2 wins / 6 games
    senna_v_cait = by_pair[("bot", "Senna", "Caitlyn")]
    assert senna_v_cait.games == 6
    assert senna_v_cait.wins_a == 2


def test_shrinkage_pulls_perfect_winrate_down(seeded_db):
    """Jhin vs Ezreal in mid lane: 4 wins / 4 games = 100% raw → shrunk ~72%."""
    mus = compute_matchups(seeded_db, ["16.1"], league="LCK")
    by_pair = {(m.role, m.champion_a, m.champion_b): m for m in mus}

    jhin_v_ezreal = by_pair[("mid", "Jhin", "Ezreal")]
    assert jhin_v_ezreal.raw_winrate_a == 1.0
    # 100% raw with 4 games + neutral prior weight=5 → (4 + 0.5*5)/(4+5) ≈ 0.722
    assert jhin_v_ezreal.shrunk_winrate_a == pytest.approx(0.722, abs=0.01)
    # Symmetric: Ezreal is 0/4 → shrunk down to ~0.278
    ezr_v_jhin = by_pair[("mid", "Ezreal", "Jhin")]
    assert ezr_v_jhin.shrunk_winrate_a == pytest.approx(0.278, abs=0.01)


def test_shrinkage_pulls_extreme_toward_center(seeded_db):
    """Caitlyn 66.7% raw with 6 games gets shrunk closer to 50%."""
    mus = compute_matchups(seeded_db, ["16.1"], league="LCK")
    by_pair = {(m.role, m.champion_a, m.champion_b): m for m in mus}

    cait_v_senna = by_pair[("bot", "Caitlyn", "Senna")]
    raw = cait_v_senna.raw_winrate_a
    shrunk = cait_v_senna.shrunk_winrate_a
    assert shrunk < raw  # shrinkage pulls toward 0.5
    assert 0.5 < shrunk < raw  # but still above 0.5


def test_no_mirror_matches(seeded_db):
    """Caitlyn vs Caitlyn would be a mirror match — excluded."""
    mus = compute_matchups(seeded_db, ["16.1"], league="LCK")
    for m in mus:
        assert m.champion_a != m.champion_b


def test_min_games_filter(seeded_db):
    """min_games=5 excludes the Jhin/Ezreal pair (4 games)."""
    mus = compute_matchups(seeded_db, ["16.1"], league="LCK", min_games=5)
    champs = {(m.champion_a, m.champion_b) for m in mus}
    assert ("Jhin", "Ezreal") not in champs
    assert ("Caitlyn", "Senna") in champs   # 6 games — kept


def test_league_filter_excludes_other(seeded_db):
    """Filtering by LPL returns no matchups since the fixture is LCK-only."""
    mus = compute_matchups(seeded_db, ["16.1"], league="LPL")
    assert mus == []


def test_unknown_patch_returns_empty(seeded_db):
    assert compute_matchups(seeded_db, ["99.99"]) == []


def test_save_and_load_roundtrip(tmp_path: Path, seeded_db):
    """save → load reproduces the same lookup behavior."""
    mus = compute_matchups(seeded_db, ["16.1"], league="LCK")
    path = tmp_path / "matchups.json"
    save_matchups(mus, path)

    loaded = load_matchups(path)
    shrunk, games = lookup_matchup(loaded, "bot", "Caitlyn", "Senna")
    assert games == 6
    assert shrunk > 0.5  # Caitlyn has the edge


def test_lookup_missing_returns_neutral(tmp_path: Path):
    """An unobserved matchup returns (0.5, 0) — neutral prior, no evidence."""
    empty = {}
    shrunk, games = lookup_matchup(empty, "mid", "Yorick", "Sett")
    assert shrunk == 0.5
    assert games == 0
