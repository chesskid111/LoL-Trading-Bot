# Comp Evaluation Engine — Implementation Plan

**Date:** 2026-06-06
**Spec:** [docs/superpowers/specs/2026-06-06-comp-evaluation-engine-design.md](../specs/2026-06-06-comp-evaluation-engine-design.md)
**Status:** Ready for execution

This plan converts the comp evaluation engine spec into a phase-by-phase build. Each phase produces independently testable artifacts. Phases run sequentially (dependencies forward only).

---

## Phase 1: Champion profile bootstrap (3 days)

**Goal:** Get a working `data/champion_profiles.json` for the top 50 most-picked champions with both qualitative dimensions (LLM-aggregated) and pro stats (auto-scraped).

### Step 1.1: Schema + loader (~3 hours)

**Create:**
- `data/champion_profiles.json` (initial empty structure)
- `data/lane_matchups.json` (initial empty structure)
- `src/loltrader/comp/__init__.py`
- `src/loltrader/comp/profiles.py` — schema definitions + load/save helpers

**Tasks:**
- Define `ChampionProfile` dataclass matching the spec schema
- Implement `load_profiles()` returning `dict[str, ChampionProfile]`
- Implement `save_profiles(profiles)` with schema validation
- Add basic loader unit tests with a 3-champion fixture

**Acceptance:**
- Can load + save without data loss
- Schema validation rejects malformed entries
- Tests pass

### Step 1.2: Pro stats ETL (~5 hours)

**Create:**
- `src/loltrader/comp/pro_stats.py` — gol.gg scraper + Oracle's Elixir queries
- `src/loltrader/tools/refresh_patch_stats.py` — CLI entry for cron job

**Tasks:**
- Scraper for gol.gg champion list page filtered by patch + league
- For each champion: pickrate, banrate, winrate, priority score, games sampled
- Oracle's Elixir queries for cross-validation (already have data in DB)
- Combine into `data/patch_stats.json` keyed by `(champion, league, patch)`
- Merge into `champion_profiles.json` (`pro_stats` sub-block)

**Acceptance:**
- Running `python -m loltrader.tools.refresh_patch_stats --patch 26.10` produces `data/patch_stats.json` with ≥ 50 champion entries
- Pro winrate values match gol.gg manually for 5 spot-check champions

### Step 1.3: LLM-assisted qualitative bootstrap — 20-champion prototype (~6 hours)

**Create:**
- `src/loltrader/comp/llm_curator.py` — prompt builder + LLM client + JSON validator
- `src/loltrader/tools/bootstrap_profiles.py` — CLI entry
- `docs/research/llm_curation_validation.md` — accuracy results

**Tasks:**
- Select 20 most-played champions from `patch_stats.json` (sorted by pickrate)
- Build LLM prompt per champion (per spec section "LLM prompt template")
- Call Claude or GPT-4 with web search enabled, expecting JSON output
- Parse + validate output against `ChampionProfile` schema
- Save to a separate `data/champion_profiles_llm_draft.json` for review

**Tasks (manual, ~2 hours):**
- Compare LLM output against your own intuition + pro analyst content
- Score accuracy per dimension (% within ±1 of reference)
- Decision gate: ≥ 80% → proceed to Phase 1.4; < 80% → refine prompt, retry

**Acceptance:**
- 20-champion LLM run completes within $1 of API cost
- Validation results documented in `docs/research/llm_curation_validation.md`
- Decision to proceed or refine documented

### Step 1.4: Full 170-champion bootstrap (~4 hours)

**Conditional on Phase 1.3 passing validation.**

**Tasks:**
- Run LLM curator on all ~170 champions
- Validate each against `patch_stats.json` (statistical disagreement flag)
- Generate validation report of flagged disagreements
- Manual review of flagged entries (~20-30 typically)
- Commit final `data/champion_profiles.json`

**Acceptance:**
- All 170 champions have entries with confidence ≥ 0.5
- < 5% of entries have unresolved validation flags
- Total LLM cost ≤ $10

### Step 1.5: Per-patch refresh workflow (~3 hours)

**Create:**
- `src/loltrader/tools/refresh_profiles.py` — patch update CLI

**Tasks:**
- CLI takes `--patch X.Y` argument
- Re-runs pro stats refresh
- Re-runs LLM curator (with prior patch's profiles as context for stability)
- Diffs against prior patch, highlights significant changes (>30% pickrate shift, scaling tier change ≥1)
- Outputs a manual review markdown report

**Acceptance:**
- Running `python -m loltrader.tools.refresh_profiles --patch 26.11` completes in < 90 min
- Diff report highlights ≥ 3 significant meta changes for spot-checking
- After spot-check, updated `champion_profiles.json` ready for use

**Phase 1 deliverables:**
- `data/champion_profiles.json` with ~170 champion entries
- `data/patch_stats.json` with current patch pro stats
- ETL CLIs for automated refresh
- Validation report from LLM bootstrap

---

## Phase 2: Comp aggregator + matchup (3 days)

### Step 2.1: Comp aggregator (~5 hours)

**Create:**
- `src/loltrader/comp/aggregator.py` — `evaluate_comp()` implementation
- `tests/test_comp_aggregator.py`

**Tasks:**
- Implement `CompProfile` dataclass per spec
- `evaluate_comp(picks, patch, players)` returning `CompProfile`
- Scaling curve evaluator (linear interp between early/mid/late points across minutes 0-40)
- Archetype classifier (reuse existing `features/draft.py` logic)
- Synergy table loader from `data/synergies.json` (manual curation, ~50 entries)
- Player×champion comfort overrides (use gol.gg per-player data from Phase 1)

**Acceptance:**
- Given a known comp (e.g., Yorick/Vi/Ahri/Lucian/Milio), produces consistent CompProfile
- Scaling curve is monotonically reasonable (no wild swings)
- Archetype classification matches manual judgment on 10 test comps
- Tests pass

### Step 2.2: Lane matchup data builder (~4 hours)

**Create:**
- `src/loltrader/comp/matchup_data.py` — gol.gg lane matchup scraper
- `src/loltrader/tools/refresh_matchups.py` — CLI

**Tasks:**
- Scrape gol.gg matchup tables for each role, filtered by region
- Compute alternative from Oracle's Elixir match data (cross-validation)
- Apply Bayesian shrinkage for low-sample matchups (<5 games → neutral 0.5 with weight)
- Save to `data/lane_matchups.json` keyed by `(role, patch, champ_a, champ_b)`

**Acceptance:**
- `python -m loltrader.tools.refresh_matchups --patch 26.10` populates ≥ 500 matchup entries
- Spot-check 5 known matchups against gol.gg manually

### Step 2.3: Matchup evaluator (~3 hours)

**Create:**
- `src/loltrader/comp/matchup.py`
- `tests/test_matchup.py`

**Tasks:**
- `lane_matchup(champ_a, champ_b, role, patch)` — read from `lane_matchups.json`
- `comp_matchup(comp_a, comp_b, minute)` — combines scaling curves + matchup advantages
- `crossover_minute(comp_a, comp_b)` — finds the minute where dominance flips

**Acceptance:**
- Given GEN vs HLE game 1 picks (Naafiri/Annie/Karma vs Caitlyn/Senna/Bard), produces a crossover around minute 25 ± 3
- Lane matchups return expected values for 5 spot-check cases
- Tests pass

**Phase 2 deliverables:**
- Working `evaluate_comp()` + `comp_matchup()` + `crossover_minute()`
- `data/lane_matchups.json` populated
- `data/synergies.json` populated (~50 entries)
- Unit tests covering the layer

---

## Phase 3: Live state integrator (2 days)

### Step 3.1: Frame loading helpers (~3 hours)

**Create:**
- `src/loltrader/winprob/__init__.py`
- `src/loltrader/winprob/state.py` — `integrate_state()` implementation
- `tests/test_state_integrator.py`

**Tasks:**
- `load_frame(game_id, minute)` reads from `live_frames` + `live_frames_details`
- `compute_objective_state(frame)` returns dragon_diff, baron_state, soul_state, etc.
- `compute_item_progression(details)` returns # completed items per side + carry item milestones
- `time_to_next_baron(frame, minute)` heuristic from spawn timer

**Acceptance:**
- Given a real frame from tonight's GEN/HLE game, produces all state features without crashes
- Item progression matches manual inspection of `live_frames_details` rows
- Tests pass

### Step 3.2: Full state integrator (~4 hours)

**Tasks:**
- `integrate_state(comp_eval, frame, details, minute)` per spec contract
- Combines: state features (15), comp features (25), team/player features (15), item-progression features (10), interaction features (10)
- Output: flat `dict[str, float]` of ~75 features
- Handles missing/null gracefully (NaN imputation or sensible defaults)

**Acceptance:**
- Producing feature dict for 100 historical frames takes < 5s
- Feature dict has no NaN values for in_game frames
- Schema is stable across runs (same keys every time)

### Step 3.3: Pre-game features (~2 hours)

**Tasks:**
- `integrate_pregame(comp_eval, draft, players, teams)` for pre-game prediction (no live state)
- Uses only static features: comp eval, team Glicko, player ratings, matchup data
- Matches the feature schema of the live integrator (state features set to "pre-game" sentinel values)

**Acceptance:**
- Pre-game feature dict has same schema as live (state features are 0 or sentinel)
- Can be fed to the model interchangeably

**Phase 3 deliverables:**
- Working `integrate_state()` and `integrate_pregame()`
- Feature schema documented in `src/loltrader/winprob/state.py` docstrings
- Unit tests

---

## Phase 4: Win-probability model (5 days)

### Step 4.1: Training data pipeline (~6 hours)

**Create:**
- `src/loltrader/winprob/dataset.py` — training data assembly
- `src/loltrader/tools/build_winprob_dataset.py` — CLI

**Tasks:**
- Pull Match-V5 timelines for last 6 months across LCK/LPL/LCS/LEC/LCP/LTA-S
- For each game, sample frames at minutes [5, 10, 15, 20, 25, 30, 35]
- Apply `integrate_state()` to each sampled frame → training row
- Label: blue won (1) or red won (0)
- Save to `data/winprob_training.parquet`

**Acceptance:**
- Dataset has ≥ 100,000 training rows (5,000+ games × ~25 frames per game)
- Feature schema consistent across rows
- No NaN labels

### Step 4.2: Model training + ensemble (~6 hours)

**Create:**
- `src/loltrader/winprob/train.py` — XGBoost training + ensemble
- `src/loltrader/winprob/model.py` — `LiveWinProbModel` class
- `src/loltrader/tools/train_winprob.py` — CLI

**Tasks:**
- 5-fold time-based cross-validation (no future leakage)
- XGBoost with L1 regularization for feature selection
- 10-member ensemble with bootstrap sampling
- Save trained model to `models/winprob_<timestamp>.pkl`

**Acceptance:**
- Training completes in < 90 minutes on local CPU
- Holdout accuracy ≥ 70% pre-game, ≥ 85% late-game (per spec targets)
- Feature importances are sensible (gold_diff, scaling_diff among top 10)

### Step 4.3: Calibration (~4 hours)

**Create:**
- `src/loltrader/winprob/calibrate.py`

**Tasks:**
- Isotonic regression on holdout predictions
- Calibration plot generation (predicted vs actual frequency)
- Per-bucket calibration error report

**Acceptance:**
- Brier score ≤ 0.20 pre-game, ≤ 0.10 mid+late (per spec)
- Max bucket calibration error ≤ 5%
- Calibration plot saved to `models/calibration_<timestamp>.png`

### Step 4.4: Model serving + uncertainty (~3 hours)

**Tasks:**
- `LiveWinProbModel.load(path)` and `predict(state)` returning `WinProbPrediction`
- Compute p10/p90 from ensemble for uncertainty band
- Top-N feature contribution via SHAP or simpler importance method
- `symlink models/winprob_latest.pkl → models/winprob_<latest_timestamp>.pkl`

**Acceptance:**
- `model.predict(state_dict)` returns `WinProbPrediction` in < 10ms
- Band width is sensible (narrower late-game, wider pre-game)
- Tests pass

### Step 4.5: Backtest framework (~5 hours)

**Create:**
- `src/loltrader/winprob/backtest.py`
- `src/loltrader/tools/backtest_winprob.py` — CLI

**Tasks:**
- Replay historical games minute-by-minute through model
- Compare model prediction to actual Kalshi market price (if available historically)
- Apply hypothetical Kelly sizing rules → compute P&L
- Output: report with hit rate, drawdown, Sharpe, calibration over time

**Acceptance:**
- Backtest on last 30 days of games produces positive Sharpe ≥ 0.5
- Max drawdown ≤ 25%
- Calibration holds over the test period (no major drift)

**Phase 4 deliverables:**
- Trained, calibrated, validated `models/winprob_latest.pkl`
- Backtest report demonstrating edge
- Reproducible training pipeline

---

## Phase 5: API + dashboard wiring (2 days)

### Step 5.1: API endpoint (~3 hours)

**Modify:**
- `src/loltrader/api/main.py` — add `/api/live_winprob/{market_ticker}`
- `src/loltrader/api/main.py` — modify `lifespan` to load model on startup

**Tasks:**
- New endpoint takes `market_ticker`, resolves to game (via market_match_links)
- Looks up latest live_frame + details + comp eval
- Computes feature dict and runs model
- Returns `WinProbPrediction` as JSON + edge vs current market price

**Acceptance:**
- `curl /api/live_winprob/KXLOLGAME-...` returns valid prediction for any active market
- Latency < 100ms

### Step 5.2: WS frame integration (~3 hours)

**Modify:**
- `src/loltrader/api/broker.py` — extend `_frame_push_pump` to compute winprob for each frame

**Tasks:**
- For each new live_frames row, compute winprob using current state + cached comp eval
- Include `winprob`, `p10`, `p90`, `edge_buy_yes`, `edge_buy_no` in the WS push payload
- Cache comp eval per game (one-time per game, reused per frame)

**Acceptance:**
- New `game_frame` WS messages include winprob fields
- Frame push latency stays under 300ms

### Step 5.3: Dashboard display (~3 hours)

**Modify:**
- `src/loltrader/api/static/app.js` — update game strip with winprob + edge
- `src/loltrader/api/static/app.css` — styling for new columns

**Tasks:**
- Strip shows: clock | score | gold_diff | model_winprob | edge_yes | edge_no
- Color-code edge: green > +5¢, red < -5¢
- Tooltip on model_winprob shows confidence band

**Acceptance:**
- Strip displays for any active game with model prediction
- Edge highlighting works
- Tooltip shows band width

### Step 5.4: Trade signal alerts (~3 hours)

**Create:**
- `src/loltrader/winprob/signals.py` — signal generator
- `src/loltrader/api/main.py` — `/api/signals` endpoint

**Tasks:**
- Signal generator scans all active markets for |edge| > 5¢ with confidence band < 20¢
- Returns list of active signals with metadata
- Push new signals via WS as toasts on the frontend
- Optional: log signals to DB for backtest validation

**Acceptance:**
- During an active game with mispricing, signals appear in dashboard within ~2.5s of livestats update
- Signal log accumulates for later analysis

**Phase 5 deliverables:**
- Working `/api/live_winprob/{ticker}` endpoint
- Frame WS pushes include winprob
- Dashboard surfaces edge per market
- Signal alerts during live games

---

## Cross-cutting concerns

### Testing strategy throughout

- **Unit tests** for each layer (Phases 1-5 each include test files)
- **Integration test** replay tonight's GEN/HLE game and walk through full pipeline; verify model behaves sensibly
- **Calibration regression test** runs daily as part of CI, alerts if drift > 5%
- **Backtest regression** runs weekly against last 7 days of games

### Deployment cadence

- Each phase commits to a feature branch, merged after acceptance criteria met
- After Phase 5, deploy to local dashboard for live observation
- Paper trade for ~2 weeks before any real-money allocation
- Real-money trading starts with strict 1% bankroll position cap

### Configuration management

- All thresholds (MIN_EDGE, MAX_BAND, KELLY_FRACTION) in `src/loltrader/config.py`
- Patch-specific data files versioned in `data/`
- Model artifacts versioned in `models/` with symlinks

### Monitoring during live use

- Daily Brier score check (model still calibrated?)
- Per-game backtest validation (model prediction was reasonable post-hoc?)
- Manual override log (when you override the model, why?) — informs feature engineering

---

## Timeline summary

| Phase | Days | Calendar weeks |
|---|---|---|
| 1. Champion profiles | 3 | Week 1 |
| 2. Comp aggregator + matchup | 3 | Week 1-2 |
| 3. Live state integrator | 2 | Week 2 |
| 4. Win-prob model | 5 | Week 2-3 |
| 5. API + dashboard | 2 | Week 3 |
| **Total** | **15** | **3 weeks** |

After ship: 2-3 weeks of paper trading + validation before real-money usage.

---

## Critical files

For quick reference during build:

- Spec: `docs/superpowers/specs/2026-06-06-comp-evaluation-engine-design.md`
- This plan: `docs/superpowers/plans/2026-06-06-comp-evaluation-engine-plan.md`
- Data substrate (already shipped):
  - `src/loltrader/db/migrations/015_live_frames_details.sql`
  - `src/loltrader/livestats/storage.py::write_frame_details`
  - `src/loltrader/livestats/poller.py` (integrated details endpoint)
- Existing relevant code:
  - `src/loltrader/features/draft.py` (archetype classifier — reuse)
  - `src/loltrader/model/serve.py` (existing pre-game model serving)
  - `src/loltrader/api/main.py` (FastAPI dashboard host)
  - `src/loltrader/api/broker.py` (WS broker — extend in Phase 5)

---

## When to come back to this plan

- After Phase 1.3 (LLM validation gate) — decision to proceed or refine
- After Phase 4.3 (calibration gate) — decision to proceed to API integration or retrain
- After Phase 5 (full ship) — paper-trade observation period before real money
- After 30+ live games — review whether Phase 2 (CV) is justified by observed edge ceilings
