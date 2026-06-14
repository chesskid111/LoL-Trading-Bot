"""One-command live launcher: dashboard + Riot ingestion together.

The dashboard (``loltrader.api.main``) only carries Kalshi prices. Live game
state (win-prob, draft breakdown, risk badge) needs ``game_discovery`` running
too. This starts BOTH, prefixes their logs, and shuts both down cleanly on
Ctrl+C — so you never hit the "dashboard but no game state" gap.

Usage:
    python -m loltrader.tools.run_live                     # LCK (default)
    python -m loltrader.tools.run_live --leagues lck lpl lec lcs
    python -m loltrader.tools.run_live --poll-interval 20

Ctrl+C stops everything (game_discovery reaps its child pollers).
"""
from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import threading
import time

IS_WIN = sys.platform == "win32"


def _pump(proc: subprocess.Popen, tag: str) -> None:
    """Prefix each line of a child's output with its tag."""
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(f"[{tag}] {line.rstrip()}\n")
        sys.stdout.flush()


def _spawn(args: list[str], tag: str) -> subprocess.Popen:
    kwargs: dict = dict(
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    if IS_WIN:
        # New process group so we can send CTRL_BREAK for graceful shutdown
        # (lets game_discovery's KeyboardInterrupt handler reap its pollers).
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen([sys.executable, "-m", *args], **kwargs)
    threading.Thread(target=_pump, args=(proc, tag), daemon=True).start()
    return proc


def _stop(proc: subprocess.Popen, tag: str) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WIN:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        print(f"[run_live] {tag} didn't exit; killing", flush=True)
        proc.kill()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--leagues", nargs="+", default=["lck"],
                   help="Leagues for game_discovery (default: lck)")
    p.add_argument("--poll-interval", type=float, default=30.0,
                   help="game_discovery getLive poll interval (s)")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    league_flags: list[str] = []
    for lg in args.leagues:
        league_flags += ["--league", lg]

    print(f"[run_live] starting dashboard + ingestion (leagues={args.leagues})", flush=True)
    dash = _spawn(["loltrader.api.main"], "dash")
    time.sleep(1.5)  # let the dashboard bind the port first
    disc = _spawn(["loltrader.tools.game_discovery", *league_flags,
                   "--poll-interval", str(args.poll_interval),
                   "--log-level", args.log_level], "disc")

    print("[run_live] both up. Dashboard: http://localhost:8502  (Ctrl+C to stop)",
          flush=True)

    procs = {"dash": dash, "disc": disc}
    try:
        while True:
            for tag, proc in procs.items():
                if proc.poll() is not None:
                    print(f"[run_live] {tag} exited (code {proc.returncode}) — "
                          f"shutting down the other", flush=True)
                    raise KeyboardInterrupt
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[run_live] stopping…", flush=True)
    finally:
        # Stop ingestion first (so it reaps pollers), then the dashboard.
        _stop(disc, "disc")
        _stop(dash, "dash")
        print("[run_live] stopped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
