"""Week-1 Kalshi go/no-go exploration.

Answers three questions:
  1. Does our auth work? (hit /portfolio/balance)
  2. What LoL markets exist right now? (search events by ticker/title)
  3. For one resolved LoL market, what historical price fidelity is available?
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running this script directly
sys.path.insert(0, str(Path(__file__).parent))
from kalshi_client import KalshiClient

# Credentials loaded from data/kalshi_creds.json (gitignored) via the package
from loltrader.config import load_config

_cfg = load_config().kalshi
KEY_ID = _cfg.key_id
KEY_PATH = str(_cfg.private_key_path)

OUT_DIR = Path(__file__).parent / "out"
OUT_DIR.mkdir(exist_ok=True)


def is_lol(event: dict) -> bool:
    pm = event.get("product_metadata") or {}
    return pm.get("competition") == "League of Legends"


def main() -> None:
    client = KalshiClient(KEY_ID, KEY_PATH)

    # --- 1. Auth check ---
    print("=" * 60)
    print("[1/3] Auth check via /portfolio/balance")
    print("=" * 60)
    try:
        bal = client.request("GET", "/portfolio/balance")
        print(f"  OK. Balance response: {json.dumps(bal, indent=2)[:300]}")
    except Exception as e:
        print(f"  FAILED: {e}")
        print("  Cannot proceed without auth. Check key ID and private key path.")
        return

    # --- 2. Find LoL events ---
    print()
    print("=" * 60)
    print("[2/3] Searching for LoL events")
    print("=" * 60)

    all_lol_events: list[dict] = []
    cursor = None
    pages = 0
    max_pages = 50  # safety cap
    while pages < max_pages:
        params: dict = {"limit": 200, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        resp = client.request("GET", "/events", params=params)
        events = resp.get("events", []) or []
        all_lol_events.extend(e for e in events if is_lol(e))
        cursor = resp.get("cursor")
        pages += 1
        if not cursor:
            break

    print(f"  Scanned {pages} pages of OPEN events.")
    print(f"  Found {len(all_lol_events)} LoL open events.")

    # Group by series_ticker so we see what market types exist
    from collections import Counter
    series_counts = Counter(e.get("series_ticker") for e in all_lol_events)
    scopes = Counter(
        ((e.get("product_metadata") or {}).get("competition_scope")) for e in all_lol_events
    )
    print(f"  Series breakdown: {dict(series_counts)}")
    print(f"  Scope breakdown:  {dict(scopes)}")

    if all_lol_events:
        print()
        print("  Sample (first 10):")
        for e in all_lol_events[:10]:
            print(f"    {e.get('event_ticker'):<40s} {e.get('title')}")

    # Save full dump for offline inspection
    (OUT_DIR / "lol_events_open.json").write_text(json.dumps(all_lol_events, indent=2))
    print(f"  Full dump: {OUT_DIR / 'lol_events_open.json'}")

    # Also try settled (closed) events — these are what we'd backtest against
    print()
    print("  Now searching SETTLED (resolved) LoL events for backtest data...")
    settled_lol: list[dict] = []
    cursor = None
    pages = 0
    while pages < max_pages:
        params = {"limit": 200, "status": "settled"}
        if cursor:
            params["cursor"] = cursor
        resp = client.request("GET", "/events", params=params)
        events = resp.get("events", []) or []
        settled_lol.extend(e for e in events if is_lol(e))
        cursor = resp.get("cursor")
        pages += 1
        if not cursor:
            break

    print(f"  Scanned {pages} pages of SETTLED events.")
    print(f"  Found {len(settled_lol)} LoL-related settled events.")
    (OUT_DIR / "lol_events_settled.json").write_text(json.dumps(settled_lol, indent=2))

    # --- 3. Probe market history for one settled event ---
    print()
    print("=" * 60)
    print("[3/3] Probe historical price data for one settled LoL market")
    print("=" * 60)

    if not settled_lol:
        print("  No settled LoL events found. Skipping price-fidelity probe.")
        print("  (This may mean Kalshi only retains recent settled events, "
              "or LoL markets haven't settled recently.)")
        return

    target_event = settled_lol[0]
    et = target_event.get("event_ticker")
    print(f"  Target event: {et}  ({target_event.get('title')})")

    # Get markets under this event
    markets_resp = client.request("GET", "/markets", params={"event_ticker": et, "limit": 50})
    markets = markets_resp.get("markets", []) or []
    print(f"  Markets under this event: {len(markets)}")
    if not markets:
        return

    target_market = markets[0]
    mt = target_market.get("ticker")
    print(f"  Target market: {mt}  ({target_market.get('title')})")
    print(f"  Open time:  {target_market.get('open_time')}")
    print(f"  Close time: {target_market.get('close_time')}")
    print(f"  Result:     {target_market.get('result')}")

    (OUT_DIR / "sample_market.json").write_text(json.dumps(target_market, indent=2))

    # Try to pull candlesticks. Endpoint:
    # GET /series/{series_ticker}/markets/{market_ticker}/candlesticks
    series_ticker = target_event.get("series_ticker") or et.split("-")[0]
    open_ts = target_market.get("open_time")
    close_ts = target_market.get("close_time")
    if not (open_ts and close_ts):
        print("  No open/close time on market; cannot fetch candlesticks.")
        return

    # Kalshi candlestick endpoint expects unix timestamps and a period_interval (in minutes)
    from datetime import datetime
    def to_unix(iso_str: str) -> int:
        return int(datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp())

    start_unix = to_unix(open_ts)
    end_unix = to_unix(close_ts)

    # Try a few intervals to see what resolution we can get
    for interval_min in (1, 60, 1440):
        path = f"/series/{series_ticker}/markets/{mt}/candlesticks"
        try:
            cs = client.request(
                "GET",
                path,
                params={
                    "start_ts": start_unix,
                    "end_ts": end_unix,
                    "period_interval": interval_min,
                },
            )
            n = len(cs.get("candlesticks", []) or [])
            print(f"  Candlesticks @ {interval_min}min interval: {n} bars")
            (OUT_DIR / f"sample_candlesticks_{interval_min}min.json").write_text(
                json.dumps(cs, indent=2)
            )
        except Exception as e:
            print(f"  Candlesticks @ {interval_min}min interval: FAILED ({e})")

    print()
    print(f"All output saved to: {OUT_DIR}")


if __name__ == "__main__":
    main()
