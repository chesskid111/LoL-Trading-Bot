"""Manual review CLI for unlinked / low-confidence Kalshi markets.

Usage:
    python -m loltrader.tools.review_links

Loops through markets in the manual_review queue and prompts you to enter
the canonical Oracle's Elixir team name for any unresolved team alias.
Writes new team_aliases rows on the fly and re-runs linkage.

Commands at any prompt:
    skip / s    skip this market
    quit / q    save and exit
    auto / a    auto-skip remaining if same parse pattern
"""
from __future__ import annotations

import sys
import time

from loltrader.db import connect
from loltrader.kalshi.linkage import LINK_CONFIDENCE_THRESHOLD, backfill_links


def _prompt(question: str) -> str:
    sys.stdout.write(question)
    sys.stdout.flush()
    return sys.stdin.readline().strip()


def main() -> int:
    conn = connect()

    while True:
        rows = conn.execute(
            """
            SELECT mr.market_ticker, mr.reason, mr.parsed_team_a, mr.parsed_team_b,
                   mr.parsed_date, m.title AS market_title, e.title AS event_title
            FROM manual_review mr
            JOIN kalshi_markets m ON m.market_ticker = mr.market_ticker
            JOIN kalshi_events  e ON e.event_ticker = m.event_ticker
            WHERE mr.resolved_at IS NULL
            ORDER BY mr.parsed_date DESC NULLS LAST
            LIMIT 50
            """
        ).fetchall()

        if not rows:
            print("No unresolved markets in review queue. Done.")
            return 0

        print(f"\n=== {len(rows)} markets need review (showing 50) ===\n")

        seen_aliases_this_session = False
        for r in rows:
            print("-" * 70)
            print(f"Market:  {r['market_ticker']}")
            print(f"Event:   {r['event_title']}")
            print(f"Title:   {r['market_title']}")
            print(f"Parsed:  {r['parsed_team_a']} vs {r['parsed_team_b']} on {r['parsed_date']}")
            print(f"Reason:  {r['reason']}")

            for raw_name in (r["parsed_team_a"], r["parsed_team_b"]):
                if not raw_name:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM teams WHERE LOWER(canonical_name) = LOWER(?) "
                    "UNION SELECT 1 FROM team_aliases WHERE LOWER(alias) = LOWER(?)",
                    (raw_name, raw_name),
                ).fetchone()
                if exists:
                    continue
                ans = _prompt(
                    f"  '{raw_name}' not in teams/aliases. Canonical name "
                    f"(or 's' to skip, 'q' to quit): "
                )
                if ans.lower() in ("q", "quit"):
                    print("Saving and exiting.")
                    conn.commit()
                    return 0
                if ans.lower() in ("s", "skip", ""):
                    continue
                # Check the proposed canonical exists in teams
                canon_row = conn.execute(
                    "SELECT canonical_name FROM teams WHERE LOWER(canonical_name) = LOWER(?)",
                    (ans,),
                ).fetchone()
                if canon_row is None:
                    print(f"  WARN: '{ans}' is not a known team. Adding alias anyway.")
                    canonical_name = ans
                else:
                    canonical_name = canon_row["canonical_name"]
                conn.execute(
                    """
                    INSERT OR IGNORE INTO team_aliases (alias, canonical_name, source, created_at)
                    VALUES (?, ?, 'manual', ?)
                    """,
                    (raw_name, canonical_name, int(time.time())),
                )
                seen_aliases_this_session = True
                print(f"  Saved alias: {raw_name} -> {canonical_name}")

        conn.commit()

        if not seen_aliases_this_session:
            print("\nNo new aliases added. Exiting.")
            return 0

        print("\nRe-running linkage backfill with new aliases...")
        counts = backfill_links(conn)
        print(f"  {counts}")
        # Loop again — newly-linked markets shouldn't appear; remaining unresolved continue


if __name__ == "__main__":
    sys.exit(main())
