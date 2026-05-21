# Operations playbook

Day-to-day routine for keeping the bot alive and learning. **This is paper-trading only.** Real-money operations are gated until you decide to flip the switch (see "Go-live gate" at the end).

---

## TL;DR — the realistic routine

| Task | Frequency | Effort | How |
|---|---|---|---|
| `daily_logger` | every 6 hours | 0 (scheduled) | Windows Task Scheduler |
| `ws_streamer` + `run_bot` | during game windows | start/stop manually | PowerShell |
| Streamlit UI | when you want to look | open browser | once per session |
| `oracle_etl` | weekly | ~30 seconds | manual after new CSVs land |
| `train_model` | after each patch (~2 weeks) | ~2 minutes | manual |
| Weekly review | weekly | ~20 minutes | look at decisions / backtest |
| Patch handoff | every ~2 weeks | ~10 minutes | retrain + spot-check |

Nothing is required to be 24/7 immediately. You can ramp up.

---

## 1. Set up `daily_logger` as a scheduled task

This is the most important automation. The logger keeps the Kalshi corpus fresh, which is what the trader reads.

### Windows Task Scheduler

1. Open Task Scheduler (Windows search → "Task Scheduler")
2. **Create Basic Task** → "LoL Bot — Daily Logger"
3. **Trigger**: Daily, recur every 6 hours, indefinitely
4. **Action**: Start a program
   - Program/script: `C:\Users\chess\Desktop\LoLTradingBot\.venv\Scripts\python.exe`
   - Add arguments: `-m loltrader.tools.daily_logger`
   - Start in: `C:\Users\chess\Desktop\LoLTradingBot`
5. **Conditions**: uncheck "Start only if on AC power" if you want it to run on battery
6. **Settings**: check "If task is already running... do not start a new instance"

Verify it works by triggering it manually from Task Scheduler ("Run") and checking `logs\daily_logger.log`.

### What it does

- Pulls all currently open + recently settled LoL events from Kalshi (3 series tickers, both statuses)
- For each event, pulls its markets
- For each market, pulls new candlesticks since last run
- Idempotent — running twice in a row produces 0 new event/market rows, only adds any new candles that came in between

### How long it takes

- First run: ~18 minutes (full historical pull)
- Subsequent runs: ~5–12 minutes (mostly market polls)

---

## 2. Live trading windows

Pro LoL games happen across regions throughout the day in UTC:

| League | Game window (UTC) | Game window (PT) |
|---|---|---|
| LCK | 08:00–13:00 | 01:00–06:00 |
| LPL | 09:00–15:00 | 02:00–08:00 |
| LEC | 16:00–22:00 | 09:00–15:00 |
| LCS / LTA N | 22:00–04:00 next day | 15:00–21:00 |
| CBLOL / LTA S | 20:00–02:00 next day | 13:00–19:00 |
| MSI / Worlds / EWC | varies (often 24h coverage) | varies |

Realistic v1 approach: **start the streamer + trader before whichever region you want to follow, leave them running, stop when games end.** During international events (MSI / Worlds) you can leave them up longer since games run nearly around the clock.

### Recommended startup sequence (3 terminals)

```powershell
# Terminal 1 — live Kalshi price stream
cd C:\Users\chess\Desktop\LoLTradingBot
.\.venv\Scripts\python.exe -m loltrader.tools.ws_streamer

# Terminal 2 — the trader (paper)
cd C:\Users\chess\Desktop\LoLTradingBot
.\.venv\Scripts\python.exe -m loltrader.tools.run_bot --bankroll 200000 --threshold 0.03

# Terminal 3 — the dashboard
cd C:\Users\chess\Desktop\LoLTradingBot
.\.venv\Scripts\python.exe -m streamlit run src/loltrader/ui/app.py
```

The dashboard opens at http://localhost:8501

### Shutdown

- Streamer + trader: Ctrl+C in their terminals (clean shutdown, session closed in DB)
- Streamlit: Ctrl+C in its terminal, then close the browser tab
- Or: touch `data\KILL_SWITCH` to soft-kill the trader (it will stop opening new positions; existing positions held to settlement)

### How long to run

- During games: at least 1–2 hours before the first game until ~1 hour after the last game
- Markets get prices a few hours before kickoff and settle within ~30 minutes after the game ends
- Trader will idle when no markets have live prices, which is fine

---

## 3. Weekly tasks

### Oracle's Elixir refresh

Oracle posts updated CSVs roughly weekly. To refresh:

1. Visit [oracleselixir.com/tools/downloads](https://oracleselixir.com/tools/downloads) (Google Drive link)
2. Re-download the 2026 CSV (the current year is the only one that changes)
3. Replace `data\raw\oracle\2026_LoL_esports_match_data_from_OraclesElixir.csv`
4. Run:
   ```powershell
   .\.venv\Scripts\python.exe -m loltrader.tools.oracle_etl
   ```

This will:
- Re-ETL the CSV (idempotent — only new games get inserted)
- Re-seed team aliases
- Re-backfill linkage with any new aliases or new matches

Takes ~30 seconds total.

### Manual review of unlinked markets

If you want to lift the link rate (currently ~64% on in-universe markets):

```powershell
.\.venv\Scripts\python.exe -m loltrader.tools.review_links
```

This walks you through markets where the linker couldn't match a team name. You enter the canonical Oracle name, it saves an alias, and re-runs linkage. After 30 minutes of this you'll probably get the link rate above 80%.

### Weekly review

Look at:
- `decisions` table: how many decisions did the bot consider this week? How many filled? What were the skip reasons?
- `paper_trades`: how many settled? PnL? Win rate?
- The Streamlit dashboard shows recent decisions + open positions

```powershell
.\.venv\Scripts\python.exe -c "
from loltrader.db import connect
c = connect()
print('Last 7 days:')
for r in c.execute(\"\"\"
    SELECT action, COUNT(*) FROM decisions
    WHERE decision_ts > strftime('%s','now','-7 days')
    GROUP BY action
\"\"\"):
    print(f'  {r[0]}: {r[1]}')

print('\\nClosed paper trades last 30 days:')
print(c.execute(\"\"\"
    SELECT COUNT(*), SUM(pnl_cents), SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END)
    FROM paper_trades WHERE closed_at > strftime('%s','now','-30 days')
\"\"\").fetchone()[:])
"
```

---

## 4. Patch handoff (every ~2 weeks)

When Riot deploys a new patch:

1. Wait ~24-48 hours after the patch lands so Oracle's first patch games show up
2. Refresh Oracle CSVs (as above)
3. Retrain the model:
   ```powershell
   .\.venv\Scripts\python.exe -m loltrader.tools.train_model
   ```
4. Re-run backtest on the new model:
   ```powershell
   .\.venv\Scripts\python.exe -m loltrader.tools.backtest
   ```
5. Read the report under `models\backtests\backtest_report_<timestamp>.md`
6. If metrics look sane, restart the trader so it loads the new artifact

The training pipeline writes `models\v1_latest.pkl` automatically — the trader picks this up on startup.

### What to look for in the new backtest report

- **Brier score**: should be ≤ previous run. If it spikes up, the model is being thrown by the new meta.
- **Edge realization correlation**: should be positive. If it goes more negative, something's drifting.
- **Trade volume**: should be similar to previous runs (a sudden drop means the model is finding fewer edges, which can be fine or a sign of overfitting).

---

## 5. Monitoring the dashboard

Streamlit refresh is manual right now (click the page or hit Ctrl+R). What to scan:

- **Header strip** (5 metrics): session number, starting bankroll, closed PnL, trade count, model load status
- **Manual kill switch**: green = bot can trade; red = killed. Touch the button to toggle.
- **Upcoming tradable markets**: shows the model's prediction vs market price for any market with a confident link and live prices, closing in the next 48h. The "suggested" column shows what the trader would do.
- **Open positions**: paper trades currently held to resolution
- **Recent decisions**: every decision the bot has made in the last ~40 evaluations, including skips and their reasons. Most decisions will be `HOLD: edge_below_threshold` — that's expected.

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `daily_logger` fails with 401 | Kalshi key revoked or expired | Regenerate at Kalshi → Account → API → Create key (read-only). Replace `data\kalshi_creds.json`. |
| Trader does 0 decisions for a long time | No markets currently have live prices | Normal between game windows. Wait for pro games to start. |
| WS streamer crashes immediately | Auth handshake failure | Check `logs\ws_streamer.log`. Same fix as 401 — regenerate key. |
| Model artifact won't load | Pickle format changed across Python versions | Retrain with current Python: `python -m loltrader.tools.train_model` |
| Streamlit dashboard says "no model loaded" | `models\v1_latest.pkl` missing | Train: `python -m loltrader.tools.train_model` |
| Test suite fails on a Windows temp folder permission | A previous admin run created the dir | Already configured to use `.pytest_tmp` in pyproject.toml. If still broken, delete `.pytest_tmp\` and retry. |
| Bot opened a wildly large position | A risk gate is misconfigured | Check `decisions` table for the trade; verify per-market cap and total exposure caps look right in [src/loltrader/trader/loop.py](../src/loltrader/trader/loop.py). |

---

## 7. The "go-live" decision

The full criteria are in [docs/superpowers/specs/2026-05-19-lol-trading-bot-design.md](superpowers/specs/2026-05-19-lol-trading-bot-design.md) Section 10. The checklist:

```
□ Layer 1: 100% unit tests passing
□ Layer 2: Sharpe > 1.0, Brier < 0.20, ECE < 0.05 on backtest
□ Layer 2: out-of-sample holdout matches walk-forward results
□ Layer 3: ≥ 50 closed paper trades
□ Layer 3: paper PnL positive, within 30% of backtest-predicted PnL
□ Layer 3: calibration stable vs backtest
□ Layer 3: no paper trade lost > 15% of bankroll
□ Account funded ≥ $500
```

**Current status of each:**

| Criterion | Status |
|---|---|
| Tests passing | ✅ 96/96 |
| Brier < 0.20 | ❌ 0.21 (close) |
| ECE < 0.05 | ⚠️ CV passes, holdout 0.10 |
| Sharpe > 1.0 backtest | ❓ small sample, unclear |
| ≥ 50 closed paper trades | ❌ 5 from backtest, 0 from live yet |
| Account funded ≥ $500 | ❌ ~$21 |

**Don't promote to real money until every box is checked.** That's the discipline that keeps you alive — the methodology was designed to be conservative for a reason. The honest path is "let it paper-trade for 6–12 weeks, accumulate enough samples to actually evaluate, then decide."

If at the 50-trade milestone the paper results show:
- Positive PnL, calibration holding, no catastrophic losses → flip `live_trading: true`, fund Kalshi to $500+, start at 0.10× Kelly for the first month
- Marginally negative or noisy → keep paper trading, retrain, look at what's not working
- Clearly negative or large drawdowns → don't trade real money. Pivot to v2 (live in-game) or accept that the thesis doesn't have edge.

A negative result here is still a successful outcome — it saved you from depositing money into a strategy that doesn't work.

---

## 8. What "running it" looks like as an honest weekly time investment

- **Setup time, one-time:** ~15 minutes (Task Scheduler config)
- **Daily during game windows:** 5 minutes to start the trader/streamer/UI, 5 minutes to glance at the dashboard mid-day
- **Weekly:** 15 minutes (Oracle refresh + dashboard review)
- **Bi-weekly (per patch):** 30 minutes (retrain + check backtest report)
- **Manual link review:** optional 30 minutes once or twice to lift the link rate

So you're looking at roughly **30–60 minutes per week** of active operations, plus passive time when the bot runs on its own.

The first 6–8 weeks are about generating paper-trade evidence. After that, you have enough samples to actually evaluate and decide what to do next.
