"""Run the v1 paper-trading bot.

Usage:
    python -m loltrader.tools.run_bot [--bankroll 200000] [--poll 30]
                                       [--iterations 0] [--model PATH]
                                       [--threshold 0.03] [--kelly 0.25]

The UI is a separate process; launch it with:
    python -m streamlit run src/loltrader/ui/app.py
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from loltrader.config import load_config
from loltrader.trader.loop import TraderConfig, run_trader


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=None,
                        help="Path to model artifact (default: models/v1_latest.pkl)")
    parser.add_argument("--bankroll", type=int, default=200_000,
                        help="Starting bankroll in cents (default 200000 = $2000)")
    parser.add_argument("--poll", type=int, default=30,
                        help="Poll interval in seconds (default 30)")
    parser.add_argument("--iterations", type=int, default=0,
                        help="Max iterations (0 = run forever)")
    parser.add_argument("--threshold", type=float, default=0.03,
                        help="Base edge threshold (default 0.03)")
    parser.add_argument("--kelly", type=float, default=0.25,
                        help="Kelly fraction (default 0.25)")
    args = parser.parse_args()

    cfg_g = load_config()
    cfg_g.logs_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(cfg_g.logs_dir / "trader.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    model_path = Path(args.model) if args.model else cfg_g.models_dir / "v1_latest.pkl"
    if not model_path.exists():
        logging.error("Model artifact not found at %s. Train first.", model_path)
        return 1

    cfg = TraderConfig(
        starting_bankroll_cents=args.bankroll,
        base_edge_threshold=args.threshold,
        kelly_fraction=args.kelly,
        poll_interval_sec=args.poll,
        max_iterations=args.iterations if args.iterations > 0 else None,
        kill_file_path=cfg_g.project_root / "data" / "KILL_SWITCH",
    )

    return run_trader(cfg, model_path)


if __name__ == "__main__":
    sys.exit(main())
