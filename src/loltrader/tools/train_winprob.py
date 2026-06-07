"""Train the win-probability XGBoost ensemble.

Usage:
    python -m loltrader.tools.train_winprob
    python -m loltrader.tools.train_winprob --dataset data/winprob_training.parquet \
        --out models/winprob_$(date +%Y%m%d).pkl

Spec §Phase 4.2-4.3.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from loltrader.winprob.train import train_full


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", default="data/winprob_training.parquet",
                   help="Parquet file from build_winprob_dataset")
    p.add_argument("--out", default=None,
                   help="Output model path (default: models/winprob_<timestamp>.pkl)")
    p.add_argument("--ensemble-size", type=int, default=10)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    log = logging.getLogger(__name__)

    out = args.out or f"models/winprob_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.pkl"

    try:
        model, metrics = train_full(
            args.dataset, out,
            ensemble_size=args.ensemble_size,
        )
    except FileNotFoundError as e:
        log.error("Dataset not found: %s", e)
        return 1
    except Exception:
        log.exception("Training failed")
        return 1

    log.info("---- summary ----")
    log.info("train=%d val=%d holdout=%d",
             metrics.n_train, metrics.n_val, metrics.n_holdout)
    log.info("Brier=%.4f AUC=%.4f Accuracy=%.4f",
             metrics.brier, metrics.auc, metrics.accuracy)
    # Symlink latest for serving
    from pathlib import Path
    latest = Path("models/winprob_latest.pkl")
    latest.parent.mkdir(parents=True, exist_ok=True)
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    # Use a regular copy on Windows where symlinks need admin
    try:
        latest.symlink_to(Path(out).resolve())
    except OSError:
        import shutil
        shutil.copy2(out, latest)
    log.info("latest -> %s", out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
