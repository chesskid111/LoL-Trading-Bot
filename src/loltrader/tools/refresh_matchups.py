"""Refresh per-role lane matchup data from local Oracle's Elixir data.

Usage:
    python -m loltrader.tools.refresh_matchups --patches 16.1,16.08,16.04,16.01
    python -m loltrader.tools.refresh_matchups --patches 16.1 --league LCK

Writes:
    data/lane_matchups.json

Spec §Phase 2.2.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from loltrader.comp.matchup_data import compute_matchups, save_matchups
from loltrader.db import connect


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--patches", required=True,
                   help="Comma-separated patch versions")
    p.add_argument("--league", default=None,
                   help="Filter by league (e.g. LCK)")
    p.add_argument("--out", default="data/lane_matchups.json")
    p.add_argument("--min-games", type=int, default=1,
                   help="Minimum games for a matchup to be persisted")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    log = logging.getLogger(__name__)

    patches = [s.strip() for s in args.patches.split(",") if s.strip()]
    with connect() as conn:
        matchups = compute_matchups(conn, patches, league=args.league,
                                     min_games=args.min_games)

    if not matchups:
        log.error("No matchups computed for patches=%s league=%s", patches, args.league)
        return 1

    save_matchups(matchups, args.out)
    log.info("Wrote %d matchup entries to %s", len(matchups), args.out)

    # Sample report: top 5 most-played matchups
    matchups.sort(key=lambda m: m.games, reverse=True)
    log.info("Most-played matchups:")
    for m in matchups[:5]:
        log.info("  %s: %s vs %s — %dg, %.0f%% wr_a (shrunk %.2f)",
                 m.role, m.champion_a, m.champion_b,
                 m.games, m.raw_winrate_a * 100, m.shrunk_winrate_a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
