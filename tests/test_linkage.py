"""Tests for the Kalshi-to-Oracle linkage logic."""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pytest

from loltrader.db import connect, migrate
from loltrader.kalshi.linkage import (
    LINK_CONFIDENCE_THRESHOLD,
    LinkResult,
    backfill_links,
    link_market,
    parse_event_title,
    parse_market_yes_team,
    parse_ticker_date,
    resolve_team_name,
)


# --- pure parser tests ----------------------------------------------------

def test_parse_event_title_basic():
    assert parse_event_title("FlyQuest vs. Cloud9: Game") == ("FlyQuest", "Cloud9")
    assert parse_event_title("Hanwha Life Esports vs. Nongshim RedForce: Map 1") == (
        "Hanwha Life Esports", "Nongshim RedForce",
    )


def test_parse_event_title_no_period():
    assert parse_event_title("G2 Esports vs Karmine Corp: Total Maps") == (
        "G2 Esports", "Karmine Corp",
    )


def test_parse_event_title_unparseable():
    assert parse_event_title("") is None
    assert parse_event_title(None) is None
    assert parse_event_title("Some random text without vs.") is None


def test_parse_market_yes_team():
    title = "Will FlyQuest win the FlyQuest vs. Cloud9 League of Legends match?"
    assert parse_market_yes_team(title) == "FlyQuest"

    title2 = "Will Cloud9 win the FlyQuest vs. Cloud9 Map 1?"
    assert parse_market_yes_team(title2) == "Cloud9"


def test_parse_market_yes_team_totals_returns_none():
    title = "Will over 3.5 maps be played in the FlyQuest vs. Cloud9 League of Legends match?"
    # Totals markets don't follow "Will X win ..." pattern
    assert parse_market_yes_team(title) is None


def test_parse_ticker_date_normal():
    assert parse_ticker_date("KXLOLGAME-26MAY231600FLYC9") == date(2026, 5, 23)
    assert parse_ticker_date("KXLOLMAP-26MAY231600FLYC9-1") == date(2026, 5, 23)
    assert parse_ticker_date("KXLOLTOTALMAPS-26MAY191600PNGAITZ") == date(2026, 5, 19)


def test_parse_ticker_date_bad():
    assert parse_ticker_date(None) is None
    assert parse_ticker_date("notaticker") is None
    assert parse_ticker_date("KXLOLGAME-NODATE") is None


# --- DB-backed scoring tests ----------------------------------------------

@pytest.fixture
def linkage_db(tmp_path: Path):
    """Empty DB with all migrations + a small set of teams and a known match."""
    db = tmp_path / "linkage.db"
    conn = connect(db)
    migrate(conn)
    # Apply Kalshi schema too (only 001 is auto-applied via migrate)
    now = int(time.time())
    # Seed teams
    for canonical in ("FlyQuest", "Cloud9", "Gen.G", "T1"):
        conn.execute(
            "INSERT INTO teams (canonical_name, region, first_seen, last_seen) "
            "VALUES (?, 'LCS', '2026-01-01', '2026-05-20')",
            (canonical,),
        )
    # Seed an alias
    conn.execute(
        "INSERT INTO team_aliases (alias, canonical_name, source, created_at) "
        "VALUES ('C9', 'Cloud9', 'seed', ?)",
        (now,),
    )
    # Seed a match: FlyQuest vs Cloud9 on 2026-05-23 (matches the ticker we test)
    fly_id = conn.execute("SELECT team_id FROM teams WHERE canonical_name='FlyQuest'").fetchone()[0]
    c9_id = conn.execute("SELECT team_id FROM teams WHERE canonical_name='Cloud9'").fetchone()[0]
    # team_a_id must be the lex-smaller name to match _match_key sorting
    a, b = sorted(["FlyQuest", "Cloud9"])
    ta = conn.execute("SELECT team_id FROM teams WHERE canonical_name=?", (a,)).fetchone()[0]
    tb = conn.execute("SELECT team_id FROM teams WHERE canonical_name=?", (b,)).fetchone()[0]
    conn.execute(
        """
        INSERT INTO matches (match_key, date, league, team_a_id, team_b_id, bo_format)
        VALUES (?, '2026-05-23', 'LCS', ?, ?, 3)
        """,
        (f"2026-05-23|{a}|{b}", ta, tb),
    )
    # Seed a Kalshi event + market so we can call link_market
    conn.execute(
        """
        INSERT INTO kalshi_events
            (event_ticker, series_ticker, title, sub_title, category,
             competition, competition_scope, mutually_exclusive,
             last_updated_ts, first_seen_at, last_seen_at)
        VALUES (?, 'KXLOLGAME', 'FlyQuest vs. Cloud9: Game', 'FLY vs C9 (May 23)',
                'Sports', 'League of Legends', 'Game', 0, NULL, ?, ?)
        """,
        ("KXLOLGAME-26MAY231600FLYC9", now, now),
    )
    conn.execute(
        """
        INSERT INTO kalshi_markets
            (market_ticker, event_ticker, series_ticker, title,
             open_time, open_time_unix, close_time, close_time_unix,
             status, first_seen_at, last_seen_at)
        VALUES (?, ?, 'KXLOLGAME',
                'Will FlyQuest win the FlyQuest vs. Cloud9 League of Legends match?',
                NULL, NULL, NULL, NULL, 'open', ?, ?)
        """,
        ("KXLOLGAME-26MAY231600FLYC9-FLY", "KXLOLGAME-26MAY231600FLYC9", now, now),
    )
    conn.commit()
    yield conn
    conn.close()


def test_resolve_team_name_direct(linkage_db):
    canon, alias = resolve_team_name(linkage_db, "FlyQuest")
    assert canon == "FlyQuest"
    assert alias is False


def test_resolve_team_name_via_alias(linkage_db):
    canon, alias = resolve_team_name(linkage_db, "C9")
    assert canon == "Cloud9"
    assert alias is True


def test_resolve_team_name_unknown(linkage_db):
    canon, alias = resolve_team_name(linkage_db, "NotARealTeam")
    assert canon is None
    assert alias is False


def test_link_market_exact_match(linkage_db):
    market = linkage_db.execute(
        "SELECT * FROM kalshi_markets WHERE market_ticker = 'KXLOLGAME-26MAY231600FLYC9-FLY'"
    ).fetchone()
    result = link_market(linkage_db, market)
    assert result.match_id is not None
    assert result.confidence == 1.0
    assert result.reason == "exact_date_match"
    assert result.side in (1, 2)  # one of the two sides since "Will FlyQuest win"


def test_link_market_via_alias(linkage_db):
    # Mutate the event title to use the alias "C9"
    linkage_db.execute(
        "UPDATE kalshi_events SET title = 'FlyQuest vs. C9: Game' "
        "WHERE event_ticker = 'KXLOLGAME-26MAY231600FLYC9'"
    )
    market = linkage_db.execute(
        "SELECT * FROM kalshi_markets WHERE market_ticker = 'KXLOLGAME-26MAY231600FLYC9-FLY'"
    ).fetchone()
    result = link_market(linkage_db, market)
    assert result.match_id is not None
    assert result.confidence == 0.8
    assert result.used_alias is True


def test_link_market_no_match(linkage_db):
    # Mutate the event to a team that doesn't exist
    linkage_db.execute(
        "UPDATE kalshi_events SET title = 'Mars Esports vs. Pluto Gaming: Game' "
        "WHERE event_ticker = 'KXLOLGAME-26MAY231600FLYC9'"
    )
    market = linkage_db.execute(
        "SELECT * FROM kalshi_markets WHERE market_ticker = 'KXLOLGAME-26MAY231600FLYC9-FLY'"
    ).fetchone()
    result = link_market(linkage_db, market)
    assert result.match_id is None
    assert result.confidence == 0.0


def test_backfill_link_writes_manual_review_for_low_confidence(linkage_db):
    linkage_db.execute(
        "UPDATE kalshi_events SET title = 'Unknown vs. AlsoUnknown: Game' "
        "WHERE event_ticker = 'KXLOLGAME-26MAY231600FLYC9'"
    )
    backfill_links(linkage_db)
    review = linkage_db.execute("SELECT * FROM manual_review").fetchone()
    assert review is not None
    assert review["resolved_at"] is None
    assert review["reason"] == "team_not_in_db"


def test_backfill_link_resolves_review_when_confidence_high(linkage_db):
    # First run with bad title creates a review row
    linkage_db.execute(
        "UPDATE kalshi_events SET title = 'Unknown vs. AlsoUnknown: Game' "
        "WHERE event_ticker = 'KXLOLGAME-26MAY231600FLYC9'"
    )
    backfill_links(linkage_db)
    assert linkage_db.execute("SELECT COUNT(*) FROM manual_review WHERE resolved_at IS NULL").fetchone()[0] == 1

    # Now restore the good title and re-link
    linkage_db.execute(
        "UPDATE kalshi_events SET title = 'FlyQuest vs. Cloud9: Game' "
        "WHERE event_ticker = 'KXLOLGAME-26MAY231600FLYC9'"
    )
    backfill_links(linkage_db)
    # The review should now be resolved
    assert linkage_db.execute("SELECT COUNT(*) FROM manual_review WHERE resolved_at IS NULL").fetchone()[0] == 0
