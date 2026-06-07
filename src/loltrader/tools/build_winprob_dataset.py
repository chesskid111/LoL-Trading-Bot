"""Build the win-prob training dataset from historical_backtest games.

Usage:
    python -m loltrader.tools.build_winprob_dataset
    python -m loltrader.tools.build_winprob_dataset --cadence 30 --out data/winprob_30s.parquet

Reads all historical_backtest games from DB, produces ~95-feature training rows
sampled at the given cadence. Default cadence is 10 seconds (every available
frame, per Phase 4.1 spec).

Spec §Phase 4.1.
"""
from __future__ import annotations

import argparse
import logging
import sys

from loltrader.db import connect
from loltrader.winprob.dataset import (
    DEFAULT_CADENCE_SEC,
    build_training_dataset,
    write_dataset_parquet,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cadence", type=int, default=DEFAULT_CADENCE_SEC,
                   help=f"Sample frames at this cadence in seconds (default {DEFAULT_CADENCE_SEC}=every frame)")
    p.add_argument("--out", default="data/winprob_training.parquet")
    p.add_argument("--profiles", default="data/champion_profiles.json")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    log = logging.getLogger(__name__)

    with connect() as conn:
        rows = build_training_dataset(
            conn, cadence_sec=args.cadence, profiles_path=args.profiles,
        )

    if not rows:
        log.error("No training rows produced. Check that historical_backtest games "
                   "exist in games_live with linkable Oracle's Elixir picks.")
        return 1

    write_dataset_parquet(rows, args.out)

    # Quick summary
    n_per_league: dict[str, int] = {}
    n_per_label: dict[int, int] = {}
    for r in rows:
        n_per_league[r.league] = n_per_league.get(r.league, 0) + 1
        n_per_label[r.label] = n_per_label.get(r.label, 0) + 1
    log.info("Dataset summary:")
    log.info("  rows per league: %s", n_per_league)
    log.info("  rows per label:  %s", n_per_label)
    log.info("  feature count:   %d", len(rows[0].features))

    return 0


if __name__ == "__main__":
    sys.exit(main())
