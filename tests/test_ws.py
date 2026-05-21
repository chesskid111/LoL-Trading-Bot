"""Tests for the Kalshi WebSocket message processing.

We don't make real WS connections in tests — we drive ``handle_message``
directly with synthetic payloads, which exercises the same logic the
live loop uses.
"""
from __future__ import annotations

from loltrader.config import KalshiConfig
from loltrader.kalshi.ws import KalshiWS, MarketState


def _make_ws(tickers=("M1", "M2")) -> KalshiWS:
    """Build a KalshiWS without a real key (handle_message doesn't need it)."""
    # Use the real config to satisfy the constructor, but we'll never call run()
    from loltrader.config import load_config
    cfg = load_config().kalshi
    return KalshiWS(market_tickers=list(tickers), cfg=cfg)


def test_ticker_update_basic():
    ws = _make_ws()
    ws.handle_message({
        "type": "ticker_v2",
        "seq": 1,
        "msg": {
            "market_ticker": "M1",
            "yes_bid": 50,
            "yes_ask": 52,
            "yes_price": 51,
            "volume": 1234.0,
        },
    })
    assert ws.state["M1"].yes_bid_cents == 50
    assert ws.state["M1"].yes_ask_cents == 52
    assert ws.state["M1"].last_price_cents == 51
    assert ws.state["M1"].volume == 1234.0
    assert ws.state["M1"].last_seq == 1
    assert ws.stats.ticker_updates == 1


def test_ticker_update_unknown_market_ignored():
    ws = _make_ws()
    ws.handle_message({
        "type": "ticker_v2",
        "seq": 1,
        "msg": {"market_ticker": "NOT_SUBSCRIBED", "yes_bid": 50, "yes_ask": 52},
    })
    # No state for unknown market
    assert "NOT_SUBSCRIBED" not in ws.state
    assert ws.stats.ticker_updates == 0


def test_sequence_gap_detected():
    ws = _make_ws()
    # Seq 1 then jump to 5 — gap of 3 missed messages
    ws.handle_message({"type": "ticker_v2", "seq": 1,
                       "msg": {"market_ticker": "M1", "yes_bid": 50, "yes_ask": 52}})
    ws.handle_message({"type": "ticker_v2", "seq": 5,
                       "msg": {"market_ticker": "M1", "yes_bid": 60, "yes_ask": 62}})
    assert ws.stats.seq_gaps == 1
    # We still applied the new data — ticker_v2 is full state, not deltas
    assert ws.state["M1"].yes_bid_cents == 60
    assert ws.state["M1"].last_seq == 5


def test_callback_invoked():
    invoked: list[MarketState] = []

    from loltrader.config import load_config
    cfg = load_config().kalshi
    ws = KalshiWS(
        market_tickers=["M1"], cfg=cfg,
        on_ticker=lambda s: invoked.append(s),
    )
    ws.handle_message({"type": "ticker_v2", "seq": 1,
                       "msg": {"market_ticker": "M1", "yes_bid": 50, "yes_ask": 52}})
    assert len(invoked) == 1
    assert invoked[0].market_ticker == "M1"
    assert invoked[0].yes_ask_cents == 52


def test_unknown_message_type_ignored():
    ws = _make_ws()
    # orderbook_delta isn't handled in v1
    ws.handle_message({"type": "orderbook_delta", "seq": 1,
                       "msg": {"market_ticker": "M1", "price": 50, "delta": 5, "side": "yes"}})
    # No ticker updates, but message counter incremented
    assert ws.stats.ticker_updates == 0
    assert ws.stats.messages_received == 1


def test_partial_payload_doesnt_clobber_known_fields():
    ws = _make_ws()
    # First message sets everything
    ws.handle_message({"type": "ticker_v2", "seq": 1,
                       "msg": {"market_ticker": "M1", "yes_bid": 50, "yes_ask": 52,
                               "yes_price": 51, "volume": 100.0}})
    # Second message only updates bid
    ws.handle_message({"type": "ticker_v2", "seq": 2,
                       "msg": {"market_ticker": "M1", "yes_bid": 49}})
    assert ws.state["M1"].yes_bid_cents == 49
    # Ask should still be from the previous message
    assert ws.state["M1"].yes_ask_cents == 52
    # Volume preserved
    assert ws.state["M1"].volume == 100.0
