"""Entry point: per-game livestats poller subprocess.

Spawned by game_discovery for each detected live game. Long-running until the
game ends or a fatal failure occurs.

Usage:
    python -m loltrader.tools.livestats_poller <game_id> <league_slug>

Spec §6.1, §11.5.
"""
from __future__ import annotations

import argparse
import logging
import sys

from loltrader.config import load_config
from loltrader.livestats.poller import run_poller


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("game_id", help="Riot esports gameId")
    p.add_argument("league_slug", help="League slug (e.g. 'lck')")
    p.add_argument("--max-runtime-sec", type=float, default=None,
                   help="Optional safety stop. Default = run until game ends.")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    cfg = load_config()
    stats = run_poller(
        game_id=args.game_id,
        league_slug=args.league_slug,
        project_root=cfg.project_root,
        max_runtime_sec=args.max_runtime_sec,
    )
    print(f"poller exiting: inserted={stats.frames_inserted}, "
          f"skipped_dup={stats.frames_skipped_dup}, api_failures={stats.api_failures}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
