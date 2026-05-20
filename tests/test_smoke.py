"""Phase 0 smoke tests — verify the package imports and config loads."""
from __future__ import annotations

import loltrader
from loltrader.config import load_config
from loltrader.kalshi.rest import KalshiClient


def test_version_exposed():
    assert loltrader.__version__ == "0.1.0"


def test_config_loads_from_creds_file():
    cfg = load_config()
    assert cfg.kalshi.key_id
    assert cfg.kalshi.private_key_path.exists(), (
        f"private key file should exist at {cfg.kalshi.private_key_path}"
    )
    assert cfg.kalshi.scope in {"read", "write"}


def test_kalshi_client_constructs():
    """Loading the client should parse the private key without errors."""
    KalshiClient()


def test_kalshi_signing_deterministic_shape():
    """Two signings of the same input shouldn't crash; signatures are
    nondeterministic (PSS uses random salt), so we only check that
    signing produces non-empty base64 of the expected length."""
    client = KalshiClient()
    sig1 = client._sign("1700000000000", "GET", "/trade-api/v2/portfolio/balance")
    sig2 = client._sign("1700000000000", "GET", "/trade-api/v2/portfolio/balance")
    assert sig1 and sig2
    assert len(sig1) == len(sig2)  # PSS sigs are fixed-length per key size
