"""Train the v1 model and save a versioned artifact.

Usage: python -m loltrader.tools.train_model
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime

from loltrader.config import load_config
from loltrader.db import connect, migrate
from loltrader.features.team_strength import rebuild_team_glicko
from loltrader.model.train import save_artifact, train


def _setup_logging() -> logging.Logger:
    cfg = load_config()
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    logfile = cfg.logs_dir / "train.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(logfile, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("train_model")


def main() -> int:
    log = _setup_logging()
    cfg = load_config()
    start = time.time()
    log.info("Starting v1 model training")
    try:
        conn = connect()
        migrate(conn)

        # Always rebuild Glicko first so the training data has fresh ratings
        n = rebuild_team_glicko(conn)
        log.info("Rebuilt %d Glicko snapshots", n)

        artifact = train(conn)

        # Save versioned + symlink "latest"
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        cfg.models_dir.mkdir(parents=True, exist_ok=True)
        path = cfg.models_dir / f"v1_{ts}.pkl"
        save_artifact(artifact, path)
        log.info("Saved artifact: %s", path)

        # Also write a "latest" copy for convenience
        latest_path = cfg.models_dir / "v1_latest.pkl"
        save_artifact(artifact, latest_path)
        log.info("Updated v1_latest.pkl")

        # Pretty metadata dump
        meta_dump = {
            k: v for k, v in artifact.metadata.items()
            if k != "feature_cols"  # too long
        }
        (cfg.models_dir / f"v1_{ts}_metadata.json").write_text(
            json.dumps(meta_dump, indent=2, default=str)
        )

        elapsed = time.time() - start
        log.info("Training complete in %.1fs", elapsed)
        conn.close()
        return 0
    except Exception:
        log.exception("Training failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
