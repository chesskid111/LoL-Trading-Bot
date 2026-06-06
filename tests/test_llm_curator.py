"""Tests for LLM curator prompt building + response parsing."""
from __future__ import annotations

import json

import pytest

from loltrader.comp.llm_curator import (
    LLMCurator,
    _parse_response,
    build_prompt,
    result_to_profile,
)
from loltrader.comp.profiles import (
    ProfileValidationError,
    Qualitative,
)


def test_build_prompt_includes_champion_and_patch():
    prompt = build_prompt("Caitlyn", "16.10", league="LCK", pickrate=0.32, winrate=0.55)
    assert "Caitlyn" in prompt
    assert "16.10" in prompt
    assert "LCK" in prompt
    assert "32.0%" in prompt  # pickrate
    assert "55.0%" in prompt  # winrate
    # Required scoring schema labels appear
    for key in ["scaling_early", "scaling_mid", "scaling_late",
                "baron_dps_tier", "peel_needs", "primary_role"]:
        assert key in prompt


def test_build_prompt_omits_stats_when_absent():
    prompt = build_prompt("Yorick", "16.10")
    assert "pickrate" not in prompt
    assert "winrate" not in prompt


def test_parse_clean_json_response():
    payload = json.dumps({
        "qualitative": {
            "scaling_early": 0, "scaling_mid": 1, "scaling_late": 3,
            "baron_dps_tier": 3, "peel_needs": 2, "peel_supply": 0,
            "split_push_threat": 1, "pick_threat": 0,
            "teamfight_score": 2, "engage_score": 0, "disengage_score": 0,
            "wave_clear": 3, "ult_impact": 1,
            "comfort_curve": "spike-3-item",
            "primary_role": "bot",
            "secondary_roles": [],
        },
        "common_partners": ["Lulu"],
        "common_counters": ["Draven"],
        "data_sources": ["test"],
        "confidence": 0.8,
        "flags": [],
    })
    result = _parse_response("Caitlyn", payload)
    assert result.champion == "Caitlyn"
    assert result.qualitative.scaling_late == 3
    assert result.qualitative.comfort_curve == "spike-3-item"
    assert result.common_partners == ["Lulu"]
    assert result.confidence == 0.8


def test_parse_tolerates_markdown_fence():
    """LLMs sometimes wrap JSON in ```json ... ``` fences."""
    payload = "```json\n" + json.dumps({
        "qualitative": {
            "scaling_early": 0, "scaling_mid": 0, "scaling_late": 0,
            "primary_role": "mid",
        },
        "confidence": 0.5,
    }) + "\n```"
    result = _parse_response("Test", payload)
    assert result.qualitative.primary_role == "mid"


def test_parse_clamps_out_of_range_values():
    """If the LLM hallucinates an out-of-range integer, clamp rather than fail."""
    payload = json.dumps({
        "qualitative": {
            "scaling_late": 99,  # clamped to 3
            "baron_dps_tier": 0,  # clamped to 1 (range is 1..5)
            "primary_role": "mid",
        },
        "confidence": 0.7,
    })
    result = _parse_response("Test", payload)
    assert result.qualitative.scaling_late == 3
    assert result.qualitative.baron_dps_tier == 1


def test_parse_clamps_invalid_role_to_mid():
    """Invalid primary_role falls back to 'mid' rather than crashing."""
    payload = json.dumps({
        "qualitative": {"primary_role": "elsewhere"},
        "confidence": 0.5,
    })
    result = _parse_response("Test", payload)
    assert result.qualitative.primary_role == "mid"


def test_parse_filters_invalid_secondary_roles():
    """Secondary roles filtered to valid lanes only."""
    payload = json.dumps({
        "qualitative": {
            "primary_role": "top",
            "secondary_roles": ["mid", "fillage", "support"],
        },
        "confidence": 0.5,
    })
    result = _parse_response("Test", payload)
    assert result.qualitative.secondary_roles == ["mid", "support"]


def test_parse_clamps_confidence():
    """Confidence > 1 is clamped to 1."""
    payload = json.dumps({
        "qualitative": {"primary_role": "mid"},
        "confidence": 1.5,
    })
    result = _parse_response("Test", payload)
    assert result.confidence == 1.0


def test_result_to_profile_validates():
    """result_to_profile produces a fully valid ChampionProfile."""
    payload = json.dumps({
        "qualitative": {
            "scaling_early": 1, "scaling_mid": 2, "scaling_late": 3,
            "primary_role": "bot",
        },
        "confidence": 0.85,
        "data_sources": ["LS analysis"],
    })
    result = _parse_response("Caitlyn", payload)
    profile = result_to_profile(result, patch="16.10", last_updated="2026-06-06T00:00:00Z")
    assert profile.name == "Caitlyn"
    assert profile.patch == "16.10"
    assert profile.qualitative.scaling_late == 3
    # Validation already passed inside result_to_profile


def test_manual_backend_uses_callable():
    """Manual backend reads canned responses via the callable, no API needed."""
    canned = json.dumps({
        "qualitative": {"primary_role": "mid"},
        "confidence": 0.7,
    })
    curator = LLMCurator(backend="manual", manual_provider=lambda c, p: canned)
    result = curator.curate_one("Yasuo", "16.10")
    assert result.champion == "Yasuo"
    assert result.cost_usd == 0.0
    assert curator.call_count == 1


def test_anthropic_backend_requires_api_key(monkeypatch):
    """Without an API key, anthropic backend refuses to construct."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="API key"):
        LLMCurator(backend="anthropic")


def test_parse_raises_on_malformed_json():
    """Plain text response is rejected."""
    with pytest.raises(json.JSONDecodeError):
        _parse_response("Test", "this is not json at all")
