# LoL Trading Bot

A paper-trading bot for League of Legends pro matches on Kalshi. Predicts series-winner probabilities with a calibrated XGBoost model and would trade against Kalshi prices when the model's number differs from the market's by enough to clear fees + slippage + uncertainty. **v1 is paper-only — no real money is at risk.**

## Status

Architecturally complete. Paper trader, model, backtest, WebSocket streamer, and Streamlit UI all working. Model has real predictive signal (66.3% holdout accuracy vs 50% random) but does not yet clear the "go-live" gate in [docs/superpowers/specs/2026-05-19-lol-trading-bot-design.md](docs/superpowers/specs/2026-05-19-lol-trading-bot-design.md) Section 10.

## What it does

```
┌───────────────────────┐
│  daily_logger (6h)    │  Pulls Kalshi events/markets/candles into SQLite
└───────────┬───────────┘
            │
┌───────────▼───────────┐
│  oracle_etl (weekly)  │  Loads pro match data from Oracle's Elixir CSVs
└───────────┬───────────┘
            │
┌───────────▼───────────┐
│  train_model          │  Builds 66-feature vector per match, trains XGBoost,
│  (after each patch)   │  calibrates with isotonic regression, saves artifact
└───────────┬───────────┘
            │
┌───────────▼───────────┐  ┌────────────────────┐
│  ws_streamer          │  │  Streamlit UI       │
│  (during game days)   │  │  (open in browser)  │
└───────────┬───────────┘  └─────────┬──────────┘
            │ live prices            │
            ▼                        │
┌────────────────────────────────────▼───────┐
│  run_bot                                   │
│  - Reads candidate markets from DB         │
│  - Predicts with model                     │
│  - Compares to current Kalshi price        │
│  - Checks risk gates (drawdown, exposure,  │
│    confidence, freshness)                  │
│  - Logs decision + opens paper trade       │
└────────────────────────────────────────────┘
```

## Quickstart (already set up)

This repo is already initialized in `C:\Users\chess\Desktop\LoLTradingBot\`. The venv at `.venv\` has all dependencies installed. SQLite database at `data\lol.db` has ~3 months of Kalshi data + 2024-2026 Oracle's Elixir CSVs ETL'd.

To run any of the commands below, open PowerShell in the project root:

```powershell
# One-time data refresh (runs in 10-20 min)
.\.venv\Scripts\python.exe -m loltrader.tools.daily_logger
.\.venv\Scripts\python.exe -m loltrader.tools.oracle_etl
.\.venv\Scripts\python.exe -m loltrader.tools.train_model

# Backtest (instant, generates report under models/backtests/)
.\.venv\Scripts\python.exe -m loltrader.tools.backtest

# Live (run during game windows)
.\.venv\Scripts\python.exe -m loltrader.tools.ws_streamer
.\.venv\Scripts\python.exe -m loltrader.tools.run_bot
.\.venv\Scripts\python.exe -m streamlit run src/loltrader/ui/app.py
```

See [docs/operations.md](docs/operations.md) for the recommended day-to-day routine, scheduling, monitoring, and the "when to consider real money" checklist.

## Repository layout

```
LoLTradingBot/
├── src/loltrader/
│   ├── config.py            # Credential + path config
│   ├── db/                  # SQLite connection + versioned migrations
│   ├── kalshi/
│   │   ├── rest.py          # REST client with RSA-PSS auth
│   │   ├── ws.py            # WebSocket client (live price stream)
│   │   ├── corpus.py        # Daily-logger ETL into kalshi_* tables
│   │   └── linkage.py       # Match Kalshi tickers to Oracle matches
│   ├── oracle/
│   │   ├── etl.py           # Oracle's Elixir CSV -> SQLite ETL
│   │   ├── champions_etl.py # Riot Data Dragon champion tags
│   │   └── seed_aliases.py  # Initial Kalshi->Oracle team-name aliases
│   ├── features/
│   │   ├── glicko.py        # Glicko-2 algorithm (standard)
│   │   ├── team_strength.py # Team Glicko snapshots + lookup
│   │   ├── recent_form.py   # Rolling winrates
│   │   ├── matchup.py       # H2H windows
│   │   ├── meta.py          # League/format/patch/playoff features
│   │   ├── schedule.py      # Rest days, back-to-back flags
│   │   ├── draft.py         # Composition tags + champion winrates
│   │   └── __init__.py      # compute_features() orchestrator
│   ├── model/
│   │   ├── dataset.py       # Builds feature matrix from corpus
│   │   ├── folds.py         # Walk-forward CV fold generator
│   │   ├── metrics.py       # Brier, ECE, reliability diagram
│   │   ├── calibrate.py     # Isotonic regression calibrator
│   │   ├── train.py         # XGBoost training + ensemble for uncertainty
│   │   └── serve.py         # Model.load() + predict_dict()
│   ├── backtest/
│   │   ├── sim.py           # Walk-forward backtest simulator
│   │   ├── portfolio.py     # Kelly sizing + risk caps + PnL
│   │   ├── fees.py          # Kalshi standard fee formula (integer math)
│   │   ├── metrics.py       # PnL, Sharpe, edge realization
│   │   └── report.py        # Markdown report + equity curve PNG
│   ├── trader/
│   │   ├── gates.py         # Pre-trade risk validation
│   │   ├── killswitch.py    # Soft/hard/emergency kill state
│   │   ├── paper.py         # Paper-fill simulation + settlement
│   │   └── loop.py          # Main decision loop (run_trader)
│   ├── ui/
│   │   └── app.py           # Streamlit dashboard
│   └── tools/               # CLI entry points (everything you `python -m`)
├── tests/                   # 96 tests, all passing
├── docs/
│   ├── operations.md        # Day-to-day playbook (read this)
│   └── superpowers/
│       ├── specs/2026-05-19-lol-trading-bot-design.md      # Full design spec
│       └── plans/2026-05-20-lol-trading-bot-v1-plan.md     # Phased plan
├── data/                    # gitignored
│   ├── lol.db               # SQLite database
│   ├── kalshi_creds.json    # API credentials
│   └── raw/                 # Oracle CSVs, champion JSON
├── models/                  # gitignored — trained artifacts + backtest reports
├── logs/                    # gitignored — log files
├── research/                # Phase 0 exploration scripts
└── pyproject.toml
```

## What v1 doesn't do (explicit deferrals)

- **No real-money trading.** Paper only. The `live_trading` flag is permanently false in this build.
- **No live in-game state.** Pre-match + draft only. Live state (gold, kills, objectives) is v2 — would use Riot Spectator API.
- **No correlated-market trading.** Only KXLOLGAME (series winner). Map markets + totals deferred.
- **No sub-3-min data.** v4 if justified (would need paid feed).

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

96 tests, all passing. The single most important one is `test_no_future_leak` in [tests/test_features.py](tests/test_features.py) — it asserts that feature values for a past match don't change when newer data is added to the DB.

## Where to read next

- **[docs/operations.md](docs/operations.md)** — the daily/weekly playbook
- **[docs/superpowers/specs/2026-05-19-lol-trading-bot-design.md](docs/superpowers/specs/2026-05-19-lol-trading-bot-design.md)** — full design spec
- **Backtest reports** under `models/backtests/` — current model performance
