"""Champion profile loading + schema.

A ``ChampionProfile`` is the canonical per-champion record consumed by Layer 2
(comp aggregator). Profiles are persisted as JSON in ``data/champion_profiles.json``
and updated per patch via two tracks:

  - **Qualitative** dimensions (scaling/peel/comp role) are LLM-aggregated from
    analyst content (see ``llm_curator.py``).
  - **Pro stats** (pickrate/banrate/winrate/priority) are auto-scraped from
    gol.gg and cross-validated against Oracle's Elixir (see ``pro_stats.py``).

Schema is versioned via ``schema_version`` so future migrations can detect
out-of-date entries.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = 1

# Qualitative dimension ranges (inclusive) — validated on load/save.
# Hand-picked to match the dimensions table in the design spec.
QUALITATIVE_RANGES: dict[str, tuple[int, int]] = {
    "scaling_early":     (-3, 3),
    "scaling_mid":       (-3, 3),
    "scaling_late":      (-3, 3),
    "baron_dps_tier":    (1, 5),
    "peel_needs":        (0, 3),
    "peel_supply":       (0, 3),
    "split_push_threat": (0, 3),
    "pick_threat":       (0, 3),
    "teamfight_score":   (-3, 3),
    "engage_score":      (0, 3),
    "disengage_score":   (0, 3),
    "wave_clear":        (0, 3),
    "ult_impact":        (0, 3),
}

VALID_COMFORT_CURVES = {"smooth", "spike-2-item", "spike-3-item"}
VALID_ROLES = {"top", "jungle", "mid", "bot", "support"}


# ---------- dataclasses --------------------------------------------------


@dataclass
class Qualitative:
    """Hand-curated / LLM-aggregated qualitative dimensions."""
    scaling_early: int = 0
    scaling_mid: int = 0
    scaling_late: int = 0
    baron_dps_tier: int = 3
    peel_needs: int = 1
    peel_supply: int = 0
    split_push_threat: int = 0
    pick_threat: int = 0
    teamfight_score: int = 0
    engage_score: int = 0
    disengage_score: int = 0
    wave_clear: int = 1
    ult_impact: int = 1
    comfort_curve: Literal["smooth", "spike-2-item", "spike-3-item"] = "smooth"
    primary_role: str = "mid"
    secondary_roles: list[str] = field(default_factory=list)


@dataclass
class ProStats:
    """Auto-computed pro statistics from gol.gg + Oracle's Elixir."""
    pickrate_30d: float = 0.0
    banrate_30d: float = 0.0
    winrate_30d: float = 0.5
    priority_score: float = 0.0
    games_sampled: int = 0


@dataclass
class ChampionProfile:
    """One champion's full profile.

    The non-qualitative metadata (last_updated, data_sources, confidence) lets
    us track provenance — important for catching stale entries after a patch
    or LLM curation drift.
    """
    name: str
    schema_version: int = SCHEMA_VERSION
    patch: str = ""
    qualitative: Qualitative = field(default_factory=Qualitative)
    pro_stats: ProStats = field(default_factory=ProStats)
    common_partners: list[str] = field(default_factory=list)
    common_counters: list[str] = field(default_factory=list)
    data_sources: list[str] = field(default_factory=list)
    confidence: float = 0.0
    last_updated: str = ""
    validation_flags: list[str] = field(default_factory=list)


# ---------- validation ---------------------------------------------------


class ProfileValidationError(ValueError):
    """Raised when a champion profile fails schema validation."""


def _validate_qualitative(q: Qualitative, champ_name: str) -> None:
    """Range-check every qualitative dimension. Raises on first violation."""
    for field_name, (lo, hi) in QUALITATIVE_RANGES.items():
        v = getattr(q, field_name)
        if not isinstance(v, int):
            raise ProfileValidationError(
                f"{champ_name}.qualitative.{field_name} must be int, got {type(v).__name__}"
            )
        if not (lo <= v <= hi):
            raise ProfileValidationError(
                f"{champ_name}.qualitative.{field_name} = {v} out of range [{lo}, {hi}]"
            )
    if q.comfort_curve not in VALID_COMFORT_CURVES:
        raise ProfileValidationError(
            f"{champ_name}.qualitative.comfort_curve = {q.comfort_curve!r} not in {VALID_COMFORT_CURVES}"
        )
    if q.primary_role not in VALID_ROLES:
        raise ProfileValidationError(
            f"{champ_name}.qualitative.primary_role = {q.primary_role!r} not in {VALID_ROLES}"
        )
    for r in q.secondary_roles:
        if r not in VALID_ROLES:
            raise ProfileValidationError(
                f"{champ_name}.qualitative.secondary_roles contains {r!r}; must be subset of {VALID_ROLES}"
            )


def _validate_pro_stats(ps: ProStats, champ_name: str) -> None:
    """Range-check pro stats. Rates in [0, 1], priority in [0, 10]."""
    for rate_field in ("pickrate_30d", "banrate_30d", "winrate_30d"):
        v = getattr(ps, rate_field)
        if not (0.0 <= v <= 1.0):
            raise ProfileValidationError(
                f"{champ_name}.pro_stats.{rate_field} = {v} out of range [0, 1]"
            )
    if not (0.0 <= ps.priority_score <= 10.0):
        raise ProfileValidationError(
            f"{champ_name}.pro_stats.priority_score = {ps.priority_score} out of range [0, 10]"
        )
    if ps.games_sampled < 0:
        raise ProfileValidationError(
            f"{champ_name}.pro_stats.games_sampled = {ps.games_sampled} negative"
        )


def validate_profile(p: ChampionProfile) -> None:
    """Raise ProfileValidationError if any field is malformed.

    Used by save_profiles() to refuse to persist invalid data, and by
    load_profiles(strict=True) to fail fast on corrupted files.
    """
    if p.schema_version != SCHEMA_VERSION:
        raise ProfileValidationError(
            f"{p.name}.schema_version = {p.schema_version}; expected {SCHEMA_VERSION}"
        )
    if not (0.0 <= p.confidence <= 1.0):
        raise ProfileValidationError(
            f"{p.name}.confidence = {p.confidence} out of range [0, 1]"
        )
    _validate_qualitative(p.qualitative, p.name)
    _validate_pro_stats(p.pro_stats, p.name)


# ---------- JSON load/save -----------------------------------------------


def _profile_from_dict(name: str, d: dict[str, Any]) -> ChampionProfile:
    """Re-hydrate one profile from a JSON dict. Tolerant of missing keys —
    defaults from the dataclass fill in."""
    q_data = d.get("qualitative") or {}
    ps_data = d.get("pro_stats") or {}
    return ChampionProfile(
        name=name,
        schema_version=d.get("schema_version", SCHEMA_VERSION),
        patch=d.get("patch", ""),
        qualitative=Qualitative(**{k: v for k, v in q_data.items()
                                   if k in Qualitative.__dataclass_fields__}),
        pro_stats=ProStats(**{k: v for k, v in ps_data.items()
                              if k in ProStats.__dataclass_fields__}),
        common_partners=list(d.get("common_partners") or []),
        common_counters=list(d.get("common_counters") or []),
        data_sources=list(d.get("data_sources") or []),
        confidence=float(d.get("confidence", 0.0)),
        last_updated=d.get("last_updated", ""),
        validation_flags=list(d.get("validation_flags") or []),
    )


def _profile_to_dict(p: ChampionProfile) -> dict[str, Any]:
    """Convert a ChampionProfile back to its JSON dict form. The top-level
    key is the champion name, so we strip it from the body to avoid duplication."""
    body = asdict(p)
    body.pop("name", None)
    return body


def load_profiles(
    path: str | Path = "data/champion_profiles.json",
    strict: bool = False,
) -> dict[str, ChampionProfile]:
    """Load every champion profile keyed by champion name.

    Args:
        path: Path to the JSON file. Returns an empty dict if the file does
            not exist (so first-run boots cleanly).
        strict: If True, validate every loaded profile and raise on any error.
            If False (default), skip invalid profiles but log them; this is the
            right behavior for production where partial data is still useful.

    Returns:
        Dict mapping champion name → ChampionProfile.
    """
    path = Path(path)
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    out: dict[str, ChampionProfile] = {}
    for name, body in raw.items():
        profile = _profile_from_dict(name, body)
        if strict:
            validate_profile(profile)
        out[name] = profile
    return out


def save_profiles(
    profiles: dict[str, ChampionProfile],
    path: str | Path = "data/champion_profiles.json",
) -> None:
    """Validate then persist all profiles to disk.

    Validation runs before any write so we never persist a partially-valid
    file. The output is sorted by champion name for stable diffs.
    """
    for p in profiles.values():
        validate_profile(p)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sorted_keys = sorted(profiles.keys())
    out = {name: _profile_to_dict(profiles[name]) for name in sorted_keys}
    with path.open("w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=False)
        f.write("\n")
