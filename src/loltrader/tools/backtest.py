"""Run a backtest of the v1 model against historical Kalshi candle data.

Usage:
    python -m loltrader.tools.backtest [--model PATH] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from loltrader.backtest.metrics import compute_metrics
from loltrader.backtest.report import write_report
from loltrader.backtest.sim import run_backtest
from loltrader.config import load_config
from loltrader.db import connect, migrate
from loltrader.model.serve import Model


def _setup_logging() -> logging.Logger:
    cfg = load_config()
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    logfile = cfg.logs_dir / "backtest.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(logfile, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("backtest")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=None,
                        help="Path to model artifact (default: models/v1_latest.pkl)")
    parser.add_argument("--start", default=None,
                        help="Start date YYYY-MM-DD (default: 14 days ago)")
    parser.add_argument("--end", default=None,
                        help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--bankroll", type=int, default=200_000,
                        help="Starting bankroll in cents (default: 200000 = $2000)")
    parser.add_argument("--edge-threshold", type=float, default=0.03,
                        help="Base edge threshold (default: 0.03 = 3%%)")
    parser.add_argument("--kelly-fraction", type=float, default=0.25,
                        help="Fraction of Kelly (default: 0.25)")
    args = parser.parse_args()

    log = _setup_logging()
    cfg = load_config()
    model_path = Path(args.model) if args.model else (cfg.models_dir / "v1_latest.pkl")
    if not model_path.exists():
        log.error("Model artifact not found at %s. Run train_model first.", model_path)
        return 1

    today = datetime.utcnow().strftime("%Y-%m-%d")
    end_date = args.end or today
    start_date = args.start or (
        datetime.utcnow() - timedelta(days=14)
    ).strftime("%Y-%m-%d")

    log.info("Backtest config:")
    log.info("  model:      %s", model_path)
    log.info("  date range: %s -> %s", start_date, end_date)
    log.info("  bankroll:   $%.2f", args.bankroll / 100)
    log.info("  threshold:  %.3f", args.edge_threshold)
    log.info("  kelly:      %.2f", args.kelly_fraction)

    start = time.time()
    try:
        conn = connect()
        migrate(conn)
        model = Model.load(model_path)
        log.info("Loaded model: %s", model)

        result = run_backtest(
            conn,
            model,
            start_date=start_date,
            end_date=end_date,
            starting_bankroll_cents=args.bankroll,
            base_edge_threshold=args.edge_threshold,
            kelly_fraction=args.kelly_fraction,
        )

        metrics = compute_metrics(result.portfolio, result.trade_log)
        log.info("Metrics: %s", asdict(metrics))

        skipped = {
            "no_edge": result.skipped_no_edge,
            "no_link": result.skipped_no_link,
            "already_traded": result.skipped_already_traded,
            "no_features": result.skipped_no_features,
            "cap": result.skipped_cap,
        }

        out_dir = cfg.project_root / "models" / "backtests"
        report_path = write_report(
            result.portfolio, metrics, result.trade_log, skipped, out_dir,
            title=f"v1 backtest {start_date} → {end_date}",
        )
        log.info("Report: %s", report_path)

        # Pretty JSON metrics dump
        metrics_path = report_path.with_suffix(".json")
        metrics_path.write_text(json.dumps({
            "config": {
                "model": str(model_path),
                "start_date": start_date,
                "end_date": end_date,
                "bankroll_cents": args.bankroll,
                "edge_threshold": args.edge_threshold,
                "kelly_fraction": args.kelly_fraction,
            },
            "metrics": asdict(metrics),
            "skipped": skipped,
        }, indent=2, default=str))

        elapsed = time.time() - start
        log.info("Backtest complete in %.1fs", elapsed)
        conn.close()
        return 0
    except Exception:
        log.exception("Backtest failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
