"""Kalshi REST client.

Signed-request auth: timestamp + method + path are signed with RSA-PSS
(SHA-256, MGF1-SHA-256, salt = digest length). Signature is base64-encoded
and sent in the ``KALSHI-ACCESS-SIGNATURE`` header along with the key ID
and timestamp.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from loltrader.config import KalshiConfig, load_config


class KalshiRestError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path} -> {status}: {body[:500]}")
        self.method = method
        self.path = path
        self.status = status
        self.body = body


class KalshiClient:
    def __init__(self, cfg: KalshiConfig | None = None) -> None:
        if cfg is None:
            cfg = load_config().kalshi
        self.cfg = cfg
        pem_bytes = Path(cfg.private_key_path).read_bytes()
        self._private_key = serialization.load_pem_private_key(pem_bytes, password=None)
        self._session = requests.Session()

    # --- internals ---------------------------------------------------

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        message = f"{timestamp_ms}{method}{path}".encode()
        sig = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        ts = str(int(time.time() * 1000))
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        full_path = f"/trade-api/v2{path}"
        sig = self._sign(ts, method.upper(), full_path)
        headers = {
            "KALSHI-ACCESS-KEY": self.cfg.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type": "application/json",
        }
        url = f"{self.cfg.base_url}{path}{query}"
        r = self._session.request(
            method.upper(), url, headers=headers, json=json_body, timeout=30
        )
        if not r.ok:
            raise KalshiRestError(method, path, r.status_code, r.text)
        return r.json()

    # --- convenience wrappers ---------------------------------------

    def get_balance(self) -> dict:
        return self.request("GET", "/portfolio/balance")

    def list_events(self, **params) -> dict:
        return self.request("GET", "/events", params=params)

    def list_markets(self, **params) -> dict:
        return self.request("GET", "/markets", params=params)

    def get_market(self, ticker: str) -> dict:
        return self.request("GET", f"/markets/{ticker}")

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self.request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    def get_candlesticks(
        self,
        series_ticker: str,
        market_ticker: str,
        start_unix: int,
        end_unix: int,
        period_interval: int,
    ) -> dict:
        return self.request(
            "GET",
            f"/series/{series_ticker}/markets/{market_ticker}/candlesticks",
            params={
                "start_ts": start_unix,
                "end_ts": end_unix,
                "period_interval": period_interval,
            },
        )
