"""Tests for ChampionProfile schema + load/save roundtrip."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from loltrader.comp.profiles import (
    ChampionProfile,
    ProfileValidationError,
    ProStats,
    Qualitative,
    SCHEMA_VERSION,
    load_profiles,
    save_profiles,
    validate_profile,
)


@pytest.fixture
def sample_profiles() -> dict[str, ChampionProfile]:
    """Three representative champions covering scaling/teamfight/pick archetypes."""
    caitlyn = ChampionProfile(
        name="Caitlyn",
        patch="26.10",
        qualitative=Qualitative(
            scaling_early=1, scaling_mid=2, scaling_late=3,
            baron_dps_tier=3, peel_needs=2,
            teamfight_score=2, wave_clear=3, ult_impact=1,
            primary_role="bot",
        ),
        pro_stats=ProStats(
            pickrate_30d=0.32, banrate_30d=0.18, winrate_30d=0.535,
            priority_score=7.8, games_sampled=41,
        ),
        common_partners=["Lulu", "Karma"],
        common_counters=["Senna+Tahm"],
        confidence=0.85,
        last_updated="2026-06-04T03:00:00Z",
    )
    naafiri = ChampionProfile(
        name="Naafiri",
        patch="26.10",
        qualitative=Qualitative(
            scaling_early=2, scaling_mid=1, scaling_late=-2,
            pick_threat=2, primary_role="jungle",
        ),
        pro_stats=ProStats(
            pickrate_30d=0.08, winrate_30d=0.48,
            priority_score=3.0, games_sampled=12,
        ),
        confidence=0.7,
        last_updated="2026-06-04T03:00:00Z",
    )
    lulu = ChampionProfile(
        name="Lulu",
        patch="26.10",
        qualitative=Qualitative(
            scaling_early=0, scaling_mid=1, scaling_late=2,
            peel_supply=3, disengage_score=2,
            primary_role="support",
        ),
        confidence=0.9,
        last_updated="2026-06-04T03:00:00Z",
    )
    return {"Caitlyn": caitlyn, "Naafiri": naafiri, "Lulu": lulu}


def test_default_profile_validates():
    """Default-constructed ChampionProfile passes validation."""
    p = ChampionProfile(name="Test")
    validate_profile(p)


def test_roundtrip(tmp_path: Path, sample_profiles):
    """Save then load reproduces the same profiles."""
    path = tmp_path / "profiles.json"
    save_profiles(sample_profiles, path)
    loaded = load_profiles(path)

    assert set(loaded.keys()) == set(sample_profiles.keys())
    for name, original in sample_profiles.items():
        got = loaded[name]
        assert got.name == original.name
        assert got.patch == original.patch
        assert got.qualitative == original.qualitative
        assert got.pro_stats == original.pro_stats
        assert got.common_partners == original.common_partners
        assert got.confidence == original.confidence


def test_load_missing_file_returns_empty(tmp_path: Path):
    """First-boot case: file doesn't exist yet."""
    loaded = load_profiles(tmp_path / "does_not_exist.json")
    assert loaded == {}


def test_save_creates_parent_dir(tmp_path: Path, sample_profiles):
    """save_profiles creates any missing parent directories."""
    nested = tmp_path / "deep" / "nested" / "profiles.json"
    save_profiles(sample_profiles, nested)
    assert nested.exists()


def test_save_is_sorted(tmp_path: Path, sample_profiles):
    """Saved JSON keys are sorted alphabetically for stable diffs."""
    path = tmp_path / "profiles.json"
    save_profiles(sample_profiles, path)
    with path.open() as f:
        raw = json.load(f)
    assert list(raw.keys()) == sorted(sample_profiles.keys())


def test_validate_rejects_scaling_out_of_range():
    """Scaling dimensions are -3..+3; reject anything else."""
    p = ChampionProfile(name="Bad")
    p.qualitative.scaling_late = 5  # out of range
    with pytest.raises(ProfileValidationError, match="scaling_late"):
        validate_profile(p)


def test_validate_rejects_pick_threat_negative():
    """Pick threat is 0..3."""
    p = ChampionProfile(name="Bad")
    p.qualitative.pick_threat = -1
    with pytest.raises(ProfileValidationError, match="pick_threat"):
        validate_profile(p)


def test_validate_rejects_invalid_role():
    """Primary role must be a real lane."""
    p = ChampionProfile(name="Bad")
    p.qualitative.primary_role = "fillage"  # not a real role
    with pytest.raises(ProfileValidationError, match="primary_role"):
        validate_profile(p)


def test_validate_rejects_invalid_comfort_curve():
    """comfort_curve must be one of the named enum values."""
    p = ChampionProfile(name="Bad")
    p.qualitative.comfort_curve = "elsewhere"  # type: ignore[assignment]
    with pytest.raises(ProfileValidationError, match="comfort_curve"):
        validate_profile(p)


def test_validate_rejects_pickrate_above_one():
    """Pickrate is a probability."""
    p = ChampionProfile(name="Bad")
    p.pro_stats.pickrate_30d = 1.5
    with pytest.raises(ProfileValidationError, match="pickrate_30d"):
        validate_profile(p)


def test_validate_rejects_priority_above_ten():
    """Priority score is 0..10."""
    p = ChampionProfile(name="Bad")
    p.pro_stats.priority_score = 12.0
    with pytest.raises(ProfileValidationError, match="priority_score"):
        validate_profile(p)


def test_validate_rejects_confidence_above_one():
    """Confidence is a probability."""
    p = ChampionProfile(name="Bad", confidence=1.5)
    with pytest.raises(ProfileValidationError, match="confidence"):
        validate_profile(p)


def test_save_refuses_invalid(tmp_path: Path, sample_profiles):
    """save_profiles validates before writing; partial files never persisted."""
    bad = ChampionProfile(name="Broken")
    bad.qualitative.baron_dps_tier = 99  # out of range
    profiles = {**sample_profiles, "Broken": bad}
    path = tmp_path / "should_not_exist.json"
    with pytest.raises(ProfileValidationError):
        save_profiles(profiles, path)
    assert not path.exists()


def test_load_skips_unknown_fields(tmp_path: Path):
    """Forward-compat: extra JSON fields are silently dropped on load."""
    path = tmp_path / "future.json"
    path.write_text(json.dumps({
        "Future": {
            "schema_version": SCHEMA_VERSION,
            "patch": "27.1",
            "qualitative": {
                "scaling_early": 0, "scaling_mid": 0, "scaling_late": 0,
                "future_field_we_dont_know": "ignore",
                "primary_role": "mid",
            },
            "pro_stats": {"pickrate_30d": 0.1, "future_stat": 99},
            "extra_top_level": [1, 2, 3],
        }
    }))
    loaded = load_profiles(path)
    assert "Future" in loaded
    assert loaded["Future"].qualitative.primary_role == "mid"


def test_strict_load_raises_on_invalid(tmp_path: Path):
    """strict=True flips validation to fail-fast on file corruption."""
    path = tmp_path / "corrupt.json"
    path.write_text(json.dumps({
        "Broken": {
            "patch": "26.10",
            "qualitative": {"scaling_late": 99, "primary_role": "mid"},
        }
    }))
    with pytest.raises(ProfileValidationError):
        load_profiles(path, strict=True)
