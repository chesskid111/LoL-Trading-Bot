"""Match-to-market linkage.

Maps Kalshi market tickers (e.g. ``KXLOLGAME-26MAY231600FLYC9``) to
specific rows in our ``matches`` table.

Inputs we work with:
  - Event title:  "FlyQuest vs. Cloud9: Game" / "...: Map 1" / "...: Total Maps"
  - Event ticker: encodes the UTC date as YYMmmDDhhmm
  - Market title: for KXLOLGAME and KXLOLMAP, "Will <YES_team> win the ..."
    tells us which side YES resolves on.

Confidence rubric (per spec):
  - both team names match canonical (no alias fallback)     -> 1.0
  - one/both names matched via alias                        -> 0.8
  - date off by ±1 day but teams match                      -> 0.7
  - multiple candidate matches found                        -> 0.3
  - no match found                                          -> 0.0

Threshold for trader to act: confidence >= 0.7.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

LINK_CONFIDENCE_THRESHOLD = 0.7

# Accept titles with OR without a trailing ": Scope" suffix.
# Examples both supported:
#   "FlyQuest vs. Cloud9"
#   "FlyQuest vs. Cloud9: Map 1"
#   "G2 Esports vs Karmine Corp: Total Maps"
_TITLE_RE = re.compile(r"^(.+?)\s+vs\.?\s+(.+?)(?:\s*:.*)?$", re.IGNORECASE)
_WILL_WIN_RE = re.compile(
    r"Will\s+(.+?)\s+win\s+the\s+(.+?)\s+vs\.?\s+(.+?)\s+(?:League of Legends|LoL|map|Map)",
    re.IGNORECASE,
)
_TICKER_DATE_RE = re.compile(r"(\d{2})([A-Z]{3})(\d{2})")
# Full game-time encoding in the ticker: YYmmmDDhhmm
_TICKER_FULL_TIME_RE = re.compile(r"(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})")

_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


@dataclass(frozen=True)
class LinkResult:
    match_id: int | None
    game_id: int | None
    side: int | None       # 1=team_a, 2=team_b, None=non-side-specific (totals)
    confidence: float
    reason: str            # human-readable description for logs / review
    used_alias: bool
    parsed_team_a: str | None
    parsed_team_b: str | None
    parsed_date: date | None


# --- parsers --------------------------------------------------------------

def parse_event_title(title: str | None) -> tuple[str, str] | None:
    """From "TeamA vs. TeamB: Scope" → (TeamA, TeamB)."""
    if not title:
        return None
    m = _TITLE_RE.match(title.strip())
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def parse_market_yes_team(market_title: str | None) -> str | None:
    """From "Will TeamX win the TeamA vs. TeamB ..." → "TeamX"."""
    if not market_title:
        return None
    m = _WILL_WIN_RE.search(market_title)
    if not m:
        return None
    return m.group(1).strip()


def parse_ticker_date(event_ticker: str | None) -> date | None:
    """From "KXLOLGAME-26MAY231600FLYC9" → date(2026, 5, 23)."""
    if not event_ticker:
        return None
    # The date appears after the last dash
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None
    payload = parts[1]
    m = _TICKER_DATE_RE.match(payload)
    if not m:
        return None
    yy, mon, dd = m.group(1), m.group(2), m.group(3)
    if mon not in _MONTH_MAP:
        return None
    try:
        return date(2000 + int(yy), _MONTH_MAP[mon], int(dd))
    except ValueError:
        return None


def parse_ticker_game_time_unix(event_ticker: str | None) -> int | None:
    """Extract the actual game time (Unix seconds, UTC) from the ticker.

    Kalshi encodes ``YYMmmDDhhmm`` after the series prefix. Example:
    ``KXLOLGAME-26MAY220400DRXDNF`` → 2026-05-22 04:00 UTC.

    This is more reliable than ``kalshi_markets.close_time`` for active
    markets, where Kalshi often puts a far-future placeholder.
    """
    if not event_ticker:
        return None
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None
    payload = parts[1]
    m = _TICKER_FULL_TIME_RE.match(payload)
    if not m:
        return None
    yy, mon, dd, hh, mm = m.groups()
    if mon not in _MONTH_MAP:
        return None
    try:
        dt = datetime(2000 + int(yy), _MONTH_MAP[mon], int(dd),
                      int(hh), int(mm), tzinfo=None)
        # The encoded time is UTC
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


# --- alias resolution -----------------------------------------------------

def resolve_team_name(conn: sqlite3.Connection, raw: str) -> tuple[str | None, bool]:
    """Return (canonical_name, used_alias). used_alias=True means resolution
    went through the team_aliases table; False means direct match in teams."""
    if not raw:
        return None, False
    raw = raw.strip()
    # Direct hit in teams
    row = conn.execute(
        "SELECT canonical_name FROM teams WHERE canonical_name = ?", (raw,)
    ).fetchone()
    if row:
        return row["canonical_name"], False
    # Case-insensitive direct hit
    row = conn.execute(
        "SELECT canonical_name FROM teams WHERE LOWER(canonical_name) = LOWER(?)", (raw,)
    ).fetchone()
    if row:
        return row["canonical_name"], False
    # Try alias
    row = conn.execute(
        "SELECT canonical_name FROM team_aliases WHERE alias = ?", (raw,)
    ).fetchone()
    if row:
        return row["canonical_name"], True
    # Case-insensitive alias
    row = conn.execute(
        "SELECT canonical_name FROM team_aliases WHERE LOWER(alias) = LOWER(?)", (raw,)
    ).fetchone()
    if row:
        return row["canonical_name"], True
    return None, False


# --- linking --------------------------------------------------------------

def _find_match(
    conn: sqlite3.Connection,
    canon_a: str,
    canon_b: str,
    target_date: date,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """Return (exact_date_matches, +/-1-day_matches) for the team pair."""
    rows = conn.execute(
        """
        SELECT m.match_id, m.date,
               ta.canonical_name AS ta_name, tb.canonical_name AS tb_name
        FROM matches m
        JOIN teams ta ON m.team_a_id = ta.team_id
        JOIN teams tb ON m.team_b_id = tb.team_id
        WHERE (ta.canonical_name = ? AND tb.canonical_name = ?)
           OR (ta.canonical_name = ? AND tb.canonical_name = ?)
        """,
        (canon_a, canon_b, canon_b, canon_a),
    ).fetchall()
    exact: list[sqlite3.Row] = []
    near: list[sqlite3.Row] = []
    for r in rows:
        d = datetime.strptime(r["date"], "%Y-%m-%d").date()
        if d == target_date:
            exact.append(r)
        elif abs((d - target_date).days) == 1:
            near.append(r)
    return exact, near


def _create_placeholder_match(
    conn: sqlite3.Connection,
    canon_a: str, canon_b: str, target_date: date,
) -> int | None:
    """Insert a placeholder match row for an upcoming game whose teams
    we know but whose result hasn't been recorded by Oracle yet.

    Used for INFERENCE on future games: the model needs ``match_id`` to
    look up team Glicko + form features. ``series_winner_id`` is left
    NULL until Oracle updates and we can fill in the outcome.

    Returns the new match_id, or None if either team isn't in the
    teams table.
    """
    # Look up team IDs
    a_row = conn.execute(
        "SELECT team_id FROM teams WHERE canonical_name = ?", (canon_a,)
    ).fetchone()
    b_row = conn.execute(
        "SELECT team_id FROM teams WHERE canonical_name = ?", (canon_b,)
    ).fetchone()
    if not a_row or not b_row:
        return None
    a_id, b_id = a_row["team_id"], b_row["team_id"]
    # Lex-sort for the match_key
    if canon_a <= canon_b:
        team_a_id, team_b_id = a_id, b_id
        match_key = f"{target_date.isoformat()}|{canon_a}|{canon_b}"
    else:
        team_a_id, team_b_id = b_id, a_id
        match_key = f"{target_date.isoformat()}|{canon_b}|{canon_a}"

    # Idempotent: if it already exists, return its id
    existing = conn.execute(
        "SELECT match_id FROM matches WHERE match_key = ?", (match_key,)
    ).fetchone()
    if existing:
        return existing["match_id"]
    conn.execute(
        """
        INSERT INTO matches
            (match_key, date, league, team_a_id, team_b_id, bo_format)
        VALUES (?, ?, NULL, ?, ?, 1)
        """,
        (match_key, target_date.isoformat(), team_a_id, team_b_id),
    )
    new_id = conn.execute(
        "SELECT match_id FROM matches WHERE match_key = ?", (match_key,)
    ).fetchone()["match_id"]
    return new_id


def _resolve_side(
    conn: sqlite3.Connection,
    match_row: sqlite3.Row,
    yes_team_canonical: str | None,
) -> int | None:
    if yes_team_canonical is None:
        return None
    if match_row["ta_name"] == yes_team_canonical:
        return 1
    if match_row["tb_name"] == yes_team_canonical:
        return 2
    return None


def link_market(conn: sqlite3.Connection, market_row: sqlite3.Row) -> LinkResult:
    """Compute the best link for a single Kalshi market row."""
    event_ticker = market_row["event_ticker"]
    series_ticker = market_row["series_ticker"]
    market_title = market_row["title"] or ""

    # Get parent event's title (has the canonical "A vs. B: Scope" form)
    ev_row = conn.execute(
        "SELECT title FROM kalshi_events WHERE event_ticker = ?", (event_ticker,)
    ).fetchone()
    event_title = ev_row["title"] if ev_row else None

    teams = parse_event_title(event_title)
    if teams is None:
        # Fall back: try parsing from market title
        teams = parse_event_title(market_title)
    yes_team_raw = parse_market_yes_team(market_title)
    target_date = parse_ticker_date(event_ticker)

    if teams is None or target_date is None:
        return LinkResult(None, None, None, 0.0, "could_not_parse",
                          False, None, None, target_date)

    raw_a, raw_b = teams
    canon_a, alias_a = resolve_team_name(conn, raw_a)
    canon_b, alias_b = resolve_team_name(conn, raw_b)
    used_alias = alias_a or alias_b

    if canon_a is None or canon_b is None:
        return LinkResult(None, None, None, 0.0, "team_not_in_db",
                          used_alias, raw_a, raw_b, target_date)

    exact, near = _find_match(conn, canon_a, canon_b, target_date)

    if len(exact) == 1:
        match_row = exact[0]
        confidence = 1.0 if not used_alias else 0.8
        # game_id only set for individual map markets; deferred for now
        side = None
        if series_ticker in ("KXLOLGAME", "KXLOLMAP"):
            yes_canon = resolve_team_name(conn, yes_team_raw)[0] if yes_team_raw else None
            side = _resolve_side(conn, match_row, yes_canon)
        return LinkResult(
            match_id=match_row["match_id"],
            game_id=None,
            side=side,
            confidence=confidence,
            reason="exact_date_match" if not used_alias else "exact_date_match_via_alias",
            used_alias=used_alias,
            parsed_team_a=canon_a,
            parsed_team_b=canon_b,
            parsed_date=target_date,
        )

    if len(exact) > 1:
        return LinkResult(
            match_id=None, game_id=None, side=None, confidence=0.3,
            reason="multiple_exact_candidates",
            used_alias=used_alias,
            parsed_team_a=canon_a, parsed_team_b=canon_b, parsed_date=target_date,
        )

    if len(near) == 1:
        match_row = near[0]
        side = None
        if series_ticker in ("KXLOLGAME", "KXLOLMAP"):
            yes_canon = resolve_team_name(conn, yes_team_raw)[0] if yes_team_raw else None
            side = _resolve_side(conn, match_row, yes_canon)
        return LinkResult(
            match_id=match_row["match_id"],
            game_id=None,
            side=side,
            confidence=0.7,
            reason="near_date_match",
            used_alias=used_alias,
            parsed_team_a=canon_a, parsed_team_b=canon_b, parsed_date=target_date,
        )

    # No match in Oracle's data yet. For future games where the result
    # hasn't been recorded, create a placeholder match so the bot can
    # still compute features for inference. We tag this with confidence
    # 0.75 (just above the 0.7 trader gate) but mark it so the backtest
    # can exclude it.
    today = date.today()
    if target_date >= today:
        placeholder_id = _create_placeholder_match(conn, canon_a, canon_b, target_date)
        if placeholder_id is not None:
            # Look up the placeholder's team_a/team_b for side resolution
            ph = conn.execute(
                """
                SELECT m.match_id, ta.canonical_name AS ta_name, tb.canonical_name AS tb_name
                FROM matches m
                JOIN teams ta ON m.team_a_id = ta.team_id
                JOIN teams tb ON m.team_b_id = tb.team_id
                WHERE m.match_id = ?
                """,
                (placeholder_id,),
            ).fetchone()
            side = None
            if ph and yes_team_raw and series_ticker in ("KXLOLGAME", "KXLOLMAP"):
                yes_canon = resolve_team_name(conn, yes_team_raw)[0] if yes_team_raw else None
                side = _resolve_side(conn, ph, yes_canon)
            return LinkResult(
                match_id=placeholder_id, game_id=None, side=side,
                confidence=0.75 if not used_alias else 0.7,
                reason="placeholder_future_match" if not used_alias else "placeholder_via_alias",
                used_alias=used_alias,
                parsed_team_a=canon_a, parsed_team_b=canon_b, parsed_date=target_date,
            )

    return LinkResult(
        match_id=None, game_id=None, side=None, confidence=0.0,
        reason="no_match",
        used_alias=used_alias,
        parsed_team_a=canon_a, parsed_team_b=canon_b, parsed_date=target_date,
    )


def write_link(conn: sqlite3.Connection, market_ticker: str, result: LinkResult) -> None:
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO market_match_links
            (market_ticker, match_id, game_id, side, confidence, manual_override,
             notes, linked_at)
        VALUES (?, ?, ?, ?, ?, 0, ?, ?)
        ON CONFLICT(market_ticker) DO UPDATE SET
            match_id=excluded.match_id,
            game_id=excluded.game_id,
            side=excluded.side,
            confidence=excluded.confidence,
            notes=excluded.notes,
            linked_at=excluded.linked_at
        WHERE manual_override = 0
        """,
        (market_ticker, result.match_id, result.game_id, result.side,
         result.confidence, result.reason, now),
    )
    if result.confidence < LINK_CONFIDENCE_THRESHOLD:
        conn.execute(
            """
            INSERT INTO manual_review
                (market_ticker, reason, parsed_team_a, parsed_team_b, parsed_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_ticker) DO UPDATE SET
                reason=excluded.reason,
                parsed_team_a=excluded.parsed_team_a,
                parsed_team_b=excluded.parsed_team_b,
                parsed_date=excluded.parsed_date
            """,
            (market_ticker, result.reason, result.parsed_team_a,
             result.parsed_team_b,
             result.parsed_date.isoformat() if result.parsed_date else None,
             now),
        )
    else:
        # Resolve any existing review entry for this market
        conn.execute(
            "UPDATE manual_review SET resolved_at = ? WHERE market_ticker = ? AND resolved_at IS NULL",
            (now, market_ticker),
        )


def backfill_links(conn: sqlite3.Connection) -> dict[str, int]:
    """Run linkage on every Kalshi market in the DB. Returns counts by outcome."""
    counts = {
        "total": 0, "linked": 0, "auto_review": 0,
        "exact": 0, "near": 0, "via_alias": 0, "no_match": 0,
    }
    markets = conn.execute(
        "SELECT market_ticker, event_ticker, series_ticker, title FROM kalshi_markets"
    ).fetchall()
    for m in markets:
        counts["total"] += 1
        result = link_market(conn, m)
        write_link(conn, m["market_ticker"], result)
        if result.confidence >= LINK_CONFIDENCE_THRESHOLD:
            counts["linked"] += 1
        else:
            counts["auto_review"] += 1
        if result.reason == "exact_date_match":
            counts["exact"] += 1
        elif result.reason == "exact_date_match_via_alias":
            counts["via_alias"] += 1
        elif result.reason == "near_date_match":
            counts["near"] += 1
        elif result.reason == "no_match":
            counts["no_match"] += 1
    conn.commit()
    log.info("backfill_links: %s", counts)
    return counts
