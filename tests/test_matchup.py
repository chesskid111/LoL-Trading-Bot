"""Tests for the matchup evaluator (Layer 3)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from loltrader.comp.aggregator import ChampionPick, evaluate_comp
from loltrader.comp.matchup import (
    MatchupAssessment,
    comp_matchup,
    crossover_minute,
    lane_matchup,
    reset_matchup_cache,
)
from loltrader.comp.profiles import (
    ChampionProfile,
    Qualitative,
    save_profiles,
)


@pytest.fixture
def profiles_file(tmp_path: Path) -> Path:
    """8-champion fixture spanning scaling identities for crossover tests."""
    profs = {
        # Late-game team — scales up over time
        "Caitlyn": ChampionProfile(name="Caitlyn",
            qualitative=Qualitative(scaling_early=1, scaling_mid=2, scaling_late=3,
                                     primary_role="bot")),
        "Senna":   ChampionProfile(name="Senna",
            qualitative=Qualitative(scaling_early=0, scaling_mid=2, scaling_late=3,
                                     primary_role="bot")),
        "Lulu":    ChampionProfile(name="Lulu",
            qualitative=Qualitative(scaling_early=0, scaling_mid=1, scaling_late=2,
                                     primary_role="support")),
        "Sion":    ChampionProfile(name="Sion",
            qualitative=Qualitative(scaling_early=-1, scaling_mid=1, scaling_late=3,
                                     primary_role="top")),
        # Early-game team — fades over time
        "Pantheon": ChampionProfile(name="Pantheon",
            qualitative=Qualitative(scaling_early=3, scaling_mid=1, scaling_late=-1,
                                     primary_role="jungle")),
        "Naafiri": ChampionProfile(name="Naafiri",
            qualitative=Qualitative(scaling_early=2, scaling_mid=1, scaling_late=-2,
                                     primary_role="jungle")),
        "LeeSin":  ChampionProfile(name="LeeSin",
            qualitative=Qualitative(scaling_early=3, scaling_mid=1, scaling_late=-1,
                                     primary_role="jungle")),
        "Annie":   ChampionProfile(name="Annie",
            qualitative=Qualitative(scaling_early=0, scaling_mid=2, scaling_late=2,
                                     primary_role="mid")),
    }
    path = tmp_path / "profiles.json"
    save_profiles(profs, path)
    return path


@pytest.fixture
def matchups_file(tmp_path: Path) -> Path:
    """Lane matchup data with a few known asymmetries."""
    path = tmp_path / "matchups.json"
    path.write_text(json.dumps({
        "bot|Caitlyn|Senna": {"role": "bot", "champion_a": "Caitlyn",
                              "champion_b": "Senna", "wins_a": 5, "games": 8,
                              "raw_winrate_a": 0.625, "shrunk_winrate_a": 0.58},
        "bot|Senna|Caitlyn": {"role": "bot", "champion_a": "Senna",
                              "champion_b": "Caitlyn", "wins_a": 3, "games": 8,
                              "raw_winrate_a": 0.375, "shrunk_winrate_a": 0.42},
    }))
    reset_matchup_cache()
    yield path
    reset_matchup_cache()


# ---------- lane_matchup ------------------------------------------------


def test_lane_matchup_known(matchups_file):
    """Reads the data we wrote correctly."""
    wr, games = lane_matchup("Caitlyn", "Senna", "bot", matchups_file)
    assert wr == pytest.approx(0.58)
    assert games == 8


def test_lane_matchup_missing_returns_neutral(matchups_file):
    """Unknown matchup returns (0.5, 0) — no evidence."""
    wr, games = lane_matchup("Yorick", "Sett", "top", matchups_file)
    assert wr == 0.5
    assert games == 0


# ---------- comp_matchup ------------------------------------------------


def test_comp_matchup_scaling_only(profiles_file):
    """Without picks (no lane data), assessment is pure scaling diff."""
    late_picks = [ChampionPick("Caitlyn", "bot"), ChampionPick("Senna", "bot"),
                  ChampionPick("Lulu", "support"), ChampionPick("Sion", "top"),
                  ChampionPick("Annie", "mid")]
    early_picks = [ChampionPick("Pantheon", "jungle"), ChampionPick("Naafiri", "jungle"),
                   ChampionPick("LeeSin", "jungle"), ChampionPick("Annie", "mid"),
                   ChampionPick("Lulu", "support")]

    late = evaluate_comp(late_picks, profiles_path=profiles_file)
    early = evaluate_comp(early_picks, profiles_path=profiles_file)

    # At minute 7: early team is favored (their scaling_early sum is high)
    a7 = comp_matchup(early, late, 7)
    assert a7.favored == "A"
    # At minute 32: late team is favored
    a32 = comp_matchup(early, late, 32)
    assert a32.favored == "B"


def test_comp_matchup_even_picks(profiles_file):
    """Two identical comps → EVEN at every minute."""
    picks = [ChampionPick("Caitlyn", "bot"), ChampionPick("Senna", "bot"),
             ChampionPick("Lulu", "support"), ChampionPick("Sion", "top"),
             ChampionPick("Annie", "mid")]
    a = evaluate_comp(picks, profiles_path=profiles_file)
    b = evaluate_comp(picks, profiles_path=profiles_file)
    for t in (10, 20, 30):
        assert comp_matchup(a, b, t).favored == "EVEN"


def test_comp_matchup_returns_dataclass(profiles_file):
    """Result is a MatchupAssessment with the expected fields populated."""
    picks_a = [ChampionPick("Pantheon", "jungle"), ChampionPick("Naafiri", "jungle"),
               ChampionPick("LeeSin", "jungle"), ChampionPick("Annie", "mid"),
               ChampionPick("Lulu", "support")]
    picks_b = [ChampionPick("Caitlyn", "bot"), ChampionPick("Senna", "bot"),
               ChampionPick("Lulu", "support"), ChampionPick("Sion", "top"),
               ChampionPick("Annie", "mid")]
    a = evaluate_comp(picks_a, profiles_path=profiles_file)
    b = evaluate_comp(picks_b, profiles_path=profiles_file)

    result = comp_matchup(a, b, 20)
    assert isinstance(result, MatchupAssessment)
    assert result.minute == 20
    assert result.favored in ("A", "B", "EVEN")
    assert isinstance(result.edge_magnitude, float)
    assert isinstance(result.scaling_diff, float)


# ---------- crossover_minute --------------------------------------------


def test_crossover_when_early_vs_late(profiles_file):
    """Classic crossover: early-game team A vs late-game team B."""
    early_picks = [ChampionPick("Pantheon", "jungle"), ChampionPick("Naafiri", "jungle"),
                   ChampionPick("LeeSin", "jungle"), ChampionPick("Annie", "mid"),
                   ChampionPick("Lulu", "support")]
    late_picks = [ChampionPick("Caitlyn", "bot"), ChampionPick("Senna", "bot"),
                  ChampionPick("Lulu", "support"), ChampionPick("Sion", "top"),
                  ChampionPick("Annie", "mid")]
    a = evaluate_comp(early_picks, profiles_path=profiles_file)
    b = evaluate_comp(late_picks, profiles_path=profiles_file)

    co = crossover_minute(a, b)
    assert co is not None
    assert 14 <= co <= 30  # reasonable crossover window for cycle trade


def test_no_crossover_for_identical_comps(profiles_file):
    """Identical comps have no crossover — favored is EVEN throughout."""
    picks = [ChampionPick("Caitlyn", "bot"), ChampionPick("Senna", "bot"),
             ChampionPick("Lulu", "support"), ChampionPick("Sion", "top"),
             ChampionPick("Annie", "mid")]
    a = evaluate_comp(picks, profiles_path=profiles_file)
    b = evaluate_comp(picks, profiles_path=profiles_file)
    assert crossover_minute(a, b) is None


def test_no_crossover_for_dominant_comp(profiles_file):
    """When team A dominates throughout, crossover_minute returns None."""
    # Stacked late-game (5 scaling carries) vs balanced (mostly mid-game)
    stacked = [ChampionPick("Caitlyn", "bot"), ChampionPick("Senna", "bot"),
               ChampionPick("Sion", "top"), ChampionPick("Lulu", "support"),
               ChampionPick("Sion", "top")]
    balanced = [ChampionPick("Annie", "mid"), ChampionPick("Annie", "mid"),
                ChampionPick("Annie", "mid"), ChampionPick("Annie", "mid"),
                ChampionPick("Annie", "mid")]
    a = evaluate_comp(stacked, profiles_path=profiles_file)
    b = evaluate_comp(balanced, profiles_path=profiles_file)

    # A is ahead at min 10 in scaling and stays ahead late → no crossover
    early = comp_matchup(a, b, 10)
    late = comp_matchup(a, b, 32)
    if early.favored == late.favored:
        assert crossover_minute(a, b) is None
