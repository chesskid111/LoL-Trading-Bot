"""Cross-reference our champion_profiles.json against gol.gg empirical winrate
data. Surfaces champions where the profile likely needs an override.

This REPLACES the planned statistical audit script — gol.gg's data is
qualitatively richer than what we'd compute from our local DB.

Usage:
    # After extracting gol.gg champion stats per role
    python -m loltrader.tools.audit_profiles_vs_gol_gg

    # Custom paths
    python -m loltrader.tools.audit_profiles_vs_gol_gg \\
        --input data/external/gol_gg/champion_stats/ \\
        --profiles data/champion_profiles.json \\
        --output data/processed/profile_validation.json

Output: prioritized list of flags for human review + override.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from loltrader.external.gol_gg_champions import (
    parse_all_tsvs,
    audit_profiles,
    save_profile_validation,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", default="data/external/gol_gg/champion_stats/",
                   help="Directory containing per-role TSV files from gol.gg")
    p.add_argument("--profiles", default="data/champion_profiles.json")
    p.add_argument("--output", default="data/processed/profile_validation.json")
    p.add_argument("--top-n", type=int, default=20,
                   help="Show top N flags in console (default 20)")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    log = logging.getLogger(__name__)

    input_dir = Path(args.input)
    if not input_dir.exists():
        log.error("input directory does not exist: %s", input_dir)
        return 1

    tsvs = list(input_dir.glob("*.tsv"))
    if not tsvs:
        log.error("no TSV files found in %s", input_dir)
        log.error("expected files like: top_s16.tsv, jungle_s16.tsv, etc.")
        return 1

    log.info("found %d TSV files", len(tsvs))

    stats_by_role = parse_all_tsvs(input_dir)
    total = sum(len(c) for c in stats_by_role.values())
    log.info("parsed %d champion stats across %d roles", total, len(stats_by_role))

    flags = audit_profiles(stats_by_role, Path(args.profiles))

    log.info("=== Audit results ===")
    log.info("total flags: %d", len(flags))
    if not flags:
        log.info("no flags — profiles agree with gol.gg data (good!)")
        return 0

    # Console preview of top flags
    log.info("\n--- Top %d flags for review ---", min(args.top_n, len(flags)))
    for i, f in enumerate(flags[:args.top_n], 1):
        sev = f["severity"]
        log.info(f"\n[{i}] {sev.upper():<8} {f['champion']:<14} ({f['role']})")
        log.info(f"    issue:      {f['issue']}")
        log.info(f"    suggested:  {f['suggested']}")
        log.info(f"    sample N:   {f['n_games']}")
        if "profile_confidence" in f:
            log.info(f"    confidence: {f['profile_confidence']:.2f}")

    save_profile_validation(flags, Path(args.output))

    log.info("\nReview the full report at %s", args.output)
    log.info("Apply overrides manually (see Olaf/XinZhao examples in champion_profiles.json)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
