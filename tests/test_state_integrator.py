"""Tests for the live state integrator (Layer 4)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from loltrader.comp.aggregator import ChampionPick, evaluate_comp
from loltrader.comp.profiles import (
    ChampionProfile,
    Qualitative,
    save_profiles,
)
from loltrader.db import connect, migrate
from loltrader.winprob.state import (
    FEATURE_SCHEMA,
    compute_item_progression,
    compute_objective_state,
    integrate_pregame,
    integrate_state,
    load_frame,
    time_to_next_baron,
)


@pytest.fixture
def profiles_file(tmp_path: Path) -> Path:
    """5+5 champion fixture covering the two comps in the integration tests."""
    profs = {
        # Late-game team A
        "Caitlyn": ChampionProfile(name="Caitlyn",
            qualitative=Qualitative(scaling_early=1, scaling_mid=2, scaling_late=3,
                                     baron_dps_tier=3, teamfight_score=2,
                                     primary_role="bot")),
        "Senna": ChampionProfile(name="Senna",
            qualitative=Qualitative(scaling_early=0, scaling_mid=2, scaling_late=3,
                                     baron_dps_tier=3, teamfight_score=2,
                                     primary_role="bot")),
        "Lulu": ChampionProfile(name="Lulu",
            qualitative=Qualitative(scaling_early=0, scaling_mid=1, scaling_late=2,
                                     peel_supply=3, primary_role="support")),
        "Sion": ChampionProfile(name="Sion",
            qualitative=Qualitative(scaling_early=-1, scaling_mid=1, scaling_late=3,
                                     baron_dps_tier=2, teamfight_score=3,
                                     primary_role="top")),
        "Annie": ChampionProfile(name="Annie",
            qualitative=Qualitative(scaling_early=0, scaling_mid=2, scaling_late=2,
                                     teamfight_score=3, primary_role="mid")),
        # Early-game team B
        "Pantheon": ChampionProfile(name="Pantheon",
            qualitative=Qualitative(scaling_early=3, scaling_mid=1, scaling_late=-1,
                                     pick_threat=3, engage_score=3,
                                     primary_role="jungle")),
        "Naafiri": ChampionProfile(name="Naafiri",
            qualitative=Qualitative(scaling_early=2, scaling_mid=1, scaling_late=-2,
                                     pick_threat=3, primary_role="jungle")),
        "LeeSin": ChampionProfile(name="LeeSin",
            qualitative=Qualitative(scaling_early=3, scaling_mid=1, scaling_late=-1,
                                     primary_role="jungle")),
        "Karma": ChampionProfile(name="Karma",
            qualitative=Qualitative(scaling_early=1, scaling_mid=2, scaling_late=2,
                                     peel_supply=2, primary_role="support")),
        "Bard": ChampionProfile(name="Bard",
            qualitative=Qualitative(scaling_early=1, scaling_mid=2, scaling_late=2,
                                     pick_threat=2, primary_role="support")),
    }
    path = tmp_path / "profiles.json"
    save_profiles(profs, path)
    return path


@pytest.fixture
def seeded_db(tmp_path: Path):
    """DB with one game in progress: 30 min in, gold diff +3000 blue, 1 dragon each."""
    db = tmp_path / "state.db"
    conn = connect(db)
    migrate(conn)
    now = int(time.time())

    conn.execute(
        """INSERT INTO games_live(game_id, league, first_seen_ts_unix, game_start_ts_unix, blue_team_code, red_team_code)
           VALUES ('test_game', 'lck', ?, ?, 'BLUE', 'RED')""",
        (now - 1800, now - 1800),  # game started 30 min ago
    )

    # One in_game frame at minute 22
    frame_ts = (now - 1800) + 22 * 60
    conn.execute(
        """INSERT INTO live_frames(game_id, frame_ts_unix, fetched_ts_unix, game_state,
                                    blue_gold, blue_kills, blue_towers, blue_inhibitors,
                                    blue_dragons_json, blue_barons,
                                    red_gold, red_kills, red_towers, red_inhibitors,
                                    red_dragons_json, red_barons)
           VALUES ('test_game', ?, ?, 'in_game',
                   45000, 10, 5, 0, ?, 1,
                   42000, 7, 3, 0, ?, 0)""",
        (frame_ts, now, json.dumps(["mountain", "ocean"]), json.dumps(["infernal"])),
    )

    # Per-player details — 5 blue, 5 red
    for pid in range(1, 11):
        side_items = [1056, 3158, 3115, 3089, 0, 2055, 3340] if pid <= 5 else [1056, 3047, 3071, 3026, 0, 2055, 3340]
        conn.execute(
            """INSERT INTO live_frames_details(game_id, frame_ts_unix, fetched_ts_unix, side, participant_id,
                                                level, kills, deaths, assists, total_gold, creep_score,
                                                items_json)
               VALUES ('test_game', ?, ?, ?, ?, 14, 2, 1, 3, 9000, 200, ?)""",
            (frame_ts, now, "blue" if pid <= 5 else "red", pid, json.dumps(side_items)),
        )

    conn.commit()
    yield conn
    conn.close()


# ---------- load_frame --------------------------------------------------


def test_load_frame_latest(seeded_db):
    """load_frame returns the latest frame + matching detail rows."""
    frame, details = load_frame(seeded_db, "test_game")
    assert frame is not None
    assert frame["game_state"] == "in_game"
    assert frame["blue_gold"] == 45000
    assert frame["red_gold"] == 42000
    assert frame["blue_dragons"] == ["mountain", "ocean"]
    assert frame["red_dragons"] == ["infernal"]
    assert len(details) == 10
    assert all("items" in d for d in details)


def test_load_frame_no_game(tmp_path: Path):
    """Missing game_id returns (None, [])."""
    db = tmp_path / "empty.db"
    conn = connect(db)
    migrate(conn)
    frame, details = load_frame(conn, "nonexistent_game")
    assert frame is None
    assert details == []
    conn.close()


# ---------- objective state ---------------------------------------------


def test_compute_objective_state_diffs():
    """Dragon diff = blue - red; baron diff likewise."""
    frame = {
        "blue_dragons": ["mountain", "ocean", "cloud"],
        "red_dragons": ["infernal"],
        "blue_barons": 1, "red_barons": 0,
    }
    obj = compute_objective_state(frame)
    assert obj["dragon_diff"] == 2
    assert obj["baron_diff"] == 1
    assert obj["soul_state"] == "none"  # both teams under 4 dragons


def test_compute_objective_state_soul():
    """4 dragons → soul for that side."""
    frame = {
        "blue_dragons": ["mountain", "ocean", "cloud", "infernal"],
        "red_dragons": ["infernal"],
        "blue_barons": 0, "red_barons": 0,
    }
    obj = compute_objective_state(frame)
    assert obj["soul_state"] == "blue"


# ---------- item progression --------------------------------------------


def test_compute_item_progression_counts(seeded_db):
    """Each participant has 3 completed items (≥3000 IDs); 5 participants → 15 per side."""
    _, details = load_frame(seeded_db, "test_game")
    ip = compute_item_progression(details)
    # Items 3158, 3115, 3089 are ≥3000 < 3900 → completed
    # Other slots: 1056 (component), 0 (empty), 2055 (ward, <3000), 3340 (trinket, <3900 but matches our range)
    # Note: 3340 is in [3000, 3900) so it counts. That's fine for v1; CV/Item DB refinement is later.
    assert ip["completed_items_a"] >= 15
    assert ip["avg_items_a"] >= 3.0


def test_compute_item_progression_empty():
    """No detail rows → all zeros."""
    ip = compute_item_progression([])
    assert ip["completed_items_a"] == 0
    assert ip["completed_items_b"] == 0
    assert ip["avg_items_a"] == 0.0


# ---------- time_to_next_baron ------------------------------------------


def test_time_to_next_baron_before_spawn():
    """Before minute 20, time = seconds until min 20."""
    assert time_to_next_baron(15, 0) == 5 * 60


def test_time_to_next_baron_after_take():
    """After a baron take, ~6-min respawn."""
    assert time_to_next_baron(25, 1) == 6 * 60


def test_time_to_next_baron_no_active():
    """Past min 20, no baron active → assume 6-min cycle."""
    assert time_to_next_baron(28, 0) == 6 * 60


# ---------- integrate_state ---------------------------------------------


def test_integrate_state_schema_stability(profiles_file, seeded_db):
    """Every integrate_state call returns exactly the schema keys, no more."""
    picks_a = [ChampionPick("Caitlyn", "bot"), ChampionPick("Senna", "bot"),
               ChampionPick("Lulu", "support"), ChampionPick("Sion", "top"),
               ChampionPick("Annie", "mid")]
    picks_b = [ChampionPick("Pantheon", "jungle"), ChampionPick("Naafiri", "jungle"),
               ChampionPick("LeeSin", "jungle"), ChampionPick("Karma", "support"),
               ChampionPick("Bard", "support")]

    a = evaluate_comp(picks_a, profiles_path=profiles_file)
    b = evaluate_comp(picks_b, profiles_path=profiles_file)

    frame, details = load_frame(seeded_db, "test_game")
    feats = integrate_state(a, b, frame, details, minute=22, picks_a=picks_a, picks_b=picks_b)
    assert set(feats.keys()) == set(FEATURE_SCHEMA)
    # Every value is a float
    for v in feats.values():
        assert isinstance(v, float)


def test_integrate_state_uses_game_state(profiles_file, seeded_db):
    """Gold diff, kill diff, etc. reflect the seeded frame."""
    picks_a = [ChampionPick("Caitlyn", "bot"), ChampionPick("Senna", "bot"),
               ChampionPick("Lulu", "support"), ChampionPick("Sion", "top"),
               ChampionPick("Annie", "mid")]
    picks_b = [ChampionPick("Pantheon", "jungle"), ChampionPick("Naafiri", "jungle"),
               ChampionPick("LeeSin", "jungle"), ChampionPick("Karma", "support"),
               ChampionPick("Bard", "support")]
    a = evaluate_comp(picks_a, profiles_path=profiles_file)
    b = evaluate_comp(picks_b, profiles_path=profiles_file)
    frame, details = load_frame(seeded_db, "test_game")

    feats = integrate_state(a, b, frame, details, minute=22, picks_a=picks_a, picks_b=picks_b)
    assert feats["gold_diff"] == 3000.0
    assert feats["kill_diff"] == 3.0
    assert feats["tower_diff"] == 2.0
    assert feats["dragon_diff"] == 1.0
    assert feats["baron_diff"] == 1.0
    assert feats["is_pregame"] == 0.0


def test_integrate_state_scaling_at_minute(profiles_file, seeded_db):
    """At minute 22, scaling comes from the curve interpolation."""
    picks_a = [ChampionPick("Caitlyn", "bot"), ChampionPick("Senna", "bot"),
               ChampionPick("Lulu", "support"), ChampionPick("Sion", "top"),
               ChampionPick("Annie", "mid")]
    picks_b = [ChampionPick("Pantheon", "jungle"), ChampionPick("Naafiri", "jungle"),
               ChampionPick("LeeSin", "jungle"), ChampionPick("Karma", "support"),
               ChampionPick("Bard", "support")]
    a = evaluate_comp(picks_a, profiles_path=profiles_file)
    b = evaluate_comp(picks_b, profiles_path=profiles_file)
    frame, details = load_frame(seeded_db, "test_game")

    feats = integrate_state(a, b, frame, details, minute=22, picks_a=picks_a, picks_b=picks_b)
    assert feats["comp_a_scaling_at_t"] == a.scaling_curve[22]
    assert feats["comp_b_scaling_at_t"] == b.scaling_curve[22]
    assert feats["scaling_diff_at_t"] == a.scaling_curve[22] - b.scaling_curve[22]


def test_integrate_state_pregame_zeros_state(profiles_file):
    """integrate_pregame returns the schema with state features zeroed."""
    picks_a = [ChampionPick("Caitlyn", "bot"), ChampionPick("Senna", "bot"),
               ChampionPick("Lulu", "support"), ChampionPick("Sion", "top"),
               ChampionPick("Annie", "mid")]
    picks_b = [ChampionPick("Pantheon", "jungle"), ChampionPick("Naafiri", "jungle"),
               ChampionPick("LeeSin", "jungle"), ChampionPick("Karma", "support"),
               ChampionPick("Bard", "support")]
    a = evaluate_comp(picks_a, profiles_path=profiles_file)
    b = evaluate_comp(picks_b, profiles_path=profiles_file)
    feats = integrate_pregame(a, b, picks_a, picks_b)
    assert feats["is_pregame"] == 1.0
    assert feats["gold_diff"] == 0.0
    assert feats["kill_diff"] == 0.0
    assert feats["minute"] == 0.0
    # But comp features still populated (team A is late-game stacked → late > 0)
    assert feats["comp_a_scaling_late"] > 0.0
    # Baron DPS is the sum of tier values; default tier is 3 so totals are > 0
    assert feats["comp_a_baron_dps"] > 0.0
    assert feats["comp_b_baron_dps"] > 0.0


def test_integrate_state_archetype_onehot_sums_to_one(profiles_file):
    """One-hot archetype for each side sums to 1.0."""
    picks_a = [ChampionPick("Caitlyn", "bot"), ChampionPick("Senna", "bot"),
               ChampionPick("Lulu", "support"), ChampionPick("Sion", "top"),
               ChampionPick("Annie", "mid")]
    picks_b = [ChampionPick("Pantheon", "jungle"), ChampionPick("Naafiri", "jungle"),
               ChampionPick("LeeSin", "jungle"), ChampionPick("Karma", "support"),
               ChampionPick("Bard", "support")]
    a = evaluate_comp(picks_a, profiles_path=profiles_file)
    b = evaluate_comp(picks_b, profiles_path=profiles_file)
    feats = integrate_pregame(a, b, picks_a, picks_b)

    a_sum = sum(feats[f"arch_a_{x}"] for x in ("scaling", "teamfight", "pick", "balanced"))
    b_sum = sum(feats[f"arch_b_{x}"] for x in ("scaling", "teamfight", "pick", "balanced"))
    assert a_sum == 1.0
    assert b_sum == 1.0


def test_integrate_state_interaction_features(profiles_file, seeded_db):
    """Interaction features compute correctly given state + comp."""
    picks_a = [ChampionPick("Caitlyn", "bot"), ChampionPick("Senna", "bot"),
               ChampionPick("Lulu", "support"), ChampionPick("Sion", "top"),
               ChampionPick("Annie", "mid")]
    picks_b = [ChampionPick("Pantheon", "jungle"), ChampionPick("Naafiri", "jungle"),
               ChampionPick("LeeSin", "jungle"), ChampionPick("Karma", "support"),
               ChampionPick("Bard", "support")]
    a = evaluate_comp(picks_a, profiles_path=profiles_file)
    b = evaluate_comp(picks_b, profiles_path=profiles_file)
    frame, details = load_frame(seeded_db, "test_game")

    feats = integrate_state(a, b, frame, details, minute=22, picks_a=picks_a, picks_b=picks_b)
    # gold_diff (3000) * time_remaining (40 - 22 = 18) = 54000
    assert feats["gold_diff_x_time_remaining"] == 3000.0 * 18.0
    # minute * scaling_diff
    assert feats["minute_x_scaling_diff"] == 22.0 * feats["scaling_diff_at_t"]


def test_integrate_state_handles_no_details(profiles_file, seeded_db):
    """When details list is empty, item-progression features default to 0."""
    picks_a = [ChampionPick("Caitlyn", "bot"), ChampionPick("Senna", "bot"),
               ChampionPick("Lulu", "support"), ChampionPick("Sion", "top"),
               ChampionPick("Annie", "mid")]
    picks_b = [ChampionPick("Pantheon", "jungle"), ChampionPick("Naafiri", "jungle"),
               ChampionPick("LeeSin", "jungle"), ChampionPick("Karma", "support"),
               ChampionPick("Bard", "support")]
    a = evaluate_comp(picks_a, profiles_path=profiles_file)
    b = evaluate_comp(picks_b, profiles_path=profiles_file)
    frame, _ = load_frame(seeded_db, "test_game")

    feats = integrate_state(a, b, frame, [], minute=22, picks_a=picks_a, picks_b=picks_b)
    assert feats["completed_items_a"] == 0.0
    assert feats["avg_items_a"] == 0.0


def test_feature_schema_size():
    """Schema is at the ~89-feature target after league + per-player + momentum adds."""
    assert 80 <= len(FEATURE_SCHEMA) <= 100
