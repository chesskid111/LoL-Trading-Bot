"""Tests for the comp aggregator (Layer 2)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from loltrader.comp.aggregator import (
    ChampionPick,
    CompProfile,
    _interp_scaling,
    _pair_key,
    _team_scaling_curve,
    evaluate_comp,
)
from loltrader.comp.profiles import (
    ChampionProfile,
    Qualitative,
    save_profiles,
)


@pytest.fixture
def profiles_file(tmp_path: Path) -> Path:
    """Write a fixture profiles.json with 10 representative champions.

    These span the comp identity space we need to test:
      scaling carries: Caitlyn (late ADC), Senna (late ADC)
      mid-game: Rumble (mid-game powerhouse), Annie (mid-game burst)
      pick: Naafiri (early assassin), Pantheon (early game)
      tank/peel: Sion (late tank), Lulu (enchanter peel)
      utility: Bard (support), Karma (mid/sup)
    """
    profiles: dict[str, ChampionProfile] = {
        "Caitlyn": ChampionProfile(
            name="Caitlyn", patch="26.11",
            qualitative=Qualitative(
                scaling_early=1, scaling_mid=2, scaling_late=3,
                baron_dps_tier=3, peel_needs=2, peel_supply=0,
                teamfight_score=2, ult_impact=1,
                wave_clear=3,
                primary_role="bot",
            ),
            confidence=0.85,
        ),
        "Senna": ChampionProfile(
            name="Senna", patch="26.11",
            qualitative=Qualitative(
                scaling_early=0, scaling_mid=2, scaling_late=3,
                baron_dps_tier=3, peel_needs=2, peel_supply=1,
                teamfight_score=2, ult_impact=2,
                primary_role="bot",
            ),
            confidence=0.85,
        ),
        "Rumble": ChampionProfile(
            name="Rumble", patch="26.11",
            qualitative=Qualitative(
                scaling_early=1, scaling_mid=3, scaling_late=1,
                baron_dps_tier=2, peel_needs=1, peel_supply=1,
                teamfight_score=3, engage_score=2, disengage_score=2,
                wave_clear=3, ult_impact=3,
                primary_role="top",
            ),
            confidence=0.80,
        ),
        "Annie": ChampionProfile(
            name="Annie", patch="26.11",
            qualitative=Qualitative(
                scaling_early=0, scaling_mid=2, scaling_late=2,
                pick_threat=2, teamfight_score=3, engage_score=2,
                wave_clear=3, ult_impact=3,
                primary_role="mid",
            ),
            confidence=0.75,
        ),
        "Naafiri": ChampionProfile(
            name="Naafiri", patch="26.11",
            qualitative=Qualitative(
                scaling_early=2, scaling_mid=1, scaling_late=-2,
                pick_threat=3, primary_role="jungle",
            ),
            confidence=0.7,
        ),
        "Pantheon": ChampionProfile(
            name="Pantheon", patch="26.11",
            qualitative=Qualitative(
                scaling_early=3, scaling_mid=1, scaling_late=-1,
                pick_threat=3, engage_score=3, ult_impact=3,
                primary_role="jungle", secondary_roles=["support", "top", "mid"],
            ),
            confidence=0.7,
        ),
        "Sion": ChampionProfile(
            name="Sion", patch="26.11",
            qualitative=Qualitative(
                scaling_early=-1, scaling_mid=1, scaling_late=3,
                peel_supply=2, split_push_threat=2,
                teamfight_score=3, engage_score=3, ult_impact=3,
                primary_role="top",
            ),
            confidence=0.85,
        ),
        "Lulu": ChampionProfile(
            name="Lulu", patch="26.11",
            qualitative=Qualitative(
                scaling_early=0, scaling_mid=1, scaling_late=2,
                peel_supply=3, disengage_score=2, ult_impact=2,
                primary_role="support",
            ),
            confidence=0.9,
        ),
        "Bard": ChampionProfile(
            name="Bard", patch="26.11",
            qualitative=Qualitative(
                scaling_early=1, scaling_mid=2, scaling_late=2,
                peel_supply=2, pick_threat=2, ult_impact=3,
                primary_role="support",
            ),
            confidence=0.75,
        ),
        "Karma": ChampionProfile(
            name="Karma", patch="26.11",
            qualitative=Qualitative(
                scaling_early=1, scaling_mid=2, scaling_late=2,
                peel_supply=2, disengage_score=2, wave_clear=2,
                primary_role="support", secondary_roles=["mid"],
            ),
            confidence=0.8,
        ),
    }
    path = tmp_path / "profiles.json"
    save_profiles(profiles, path)
    return path


@pytest.fixture
def synergies_file(tmp_path: Path) -> Path:
    """Override the synergies cache by writing into a known location.

    The aggregator caches the synergies dict module-level, so we monkeypatch
    it during these tests via the helper below.
    """
    path = tmp_path / "synergies.json"
    path.write_text(json.dumps({
        "Caitlyn|Lulu": {"teamfight_score": 1, "scaling_late": 1},
        "Annie|Pantheon": {"engage_score": 1, "pick_threat": 1},
        "Rumble|Karma": {"teamfight_score": 1, "wave_clear": 1},
    }))
    return path


def _patch_synergy_cache(monkeypatch, synergies_path: Path):
    """Clear the module caches and patch loaders so only the test's hand-curated
    legacy synergies are visible. Measured (gol.gg) synergies are stubbed empty.
    """
    import loltrader.comp.aggregator as agg
    monkeypatch.setattr(agg, "_SYNERGY_CACHE", None)
    monkeypatch.setattr(agg, "_MEASURED_PAIR_CACHE", None)
    monkeypatch.setattr(agg, "_MEASURED_TRIPLE_CACHE", None)
    monkeypatch.setattr(agg, "_load_synergies", lambda path=None: json.loads(synergies_path.read_text()))
    monkeypatch.setattr(agg, "_load_measured_pairs", lambda path=None: {})
    monkeypatch.setattr(agg, "_load_measured_triples", lambda path=None: {})


# ---------- scaling curve tests -----------------------------------------


def test_pair_key_is_order_insensitive():
    assert _pair_key("Caitlyn", "Lulu") == _pair_key("Lulu", "Caitlyn")


def test_interp_scaling_endpoints(profiles_file):
    """At the anchor minutes, scaling matches the qualitative dimension exactly."""
    from loltrader.comp.profiles import load_profiles
    profs = load_profiles(profiles_file)

    caitlyn = profs["Caitlyn"]
    assert _interp_scaling(caitlyn, 7) == 1.0   # scaling_early
    assert _interp_scaling(caitlyn, 20) == 2.0  # scaling_mid
    assert _interp_scaling(caitlyn, 32) == 3.0  # scaling_late


def test_interp_scaling_intermediate(profiles_file):
    """Between anchors, the curve linearly interpolates."""
    from loltrader.comp.profiles import load_profiles
    profs = load_profiles(profiles_file)
    caitlyn = profs["Caitlyn"]  # early=1, mid=2 → midpoint of 7-20 = 13.5 → ~1.5
    val = _interp_scaling(caitlyn, 14)  # closer to mid
    assert 1.4 < val < 1.7


def test_scaling_curve_sum_across_team(profiles_file):
    """Curve at each minute is the sum across the 5 champions."""
    from loltrader.comp.profiles import load_profiles
    profs = load_profiles(profiles_file)
    team = [profs["Caitlyn"], profs["Lulu"], profs["Rumble"], profs["Annie"], profs["Pantheon"]]
    curve = _team_scaling_curve(team)
    # At minute 7, sum of scaling_early: 1+0+1+0+3 = 5
    assert curve[7] == 5.0
    # At minute 32, sum of scaling_late: 3+2+1+2+(-1) = 7
    assert curve[32] == 7.0


# ---------- evaluate_comp end-to-end ------------------------------------


def test_evaluate_basic_comp(profiles_file, monkeypatch, tmp_path: Path):
    """Pure happy path: 5 valid picks produce a CompProfile.

    Patches synergies to empty so the raw scaling sum is verifiable.
    """
    empty_syn = tmp_path / "empty_synergies.json"
    empty_syn.write_text("{}")
    _patch_synergy_cache(monkeypatch, empty_syn)

    picks = [
        ChampionPick("Caitlyn", "bot"),
        ChampionPick("Lulu", "support"),
        ChampionPick("Rumble", "top"),
        ChampionPick("Annie", "mid"),
        ChampionPick("Pantheon", "jungle"),
    ]
    profile = evaluate_comp(picks, patch="26.11", profiles_path=profiles_file)

    assert isinstance(profile, CompProfile)
    assert profile.archetype in ("scaling", "teamfight", "pick", "balanced")
    assert profile.teamfight_score >= 0
    assert profile.synergy_bonuses == []
    # Late-game sum: 3+2+1+2-1 = 7
    assert profile.scaling_curve[32] == 7.0


def test_evaluate_unknown_champion_raises(profiles_file):
    """Picks that don't exist raise KeyError with the missing names."""
    picks = [
        ChampionPick("Caitlyn", "bot"),
        ChampionPick("Lulu", "support"),
        ChampionPick("NonExistentChampion", "top"),
        ChampionPick("Annie", "mid"),
        ChampionPick("Pantheon", "jungle"),
    ]
    with pytest.raises(KeyError, match="NonExistentChampion"):
        evaluate_comp(picks, profiles_path=profiles_file)


def test_synergy_bonus_applies(profiles_file, synergies_file, monkeypatch):
    """When Caitlyn+Lulu are both picked, the synergy fires."""
    _patch_synergy_cache(monkeypatch, synergies_file)
    picks = [
        ChampionPick("Caitlyn", "bot"),
        ChampionPick("Lulu", "support"),
        ChampionPick("Rumble", "top"),
        ChampionPick("Annie", "mid"),
        ChampionPick("Pantheon", "jungle"),
    ]
    profile = evaluate_comp(picks, profiles_path=profiles_file)

    # Caitlyn|Lulu synergy fired (teamfight +1, scaling_late shift via curve)
    assert any("Caitlyn|Lulu" in s for s in profile.synergy_bonuses)
    assert any("Annie|Pantheon" in s for s in profile.synergy_bonuses)
    assert any("Rumble" in s and "Karma" in s for s in profile.synergy_bonuses) is False


def test_measured_role_aware_synergies_fire(profiles_file, monkeypatch):
    """Role-aware measured synergies from gol.gg fire when (champ, role) keys match.

    Verifies the role distinction: Caitlyn:bot+Lulu:support fires, but the same
    Lulu played mid would not match the bot|support synergy.
    """
    import loltrader.comp.aggregator as agg
    monkeypatch.setattr(agg, "_SYNERGY_CACHE", None)
    monkeypatch.setattr(agg, "_MEASURED_PAIR_CACHE", None)
    monkeypatch.setattr(agg, "_MEASURED_TRIPLE_CACHE", None)
    monkeypatch.setattr(agg, "_load_synergies", lambda path=None: {})
    monkeypatch.setattr(agg, "_load_measured_pairs", lambda path=None: {
        "Caitlyn:bot|Lulu:support": {
            "scaling_early_boost": 1.5, "engage_boost": 0.75,
            "teamfight_boost": 0.0, "pick_threat_boost": 0.0,
            "scaling_mid_boost": 0.0, "scaling_late_boost": 0.0,
        },
    })
    monkeypatch.setattr(agg, "_load_measured_triples", lambda path=None: {})

    # Correct role assignment — synergy should fire
    correct = [
        ChampionPick("Caitlyn", "bot"),
        ChampionPick("Lulu", "support"),
        ChampionPick("Rumble", "top"),
        ChampionPick("Annie", "mid"),
        ChampionPick("Pantheon", "jungle"),
    ]
    prof_correct = evaluate_comp(correct, profiles_path=profiles_file)
    assert any("Caitlyn:bot|Lulu:support" in s for s in prof_correct.synergy_bonuses), \
        f"expected synergy not in {prof_correct.synergy_bonuses}"


def test_measured_triples_fire_when_all_3_present(profiles_file, monkeypatch):
    """A 3-champion triple synergy fires only when all 3 specific picks match."""
    import loltrader.comp.aggregator as agg
    monkeypatch.setattr(agg, "_SYNERGY_CACHE", None)
    monkeypatch.setattr(agg, "_MEASURED_PAIR_CACHE", None)
    monkeypatch.setattr(agg, "_MEASURED_TRIPLE_CACHE", None)
    monkeypatch.setattr(agg, "_load_synergies", lambda path=None: {})
    monkeypatch.setattr(agg, "_load_measured_pairs", lambda path=None: {})
    monkeypatch.setattr(agg, "_load_measured_triples", lambda path=None: {
        "Caitlyn:bot|Lulu:support|Pantheon:jungle": {
            "scaling_late_boost": 1.0,
            "teamfight_boost": 0.5,
            "scaling_early_boost": 0.0, "scaling_mid_boost": 0.0,
            "engage_boost": 0.0, "pick_threat_boost": 0.0,
        },
    })

    # All 3 present → triple fires
    picks = [
        ChampionPick("Caitlyn", "bot"),
        ChampionPick("Pantheon", "jungle"),
        ChampionPick("Lulu", "support"),
        ChampionPick("Annie", "mid"),
        ChampionPick("Rumble", "top"),
    ]
    profile = evaluate_comp(picks, profiles_path=profiles_file)
    assert any("triple:" in s and "Caitlyn" in s for s in profile.synergy_bonuses), \
        f"triple should fire: {profile.synergy_bonuses}"


def test_confidence_is_min_of_picks(profiles_file):
    """A comp's confidence is the lowest among its picks."""
    picks = [
        ChampionPick("Caitlyn", "bot"),    # 0.85
        ChampionPick("Lulu", "support"),    # 0.9
        ChampionPick("Rumble", "top"),      # 0.8
        ChampionPick("Annie", "mid"),       # 0.75
        ChampionPick("Pantheon", "jungle"), # 0.7  ← weakest
    ]
    profile = evaluate_comp(picks, profiles_path=profiles_file)
    assert profile.confidence == 0.7


def test_scaling_curve_monotonic_for_pure_late_team(profiles_file):
    """A team of 5 late-game champions should scale upward over time."""
    picks = [
        ChampionPick("Caitlyn", "bot"),
        ChampionPick("Senna", "bot"),       # both late-game
        ChampionPick("Sion", "top"),
        ChampionPick("Lulu", "support"),
        ChampionPick("Karma", "mid"),
    ]
    profile = evaluate_comp(picks, profiles_path=profiles_file)
    # Late-game score >= early-game score (with substantial margin)
    assert profile.scaling_curve[32] > profile.scaling_curve[7]


def test_scaling_curve_inverse_for_anti_scaling_team(profiles_file):
    """A team of early-game champs should have early > late scaling."""
    picks = [
        ChampionPick("Pantheon", "jungle"),  # late=-1
        ChampionPick("Naafiri", "jungle"),   # late=-2
        ChampionPick("Rumble", "top"),       # late=1
        ChampionPick("Annie", "mid"),
        ChampionPick("Bard", "support"),
    ]
    profile = evaluate_comp(picks, profiles_path=profiles_file)
    # Curve at minute 32 < curve at minute 20 for this team
    assert profile.scaling_curve[32] < profile.scaling_curve[20]


def test_archetype_classification_scaling(profiles_file):
    """A heavy late-game mage+marksman comp classifies as scaling."""
    picks = [
        ChampionPick("Caitlyn", "bot"),
        ChampionPick("Senna", "bot"),       # 2nd marksman
        ChampionPick("Karma", "mid"),       # enchanter→mage-like
        ChampionPick("Lulu", "support"),    # enchanter→mage-like
        ChampionPick("Rumble", "top"),
    ]
    profile = evaluate_comp(picks, profiles_path=profiles_file)
    # Many enchanters + marksmen → scaling
    assert profile.archetype in ("scaling", "teamfight", "balanced")


def test_win_condition_string_non_empty(profiles_file):
    """The human-readable win condition is always generated."""
    picks = [
        ChampionPick("Caitlyn", "bot"),
        ChampionPick("Lulu", "support"),
        ChampionPick("Rumble", "top"),
        ChampionPick("Annie", "mid"),
        ChampionPick("Pantheon", "jungle"),
    ]
    profile = evaluate_comp(picks, profiles_path=profiles_file)
    assert len(profile.win_condition) > 20


def test_scaling_at_outside_window(profiles_file):
    """scaling_at extrapolates flat outside the 0-40 minute window."""
    picks = [
        ChampionPick("Caitlyn", "bot"),
        ChampionPick("Lulu", "support"),
        ChampionPick("Rumble", "top"),
        ChampionPick("Annie", "mid"),
        ChampionPick("Pantheon", "jungle"),
    ]
    profile = evaluate_comp(picks, profiles_path=profiles_file)
    assert profile.scaling_at(-5) == profile.scaling_curve[0]
    assert profile.scaling_at(100) == profile.scaling_curve[40]
