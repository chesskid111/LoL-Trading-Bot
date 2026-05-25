"""Long-running per-game livestats poller.

Runs as a subprocess child of game_discovery (one poller per active game).
Polls the Riot livestats `window` endpoint at 2s cadence, writes new frames
to `live_frames`, touches a heartbeat file every 10s. Exits cleanly when the
game's been in state `finished` for ≥ END_HOLD_SEC.

Spec §6.1, §11.5 (heartbeat / supervision).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from loltrader.db import connect
from loltrader.livestats import discovery, storage

# Poll cadence. Riot emits frames every 10s; polling at 2s ensures we catch
# each new frame within ~1s of availability. (Spec §6.1.)
POLL_INTERVAL_SEC = 2.0

# Heartbeat refresh cadence.
HEARTBEAT_INTERVAL_SEC = 10.0

# How long the game must remain in state=finished before we exit. Guards
# against early-exit on transient post-game state flicker.
END_HOLD_SEC = 120.0

# Maximum consecutive transport failures before we declare the game lost
# and exit. Watchdog will restart us if game_discovery still considers the
# game live.
MAX_CONSECUTIVE_FAILURES = 30

log = logging.getLogger(__name__)


@dataclass
class PollerStats:
    frames_inserted: int = 0
    frames_skipped_dup: int = 0
    api_failures: int = 0
    started_ts: float = 0.0


def heartbeat_path(game_id: str, project_root: Path) -> Path:
    """Return the heartbeat file path for this game's poller."""
    return project_root / "data" / "heartbeat" / f"livestats_poller_{game_id}"


def _touch_heartbeat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def run_poller(game_id: str, league_slug: str, project_root: Path,
               max_runtime_sec: float | None = None) -> PollerStats:
    """Main poller loop.

    Args:
        game_id: Riot esports gameId.
        league_slug: For the games_live row.
        project_root: Project root path (for heartbeat location).
        max_runtime_sec: Optional safety stop. None = run until game ends.

    Returns:
        PollerStats covering the run.
    """
    stats = PollerStats(started_ts=time.time())
    log.info("livestats_poller starting", extra={"game_id": game_id})

    # Probe adaptive delay once at startup. Cache it on games_live.
    delay = discovery.probe_minimum_delay(game_id)
    if delay is None:
        log.error("Cannot probe delay for game; aborting", extra={"game_id": game_id})
        return stats
    if delay > discovery.MAX_DELAY_SEC:
        log.error("Probed delay %ds exceeds MAX_DELAY_SEC %ds; aborting",
                  delay, discovery.MAX_DELAY_SEC, extra={"game_id": game_id})
        return stats

    # Look up team sides for the games_live row.
    sides = discovery.get_team_sides(game_id)

    conn = connect()
    try:
        storage.register_game_first_seen(
            conn, game_id, league_slug,
            blue_team_code=sides.blue_team_code if sides else None,
            red_team_code=sides.red_team_code if sides else None,
            blue_esports_team_id=sides.blue_esports_team_id if sides else None,
            red_esports_team_id=sides.red_esports_team_id if sides else None,
        )
        storage.set_adaptive_delay(conn, game_id, delay)

        hb_path = heartbeat_path(game_id, project_root)
        last_heartbeat = 0.0
        last_seen_finished: float | None = None
        consecutive_failures = 0

        while True:
            now = time.time()

            if max_runtime_sec is not None and (now - stats.started_ts) > max_runtime_sec:
                log.info("Max runtime reached", extra={"game_id": game_id})
                break

            # Heartbeat — even on a busy loop, ensure watchdog sees us alive.
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
                _touch_heartbeat(hb_path)
                last_heartbeat = now

            # Fetch + write.
            try:
                frame = discovery.get_frame(game_id, delay)
                consecutive_failures = 0
            except discovery.RiotApiError as e:
                consecutive_failures += 1
                stats.api_failures += 1
                log.warning("API failure %d/%d for %s: %s",
                            consecutive_failures, MAX_CONSECUTIVE_FAILURES, game_id, e)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.error("Too many consecutive failures; aborting",
                              extra={"game_id": game_id})
                    break
                time.sleep(POLL_INTERVAL_SEC)
                continue

            if frame is not None:
                inserted = storage.write_frame(conn, game_id, frame)
                if inserted:
                    stats.frames_inserted += 1
                else:
                    stats.frames_skipped_dup += 1

                # Track game-finished state for graceful exit.
                if frame.get("gameState") == "finished":
                    if last_seen_finished is None:
                        last_seen_finished = now
                    elif (now - last_seen_finished) >= END_HOLD_SEC:
                        storage.set_game_end(conn, game_id, int(now))
                        log.info("Game ended; poller exiting", extra={"game_id": game_id})
                        break
                else:
                    last_seen_finished = None

            time.sleep(POLL_INTERVAL_SEC)
    finally:
        conn.close()

    return stats
