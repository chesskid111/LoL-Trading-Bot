"""Probe #2 — answer the remaining go/no-go questions.

Q1: How far back does settled-event history go? (Filter by series_ticker)
Q2: Liquidity per market (orderbook + recent trades)
Q3: Candlestick payload contents (OHLC, volume, open interest?)
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kalshi_client import KalshiClient

# Credentials loaded from data/kalshi_creds.json (gitignored) via the package
from loltrader.config import load_config

_cfg = load_config().kalshi
KEY_ID = _cfg.key_id
KEY_PATH = str(_cfg.private_key_path)

LOL_SERIES = ["KXLOLGAME", "KXLOLMAP", "KXLOLTOTALMAPS"]

OUT_DIR = Path(__file__).parent / "out"
OUT_DIR.mkdir(exist_ok=True)


def pull_all_settled(client: KalshiClient, series_ticker: str) -> list[dict]:
    """Pull every settled event for a series, paginating to exhaustion."""
    events: list[dict] = []
    cursor = None
    while True:
        params: dict = {
            "limit": 200,
            "status": "settled",
            "series_ticker": series_ticker,
        }
        if cursor:
            params["cursor"] = cursor
        resp = client.request("GET", "/events", params=params)
        chunk = resp.get("events", []) or []
        events.extend(chunk)
        cursor = resp.get("cursor")
        if not cursor or not chunk:
            break
    return events


def main() -> None:
    client = KalshiClient(KEY_ID, KEY_PATH)

    # --- Q1: historical depth per series ---
    print("=" * 60)
    print("[Q1] Historical settled-event depth per LoL series")
    print("=" * 60)
    all_settled: dict[str, list[dict]] = {}
    for s in LOL_SERIES:
        ev = pull_all_settled(client, s)
        all_settled[s] = ev
        if ev:
            times = sorted(e.get("close_time") or e.get("last_updated_ts", "") for e in ev)
            print(f"  {s:<20s} {len(ev):>5d} settled events  "
                  f"({times[0][:10]} -> {times[-1][:10]})")
        else:
            print(f"  {s:<20s} 0 settled events")
        (OUT_DIR / f"settled_{s}.json").write_text(json.dumps(ev, indent=2))

    # --- Q2: liquidity check on currently-open markets ---
    print()
    print("=" * 60)
    print("[Q2] Liquidity on currently-open markets")
    print("=" * 60)

    # Get a few open KXLOLGAME events (series winners — the main target)
    open_resp = client.request(
        "GET", "/events",
        params={"limit": 10, "status": "open", "series_ticker": "KXLOLGAME"},
    )
    open_events = open_resp.get("events", []) or []
    print(f"  Probing {min(5, len(open_events))} open KXLOLGAME events for orderbook/volume")

    liquidity_rows = []
    for ev in open_events[:5]:
        et = ev.get("event_ticker")
        markets_resp = client.request("GET", "/markets", params={"event_ticker": et, "limit": 10})
        for m in (markets_resp.get("markets", []) or [])[:1]:  # 1 market per event is enough
            mt = m.get("ticker")
            # Orderbook
            try:
                ob = client.request("GET", f"/markets/{mt}/orderbook", params={"depth": 10})
                ob_data = ob.get("orderbook", {}) or {}
                yes_bids = ob_data.get("yes", []) or []
                no_bids = ob_data.get("no", []) or []
                # Best yes bid (max price) and best yes ask = 100 - best no bid
                best_yes_bid = max((lvl[0] for lvl in yes_bids), default=None)
                best_no_bid = max((lvl[0] for lvl in no_bids), default=None)
                yes_ask = (100 - best_no_bid) if best_no_bid is not None else None
                spread = (yes_ask - best_yes_bid) if (yes_ask and best_yes_bid) else None
            except Exception as e:
                yes_bids, no_bids, best_yes_bid, yes_ask, spread = [], [], None, None, f"ERR: {e}"

            liquidity_rows.append({
                "market": mt,
                "title": m.get("title", "")[:60],
                "last_price": m.get("last_price"),
                "yes_bid": m.get("yes_bid"),
                "yes_ask": m.get("yes_ask"),
                "volume": m.get("volume"),
                "volume_24h": m.get("volume_24h"),
                "open_interest": m.get("open_interest"),
                "ob_best_yes_bid": best_yes_bid,
                "ob_yes_ask": yes_ask,
                "ob_spread_cents": spread,
                "ob_yes_depth_levels": len(yes_bids),
                "ob_no_depth_levels": len(no_bids),
            })

    for row in liquidity_rows:
        print()
        print(f"  {row['market']}")
        print(f"    title:        {row['title']}")
        print(f"    last_price:   {row['last_price']}  (cents)")
        print(f"    yes_bid/ask:  {row['yes_bid']} / {row['yes_ask']}  "
              f"(spread {row['ob_spread_cents']}c)")
        print(f"    volume total: {row['volume']}    24h: {row['volume_24h']}")
        print(f"    open_interest:{row['open_interest']}")
        print(f"    book depth:   yes={row['ob_yes_depth_levels']}  "
              f"no={row['ob_no_depth_levels']} levels")

    (OUT_DIR / "liquidity_sample.json").write_text(json.dumps(liquidity_rows, indent=2))

    # --- Q3: candlestick payload structure ---
    print()
    print("=" * 60)
    print("[Q3] Candlestick payload structure")
    print("=" * 60)

    # Use a recent settled KXLOLGAME market for this
    if all_settled["KXLOLGAME"]:
        target = all_settled["KXLOLGAME"][0]
        et = target.get("event_ticker")
        markets_resp = client.request("GET", "/markets", params={"event_ticker": et, "limit": 5})
        markets = markets_resp.get("markets", []) or []
        if markets:
            m = markets[0]
            mt = m.get("ticker")
            print(f"  Sample market: {mt}")
            print(f"  Title: {m.get('title')}")
            print(f"  Open: {m.get('open_time')}  Close: {m.get('close_time')}")

            from datetime import datetime
            def to_unix(s): return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
            start_unix = to_unix(m.get("open_time"))
            end_unix = to_unix(m.get("close_time"))

            # Chunk if needed to stay under 5000 bars at 1-minute resolution
            duration_min = (end_unix - start_unix) / 60
            print(f"  Market duration: {duration_min:.0f} min  "
                  f"({duration_min/60:.1f} hrs)")

            if duration_min <= 5000:
                interval = 1
            elif duration_min <= 5000 * 60:
                interval = 60
            else:
                interval = 1440
            print(f"  Using interval: {interval}min (single-request fits under 5000-bar cap)")

            series_ticker = target.get("series_ticker")
            cs = client.request(
                "GET",
                f"/series/{series_ticker}/markets/{mt}/candlesticks",
                params={"start_ts": start_unix, "end_ts": end_unix, "period_interval": interval},
            )
            bars = cs.get("candlesticks", []) or []
            print(f"  Got {len(bars)} bars")
            if bars:
                print(f"  Sample bar (first):")
                print(json.dumps(bars[0], indent=4))
                print(f"  Sample bar (mid):")
                print(json.dumps(bars[len(bars)//2], indent=4))
                print(f"  Sample bar (last):")
                print(json.dumps(bars[-1], indent=4))
            (OUT_DIR / "sample_full_candlesticks.json").write_text(json.dumps(cs, indent=2))

    print()
    print(f"All output saved to: {OUT_DIR}")


if __name__ == "__main__":
    main()
