"""Verify Twitch auth and stream access end-to-end.

Usage:
    python -m loltrader.tools.verify_twitch [--channel lck]

What it does:
    1. Loads creds from data/twitch_creds.json
    2. Invokes `streamlink --json` against the channel using those creds
    3. Reports either the available qualities (channel live, auth working)
       or a diagnosed failure mode

This is the Phase 1 acceptance gate per the implementation plan.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from shutil import which

from loltrader.twitch.auth import (
    TwitchAuthError,
    load_twitch_auth,
    streamlink_auth_args,
)


def _find_streamlink() -> str | None:
    """Locate streamlink, checking the running Python's venv Scripts dir first."""
    py_dir = Path(sys.executable).parent
    for candidate in (py_dir / "streamlink.exe", py_dir / "streamlink"):
        if candidate.exists():
            return str(candidate)
    return which("streamlink")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--channel", default="lck", help="Twitch channel name (default: lck)")
    args = p.parse_args(argv)

    streamlink_path = _find_streamlink()
    if streamlink_path is None:
        print("ERROR: `streamlink` CLI not found.", file=sys.stderr)
        print("       Install via `pip install streamlink` inside the venv.", file=sys.stderr)
        return 2

    try:
        auth = load_twitch_auth()
    except TwitchAuthError as e:
        print(f"ERROR: Twitch auth not configured.\n{e}", file=sys.stderr)
        return 3

    cmd = [
        streamlink_path,
        *streamlink_auth_args(auth),
        f"twitch.tv/{args.channel}",
        "--json",
    ]

    # Don't log full command — it contains the OAuth token.
    print(f"Probing twitch.tv/{args.channel} (auth: {auth.auth_token[:6]}...)")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print("ERROR: streamlink timed out after 30s.", file=sys.stderr)
        return 4

    if not result.stdout.strip():
        print("ERROR: streamlink produced no output.", file=sys.stderr)
        if result.stderr.strip():
            print(f"stderr: {result.stderr.strip()}", file=sys.stderr)
        return 5

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("ERROR: streamlink output was not valid JSON:", file=sys.stderr)
        print(result.stdout[:500], file=sys.stderr)
        return 6

    if "error" in data:
        msg = data["error"]
        if "No playable streams" in msg:
            print(f"OK: auth accepted by Twitch. Channel `{args.channel}` is currently offline.")
            print("    (This is the expected `success` signal when no game is live.)")
            return 0
        print(f"FAIL: Twitch returned error: {msg}", file=sys.stderr)
        return 7

    streams = data.get("streams") or {}
    if not streams:
        print("WARN: Auth worked but no streams listed. Channel may be in maintenance.")
        return 0

    qualities = sorted(streams.keys())
    print(f"OK: live stream available. Qualities: {', '.join(qualities)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
