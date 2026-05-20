"""Minimal Kalshi REST client. Research-grade; not the production wrapper.

Loads RSA private key from disk, signs requests per Kalshi auth spec:
  message = timestamp_ms + method + path
  signature = RSA-PSS(SHA-256, salt=digest-length).sign(message)
  headers = KALSHI-ACCESS-KEY / KALSHI-ACCESS-TIMESTAMP / KALSHI-ACCESS-SIGNATURE
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiClient:
    def __init__(self, key_id: str, private_key_path: str | Path):
        self.key_id = key_id
        pem_bytes = Path(private_key_path).read_bytes()
        self.private_key = serialization.load_pem_private_key(pem_bytes, password=None)

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        message = f"{timestamp_ms}{method}{path}".encode()
        sig = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def request(self, method: str, path: str, params: dict | None = None) -> dict:
        ts = str(int(time.time() * 1000))
        # path used for signing must include query string if any
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        sig = self._sign(ts, method.upper(), f"/trade-api/v2{path}")
        headers = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type": "application/json",
        }
        url = f"{BASE_URL}{path}{query}"
        r = requests.request(method.upper(), url, headers=headers, timeout=30)
        if not r.ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json()
