# LoL Trading Bot v1 — Implementation Plan

**Date:** 2026-05-20
**Spec:** [2026-05-19-lol-trading-bot-design.md](../specs/2026-05-19-lol-trading-bot-design.md)
**Estimated total:** 4–6 weeks full-time, AI-assisted
**Terminal state:** paper-trading bot meeting Section 10 "go live" gate criteria

---

## How to read this plan

Eight phases. Each phase has:
- **Goal:** what we get out of it
- **Tasks:** numbered, in order; checkable
- **Dependencies:** other phases / external prereqs
- **Deliverable:** verifiable artifact at the end of the phase
- **Acceptance:** how we know the phase is done

Phases are roughly sequential, but Phase 2 (Oracle's Elixir ETL) and Phase 4 (Kalshi WebSocket) can be interleaved with their predecessors.

Don't start a phase until its predecessor's acceptance criteria are green. Skipping ahead is how we end up with a bot that "works" but lies to us.

---

## Phase 0: Project skeleton (½ day)

**Goal:** Repo / module structure that everything else snaps into.

**Tasks:**
1. Create directory structure under `C:\Users\chess\Desktop\LoLTradingBot\`:
   ```
   src/
     loltrader/          ← Python package
       __init__.py
       config.py         ← all configurable thresholds + key paths
       db.py             ← SQLite connection, schema migrations
       kalshi/           ← Kalshi client (REST + WS)
       oracle/           ← Oracle's Elixir ETL
       features/         ← feature engineering
       model/            ← train + serve
       backtest/
       trader/           ← live trading loop
       ui/               ← Streamlit app
       util/             ← logging, time helpers
   tests/
   data/                 ← SQLite DB + raw CSVs (gitignored)
   models/               ← trained artifacts (gitignored)
   logs/                 ← JSON log stream (gitignored)
   docs/superpowers/
     specs/
     plans/
   research/             ← exploration scripts (already exists)
   ```
2. Initialize git, write `.gitignore` (excludes `data/`, `models/`, `logs/`, `.venv/`, `*.pem`, any path to `.kalshi`).
3. Write `pyproject.toml` with deps: `requests`, `cryptography`, `websockets`, `xgboost`, `lightgbm`, `scikit-learn`, `pandas`, `numpy`, `streamlit`, `pytest`, `ruff`, `mypy`.
4. Move existing `research/kalshi_client.py` content into `src/loltrader/kalshi/rest.py` (factor properly, will be reused).
5. Set up `pytest` config, basic CI-style local script (`make test` or `nox`).

**Dependencies:** none.
**Deliverable:** empty-but-correct project skeleton, `pip install -e .` succeeds, `pytest` runs (no tests yet).
**Acceptance:** can `import loltrader` from venv; `git status` clean.

---

## Phase 1: Database + Kalshi corpus logger (3–4 days)

**Goal:** Daily logger that builds the historical Kalshi corpus, idempotent and restart-safe.

**Tasks:**
1. `db.py`: SQLite connection helper. WAL mode. Foreign keys on. Single `migrate()` function that applies versioned schema files from `src/loltrader/db/migrations/`.
2. Schema migration `001_kalshi.sql`: tables `kalshi_markets`, `kalshi_candles`, `kalshi_book_snapshots`, `kalshi_events` (mostly metadata).
3. `kalshi/rest.py`: clean up existing client, add helpers for paginated `/events`, `/markets`, `/markets/{ticker}/candlesticks`, `/markets/{ticker}/orderbook`, `/portfolio/balance`.
4. `kalshi/corpus.py`: `snapshot_all_lol_markets()` — pulls open + settled LoL events, syncs to DB. Idempotent (UPSERT on `market_ticker`).
5. Candlestick chunking: for any market open > ~3.5 days, chunk into multiple requests staying under the 5000-bar/request limit.
6. `tools/daily_logger.py`: entry point. Runs `snapshot_all_lol_markets()`, logs to `logs/`. Hookable into Windows Task Scheduler.
7. Tests:
   - Mock Kalshi responses; verify UPSERT correctness on repeated runs (no dupe rows).
   - Verify chunked candlestick fetches assemble correctly.
   - Verify integer-cents conversion (`"$0.4500"` → `45`).

**Dependencies:** Phase 0.
**Deliverable:** `python -m loltrader.tools.daily_logger` populates SQLite with all current LoL market data.
**Acceptance:** Two consecutive runs produce no new rows (idempotency). DB has ≥ 3 months of `KXLOLGAME` candles. Schema diagrams generated.

---

## Phase 2: Oracle's Elixir ETL + match-to-market linkage (4–5 days)

**Goal:** Pro-match data in DB, reliably linked to Kalshi markets.

**Tasks:**
1. Download Oracle's Elixir annual CSVs (2024, 2025, 2026 YTD) into `data/raw/oracle/`.
2. Schema migration `002_oracle.sql`: `patches`, `teams`, `players`, `matches`, `match_games`, `match_drafts`, `match_player_stats`.
3. `oracle/etl.py`:
   - Parse CSVs (one row per player per game in Oracle's Elixir format)
   - Group into matches and games
   - Canonicalize team names (build initial `team_aliases` table from observed variants)
   - Extract drafts from the pick columns
   - Idempotent INSERT (skip if `match_id` already exists)
4. Schema migration `003_linkage.sql`: `team_aliases`, `market_match_links`, `manual_review`.
5. `kalshi/linkage.py`:
   - Parse `event_ticker` into (date, team_abbrev_a, team_abbrev_b)
   - Map abbreviations through `team_aliases` to canonical team names
   - Score and write `market_match_links` per the spec's confidence rubric
   - Unlinked or low-confidence → `manual_review`
6. Linkage backfill: run linkage on every existing Kalshi market in DB.
7. Manual review CLI: `python -m loltrader.tools.review_links` — shows unlinked markets, lets you type the matching canonical names; writes alias rows and re-runs linkage.
8. Tests:
   - Parse a handful of real `event_tickers`, assert (date, abbrev_a, abbrev_b)
   - Verify confidence scoring across the 5 cases in the spec
   - Round-trip: Oracle CSV row → matches table → linkage → looking up linked match yields original row

**Dependencies:** Phase 1.
**Deliverable:** Both data sources unified. `SELECT count(*) FROM market_match_links WHERE confidence >= 0.7` shows ≥ 80% of recent Kalshi markets linked.
**Acceptance:** Linkage rate ≥ 80% on settled markets in the last 30 days. Manual review CLI works.

---

## Phase 3: Feature engineering (5–7 days)

**Goal:** `compute_features(match_id, as_of=t, draft=None) → dict` producing the v1 feature set.

**Tasks:**
1. `features/team_strength.py`: Glicko-2 implementation, walks `matches` chronologically to compute per-team rating at any timestamp. Cached results.
2. `features/recent_form.py`: rolling 5/10/20-game winrates, patch-specific winrates.
3. `features/roster.py`: player Glicko-2, roster-instability flag (any player joined < 30 days ago).
4. `features/matchup.py`: H2H windows.
5. `features/meta.py`: patch days, league one-hot, format, playoff flag.
6. `features/schedule.py`: rest days, back-to-back flag.
7. `features/draft.py`: per-champion Glicko-2, player-on-champion winrate, composition archetype (start with hand-coded rules: scaling/teamfight/pick/poke based on champion tags).
8. `features/lane_matchup.py`: counter-pick winrates per role from historical drafts.
9. `features/__init__.py`: top-level `compute_features(...)` orchestrator. **Hard runtime assertion:** every data source read inside must filter by `<= as_of` timestamp.
10. `features/test_no_leak.py`: dedicated test file that constructs synthetic future-leak attempts and asserts they raise.
11. Tests:
   - Glicko outputs are reproducible (deterministic)
   - Recent form computes the right windows
   - `compute_features` returns a fixed-shape dict (same keys every call)
   - No-leak test: feature values for match X with `as_of=match_X.date − 1s` cannot differ between two calls with different `as_of` futures

**Dependencies:** Phase 2.
**Deliverable:** `compute_features(match_id, as_of=t)` returns ~30–50 features deterministically.
**Acceptance:** No-leak tests pass. Feature distribution sanity check: Glicko ratings, recent form, etc., look reasonable when spot-checked on known teams.

---

## Phase 4: Kalshi WebSocket client (3–4 days, can run parallel to Phase 3)

**Goal:** Persistent WS subscription that produces a queryable in-memory snapshot of all subscribed markets, with proper reconnect/resync handling.

**Tasks:**
1. `kalshi/ws.py`:
   - Async WS connection with the same RSA-PSS signed handshake auth as REST
   - Subscribe to channels: `ticker_v2`, `orderbook_delta`, `fill` (last for v3, but stub now)
   - Maintain in-memory dict: `market_state[market_ticker] = MarketState(...)` with current bid/ask/last/volume/orderbook
   - Apply deltas correctly; on snapshot, replace state; on delta, mutate
2. Reconnect logic: exponential backoff, on reconnect re-subscribe and request snapshots before applying any deltas (avoid silent drift).
3. Sequence number tracking — if a delta's seq isn't expected_next_seq, hard re-subscribe.
4. Heartbeat / freshness tracking: `last_message_ts[market_ticker]`. Exposed to risk module.
5. Periodic persistence: every 60s, snapshot subscribed-market orderbooks to `kalshi_book_snapshots`. (Persistence is throttled, not every delta.)
6. Tests:
   - Unit-test delta application against synthetic message streams
   - Reconnect path: kill connection mid-stream, verify re-subscribe and snapshot replace
   - Sequence-gap detection

**Dependencies:** Phase 1 (DB).
**Deliverable:** `from loltrader.kalshi.ws import live_market_state; state = live_market_state["KXLOLGAME-..."]` returns fresh data while subscribed.
**Acceptance:** 10-minute soak test holding a subscription with no drops; `last_message_ts` consistently < 5s old during active hours.

---

## Phase 5: Model training + calibration (5–7 days)

**Goal:** `train.py` produces a versioned, calibrated model artifact. `model_serve.py` loads it and serves `predict(...) → (yes_prob, p10, p90)`.

**Tasks:**
1. `model/dataset.py`: builds training matrix by iterating `matches` chronologically, calling `compute_features(match_id, as_of=match.date)`, joining with binary label.
2. `model/train.py`:
   - Walk-forward fold generator (configurable window sizes)
   - Trains three boosters: median (default), p10, p90 (quantile regression)
   - Log-loss objective for the median model
   - Time-decay sample weights
   - CV grid for: learning rate, max depth, num rounds, decay constant
   - Picks best hyperparameters by mean out-of-fold log loss
3. `model/calibrate.py`: isotonic regression fit on out-of-fold predictions, applied as post-hoc layer.
4. `model/metrics.py`: Brier, ECE, reliability diagram (matplotlib), per-fold report.
5. Artifact format: `pickle` containing `{model, calibrator, feature_spec, training_metadata}`. Filename: `models/v1_<patch>_<utc_ts>.pkl`.
6. `model/serve.py`: `Model.load(path)`, `Model.predict(features) → (yes_prob, p10, p90)`. Validates feature dict shape against spec; raises on shape mismatch.
7. CLI: `python -m loltrader.model.train` produces an artifact.
8. Tests:
   - Train on a tiny synthetic dataset; verify artifact loads and predicts
   - Verify calibration: synthetic data with known noise → reliability diagram on diagonal
   - Verify quantile relationship: p10 ≤ median ≤ p90 always

**Dependencies:** Phase 3.
**Deliverable:** First trained artifact. Reliability diagram saved to `models/v1_<patch>_<utc_ts>_reliability.png`.
**Acceptance:** Brier < 0.20 and ECE < 0.05 on walk-forward folds. Reliability diagram visually close to the diagonal.

---

## Phase 6: Backtest framework (4–5 days)

**Goal:** Replay historical Kalshi candle data against the model, simulate trades, output Section 10 backtest metrics.

**Tasks:**
1. `backtest/sim.py`:
   - Iterate over settled LoL markets, walk forward through candles
   - At each candle close: `features = compute_features(match_id, as_of=candle.end_ts)` (hard `as_of` enforcement)
   - `p_model = model.predict(features)`
   - Compare against `candle.yes_ask_close / 100`
   - If `edge > threshold(uncertainty)`: simulate trade — fill at `yes_ask_close + 1¢ slippage`, subtract Kalshi fees
   - At market resolution: close to 100¢ or 0¢
2. `backtest/portfolio.py`: position-sizing module reused from spec (0.25× Kelly + caps). Tracks correlated exposure across markets on same match.
3. `backtest/metrics.py`: Total PnL, Sharpe, Max DD, Win rate, Brier on traded probs only, edge realization, per-market profitability.
4. `backtest/report.py`: writes a markdown report with metrics + reliability diagram + PnL curve.
5. Per the spec: ≥ 200 trades = "validated"; < 200 = "directional".
6. Tests:
   - Run on a tiny synthetic market set; verify deterministic PnL
   - Future-leak assertion: corrupted call with `as_of` in the future raises
   - Slippage and fee math match expected

**Dependencies:** Phases 1, 2, 3, 5.
**Deliverable:** `python -m loltrader.backtest.sim --model models/v1_*.pkl` produces a backtest report.
**Acceptance:** Report generates without errors. Edge realization correlation is positive (predicted edge correlates with realized PnL).

---

## Phase 7: Trader + risk gates + live UI (5–7 days)

**Goal:** End-to-end paper-trading bot with manual override UI.

**Tasks:**
1. `trader/gates.py`: implements every Section 9 pre-trade gate. `validate_decision(d) → Reason | None`.
2. `trader/sizing.py`: position sizing reused from backtest portfolio module.
3. `trader/loop.py`:
   - On startup: load model, connect WS, identify markets in trading window (any market with `now < close_time < now + 14d` AND linked match within 24h of `close_time`)
   - Subscribe to those markets via WS
   - On each market state update OR every 5 seconds: compute features → predict → decide → gate → record decision → place paper trade if approved
   - Reload model on file change (poll `models/` dir every minute)
4. `trader/paper.py`: simulates fills (uses current `yes_ask` + 1¢), creates `paper_trades` rows. Settles on market resolution (polled from REST/WS).
5. `trader/killswitch.py`: monitors `data/KILL_SWITCH` file, drawdown thresholds, data freshness. Implements soft/hard/emergency kills.
6. `ui/app.py`: Streamlit. Live tables of:
   - Currently-active markets with: model prob, p10/p90, current bid/ask, edge, proposed action
   - Recent decisions log
   - Recent paper trades + cumulative PnL
   - Three buttons per active market: Accept, Skip, Override (with size/side inputs)
7. `ui/state.py`: reads from the same SQLite (read-only connection) + in-memory market state from the WS module.
8. `tools/run_bot.py`: starts trader loop + WS subscriber + scheduled corpus logger as one process (or three with a supervisor — pick at impl time).
9. Logging:
   - `logs/trader.jsonl` JSON lines, daily rotation
   - Every decision/gate/fill/error logged with `decision_id` correlator
10. Tests:
    - All pre-trade gates: synthetic inputs to verify each fires correctly
    - Trader loop end-to-end with mocked WS + mocked model
    - Kill switch: each level triggers correctly

**Dependencies:** Phases 4, 5, 6.
**Deliverable:** `python -m loltrader.tools.run_bot` starts the full system; UI accessible at http://localhost:8501; paper trades populate DB.
**Acceptance:** Runs for 1 hour unattended without crashes. Can place an Accept paper trade through UI and see it land in DB. Kill switch works.

---

## Phase 8: Paper-trading validation (≥ 4 weeks, can extend further)

**Goal:** Satisfy Section 10 Layer 3 acceptance: ≥ 50 closed paper trades meeting all "go live" criteria.

**Tasks:**
1. Run the bot continuously through 4+ weeks of pro games.
2. Weekly:
   - Pull last week's paper trades + decisions
   - Regenerate reliability diagram on these out-of-sample trades
   - Compute paper Sharpe / Brier / ECE
   - Compare to backtest predictions; flag if delta > 30%
3. After each LoL patch:
   - Trigger retraining via `train.py`
   - Re-run backtest
   - Re-run Layer 1 unit tests
   - Compare new model's calibration to previous; if drift unacceptable, hold trading pending investigation
4. Each Friday: short markdown post-mortem in `docs/postmortems/YYYY-WW.md` — what went well, what went weird, anything to investigate.
5. If at any point a single paper trade loses > 15% of bankroll OR cumulative paper PnL goes negative beyond expected variance: pause, investigate, *do not* push to live.

**Dependencies:** Phase 7.
**Deliverable:** `paper_trades` table has ≥ 50 closed rows; all "go live" gate criteria green.
**Acceptance:** All criteria in Section 10 "go live" gate green. User reviews and either:
- Proceeds to v2 (live data layer), OR
- Flips `live_trading: true` for v3-style autonomous live (depends on whether v2 was started in parallel).

---

## Phase order rationale

```
0 ─ 1 ─ 2 ─ 3 ─ 5 ─ 6 ─ 7 ─ 8
        │       │
        └── 4 ──┘   (WS can run parallel to Phases 2–3)
```

- Phase 4 (WS) is independent of feature/model work and can be developed in parallel
- Phase 5 (model) requires Phase 3 (features)
- Phase 6 (backtest) requires both model and features
- Phase 7 (trader) requires WS, model, and backtest (sizing/portfolio code reused)

## Time budget (full-time, AI-assisted)

| Phase | Days |
|---|---|
| 0 — skeleton | 0.5 |
| 1 — DB + corpus logger | 4 |
| 2 — Oracle ETL + linkage | 5 |
| 3 — feature engineering | 7 |
| 4 — WebSocket client (parallel) | 4 |
| 5 — model + calibration | 7 |
| 6 — backtest | 5 |
| 7 — trader + UI | 7 |
| **Sequential subtotal** | **35.5 days ≈ 5 weeks** |
| 8 — paper validation | ≥ 4 weeks (concurrent with stop-the-world bug fixes) |

Realistic v1 ship: **5 weeks build + 4–8 weeks paper validation = 9–13 weeks** before the "go live" decision is even on the table. This is the honest number. If anything compresses, it'll be specific phases where AI tooling shaves time on boilerplate — but model training, calibration, backtest correctness, and especially debugging cannot be compressed by writing code faster.

## What success looks like at end of v1

- A bot that runs unattended, paper-trades every LoL pro match, and logs everything
- A backtest that passes the Section 10 Layer 2 gates
- ≥ 50 paper trades with calibration matching backtest
- An honest answer to the question "does the edge exist?" — yes or no

A "no" answer is success. It means the project saved us months of v2/v3 work building on a non-existent edge.

## What we do after v1 succeeds (preview, not part of this plan)

- **v2:** add live in-game data via Riot Spectator API (3-min delay, free), train live in-game model, paper-trade live
- **v3:** wire live model to trader for autonomous live trading; ramp real money per Section 3
- **v4 (only if justified):** sub-3-min data (in-client spectator or paid feed)

Each of these gets its own design spec + implementation plan when its time comes.

---

**End of v1 implementation plan.**
