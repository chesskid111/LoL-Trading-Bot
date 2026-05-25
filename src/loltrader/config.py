"""Configuration loader.

Credentials live in ``data/kalshi_creds.json`` (gitignored). All other
config values have sensible defaults; override via environment variables
if needed.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _project_root() -> Path:
    # src/loltrader/config.py  -> two parents up = project root
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class KalshiConfig:
    key_id: str
    private_key_path: Path
    scope: str = "read"  # "read" or "write" — write is gated until go-live
    base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"


@dataclass(frozen=True)
class Config:
    kalshi: KalshiConfig
    project_root: Path = field(default_factory=_project_root)

    @property
    def db_path(self) -> Path:
        return self.project_root / "data" / "lol.db"

    @property
    def data_dir(self) -> Path:
        return self.project_root / "data"

    @property
    def logs_dir(self) -> Path:
        return self.project_root / "logs"

    @property
    def models_dir(self) -> Path:
        return self.project_root / "models"


def load_config(creds_path: Path | None = None) -> Config:
    """Load config. Credentials JSON path can be overridden for tests."""
    root = _project_root()
    creds_path = creds_path or root / "data" / "kalshi_creds.json"

    # Env vars take precedence over file (handy for CI / one-off scripts)
    key_id = os.environ.get("KALSHI_KEY_ID")
    key_path_str = os.environ.get("KALSHI_KEY_PATH")
    scope = os.environ.get("KALSHI_SCOPE", "read")

    if key_id is None or key_path_str is None:
        if not creds_path.exists():
            raise FileNotFoundError(
                f"Kalshi credentials not found. Expected env vars "
                f"KALSHI_KEY_ID + KALSHI_KEY_PATH, or a JSON file at {creds_path}."
            )
        data = json.loads(creds_path.read_text())
        key_id = key_id or data["key_id"]
        key_path_str = key_path_str or data["private_key_path"]
        scope = data.get("scope", scope)

    key_path = Path(key_path_str)
    if not key_path.exists():
        raise FileNotFoundError(f"Kalshi private key not found at {key_path}")

    return Config(
        kalshi=KalshiConfig(
            key_id=key_id,
            private_key_path=key_path,
            scope=scope,
        ),
        project_root=root,
    )
