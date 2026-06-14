"""Tests for league classification from Kalshi market titles.

Regression coverage for the dashboard "wrong regions / random matches" bug:
- title extractor must handle all three Kalshi formats (series / map / totalmaps)
- academy/Challenger teams must NOT inherit their parent org's tier-1 league
- minor/ERL/LJL teams must classify as their real league or 'Other', not a major
"""
from __future__ import annotations

import pytest

from loltrader.ui.leagues import (
    _extract_teams_from_title, league_for_match, TIER1_DEFAULT,
)


# (title, expected_teams)
EXTRACT_CASES = [
    ("Will Rising Gaming win the FENNEL vs. Rising Gaming League of Legends match?",
     ("FENNEL", "Rising Gaming")),
    ("Will Rising Gaming win map 1 in the FENNEL vs. Rising Gaming match?",
     ("FENNEL", "Rising Gaming")),
    ("Will over 4.5 maps be played in the FENNEL vs. Rising Gaming League of Legends match?",
     ("FENNEL", "Rising Gaming")),
    ("Will T1 win the Gen.G vs. T1 League of Legends match?",
     ("Gen.G", "T1")),
    ("Will T1 win map 2 in the Gen.G vs. T1 match?",
     ("Gen.G", "T1")),
]


@pytest.mark.parametrize("title,expected", EXTRACT_CASES)
def test_extract_handles_all_title_formats(title, expected):
    assert _extract_teams_from_title(title) == expected


# (title, expected_league)
CLASSIFY_CASES = [
    # tier-1 majors
    ("Will T1 win the Gen.G vs. T1 League of Legends match?", "LCK"),
    ("Will Top Esports win the Bilibili Gaming vs. Top Esports League of Legends match?", "LPL"),
    ("Will Cloud9 win the Cloud9 vs. Team Liquid League of Legends match?", "LCS"),
    # minor / regional -> Other or their real league, NOT a major
    ("Will Rising Gaming win the FENNEL vs. Rising Gaming League of Legends match?", "Other"),
    ("Will Galions win the Eintracht Spandau vs. Galions League of Legends match?", "Other"),
    ("Will UCAM Esports Club win the UCAM Esports Club vs. Solary League of Legends match?", "Other"),
    ("Will RED Canids win the LOS vs. RED Canids League of Legends match?", "CBLOL"),
    # academy/Challenger must NOT inherit parent tier-1 league
    ("Will Saigon Warriors win the Saigon Warriors vs. KT Rolster Challengers League of Legends match?", "Other"),
    ("Will Top Esports Challenger win the KT Rolster Challengers vs. Top Esports Challenger League of Legends match?", "Other"),
]


@pytest.mark.parametrize("title,expected", CLASSIFY_CASES)
def test_classify_league(title, expected):
    assert league_for_match(title) == expected


def test_classification_consistent_across_title_formats():
    """Series, map, and total-maps rows of the same match get the same league."""
    series = "Will T1 win the Gen.G vs. T1 League of Legends match?"
    mp = "Will T1 win map 2 in the Gen.G vs. T1 match?"
    tot = "Will over 3.5 maps be played in the Gen.G vs. T1 League of Legends match?"
    assert league_for_match(series) == league_for_match(mp) == league_for_match(tot) == "LCK"


def test_academy_not_in_tier1_default():
    # academy/minor matches resolve to leagues outside the default majors view
    assert league_for_match(
        "Will Saigon Warriors win the Saigon Warriors vs. KT Rolster Challengers match?"
    ) not in TIER1_DEFAULT
