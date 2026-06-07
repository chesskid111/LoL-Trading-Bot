"""Backfill /details endpoint for games that already have live_frames but no
live_frames_details rows.

When ``backtest_extract`` was run before the --with-details flag existed, it
only stored window frames. This tool patches that gap by re-fetching details
for those games.

Usage:
    python -m loltrader.tools.backfill_details
    python -m loltrader.tools.backfill_details --max-games 50

Spec: Phase 4 prep.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from loltrader.db import connect
from loltrader.livestats.historical import fetch_game_details
from loltrader.livestats.storage import write_frame_details


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--max-games", type=int, default=200,
                   help="Maximum games to process this run")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger(__name__)

    conn = connect()

    # Find historical games with frames but no details
    rows = conn.execute(
        """
        SELECT g.game_id
        FROM games_live g
        WHERE g.source = 'historical_backtest'
          AND EXISTS (
              SELECT 1 FROM live_frames f WHERE f.game_id = g.game_id LIMIT 1
          )
          AND NOT EXISTS (
              SELECT 1 FROM live_frames_details d WHERE d.game_id = g.game_id LIMIT 1
          )
        ORDER BY g.first_seen_ts_unix DESC
        LIMIT ?
        """,
        (args.max_games,),
    ).fetchall()
    game_ids = [r["game_id"] for r in rows]
    log.info("Found %d historical games needing details backfill", len(game_ids))

    completed = 0
    failed = 0

    for i, gid in enumerate(game_ids, start=1):
        # Read existing window frames to determine time range
        frames = conn.execute(
            """
            SELECT raw_json FROM live_frames
            WHERE game_id = ?
            ORDER BY frame_ts_unix ASC
            """,
            (gid,),
        ).fetchall()
        if not frames:
            continue

        # The raw_json column contains the full frame dict. We need
        # rfc460Timestamp for the details walker — which is inside raw_json.
        import json as _json
        window_frames = []
        for r in frames:
            try:
                window_frames.append(_json.loads(r["raw_json"]))
            except (TypeError, ValueError):
                continue
        if not window_frames:
            log.warning("[%d/%d] %s no parseable raw_json frames, skipping",
                        i, len(game_ids), gid)
            continue

        log.info("[%d/%d] %s — %d window frames, fetching details",
                 i, len(game_ids), gid, len(window_frames))
        t0 = time.time()
        try:
            details = fetch_game_details(gid, window_frames)
            inserted = 0
            for d in details:
                inserted += write_frame_details(conn, gid, d)
            elapsed = time.time() - t0
            log.info("  → stored %d detail frames in %.0fs", inserted, elapsed)
            completed += 1
        except Exception as e:
            log.error("  FAILED: %s", e)
            failed += 1

    log.info("DONE. completed=%d failed=%d", completed, failed)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
