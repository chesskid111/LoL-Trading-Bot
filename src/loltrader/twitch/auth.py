"""Load Twitch session-cookie auth and produce streamlink-compatible args.

Auth choice rationale (spec §14):
    We use the browser session cookie `auth-token` because (1) the user is
    subscribed to the LCK channel on their personal account, so the same
    auth that grants ad-free playback in their browser grants ad-free HLS
    to streamlink, and (2) the OAuth-dev-console alternative requires
    empirical verification that ad-free playback still works through it
    (open question §17 in spec).

Cookie extraction:
    1. Log into twitch.tv in any browser.
    2. DevTools → Application → Cookies → twitch.tv → copy value of `auth-token`.
    3. Save to data/twitch_creds.json as {"auth_token": "<value>"}.

The cookie is a full session credential. Anyone with the file can act as
the user on Twitch. The credentials file lives under data/ which is
gitignored. Treat it as a secret; revoke via Twitch Settings →
"Disconnect all sessions" if leaked.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from loltrader.config import load_config


class TwitchAuthError(RuntimeError):
    """Raised when Twitch credentials are missing or malformed."""


@dataclass(frozen=True)
class TwitchAuth:
    """Resolved Twitch credentials suitable for handing to streamlink."""

    auth_token: str

    def header_value(self) -> str:
        """Return the `Authorization` header value used by Twitch's HLS API."""
        return f"OAuth {self.auth_token}"


def _creds_path() -> Path:
    """Return the configured creds path, env-var override wins.

    Env var `LOLTRADER_TWITCH_CREDS` overrides for CI / test scenarios.
    Default: <config.data_dir>/twitch_creds.json.
    """
    override = os.environ.get("LOLTRADER_TWITCH_CREDS")
    if override:
        return Path(override)
    cfg = load_config()
    return cfg.data_dir / "twitch_creds.json"


def load_twitch_auth(path: Path | None = None) -> TwitchAuth:
    """Load Twitch creds from disk.

    Raises TwitchAuthError with actionable message if the file is missing
    or malformed. Callers should let this propagate at startup so the
    operator sees the problem before the process tries to stream.
    """
    p = path or _creds_path()
    if not p.exists():
        raise TwitchAuthError(
            f"Twitch creds file not found at {p}.\n"
            "To create it:\n"
            "  1. Log into twitch.tv in any browser\n"
            "  2. DevTools -> Application -> Cookies -> twitch.tv -> copy value of `auth-token`\n"
            f"  3. echo '{{\"auth_token\": \"<value>\"}}' > {p}\n"
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise TwitchAuthError(f"Twitch creds file at {p} is not valid JSON: {e}") from e
    token = data.get("auth_token")
    if not isinstance(token, str) or not token.strip():
        raise TwitchAuthError(
            f"Twitch creds file at {p} is missing a non-empty `auth_token` field."
        )
    return TwitchAuth(auth_token=token.strip())


def streamlink_auth_args(auth: TwitchAuth) -> list[str]:
    """Return CLI args to authenticate streamlink against Twitch.

    Use as:
        subprocess.Popen(["streamlink", *streamlink_auth_args(auth),
                          "twitch.tv/lck", "source"], ...)
    """
    return [
        f"--twitch-api-header=Authorization={auth.header_value()}",
        # --twitch-low-latency drops viewer-side delay from ~20s to ~5s when
        # the broadcast supports it. Per spec §17 #13 this needs empirical
        # verification on LCK/LCS but enabling it costs nothing if unsupported.
        "--twitch-low-latency",
        # Disable Twitch's session-token-required ad-injection workaround.
        # We rely on the subscription tied to the auth-token instead.
        "--twitch-disable-ads",
    ]
