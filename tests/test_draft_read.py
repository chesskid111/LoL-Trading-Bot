"""Tests for the draft-read generator (loltrader.comp.draft_read)."""
from __future__ import annotations

from loltrader.comp.aggregator import CompProfile, ChampionPick
from loltrader.comp.draft_read import build_draft_read, _crossover_minute


def _curve(early, late):
    """Linear scaling curve from `early` (min 0) to `late` (min 40)."""
    return {m: early + (late - early) * (m / 40.0) for m in range(0, 41)}


def _comp(early=0, late=0, teamfight=0, engage=0, disengage=0, pick=0,
          baron=0, peel_supply=0, peel_demand=0, archetype="balanced",
          synergies=None, win_condition="", confidence=0.8):
    return CompProfile(
        scaling_curve=_curve(early, late),
        baron_dps_total=baron, peel_supply_total=peel_supply,
        peel_demand_total=peel_demand, split_push_threat=0, pick_threat=pick,
        teamfight_score=teamfight, engage_score=engage, disengage_score=disengage,
        wave_clear=0, ult_impact=0, archetype=archetype,
        synergy_bonuses=synergies or [], win_condition=win_condition,
        confidence=confidence,
    )


def _picks(*champs):
    roles = ["top", "jungle", "mid", "bot", "support"]
    return [ChampionPick(c, roles[i]) for i, c in enumerate(champs)]


def test_scaling_crossover_detected():
    # blue weak early/strong late, red strong early/weak late -> they cross
    blue = _comp(early=2, late=12)
    red = _comp(early=10, late=2)
    x = _crossover_minute(blue, red)
    assert x is not None and 5 <= x <= 40


def test_no_crossover_when_one_side_always_ahead():
    blue = _comp(early=10, late=12)
    red = _comp(early=2, late=3)
    assert _crossover_minute(blue, red) is None


def test_favored_side_reflects_scaling_and_teamfight():
    blue = _comp(late=12, teamfight=11)
    red = _comp(late=4, teamfight=4)
    r = build_draft_read(blue, red, _picks("A"), _picks("B"),
                         blue_team="T1", red_team="GEN")
    assert r["favored_side"] == "blue"
    assert r["favored_team"] == "T1"
    assert r["draft_lean_pct"] > 0.5


def test_engage_vs_disengage_dynamic_surfaces():
    # red has heavy engage, blue has no disengage -> vulnerability called out
    blue = _comp(disengage=0, late=6)
    red = _comp(engage=9, late=6)
    r = build_draft_read(blue, red, _picks("A"), _picks("B"),
                         blue_team="T1", red_team="GEN")
    joined = " ".join(r["dynamics"]).lower()
    assert "forced fights" in joined
    assert "t1 is vulnerable" in joined or "vulnerable to forced fights" in joined


def test_late_scaling_dynamic_and_headline():
    blue = _comp(early=3, late=13)   # scales hard
    red = _comp(early=8, late=4)
    r = build_draft_read(blue, red, _picks("A"), _picks("B"),
                         blue_team="T1", red_team="GEN")
    assert any("scales harder" in d for d in r["dynamics"])
    assert "Draft leans" in r["headline"]
    assert r["disclaimer"]


def test_side_payload_shape():
    blue = _comp(late=8, archetype="scaling", synergies=["Caitlyn:bot|Lux:support"],
                 win_condition="Scale and teamfight at 3 items")
    red = _comp(late=5, archetype="pick")
    r = build_draft_read(blue, red, _picks("Caitlyn", "LeeSin"), _picks("Zed"),
                         blue_team="T1", red_team="GEN")
    b = r["blue"]
    assert b.team == "T1"
    assert b.archetype == "scaling"
    assert b.picks[0]["champion"] == "Caitlyn"
    assert b.synergies == ["Caitlyn:bot|Lux:support"]
    assert b.win_condition.startswith("Scale")


def test_dynamics_capped_at_three():
    blue = _comp(early=10, late=14, teamfight=12, engage=10, pick=8, baron=10)
    red = _comp(early=1, late=1, teamfight=1, disengage=0, pick=0, baron=0)
    r = build_draft_read(blue, red, _picks("A"), _picks("B"))
    assert len(r["dynamics"]) <= 3
