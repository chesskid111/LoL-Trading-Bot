"""Entry point: always-on game-discovery loop.

Polls Riot's persisted/getLive endpoint every 30s for live LCK games. When a
new game is detected, spawns a livestats_poller subprocess for it. Tracks
spawned children, cleans up when their game ends.

CV-pipeline spawning is layered on in Phase 3 (this entry point will spawn it
alongside the livestats poller once it exists).

Usage:
    python -m loltrader.tools.game_discovery [--league lck] [--poll-interval 30]

Spec §5 (architecture), §11.5 (process supervision), §13.1 (game-driven cold-start).
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from loltrader.config import load_config
from loltrader.livestats import discovery

DEFAULT_POLL_INTERVAL_SEC = 30.0

# How long a discovered game can be absent from getLive before we consider it
# ended and reap its poller. Short outages on the discovery endpoint shouldn't
# kill an in-progress game's poller.
ABSENT_TIMEOUT_SEC = 180.0

# Hard subprocess timeout when reaping a poller. SIGTERM first, then SIGKILL.
CHILD_REAP_TIMEOUT_SEC = 30.0

log = logging.getLogger(__name__)


@dataclass
class ChildPoller:
    game_id: str
    league_slug: str
    process: subprocess.Popen
    spawned_at: float
    last_seen_in_getlive: float = field(default_factory=time.time)


def _spawn_poller(game_id: str, league_slug: str, project_root: Path,
                  python_exe: str) -> ChildPoller:
    """Spawn a livestats_poller subprocess for a game."""
    cmd = [python_exe, "-m", "loltrader.tools.livestats_poller",
           game_id, league_slug]
    log.info("Spawning poller for %s (%s)", game_id, league_slug)
    proc = subprocess.Popen(
        cmd,
        cwd=str(project_root),
        # Inherit stdio so subprocess logs reach our terminal during dev.
        # In production these will be redirected to per-process log files
        # by the watchdog (Phase 9).
    )
    return ChildPoller(
        game_id=game_id,
        league_slug=league_slug,
        process=proc,
        spawned_at=time.time(),
    )


def _reap_child(child: ChildPoller) -> None:
    """Stop a child poller cleanly."""
    if child.process.poll() is None:
        log.info("Reaping poller for %s", child.game_id)
        child.process.terminate()
        try:
            child.process.wait(timeout=CHILD_REAP_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            log.warning("Poller for %s did not exit; killing", child.game_id)
            child.process.kill()
            child.process.wait()


def discovery_cycle(
    children: dict[str, ChildPoller],
    league_slugs: list[str],
    project_root: Path,
    python_exe: str,
) -> None:
    """One iteration of the discovery loop. Spawns/reaps as needed."""
    try:
        live = discovery.find_live_games(league_slugs=league_slugs)
    except discovery.RiotApiError as e:
        log.warning("getLive failure: %s — will retry next cycle", e)
        return

    now = time.time()
    seen_now = {g.game_id for g in live}

    # Spawn pollers for newly-detected games
    for g in live:
        if g.game_id in children:
            children[g.game_id].last_seen_in_getlive = now
            continue
        child = _spawn_poller(g.game_id, g.league_slug, project_root, python_exe)
        children[g.game_id] = child

    # Reap pollers whose game has been absent from getLive for too long, or
    # whose process has already exited on its own (e.g. game ended cleanly).
    for game_id in list(children.keys()):
        child = children[game_id]
        absent_for = now - child.last_seen_in_getlive
        exited = child.process.poll() is not None
        if game_id not in seen_now and (exited or absent_for > ABSENT_TIMEOUT_SEC):
            _reap_child(child)
            del children[game_id]
            log.info("Removed child for %s (absent %.0fs, exited=%s)",
                     game_id, absent_for, exited)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--league", action="append", default=None,
                   help="League slug to monitor (repeatable). Default: lck.")
    p.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SEC,
                   help=f"Seconds between getLive polls. Default {DEFAULT_POLL_INTERVAL_SEC}.")
    p.add_argument("--once", action="store_true",
                   help="Run a single discovery cycle and exit. For testing.")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    leagues = args.league or ["lck"]
    cfg = load_config()
    python_exe = sys.executable
    children: dict[str, ChildPoller] = {}

    log.info("game_discovery starting. leagues=%s poll=%ss", leagues, args.poll_interval)

    try:
        while True:
            discovery_cycle(children, leagues, cfg.project_root, python_exe)
            if args.once:
                break
            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        log.info("Interrupt received; reaping children")
    finally:
        for child in children.values():
            _reap_child(child)

    return 0


if __name__ == "__main__":
    sys.exit(main())
