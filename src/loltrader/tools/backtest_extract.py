"""Bulk-extract historical LCK livestats into live_frames for backtest training.

Usage:
    python -m loltrader.tools.backtest_extract --max-matches 15

Idempotent: skips games already in games_live with source='historical_backtest'.
Run unattended; takes ~4 min per game (~10 min per typical bo3 match).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from loltrader.db import connect
from loltrader.livestats.historical import (
    LCK_LEAGUE_ID,
    LCS_LEAGUE_ID,
    LEC_LEAGUE_ID,
    LPL_LEAGUE_ID,
    fetch_game_details,
    fetch_match_frames,
    get_completed_matches,
    store_historical_match,
)
from loltrader.livestats.storage import write_frame_details

LEAGUES = {
    "lck": LCK_LEAGUE_ID,
    "lcs": LCS_LEAGUE_ID,
    "lec": LEC_LEAGUE_ID,
    "lpl": LPL_LEAGUE_ID,
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--league", choices=list(LEAGUES.keys()), default="lck")
    p.add_argument("--max-matches", type=int, default=15,
                   help="Max series (matches) to extract. Default 15 = ~30 games.")
    p.add_argument("--skip-existing", action="store_true", default=True,
                   help="Skip matches whose games are already in games_live (default).")
    p.add_argument("--with-details", action="store_true",
                   help="Also pull the /details endpoint per game (per-player items, "
                        "runes, stats). Adds ~30-40%% to runtime but enables item-progression "
                        "features in the win-prob model.")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger(__name__)

    matches = get_completed_matches(LEAGUES[args.league], args.league)
    log.info("Found %d completed %s matches", len(matches), args.league.upper())

    conn = connect()
    extracted = 0
    skipped = 0
    for m in matches:
        if extracted >= args.max_matches:
            break

        # Skip if already extracted (game 1 + game 2 already in DB)
        if args.skip_existing:
            existing = conn.execute(
                "SELECT COUNT(*) c FROM games_live WHERE esports_match_id = ? AND source = 'historical_backtest'",
                (m.match_id,),
            ).fetchone()["c"]
            played_games = sum(1 for s in m.game_states if s == "completed")
            if existing >= played_games:
                log.info("[%d/%d] SKIP %s vs %s (already extracted, %d games)",
                         extracted + skipped + 1, args.max_matches,
                         m.team_a_name, m.team_b_name, existing)
                skipped += 1
                continue

        log.info("[%d/%d] EXTRACT %s vs %s (%s)",
                 extracted + 1, args.max_matches,
                 m.team_a_name, m.team_b_name, m.scheduled_start_utc)
        t0 = time.time()
        try:
            frames_by_game = fetch_match_frames(m)
            stats = store_historical_match(conn, m, frames_by_game)
            elapsed = time.time() - t0
            log.info("  → stored %d games (window), frames: %s, %.0fs",
                     len(stats), stats, elapsed)

            # Optionally pull and store the /details endpoint for each game.
            # Details are time-aligned with window frames, so we walk only
            # the known time range (set by the window pass).
            if args.with_details:
                t1 = time.time()
                details_stats: dict[str, int] = {}
                for gid, frames in frames_by_game.items():
                    if not frames:
                        continue
                    try:
                        details = fetch_game_details(gid, frames)
                    except Exception as e:
                        log.warning("  details fetch failed for %s: %s", gid, e)
                        continue
                    inserted = 0
                    for d in details:
                        try:
                            inserted += write_frame_details(conn, gid, d)
                        except Exception as e:
                            log.debug("write_frame_details %s: %s", gid, e)
                    details_stats[gid] = inserted
                d_elapsed = time.time() - t1
                log.info("  → stored details: %s, %.0fs",
                         details_stats, d_elapsed)

            extracted += 1
        except Exception as e:
            log.error("  FAILED: %s", e)

    conn.close()
    log.info("DONE. extracted=%d, skipped=%d", extracted, skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
