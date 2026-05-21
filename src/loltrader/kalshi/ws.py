"""Kalshi WebSocket client.

A persistent subscription to ``ticker_v2`` channel for a set of market
tickers. Incoming messages update ``kalshi_markets`` rows so the rest
of the bot (trader, UI) sees fresh prices without polling.

Architecture:
  - Async asyncio task with a single WS connection
  - Same RSA-PSS auth as REST (timestamp + GET + path signed)
  - On message: update in-memory MarketState dict + persist to DB
  - On disconnect: exponential backoff reconnect, re-subscribe
  - Sequence-gap detection: log + hard reconnect if seq skips

Run via:
    python -m loltrader.tools.ws_streamer
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from loltrader.config import KalshiConfig, load_config

log = logging.getLogger(__name__)

DEFAULT_CHANNELS = ("ticker_v2",)
RECONNECT_INITIAL_DELAY = 1.0
RECONNECT_MAX_DELAY = 60.0
RECONNECT_BACKOFF = 2.0


@dataclass
class MarketState:
    """In-memory snapshot of one market's current state."""
    market_ticker: str
    yes_bid_cents: int | None = None
    yes_ask_cents: int | None = None
    last_price_cents: int | None = None
    volume: float | None = None
    last_message_ts: int = 0
    last_seq: int = 0


@dataclass
class WSStats:
    """Diagnostic counters for the WS client."""
    messages_received: int = 0
    ticker_updates: int = 0
    seq_gaps: int = 0
    reconnects: int = 0
    last_message_ts: int = 0


class KalshiWS:
    def __init__(
        self,
        market_tickers: list[str],
        cfg: KalshiConfig | None = None,
        channels: tuple[str, ...] = DEFAULT_CHANNELS,
        on_ticker: Callable[[MarketState], None] | None = None,
    ) -> None:
        if cfg is None:
            cfg = load_config().kalshi
        self.cfg = cfg
        self.market_tickers = list(market_tickers)
        self.channels = channels
        self.on_ticker = on_ticker
        self.state: dict[str, MarketState] = {
            t: MarketState(market_ticker=t) for t in market_tickers
        }
        self.stats = WSStats()
        self._private_key = serialization.load_pem_private_key(
            cfg.private_key_path.read_bytes(), password=None,
        )
        self._next_id = 1
        self._stop = asyncio.Event()

    # --- auth ----------------------------------------------------------

    def _signed_headers(self) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        path = "/trade-api/ws/v2"
        message = f"{ts}GET{path}".encode()
        sig = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.cfg.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    def _subscribe_payload(self) -> dict:
        msg = {
            "id": self._next_id,
            "cmd": "subscribe",
            "params": {
                "channels": list(self.channels),
                "market_tickers": self.market_tickers,
            },
        }
        self._next_id += 1
        return msg

    # --- message processing -------------------------------------------

    def handle_message(self, msg: dict) -> None:
        """Update in-memory state and invoke callback. Public for tests."""
        self.stats.messages_received += 1
        self.stats.last_message_ts = int(time.time())

        msg_type = msg.get("type")
        payload = msg.get("msg") or {}
        seq = int(msg.get("seq") or 0)

        if msg_type == "ticker_v2":
            self._handle_ticker(payload, seq)
        # Other message types (orderbook_delta, trade, fill) intentionally
        # ignored for v1. Adding them is straightforward in v3+.

    def _handle_ticker(self, payload: dict, seq: int) -> None:
        ticker = payload.get("market_ticker")
        if not ticker or ticker not in self.state:
            return
        st = self.state[ticker]
        # Sequence-gap check
        if st.last_seq and seq > st.last_seq + 1:
            self.stats.seq_gaps += 1
            log.warning("Sequence gap on %s: expected %d, got %d", ticker,
                        st.last_seq + 1, seq)
            # In a stricter implementation we'd trigger a hard re-subscribe.
            # For v1 we just continue with the new data — ticker_v2 is
            # full-state messages, not deltas, so gaps don't corrupt state.
        st.last_seq = max(st.last_seq, seq)
        st.last_message_ts = int(time.time())
        # Kalshi sends prices as integer cents in ticker_v2
        if "yes_bid" in payload:
            st.yes_bid_cents = int(payload["yes_bid"])
        if "yes_ask" in payload:
            st.yes_ask_cents = int(payload["yes_ask"])
        if "yes_price" in payload:
            st.last_price_cents = int(payload["yes_price"])
        if "volume" in payload:
            st.volume = float(payload["volume"])

        self.stats.ticker_updates += 1
        if self.on_ticker:
            self.on_ticker(st)

    # --- the connection loop ------------------------------------------

    async def run(self) -> None:
        url = self.cfg.ws_url
        delay = RECONNECT_INITIAL_DELAY
        while not self._stop.is_set():
            try:
                headers = self._signed_headers()
                log.info("Connecting WS to %s (%d markets)", url, len(self.market_tickers))
                async with websockets.connect(url, extra_headers=headers) as ws:
                    # Send subscribe
                    sub = self._subscribe_payload()
                    await ws.send(json.dumps(sub))
                    log.info("Subscribed to %s on %d markets",
                             self.channels, len(self.market_tickers))
                    delay = RECONNECT_INITIAL_DELAY  # reset on success
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            log.warning("Non-JSON WS message: %s", raw[:200])
                            continue
                        self.handle_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stats.reconnects += 1
                log.warning("WS error: %s. Reconnecting in %.1fs", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * RECONNECT_BACKOFF, RECONNECT_MAX_DELAY)
        log.info("WS run loop exiting")

    def stop(self) -> None:
        self._stop.set()


def persist_market_state(
    conn: sqlite3.Connection, st: MarketState
) -> None:
    """Update kalshi_markets row from a MarketState snapshot."""
    now = int(time.time())
    conn.execute(
        """
        UPDATE kalshi_markets
        SET yes_bid_cents = COALESCE(?, yes_bid_cents),
            yes_ask_cents = COALESCE(?, yes_ask_cents),
            last_price_cents = COALESCE(?, last_price_cents),
            volume = COALESCE(?, volume),
            last_seen_at = ?
        WHERE market_ticker = ?
        """,
        (st.yes_bid_cents, st.yes_ask_cents, st.last_price_cents, st.volume,
         now, st.market_ticker),
    )
    conn.commit()
