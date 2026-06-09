"""Cleanse the raw training parquet before model training.

Supports both strict and moderate coverage modes — use --coverage strict
and --coverage moderate to A/B test which produces a better-trained model.

Usage:
    # Default: moderate coverage (any frame in 10-25 range)
    python -m loltrader.tools.cleanse_training_dataset

    # Strict: require frames in 10-15, 15-20, AND 20-25
    python -m loltrader.tools.cleanse_training_dataset --coverage strict

    # Custom paths
    python -m loltrader.tools.cleanse_training_dataset \\
        --input data/winprob_training.parquet \\
        --output data/winprob_training_clean.parquet \\
        --report data/cleansing_report.md

Spec: Phase 4 — pre-training cleansing.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from loltrader.winprob.cleanse import (
    cleanse_dataframe,
    write_cleansing_report,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", default="data/winprob_training.parquet")
    p.add_argument("--output", default=None,
                   help="Default: data/winprob_training_clean_<mode>.parquet")
    p.add_argument("--report", default=None,
                   help="Default: data/cleansing_report_<mode>.md")
    p.add_argument("--profiles", default="data/champion_profiles.json")
    p.add_argument("--coverage", choices=["strict", "moderate"], default="moderate",
                   help="Minute-coverage filter strictness")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    log = logging.getLogger(__name__)

    input_path = Path(args.input)
    if not input_path.exists():
        log.error("input parquet not found: %s", input_path)
        log.error("run `python -m loltrader.tools.build_winprob_dataset` first")
        return 1

    output_path = Path(args.output or
                       f"data/winprob_training_clean_{args.coverage}.parquet")
    report_path = Path(args.report or
                       f"data/cleansing_report_{args.coverage}.md")

    try:
        import pandas as pd
    except ImportError:
        log.error("pandas required: pip install pandas pyarrow")
        return 1

    log.info("loading %s", input_path)
    df = pd.read_parquet(input_path)
    log.info("loaded %d rows, %d unique games", len(df), df["game_id"].nunique())

    log.info("applying cleansing with coverage=%s", args.coverage)
    cleaned, stats = cleanse_dataframe(df, args.profiles,
                                        coverage_mode=args.coverage)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned.to_parquet(output_path, index=False)
    log.info("wrote %d cleaned rows to %s", len(cleaned), output_path)

    write_cleansing_report(stats, report_path, args.coverage,
                            input_path, output_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
