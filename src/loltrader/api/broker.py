"""In-process Kalshi WS broker for the FastAPI dashboard.

Connects to Kalshi WebSocket, subscribes to active LoL markets, maintains
in-memory orderbook + ticker state, and broadcasts updates to every
connected browser WebSocket client.

This is the heart of the real-time dashboard: when Kalshi sends a
book update, it propagates to the user's screen within ~100-200ms total.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from loltrader.config import load_config
from loltrader.db import connect
from loltrader.kalshi.corpus import fast_refresh_active_lol_markets
from loltrader.kalshi.rest import KalshiClient
from loltrader.kalshi.ws import KalshiWS, MarketState, persist_market_state, persist_orderbook

log = logging.getLogger(__name__)

# Re-query active markets list every N seconds
REFRESH_MARKETS_SEC = 300
# Persist book state to DB every N seconds (for Streamlit compat + restart recovery)
DB_FLUSH_INTERVAL_SEC = 1.0


@dataclass
class ConnectedClient:
    """A browser WebSocket client and the markets they care about."""
    ws: WebSocket
    subscribed_tickers: set[str] = field(default_factory=set)
    last_ping_ts: float = field(default_factory=time.time)


class MarketBroker:
    """Owns the Kalshi WS connection + the set of browser clients."""

    def __init__(self) -> None:
        self.cfg = load_config()
        self.rest = KalshiClient(self.cfg.kalshi)
        self.ws_client: KalshiWS | None = None
        self.clients: list[ConnectedClient] = []
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        # Phase 5: live win-prob service. Lazy-loaded so a missing model file
        # doesn't break the broker — it just disables winprob_update messages.
        from loltrader.winprob.serve import WinprobService
        self.winprob = WinprobService()

    # --- client management -------------------------------------------------

    async def add_client(self, ws: WebSocket) -> ConnectedClient:
        client = ConnectedClient(ws=ws)
        async with self._lock:
            self.clients.append(client)
        log.info("Browser connected (total: %d)", len(self.clients))
        return client

    async def remove_client(self, client: ConnectedClient) -> None:
        async with self._lock:
            if client in self.clients:
                self.clients.remove(client)
        log.info("Browser disconnected (total: %d)", len(self.clients))

    async def _broadcast(self, payload: dict[str, Any],
                         ticker_filter: str | None = None) -> None:
        """Send a JSON message to all clients (or only those subscribed to ticker)."""
        msg = json.dumps(payload)
        async with self._lock:
            stale: list[ConnectedClient] = []
            for client in self.clients:
                if ticker_filter and ticker_filter not in client.subscribed_tickers:
                    continue
                try:
                    await client.ws.send_text(msg)
                except Exception:
                    stale.append(client)
            for s in stale:
                self.clients.remove(s)

    # --- Kalshi WS lifecycle ----------------------------------------------

    def _on_ticker(self, state: MarketState) -> None:
        """Called synchronously by KalshiWS from within its async task.
        We're already on the event loop, so just create_task for the broadcast.
        Also persists to DB asynchronously."""
        payload = {
            "type": "ticker",
            "market_ticker": state.market_ticker,
            "yes_bid": state.yes_bid_cents,
            "yes_ask": state.yes_ask_cents,
            "last_price": state.last_price_cents,
            "volume": state.volume,
            "ts": int(time.time() * 1000),
        }
        try:
            asyncio.create_task(
                self._broadcast(payload, ticker_filter=state.market_ticker)
            )
        except RuntimeError:
            # No running event loop (e.g., shutdown) — drop silently
            pass

    async def _orderbook_pump(self) -> None:
        """Watch in-memory orderbook state and broadcast/persist changes.

        Runs at ~10Hz, checks for dirty books, broadcasts deltas, writes to DB.
        """
        last_db_flush = time.time()
        while not self._stop.is_set():
            await asyncio.sleep(0.1)
            if not self.ws_client:
                continue
            now = time.time()
            for state in self.ws_client.state.values():
                if not state.book_dirty:
                    continue
                # Broadcast to subscribed clients
                bids = sorted(state.yes_bids.items(), key=lambda x: -x[0])[:10]
                asks = sorted(state.yes_asks.items(), key=lambda x: x[0])[:10]
                payload = {
                    "type": "orderbook",
                    "market_ticker": state.market_ticker,
                    "bids": [[p, s] for p, s in bids],
                    "asks": [[p, s] for p, s in asks],
                    "ts": int(now * 1000),
                }
                await self._broadcast(payload, ticker_filter=state.market_ticker)
                state.book_dirty = False
            # Periodic DB flush so Streamlit + restart recovery work
            if now - last_db_flush >= DB_FLUSH_INTERVAL_SEC:
                try:
                    with connect() as c:
                        for state in self.ws_client.state.values():
                            persist_orderbook(c, state)
                            persist_market_state(c, state)
                except Exception as e:
                    log.warning("DB flush failed: %s", e)
                last_db_flush = now

    async def _frame_push_pump(self) -> None:
        """Watch live_frames for new rows and push them via WebSocket.

        Runs every 250ms — about as fast as we can react to data the moment
        livestats_poller writes it to the DB. Reduces dashboard latency from
        ~1-2s (client polling /api/games every 1s) to ~250-300ms from when
        Riot's API publishes a new frame.
        """
        last_frame_id = 0
        # Prime: skip everything that already exists at startup
        try:
            with connect() as c:
                row = c.execute(
                    "SELECT COALESCE(MAX(frame_id), 0) AS mx FROM live_frames"
                ).fetchone()
                last_frame_id = int(row["mx"] or 0)
        except Exception as e:
            log.warning("frame_push_pump prime failed: %s", e)

        while not self._stop.is_set():
            await asyncio.sleep(0.25)
            try:
                with connect() as c:
                    rows = c.execute(
                        """
                        SELECT f.frame_id, f.game_id, f.frame_ts_unix, f.game_state,
                               f.blue_gold, f.blue_kills, f.blue_towers, f.blue_inhibitors,
                               f.blue_barons, f.blue_dragons_json,
                               f.red_gold, f.red_kills, f.red_towers, f.red_inhibitors,
                               f.red_barons, f.red_dragons_json,
                               g.blue_team_code, g.red_team_code, g.game_start_ts_unix
                        FROM live_frames f
                        JOIN games_live g ON g.game_id = f.game_id
                        WHERE f.frame_id > ?
                        ORDER BY f.frame_id ASC
                        LIMIT 50
                        """,
                        (last_frame_id,),
                    ).fetchall()
            except Exception as e:
                log.warning("frame_push_pump query failed: %s", e)
                continue

            for r in rows:
                d = dict(r)
                bg, rg = d.get("blue_gold"), d.get("red_gold")
                d["gold_diff"] = (bg - rg) if (bg is not None and rg is not None) else None
                # Parse dragons JSON to counts for the wire format
                for side in ("blue", "red"):
                    raw = d.pop(f"{side}_dragons_json", None)
                    try:
                        d[f"{side}_dragons"] = len(json.loads(raw)) if raw else 0
                    except (TypeError, ValueError):
                        d[f"{side}_dragons"] = 0
                payload = {"type": "game_frame", "frame": d, "ts": int(time.time() * 1000)}
                await self._broadcast(payload)
                last_frame_id = int(d["frame_id"])

                # Phase 5: compute + push a calibrated win-prob alongside the
                # frame. Uses the latest frame in the DB (which is the one we
                # just pushed). Cheap — model.predict is <10ms.
                if self.winprob.is_ready:
                    try:
                        with connect() as wp_conn:
                            pred = self.winprob.predict(wp_conn, d["game_id"])
                        if pred is not None:
                            wp_payload = {
                                "type": "winprob_update",
                                "prediction": self.winprob.to_wire(pred),
                                "ts": int(time.time() * 1000),
                            }
                            await self._broadcast(wp_payload)
                    except Exception as e:
                        log.debug("winprob push failed for %s: %s", d["game_id"], e)

    async def _periodic_refresh(self) -> None:
        """Re-query active markets list every REFRESH_MARKETS_SEC."""
        last = 0.0
        while not self._stop.is_set():
            await asyncio.sleep(30)
            if time.time() - last < REFRESH_MARKETS_SEC:
                continue
            try:
                with connect() as c:
                    n = fast_refresh_active_lol_markets(self.rest, c)
                log.info("Periodic refresh: %d markets", n)
                # If new markets appeared, restart subscription
                tickers = self._active_tickers()
                if self.ws_client and set(tickers) != set(self.ws_client.market_tickers):
                    log.info("Restarting subscription: %d -> %d markets",
                             len(self.ws_client.market_tickers), len(tickers))
                    self.ws_client.market_tickers = tickers
                    for t in tickers:
                        if t not in self.ws_client.state:
                            self.ws_client.state[t] = MarketState(market_ticker=t)
                    self.ws_client._stop.set()
                    await asyncio.sleep(0.5)
                    self.ws_client._stop.clear()
            except Exception as e:
                log.warning("Periodic refresh failed: %s", e)
            last = time.time()

    def _active_tickers(self) -> list[str]:
        with connect() as c:
            rows = c.execute(
                """
                SELECT market_ticker FROM kalshi_markets
                WHERE status IN ('active', 'open')
                  AND (series_ticker IN ('KXLOLGAME', 'KXLOLMAP', 'KXLOLTOTALMAPS')
                    OR market_ticker LIKE 'KXLOLGAME-%'
                    OR market_ticker LIKE 'KXLOLMAP-%'
                    OR market_ticker LIKE 'KXLOLTOTALMAPS-%')
                """
            ).fetchall()
        return [r["market_ticker"] for r in rows]

    # --- startup / shutdown -----------------------------------------------

    async def startup(self) -> None:
        """Bootstrap: refresh markets, load win-prob model, connect to Kalshi WS."""
        log.info("Broker startup")
        # Load the live win-prob model (Phase 5). Best-effort — if no model
        # file exists, the broker still serves frames without predictions.
        self.winprob.load()

        with connect() as c:
            n = fast_refresh_active_lol_markets(self.rest, c)
        log.info("Bootstrap refresh: %d markets", n)

        tickers = self._active_tickers()
        if not tickers:
            log.warning("No active LoL markets found; broker idle")
            return

        log.info("Subscribing to %d markets", len(tickers))
        self.ws_client = KalshiWS(
            market_tickers=tickers,
            cfg=self.cfg.kalshi,
            channels=("ticker", "orderbook_delta"),
            on_ticker=self._on_ticker,
        )
        # Run Kalshi WS + background pumps as fire-and-forget asyncio tasks
        asyncio.create_task(self.ws_client.run())
        asyncio.create_task(self._orderbook_pump())
        asyncio.create_task(self._periodic_refresh())
        asyncio.create_task(self._frame_push_pump())

    async def shutdown(self) -> None:
        log.info("Broker shutdown")
        self._stop.set()
        if self.ws_client:
            self.ws_client.stop()


# Singleton — the FastAPI app holds one of these on `app.state.broker`
def get_broker(app) -> MarketBroker:
    return app.state.broker
