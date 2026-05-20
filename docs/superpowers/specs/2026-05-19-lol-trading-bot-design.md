# LoL Trading Bot — v1 Design Spec

**Date:** 2026-05-19
**Owner:** mil197@ucsd.edu
**Status:** Draft, awaiting user review
**Target ship:** v1 paper-trading in ~4–6 weeks (full-time, AI-assisted)

---

## 1. Edge hypothesis (the North Star)

Kalshi LoL markets are a mix of casual fans and a small number of bots. The marginal price-setting trader is most likely on the same 3-minute Riot-policy data delay we would be — sub-3-min data costs significant money (GRID/Bayes licensing) and is not the universal baseline.

The edge we are pursuing is **calibrated probabilities derived from feature engineering informed by LoL domain knowledge**, applied to a market that has both unsophisticated participants and a tractable latency floor. Specifically:

- Domain-informed features (composition / win-condition analysis, draft-phase interpretation, patch-relative team form) that competing bots trained only on aggregate stats may underweight.
- Rigorous calibration so that "predicted 70%" actually means 70% — most participants do not validate this.
- Disciplined position sizing and risk controls so a real edge compounds rather than blows up on variance.

What this is **not**: a research-grade teamfight evaluator, a sub-second computer-vision pipeline, or a low-latency arbitrage system. Those are explicitly out of scope for v1.

## 2. Trading style and scope

- **Style:** hybrid directional trading. v1 is autonomous in the pre-game window + human-in-loop during live games; v2+ extends autonomy into live.
- **Markets traded:**
  - `KXLOLGAME` — series winner
  - `KXLOLMAP-...-N` — individual map/game winner
  - `KXLOLTOTALMAPS` — totals (over/under series length) [deferred until series/map perform]
- **Leagues:** all majors (LCK, LPL, LEC, LCS) and international events (MSI, Worlds, First Stand, EWC, ENC).
- **Trading window:** ~2 hours pre-kickoff through end of game. Pre-game empty-book period (days before) is explicitly skipped — no participants are pricing, so there is no edge to capture.

## 3. Capital and risk parameters

- **Bankroll:** $500–$2,000 (treated as 100% risk capital).
- **Max position per market:** 5% of bankroll.
- **Max total exposure at any time:** 20% of bankroll, computed across *all* correlated markets on the same match.
- **Daily stop-loss:** −10% of bankroll triggers soft kill.
- **Session stop-loss:** −30% of bankroll triggers emergency kill.
- **Position sizing:** 0.25× Kelly during paper trading and steady-state live. **Ramp-up**: after passing the "go live" gate, real trading starts at **0.10× Kelly** for the first month, then ramps to 0.25× if calibration holds and no catastrophic single-trade losses occur.

## 4. Roadmap

| Phase | Ships | Estimated duration |
|---|---|---|
| **v1** | Pre-match + draft model, paper trading, autonomous in pre-game window, live-aid UI for in-game manual trades | 4–6 weeks full-time, AI-assisted |
| **v2** | Live data ingestion via Riot Spectator API (3-min delay, free), live in-game model, still paper or small live | 4–6 weeks after v1 |
| **v3** | Autonomous live trading, full risk monitor, real-money scaling | 6–10 weeks after v2 |
| **v4** *(optional)* | Sub-3-min data (in-client spectator or paid feed) if v3 confirms edge AND latency is the bottleneck | 6+ weeks, only if justified |

Total to autonomous live trading: ~3.5–5 months full-time, AI-assisted.

## 5. Architecture

A single Python application running locally on the developer's laptop. Three logical processes share one SQLite database.

```
┌────────────────────────────────────────────────────────────────────┐
│                         LOCAL LAPTOP                               │
│                                                                    │
│  daily_logger.py  ── builds historical corpus (every 6h)           │
│         │                                                          │
│         ▼                                                          │
│  ┌─────────────────────────────────────────────────────┐           │
│  │            SQLite DB  (data/lol.db, WAL mode)       │           │
│  └─────────────┬────────────────────────────┬──────────┘           │
│                │                            │                      │
│   model_serve.py (in-process module)        │                      │
│                │                            │                      │
│                ▼                            ▼                      │
│        trader.py  ──── WebSocket ───▶ Kalshi (live state)          │
│                │       subscription                                │
│                ▼                                                   │
│        live_ui.py (Streamlit, http://localhost:8501)               │
│                                                                    │
│  train.py — manual script, retrains on new data / new patch       │
└────────────────────────────────────────────────────────────────────┘
```

**Components:**

1. **`daily_logger.py`** — scheduled (Windows Task Scheduler) every ~6h. Snapshots Kalshi market state, pulls Oracle's Elixir CSV diffs, reconciles match→market links. Idempotent.
2. **SQLite (`data/lol.db`)** — single source of truth, WAL mode for concurrent reads.
3. **`model_serve.py`** — module imported by trader and UI. Exposes `predict(match_id, draft=None) -> (yes_prob, lower_ci, upper_ci)`.
4. **`trader.py`** — long-running process. Subscribes to Kalshi WebSocket for markets in trading window. For each tick, computes edge vs the model and places paper trades when edge passes gates.
5. **`live_ui.py`** — Streamlit dashboard. Shows model probs, current market prices, computed edge, proposed actions. Accept / Skip / Override buttons. User-facing override layer.
6. **`train.py`** — manual script. Builds features, trains XGBoost, calibrates, writes versioned artifact to `models/`.

**Explicitly not in v1:** microservices, Redis, FastAPI, Docker, cloud deployment, Rust execution engine. Latency targets are seconds, not milliseconds; the laptop is enough.

## 6. Data layer

### Sources

| Source | What | How | Cadence |
|---|---|---|---|
| Kalshi REST API | Markets, candlesticks, settled results, account state | Authenticated signed requests via `kalshi_client.py` | corpus: every 6h |
| Kalshi WebSocket | Real-time orderbook / ticker / fill updates | `wss://api.elections.kalshi.com/trade-api/ws/v2`, subscription per active market | continuous during trading windows |
| Oracle's Elixir | Pro match data: rosters, drafts, per-game stats, outcomes | Manually download annual CSVs from oracleselixir.com; ETL into SQLite | weekly |
| Riot API | Solo-queue player form, patch-specific champion winrates | *(deferred to v2)* | — |

### Polling and subscription cadence

| Process | Source | Cadence |
|---|---|---|
| `daily_logger.py` (corpus) | Kalshi REST | every 6h |
| `trader.py` (pre-game) | Kalshi WebSocket subscription | continuous, server-pushed |
| `trader.py` (in-game) | Kalshi WebSocket subscription | continuous, server-pushed |
| `live_ui.py` | Kalshi WebSocket (shared with trader via in-memory cache) | continuous |
| Oracle's Elixir ETL | Weekly CSV | weekly |

### SQLite schema

**Pro-match data (Oracle's Elixir derived):**
- `patches(patch_id, version, deploy_date)`
- `teams(team_id, canonical_name, region)`
- `players(player_id, ign, role)`
- `matches(match_id, date, league, patch_id, team_a_id, team_b_id, bo_format, series_winner)`
- `match_games(game_id, match_id, game_number, blue_team_id, red_team_id, winner, duration_sec)`
- `match_drafts(game_id, team_id, pick_order, champion_id, role, is_ban)`
- `match_player_stats(game_id, player_id, champion_id, kda, gpm, cspm, vision)`

**Kalshi market data:**
- `kalshi_markets(market_ticker, event_ticker, series_ticker, title, open_time, close_time, result, settled_at)`
- `kalshi_candles(market_ticker, end_period_ts, period_interval, price_open, price_close, price_high, price_low, yes_bid_close, yes_ask_close, volume_fp, open_interest_fp)` — PK on (market_ticker, end_period_ts, period_interval)
- `kalshi_book_snapshots(market_ticker, snapshot_ts, yes_levels_json, no_levels_json)`

**Bridge (match-to-market linkage):**
- `team_aliases(canonical_name, alias)` — manually seeded (~50–100 rows)
- `market_match_links(market_ticker, match_id, game_id NULL, side INTEGER, confidence, manual_override BOOL)`
  - `game_id` NULL for series-winner markets, set for individual-map markets
  - `side`: which team the YES contract resolves on

**Predictions and trades:**
- `predictions(prediction_id, match_id, game_id NULL, made_at, model_version, yes_prob, lower_ci, upper_ci, features_json)`
- `decisions(decision_id, prediction_id, market_ticker, market_yes_ask_at_decision, market_yes_bid_at_decision, edge, action, size_contracts, reason, made_at, made_by [bot|user])`
- `paper_trades(trade_id, decision_id, side, fill_price, contracts, opened_at, closed_at, resolution_value, pnl)`

### Match-to-market linkage rules

1. Parse Kalshi `event_ticker` (e.g., `KXLOLGAME-26MAY231600FLYC9`) for date + team abbreviation pair.
2. Normalize abbreviations through `team_aliases` to canonical team names.
3. Look up `matches` by (team_a, team_b, date ± 1 day for timezone tolerance). Score the candidate:
   - Both team names match canonical (no alias fallback): `confidence = 1.0`
   - One/both names matched via alias: `confidence = 0.8`
   - Date off by ±1 day but teams match: `confidence = 0.7`
   - Multiple matches found: `confidence = 0.3` per candidate
   - No match found: `confidence = 0.0`
4. **Required confidence ≥ 0.7** for the trader to act on the linked market. Anything below → row in `manual_review` table; trader skips.

### Data quality rules

- All monetary values stored as integer cents, never float.
- Resolution sanity check: when `kalshi_markets.result` populates, compare to linked `matches.series_winner`. Disagreement → flag and exclude from training.
- Anomaly flag on candles where `price_high - price_low > 0.5` AND `volume_fp > 0`. Manual inspection of first ~10.
- All schemas have CHECK constraints on probability/percentage ranges.

## 7. Model layer

### Two model variants

- **`model_prematch(match_id) → (yes_prob, p10, p90)`** — uses only pre-game data
- **`model_draft(game_id) → (yes_prob, p10, p90)`** — pre-match data PLUS the locked draft for this specific game

`model_prematch` predicts series winner. `model_draft` predicts each game's winner (after that game's draft is locked).

### Algorithm: XGBoost (or LightGBM)

Justification:
- Tabular data, mixed types — GBT native handling
- ~5–10k pro matches in scope — too small for neural nets
- Fast iteration (train + evaluate ~1 minute)
- Interpretable (`feature_importances_`, SHAP)
- Well-understood calibration approaches

Choice between XGBoost and LightGBM deferred to implementation time.

### Features (initial set)

**Pre-match (~20–30 features):**
- Glicko-2 team rating, recent form (5/10/20-game winrate), patch-specific winrate
- Player Glicko-2, roster instability flags
- H2H (≤ 6 months, ≤ 2 patches)
- Patch context (days into patch, league, bo_format, playoff_flag)
- Schedule (days since last match, back-to-back flag)

**Draft (add ~15–25 more features):**
- Per-champion one-hot or per-champion Glicko-2 on current patch
- Player-on-champion historical winrate
- Composition archetype (hand-coded rules first, cluster-derived later)
- Counter-pick lane matchup winrates per role
- Flex-pick deception flag

Total starter feature set: ~30–50 features. Expect ~20 to actually matter post feature-importance analysis.

### Training

- **Walk-forward validation only** — no random splits, ever
- **Time-decayed sample weighting**: `weight = exp(-age_in_patches / decay)`, decay tuned via CV (~4–8 patches)
- **Loss function**: log loss / cross-entropy. NOT accuracy. Calibrated probabilities are the goal.

### Calibration (post-hoc layer)

- **Isotonic regression** as the default. Platt scaling as fallback.
- Validation: reliability diagram (10 deciles), Brier score, expected calibration error (ECE).
- **Recalibrate after every patch**, even if model unchanged.

### Confidence intervals

Quantile regression boosters: train 3 models (median, p10, p90). The (p90 − p10) gap is the prediction's uncertainty band. Used by the trader to scale position size and adjust the edge threshold.

### Versioning and retraining

- Artifact: pickle of (model + calibrator + feature spec + training metadata). Filename: `models/v1_<patch>_<utc_ts>.pkl`.
- Trader loads newest artifact on startup; supports SIGHUP-style reload command.
- Trigger retraining: new patch detected in data, or significant new training data landed.
- Each decision logs the model_version it used.

### Patch handling

- First 24–48h of a new patch: trading gated until at least one pro day has played on the patch.
- First 7 days: uncertainty band widens automatically; position size scales down.
- The "patch" feature is included as a categorical so the model can learn patch-specific dynamics.

## 8. Trading layer

### Decision rule

```
p_model       = model.predict(...)
p_market_ask  = current_yes_ask / 100
p_market_bid  = current_yes_bid / 100
edge_yes      = p_model - p_market_ask
edge_no       = p_market_bid - p_model
uncertainty   = p90 - p10

E_threshold   = fee_buffer (≈0.02)
              + slippage_buffer (≈0.01)
              + uncertainty_buffer (≈0.5 × uncertainty)

if edge_yes > E_threshold:    BUY YES
elif edge_no > E_threshold:   BUY NO
else:                         HOLD
```

Constants are initial values, refined during backtest tuning.

### Position sizing

```
f_kelly = (p_model × win_payout − (1 − p_model) × lose_payout) / win_payout
size    = min(
    bankroll × 0.25 × f_kelly,                    # 0.25× Kelly
    bankroll × 0.05,                              # per-market cap
    bankroll × 0.20 − current_correlated_exposure,# portfolio cap
    available_balance                             # solvency
)
```

Round down to whole contracts. Series-winner and map-winner markets on the same match treated as ~80% correlated for exposure accounting.

### Order placement

- **Limit orders only.** Market orders banned in v1.
- **Aggressive limit** (place at current ask for buys, bid for sells) is default.
- **Order lifecycle:**
  - Place → record `decisions` row → wait for fill via WS
  - Unfilled after 30s → re-evaluate edge; if still valid, reprice one tick more aggressive; if not, cancel
  - Stale orders never sit through major game events

### Paper vs real

**v1 is paper-only.** Real money requires passing the "go live" gate (Section 10).

Paper trading flow:
1. Decision → `paper_trades` row with `fill_price = current_yes_ask` (modeled, not observed)
2. Slippage model added: assume fill at ask + 1¢
3. At resolution: `pnl = contracts × (resolution_value − fill_price)`

### Live-aid UI override layer

Every market in the trading window shows:
- Model prob, current bid/ask, computed edge, proposed action

User actions logged with `made_by` field:
- **Accept** → paper-trade executes via trader (`made_by = bot`)
- **Skip** → decision logged, no trade
- **Override** (different side, different size) → trade executes with new params (`made_by = user`)

Post-period analysis compares bot-autonomous PnL vs user-override PnL — the empirical test of the LoL-knowledge-as-edge thesis.

### Multi-market correlation

Series-winner and per-map-winner markets on the same match are not independent. v1 treats them as ~80% correlated for the portfolio exposure cap. Proper portfolio optimization across correlated markets is a v3 concern.

### Excluded from v1

- No real-money orders
- No order-book sweeping or icebergs
- No market making
- No cross-market arbitrage
- No reactive cancels on detected game events (no live game state yet)

## 9. Risk and safety

### Pre-trade gates (all must pass)

```
□ Account balance > 0
□ Total open exposure + proposed size ≤ 20% bankroll
□ Single market exposure + proposed size ≤ 5% bankroll
□ Model uncertainty band ≤ max_uncertainty (config)
□ Edge > E_threshold (uncertainty-adjusted)
□ Daily PnL > −10% bankroll
□ Total PnL > −30% bankroll
□ Data freshness: last WS message < 5s ago
□ Market still open AND closes within next 14 days (i.e., now < close_time < now + 14d)
□ Market in known LoL series ticker
□ market_match_link.confidence ≥ threshold, not pending review
```

Single function `validate_decision(d) → Reason | None`; logs first failing reason.

### Kill switches

| Level | Trigger | Action | Recovery |
|---|---|---|---|
| **Soft kill** | −10% daily DD, WS stale > 60s, uncertainty explosion | Stop new positions, hold existing | Auto on clear, or manual |
| **Hard kill** | −20% DD, source disagreement, NaN/invalid model output | Cancel all orders, hold positions | Manual with `--force-resume` |
| **Emergency kill** | −30% DD, account error, unhandled exception | Cancel orders, attempt to flatten, halt | Manual post-mortem required |

Plus **manual kill**: presence of `data/KILL_SWITCH` file forces soft kill within 5 seconds. Lowest-tech, most reliable failsafe.

### Failure modes catalog

| Failure | Detection | Response |
|---|---|---|
| Internet drops | WS heartbeat, REST timeout | Hard kill |
| Kalshi 5xx | Caught in client | Exponential backoff; 5 failures in 60s → soft kill |
| Kalshi 401/403 | Auth failure | Hard kill, alert |
| Laptop crash | Watchdog restart | On startup: reconcile open orders against Kalshi state |
| NaN / invalid model output | Validated after `predict()` | Hard kill |
| Unlinked market | Pre-trade gate | Skip, queue for review |
| Settlement discrepancy | Periodic reconciliation | PnL from actual settle; flag |
| Unknown WS message | Logged + counted | If > 10/min → soft kill |

### Logging

Three streams, all mandatory:
1. **`logs/trader.jsonl`** — structured JSON, append-only, daily rotation. Every event with `decision_id`, `market_ticker`, `kind`.
2. **`decisions` table in SQLite** — same data, queryable.
3. **Stderr** at appropriate log levels for live debugging.

### Account safety

- Two Kalshi API keys: read-only (default) and read-write (only loaded when `live_trading: true`)
- Keys live in `C:\Users\chess\.kalshi\`, outside the project tree
- `.gitignore` excludes any key paths and all `data/` and `models/`

## 10. Testing and validation

Three layers.

### Layer 1: unit tests
- Feature engineering correctness (causal `as_of` enforcement is most important)
- Kalshi signature generation
- Position sizing math
- Edge calculation
- Decision-gate logic

Run on every commit.

### Layer 2: backtest

Walk-forward simulation on Kalshi candlestick history (~3 months currently, growing as `daily_logger.py` runs):

```
For each fold:
    train_data = matches WHERE date <= fold.train_end
    model      = train(train_data)
    for market M in [fold.test_start, fold.test_end]:
        for candle C in M.candles:
            features = compute_features(linked_match, as_of=C.end_ts)
            p_model  = model.predict(features)
            if abs(p_model − C.yes_ask_close/100) > threshold:
                simulate_trade(fill=C.yes_ask_close + slippage)
```

Correctness rules (enforced in code, not by convention):
- All feature computation passes `as_of=t`; runtime assertions check no future leak
- Trades fill at ask + slippage, NEVER at mid
- Kalshi fee schedule subtracted from every PnL
- Partial-fill modeling: if order > candle volume, fill progressively worse OR don't fill

Metrics reported per backtest run:
- Total PnL, Sharpe (gate: > 1.0), Max DD (gate: < 25%)
- Brier score (gate: < 0.20), ECE (gate: < 0.05)
- Win rate, avg win / avg loss, edge realization, per-market profitability
- Sample size per fold (< 100 trades = flag)
- Reliability diagram (visual)

Backtest-lie defenses:
- Lookahead bias → `as_of` timestamps, assertions
- Survivorship → include all markets, no clean-resolutions-only filter
- Overfitting → walk-forward only; final 2 weeks held out as one-shot pre-launch check
- Optimistic execution → fill at ask + slippage; fees applied
- Sample-size delusion → confidence intervals on PnL; < 200 trades = "directional"
- Threshold cherry-picking → edge threshold chosen via CV on training folds, not on test

### Layer 3: paper trading

After backtest passes, **≥ 50 closed paper trades OR 4 weeks**, whichever is later.

Measures same metrics as backtest plus:
- Reality-vs-backtest delta (paper PnL trending as backtest predicted?)
- Out-of-sample calibration stability
- Catastrophic single-trade smell test

### "Go live" gate

ALL must be true before flipping `live_trading: true`:

```
□ Layer 1: 100% unit tests passing
□ Layer 2: Sharpe > 1.0, Brier < 0.20, ECE < 0.05
□ Layer 2: out-of-sample holdout matches walk-forward
□ Layer 3: ≥ 50 closed paper trades
□ Layer 3: paper PnL positive, within 30% of backtest-predicted PnL
□ Layer 3: calibration stable vs backtest
□ Layer 3: no paper trade lost > 15% of bankroll
□ Account funded ≥ $500
```

If any red: keep paper trading and investigate.

### Continuous validation post-launch

- Weekly: reliability diagram on last N real trades
- Per patch: full retrain + Layer 2 + Layer 3 re-check before resuming
- Monthly: PnL post-mortem (am I making money where I expected?)

## 11. Section 0 prerequisites — completed findings

Captured here for reference; these informed the design.

| Question | Finding |
|---|---|
| Does Kalshi list LoL markets? | Yes. 160 open events on a single snapshot. 3 market types per match (series winner, per-map winner, totals). |
| Historical data depth? | 3 months of settled events available via API (~4,000 LoL events total across the three series). Daily logger extends from now forward. |
| Backtest fidelity? | Minute-level OHLC for last-traded price AND separately for yes_bid and yes_ask. Volume + open interest per bar. Excellent. |
| Liquidity? | Markets open days early but empty until ~game time. Real volume only in the hours before kickoff through end of game. Implication: trading window is ~2h pre-game + live, not "pre-match" broadly. |
| Pro game data source? | Oracle's Elixir is primary (free, comprehensive). Riot Match-V5 deferred to v2 (solo-queue only; not pro). |
| Riot API needed for v1? | No. |

## 12. Out of scope for v1 (explicit deferrals)

- Live in-game data and live in-game model (v2)
- Autonomous live trading and real-money orders (v3)
- Sub-3-min data, in-client spectating, broadcast computer vision (v4 if justified)
- Microservices, Redis, FastAPI, Docker, cloud deployment (v3+ if scale demands)
- Rust execution engine (never, for this scale)
- Sequence models / LSTMs / Transformers (research, not v1)
- Champion embeddings beyond one-hot or per-champ Glicko (v2+)
- Region-specific submodels (v2+ if calibration diverges)
- Market making (out of scope entirely)
- Cross-market arbitrage (v2+ research)
- Totals (`KXLOLTOTALMAPS`) trading (deferred until series/maps perform)

## 13. Open questions / known risks

- **Kalshi historical retention limit** — only 3 months currently. Building corpus going forward is the mitigation, but if Kalshi changes retention policy, backtest depth is fixed.
- **Match-to-market linkage failure rate** — unknown until implemented at scale; expect 5–15% of markets to require manual linkage in the first weeks.
- **Edge may not exist** — the honest open question. v1 is structured precisely to answer this with minimum sunk cost.
- **Patch cadence vs retraining cadence** — if patches land during a tournament, retraining mid-tournament needs care.
- **Account funding for meaningful sizing** — $21 current balance; meaningful v1 paper trading is fine, but post-validation real trading requires deliberate top-up.

## 14. Success criteria for v1

v1 is a success if, after the paper-trading period:
1. The full pipeline (logger → model → trader → UI) runs unattended for at least 2 weeks without intervention.
2. Backtest passes the Layer 2 gates above.
3. Paper trading produces an honest signal — positive PnL with calibration matching backtest, OR a clear negative result that tells us the edge doesn't exist before we've spent money.

Either outcome is a successful v1. A negative result that saves us from building v2/v3 on a non-existent edge is more valuable than continuing to invest blind.

---

**End of design spec.**
