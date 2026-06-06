"""Tests for pro champion stats computation."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from loltrader.comp import pro_stats as ps
from loltrader.db import connect, migrate


@pytest.fixture
def seeded_db(tmp_path: Path):
    """DB with a tiny pro-game fixture: 8 games on patch 16.1 in LCK.

    8 games keeps every picked champion above MIN_GAMES_FOR_WINRATE so the
    winrate code path is exercised (not the sparse-sample prior).

    Champion outcomes:
      Caitlyn (always team 1):   picked 8 times, won 6 → wr 0.75
      Senna   (always team 2):   picked 8 times, won 2 → wr 0.25
      Lulu    (always team 1):   picked 8 times, won 6 → wr 0.75
      Aphelios(always team 2):   picked 8 times, won 2 → wr 0.25
      Lee Sin (banned both):     0 picks, 16 bans
    """
    db = tmp_path / "stats.db"
    conn = connect(db)
    migrate(conn)
    now = int(time.time())

    # Need a patch row referenced by patch_id
    conn.execute("INSERT INTO patches(version, first_seen, last_seen) VALUES ('16.1','2026-05-01','2026-05-14')")
    patch_id = conn.execute("SELECT patch_id FROM patches WHERE version='16.1'").fetchone()[0]

    # Two teams
    conn.execute("INSERT INTO teams(team_id, oracle_teamid, canonical_name, region, first_seen, last_seen) VALUES (1,'oe:t:a','TeamA','LCK',?,?)", (now, now))
    conn.execute("INSERT INTO teams(team_id, oracle_teamid, canonical_name, region, first_seen, last_seen) VALUES (2,'oe:t:b','TeamB','LCK',?,?)", (now, now))

    # 8 matches in LCK (one game each). Team 1 wins 6 of 8.
    winners = [1, 2, 1, 1, 1, 2, 1, 1]
    for i, winner_tid in enumerate(winners, start=1):
        conn.execute(
            """INSERT INTO matches(match_id, match_key, date, league, split, playoffs, patch_id, team_a_id, team_b_id, series_winner_id, bo_format)
               VALUES (?, ?, ?, 'LCK', 'Spring', 0, ?, 1, 2, ?, 1)""",
            (i, f"m{i}", f"2026-05-0{i+1}", patch_id, winner_tid),
        )
        conn.execute(
            """INSERT INTO match_games(game_id, oracle_gameid, match_id, game_number, blue_team_id, red_team_id, winner_team_id, duration_sec, patch_id)
               VALUES (?, ?, ?, 1, 1, 2, ?, 1800, ?)""",
            (i, f"og{i}", i, winner_tid, patch_id),
        )

    # Drafts: each game has 4 picks/team and 4 bans/team. We focus on a few champs.
    def add(game_id, team_id, is_ban, order, champ, role):
        conn.execute(
            """INSERT INTO match_drafts(game_id, team_id, is_ban, pick_order, champion, role)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (game_id, team_id, is_ban, order, champ, role),
        )

    for g in range(1, 9):
        # team 1 picks Caitlyn + Lulu (wins g1, g3, g4)
        add(g, 1, 0, 1, "Caitlyn", "bot")
        add(g, 1, 0, 2, "Lulu",    "support")
        # team 2 picks Senna + Aphelios (wins g2)
        add(g, 2, 0, 1, "Senna",    "bot")
        add(g, 2, 0, 2, "Aphelios", "support")
        # Lee Sin banned twice per game
        add(g, 1, 1, 1, "Lee Sin", "jungle")
        add(g, 2, 1, 1, "Lee Sin", "jungle")

    conn.commit()
    yield conn
    conn.close()


def test_compute_patch_stats_picks_winrates(seeded_db):
    """Pick counts + winrates per champion match fixture."""
    stats = ps.compute_patch_stats(seeded_db, "16.1", league="LCK")
    by_champ = {s.champion: s for s in stats}

    # Caitlyn on team 1: picked 8 games, team 1 won 6 → wr = 0.75
    cait = by_champ["Caitlyn"]
    assert cait.times_picked == 8
    assert cait.times_won == 6
    assert cait.winrate == 0.75

    # Senna on team 2: picked 8 games, team 2 won 2 → wr = 0.25
    senna = by_champ["Senna"]
    assert senna.times_picked == 8
    assert senna.times_won == 2
    assert senna.winrate == 0.25

    # Lulu on team 1: same as Caitlyn (always team 1)
    assert by_champ["Lulu"].winrate == 0.75

    # Lee Sin banned 16 times total (2 per game x 8 games), never picked
    lee = by_champ["Lee Sin"]
    assert lee.times_picked == 0
    assert lee.times_banned == 16
    # Winrate when picked < threshold falls back to 0.5 prior
    assert lee.winrate == 0.5


def test_pickrate_uses_total_games_denominator(seeded_db):
    """Pickrate = picks / total games in window, not per-team."""
    stats = ps.compute_patch_stats(seeded_db, "16.1", league="LCK")
    by_champ = {s.champion: s for s in stats}

    # 8 games total in window; Caitlyn picked 8 times → pickrate = 1.0
    assert by_champ["Caitlyn"].pickrate == 1.0
    # Lee Sin: 16 bans / 8 games = 2.0 banrate (yes, intentionally — banrate
    # can exceed 1.0 when a champ is banned by both teams)
    assert by_champ["Lee Sin"].banrate == 2.0


def test_league_filter(seeded_db):
    """A league with no games returns empty stats."""
    stats = ps.compute_patch_stats(seeded_db, "16.1", league="LPL")
    assert stats == []


def test_priority_score_caps_at_ten(seeded_db):
    """priority_score is bounded 0-10 even for extreme cases."""
    stats = ps.compute_patch_stats(seeded_db, "16.1", league="LCK")
    for s in stats:
        assert 0.0 <= s.priority_score <= 10.0


def test_save_patch_stats_roundtrip(tmp_path: Path, seeded_db):
    """save_patch_stats writes valid JSON with computed-property fields."""
    stats = ps.compute_patch_stats(seeded_db, "16.1", league="LCK")
    path = tmp_path / "patch_stats.json"
    ps.save_patch_stats(stats, path)
    import json
    payload = json.loads(path.read_text())
    assert len(payload) == len(stats)
    entry = next(e for e in payload if e["champion"] == "Caitlyn")
    assert entry["winrate"] == 0.75
    assert "priority_score" in entry
    assert "confidence" in entry


def test_merge_into_profiles_updates_pro_stats(seeded_db):
    """merge_into_profiles updates only champions that already have a profile."""
    from loltrader.comp.profiles import ChampionProfile, Qualitative

    profiles = {
        "Caitlyn": ChampionProfile(name="Caitlyn", qualitative=Qualitative(primary_role="bot")),
        "Senna":   ChampionProfile(name="Senna",   qualitative=Qualitative(primary_role="bot")),
        # Lulu absent — should be skipped
    }
    stats = ps.compute_patch_stats(seeded_db, "16.1", league="LCK")
    n = ps.merge_into_profiles(profiles, stats, "16.1", league="LCK")
    assert n == 2
    assert profiles["Caitlyn"].pro_stats.winrate_30d == 0.75
    assert profiles["Caitlyn"].patch == "16.1"
    assert profiles["Senna"].pro_stats.winrate_30d == 0.25


def test_unknown_patch_returns_empty(seeded_db):
    assert ps.compute_patch_stats(seeded_db, "99.99") == []
