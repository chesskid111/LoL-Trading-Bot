"""Tests for kalshi.corpus helpers and UPSERT idempotency (with a fake client)."""
from __future__ import annotations

from pathlib import Path

from loltrader.db import connect, migrate
from loltrader.kalshi.corpus import (
    dollars_str_to_cents,
    is_lol_event,
    iso_to_unix,
    snapshot_events,
)


# --- pure-function tests --------------------------------------------------

def test_dollars_str_to_cents() -> None:
    assert dollars_str_to_cents("0.4500") == 45
    assert dollars_str_to_cents("0.99") == 99
    assert dollars_str_to_cents("1.0000") == 100
    assert dollars_str_to_cents("0.0000") == 0
    assert dollars_str_to_cents("0.3333") == 33  # rounding
    assert dollars_str_to_cents(None) is None
    assert dollars_str_to_cents("") is None


def test_iso_to_unix() -> None:
    # 2026-05-19T16:00:00Z
    assert iso_to_unix("2026-05-19T16:00:00Z") == 1779206400
    # ISO 8601 with no Z still works
    assert iso_to_unix("2026-05-19T16:00:00+00:00") == 1779206400
    assert iso_to_unix(None) is None
    assert iso_to_unix("") is None


def test_is_lol_event_positive() -> None:
    ev = {
        "event_ticker": "KXLOLGAME-26MAY231600FLYC9",
        "product_metadata": {"competition": "League of Legends"},
    }
    assert is_lol_event(ev)


def test_is_lol_event_negative() -> None:
    cases = [
        {"product_metadata": {"competition": "La Liga"}},
        {"product_metadata": {}},
        {},
        {"product_metadata": None},
    ]
    for ev in cases:
        assert not is_lol_event(ev), ev


# --- UPSERT idempotency with a fake Kalshi client -------------------------

class _FakeClient:
    """Returns a fixed events response, no cursor pagination."""

    def __init__(self, events: list[dict]) -> None:
        self._events = events
        self.calls = 0

    def list_events(self, **params):
        self.calls += 1
        # Only return events that match the requested status to mimic Kalshi.
        # Tests below don't care about status, so we just return all.
        return {"events": self._events, "cursor": None}


def test_snapshot_events_upsert_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    conn = connect(db)
    migrate(conn)

    sample = [
        {
            "event_ticker": "KXLOLGAME-26MAY231600FLYC9",
            "series_ticker": "KXLOLGAME",
            "title": "FlyQuest vs. Cloud9",
            "sub_title": "FLY vs C9 (May 23)",
            "category": "Sports",
            "product_metadata": {
                "competition": "League of Legends",
                "competition_scope": "Game",
            },
            "mutually_exclusive": True,
            "last_updated_ts": "2026-05-19T16:00:00Z",
        }
    ]
    fake = _FakeClient(sample)

    n1 = snapshot_events(fake, conn)
    n2 = snapshot_events(fake, conn)

    # Both runs upsert once per (series, status) combo. There are
    # len(LOL_SERIES) * 2 such combos; each returns 1 event.
    from loltrader.kalshi.corpus import LOL_SERIES
    expected_per_run = len(LOL_SERIES) * 2
    assert n1 == expected_per_run
    assert n2 == expected_per_run

    # But the row count in DB doesn't grow on second run.
    row_count = conn.execute("SELECT COUNT(*) AS n FROM kalshi_events").fetchone()["n"]
    assert row_count == 1  # only one distinct event_ticker
    conn.close()
