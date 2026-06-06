"""Compute pro champion stats for a patch (or recent patches) from Oracle's
Elixir data already in SQLite.

Usage:
    python -m loltrader.tools.refresh_patch_stats --patch 16.1
    python -m loltrader.tools.refresh_patch_stats --patch 16.1 --league LCK
    python -m loltrader.tools.refresh_patch_stats --patches 16.1,16.08,16.04

Writes:
    data/patch_stats.json
    data/champion_profiles.json (pro_stats subblock merged if it exists)

Spec §1.2.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from loltrader.comp import pro_stats as ps
from loltrader.comp.profiles import load_profiles, save_profiles
from loltrader.db import connect


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--patch", help="Single patch version, e.g. 16.1")
    grp.add_argument("--patches", help="Comma-separated list of patches to merge")
    p.add_argument("--league", default=None, help="Filter by league (e.g. LCK)")
    p.add_argument("--out-stats", default="data/patch_stats.json")
    p.add_argument("--profiles", default="data/champion_profiles.json")
    p.add_argument("--no-merge", action="store_true",
                   help="Compute stats but skip merging into champion_profiles.json")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    log = logging.getLogger(__name__)

    with connect() as conn:
        if args.patches:
            patches = [s.strip() for s in args.patches.split(",") if s.strip()]
            stats = ps.compute_recent_stats(conn, patches, league=args.league)
            patch_label = ",".join(patches)
        else:
            stats = ps.compute_patch_stats(conn, args.patch, league=args.league)
            patch_label = args.patch

    if not stats:
        log.error("No stats computed — check patch version + league filter")
        return 1

    Path(args.out_stats).parent.mkdir(parents=True, exist_ok=True)
    ps.save_patch_stats(stats, args.out_stats)
    log.info("Wrote %d champion entries to %s", len(stats), args.out_stats)
    log.info("Top 5 by priority: %s",
             ", ".join(f"{s.champion}({s.priority_score:.1f})" for s in stats[:5]))

    if not args.no_merge:
        profiles = load_profiles(args.profiles)
        if profiles:
            n = ps.merge_into_profiles(profiles, stats, patch_label, league=args.league)
            save_profiles(profiles, args.profiles)
            log.info("Merged pro_stats into %d profiles in %s", n, args.profiles)
        else:
            log.info("No existing profiles; skipping merge (Phase 1.3 will create them)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
