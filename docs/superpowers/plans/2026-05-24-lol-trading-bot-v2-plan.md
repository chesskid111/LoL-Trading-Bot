# LoL Trading Bot v2 — Implementation Plan

**Date:** 2026-05-24
**Spec:** [2026-05-24-lol-trading-bot-v2-design.md](../specs/2026-05-24-lol-trading-bot-v2-design.md)
**Estimated total:** 10–12 weeks full-time, AI-assisted
**Terminal state:** Paper-trading live LCK system passing spec §12.2 stage-2 criteria (30 games, decision audit, positive simulated EV across ≥20 games, calibration within ±0.02 of stage-1 numbers)

---

## How to read this plan

Eleven phases. Each phase has:
- **Goal:** what we get out of it
- **Tasks:** numbered, in order; checkable
- **Dependencies:** other phases / external prereqs
- **Deliverable:** verifiable artifact at the end of the phase
- **Acceptance:** how we know the phase is done

Phases are roughly sequential. Two interleavable seams:
- **Phase 6 (VOD-replay training data)** can run in parallel with Phase 4 (CV extractors) once Phase 3 is done — both need CV but Phase 6 is offline batch and Phase 4 is realtime.
- **Phase 9 (Risk + ops)** is interleavable with Phase 7-8 — the watchdog and pager can be wired up alongside the model/trader.

Don't start a phase until its predecessor's acceptance criteria are green. Each phase ships an empirically verifiable artifact, not just code.

**Stage 3 (micro-real-money) is gated separately on Stage 2 success — not a build phase.**

---

## Phase 1: Foundation + Twitch auth (2–3 days)

**Goal:** Tools, dependencies, schema, and auth in place. v2 can boot.

**Tasks:**
1. Install system dependencies: `streamlink`, `ffmpeg`, `tesseract-ocr` (Tesseract on Windows: install via UB Mannheim installer, add to PATH).
2. Add Python deps to `pyproject.toml`: `opencv-python`, `pytesseract`, `numpy` (already in), `Pillow`, `streamlink` (CLI wrapper). PaddleOCR deferred to Phase 4 if Tesseract is insufficient.
3. Twitch auth setup:
   - User logs into twitch.tv in a normal browser
   - Export `auth-token` cookie via DevTools → Application → Cookies
   - Save to `data/twitch_creds.json` as `{"auth_token": "..."}`
   - Verify: `streamlink --twitch-api-header="Authorization=OAuth $TOKEN" twitch.tv/lck` lists qualities
4. Write `src/loltrader/twitch/auth.py`: load creds, return streamlink command-line args.
5. Schema migrations:
   - `008_v2_live_frames.sql`: `live_frames` (frame_id PK, game_id, frame_ts_unix, raw_json BLOB, parsed gold/kills/towers/etc cols), `games_live` (game_id PK, game_start_ts_unix, first_seen_ts_unix, league, blue_team_id, red_team_id)
   - `009_v2_cv_frames.sql`: `cv_frames` (frame_id, game_id, frame_ts_unix, classifier_class, classifier_confidence, ocr_gold_blue, ocr_gold_red, ocr_kills_blue, ocr_kills_red, ocr_towers, ocr_dragons, ocr_barons, ocr_timer_seconds, minimap_dots JSON, items JSON)
   - `010_v2_live_decisions.sql`: extends `decisions` with `frame_id` FK, `model_uncertainty_width REAL`, `action_type TEXT` (OPEN/ADD/HOLD/CLOSE/REVERSE), `data_source TEXT` (cv_primary/livestats_fallback/both)
   - `011_v2_live_features.sql`: `live_features_cache` (frame_id, feature_name, value) for per-frame reproducibility
   - `012_v2_risk_state.sql`: single-row `risk_state` (current_total_exposure_cents, daily_pnl_cents, session_pnl_cents, last_updated_ts) for concurrent-game accounting per spec §11.6
6. Apply migrations against a fresh dev DB, verify schema with `sqlite3 .schema`.
7. Wire `data/twitch_creds.json` into `.gitignore` (already covered by `data/*creds*.json` pattern — verify).

**Dependencies:** v1 codebase exists.
**Deliverable:** `python -m loltrader.tools.migrate` applies 008–012. `streamlink` pulls LCK channel without ads. Twitch cookie loaded by Python.
**Acceptance:** `streamlink --twitch-api-header=... twitch.tv/lck --stream-url 720p60 | head -20` returns an HLS URL. No ads in 30s test playback. Schema diagram regenerated.

---

## Phase 2: Livestats pipeline (3–4 days)

**Goal:** Always-on game discovery + per-game frame polling, writes to `live_frames` and `games_live`.

**Tasks:**
1. Refactor `research/live_lol_demo.py` into `src/loltrader/livestats/`:
   - `discovery.py`: `find_live_games()`, `probe_minimum_delay()`, `get_frame()`, `find_game_start_ts()` (binary-search version — never use the linear-scan fallback)
   - `poller.py`: long-running per-game poller, 2s cadence, writes to `live_frames`
2. `tools/game_discovery.py`: main loop, polls `persisted/gw/getLive` every 30s. League filter: LCK only. Spawns `livestats_poller.py` subprocess per detected game. Cleans up on game end (gameState=ended for >2 min).
3. **Game-start caching** (spec §6.1, §10.5): when discovery first sees a game in `state=in_game`, write to `games_live` immediately. All subsequent in-game-clock calculations read from this cache. Never re-probe.
4. Adaptive delay caching: first frame fetched probes minimum delay; cached per gameId for the duration.
5. Frame-dedup write: UPSERT on `(game_id, frame_ts)` to handle out-of-order responses (spec §17 #7).
6. Heartbeat file: poller touches `data/heartbeat/livestats_poller_{game_id}` every 10s.
7. Tests:
   - Mock Riot API responses; verify UPSERT idempotency on repeated frame_ts
   - Verify `find_game_start_ts` returns within ±10s of injected ground-truth game start
   - Verify discovery filters LCK and ignores other leagues

**Dependencies:** Phase 1.
**Deliverable:** Run discovery during LCK game time → DB has `games_live` row + ≥150 `live_frames` rows for a real LCK game with no gaps >30s.
**Acceptance:** Two consecutive game-discovery cycles produce no duplicate frames. Game-start cache survives discovery process restart. Heartbeat file updates every 10s.

---

## Phase 3: CV ingestion + frame classifier (5–7 days)

**Goal:** Pull Twitch broadcast at 1fps, classify each frame, drop garbage.

**Tasks:**
1. `src/loltrader/cv/stream.py`: wraps `streamlink + ffmpeg | OpenCV imdecode`. Returns iterator of `(timestamp, PIL.Image)` at 1fps. Use `--twitch-low-latency` (verify per spec §17 #13).
2. `src/loltrader/cv/classifier.py`: frame classifier returning one of `in_game`, `studio`, `replay`, `ads`, `unknown`.
   - **First pass: template-difference heuristic.** Maintain a small library of reference frames per class. Compute structural similarity (SSIM) between input frame and references; pick highest-similarity class.
   - If accuracy <90% in stage 1, upgrade to small CNN (Phase 4 task).
3. Reference-frame library: capture ~30 reference frames per class from LCK broadcasts (5–10 min of manual capture during a live game). Store in `data/cv_references/{class}/*.png`.
4. `tools/cv_pipeline.py`: long-running process. Per spawned game: open stream, iterate frames, classify, write `cv_frames` rows (with classifier_class + confidence). Drop frames classified as ads/studio/unknown from downstream extractors but still log the classification.
5. PNG retention: per spec §13.4 — keep frame PNGs in `data/cv_frames/{game_id}/{frame_ts}.png` for 30 days, then compressed-tar.
6. Watchdog integration: per-process heartbeat file.
7. Tests:
   - Inject known-class reference frames, verify classifier accuracy >95%
   - Verify ffmpeg pipe survives a brief Twitch HLS drop (≤5s) without crashing
   - Verify 1fps cadence holds within ±100ms

**Dependencies:** Phase 1, Phase 2 (for the spawn-by-discovery wiring).
**Deliverable:** Run cv_pipeline against live LCK → `cv_frames` rows accumulate at ~1/sec with classifier_class populated. Ads detected and labeled.
**Acceptance:** During a 15-min LCK clip, classifier_class distribution matches manual annotation within ±5% per class. No crashes on stream drop.

---

## Phase 4: CV extractors (OCR + minimap + items) (7–10 days)

**Goal:** Extract numerical and positional state from in_game frames. The single longest phase.

**Tasks:**
1. `src/loltrader/cv/regions.py`: define rectangular regions per LCK broadcast layout (calibrated from reference frames). Regions: scoreboard top-bar (gold, timer), team-scoreboard (kills, dragons, towers, barons), minimap, item slots × 10 players. Define both 1080p and 720p coordinate sets (spec §17 #2).
2. `src/loltrader/cv/ocr.py`: Tesseract wrapper with digit-only PSM (page-segmentation mode 7, whitelist `0123456789,:`). Functions: `ocr_gold(image)`, `ocr_kills(image)`, `ocr_timer(image)`. Apply CLAHE preprocessing for contrast.
3. **OCR vs livestats watchdog** (spec §6.3): writer in `cv_pipeline.py` joins each new `cv_frames` row against nearest-timestamp `live_frames` row, computes per-field divergence, logs to a new `cv_validation` table for stage-1 analysis.
4. `src/loltrader/cv/minimap.py`: template-match team-coded champion icons against minimap region. Champion icon library: download from Riot DataDragon CDN. Output: list of `(team, champion, x_normalized, y_normalized)` per frame.
5. `src/loltrader/cv/items.py`: template-match item icons against item icon library (also from DataDragon). Per-player slot detection: 6 item slots + trinket. Output: per-champion item list.
6. **PaddleOCR fallback**: if Tesseract OCR-vs-livestats divergence exceeds threshold from stage 1 calibration, swap to PaddleOCR (deferred per spec §17 #4). Implementation behind a config flag.
7. Tests:
   - Synthetic test: render known gold values as broadcast-style overlay, verify OCR reads them ±0% error
   - Real broadcast samples: hand-annotate 50 in_game frames, measure OCR accuracy per field
   - Minimap: hand-annotate 20 frames with champ positions, verify template matcher within ±5% normalized coord
   - Item detection: hand-annotate 10 frames with item slots, verify ≥90% item-icon recall

**Dependencies:** Phase 3.
**Deliverable:** During a live LCK game, `cv_frames` rows have populated ocr_*, minimap_dots, items columns. OCR-vs-livestats watchdog shows <2% divergence on gold field.
**Acceptance:** OCR-gold matches livestats gold within 2% on >98% of frames during a full game. Minimap dot detection finds ≥9/10 champs on >90% of frames. Item detection ≥85% recall.

---

## Phase 5: Feature engineering (5–7 days)

**Goal:** Per-frame 220-feature vector from CV + livestats + v1 prior, ready for model training and live serving.

**Tasks:**
1. `src/loltrader/features/live/` package. One module per category:
   - `numerical.py` — category A (gold diff, gold-per-min, K/T/I/D/B diffs, jungle objective tempo, recent-window deltas at 30/60/120s)
   - `participant.py` — category B (per-role gold lead, level diff, item count, recent-kill participation)
   - `positional.py` — category C (CV-derived team-grouping metric, vision-control quadrants, side-lane pressure, recall states, item-build-stage progression)
   - `draft.py` — category D (reuse v1 draft features)
   - `team_strength.py` — category E (reuse v1 Glicko + roster features)
   - `prior.py` — category F (v1 model output + top-25 SHAP features)
2. `assemble.py`: orchestrator. Input: a (game_id, frame_ts) row. Output: 220-length feature vector + feature-name list. Reads from `live_frames`, `cv_frames`, `matches`, `match_drafts`, v1 model output.
3. **Mirror augmentation utility:** `mirror_features(vec)` swaps blue/red sides and inverts label. Free 2× training data.
4. **Data-source flag per feature:** each feature tags its source (`cv_primary`, `livestats_secondary`, `static`). Lets us re-run with degraded inputs in tests.
5. `tests/features/live/`:
   - Assert feature count = 220 ±5 across realistic frame
   - Assert mirror augmentation is exactly symmetric (P(team_a wins | features) = 1 - P(team_b wins | mirror(features)))
   - Per-category unit tests on synthetic frames

**Dependencies:** Phase 2, Phase 4, v1 model artifact.
**Deliverable:** `compute_live_features(game_id, frame_ts)` returns a deterministic 220-vector. Feature catalog markdown auto-generated to `docs/v2_features.md`.
**Acceptance:** Feature vector is reproducible (same input → same output). Mirror augmentation passes symmetry test. v1 prior integrates without errors.

---

## Phase 6: VOD-replay training data (7–10 days)

**Goal:** Historical training corpus across ~500 LCK games × ~300 frames each = ~150k rows.

**Tasks:**
1. `src/loltrader/vod/` package.
2. `vod/fetch.py`: `yt_dlp` wrapper. Pulls LCK official YouTube VODs by date range. Saves to `data/vods/{date}/{game_id}.mp4` at highest available quality (720p minimum, 1080p preferred).
3. `vod/livestats_archive.py`: probes Riot livestats API for each historical gameId. If retention is sufficient (spec §17 #1), pulls full frame history. If not, marks game as CV-only-numerical (we'll OCR everything from VOD).
4. `vod/cv_offline.py`: runs CV pipeline against a downloaded VOD file (vs live stream). Same classifier + extractors, just fed from file:// source.
5. `vod/join.py`: per-game, join livestats frames + CV frames on nearest timestamp. Apply mirror augmentation. Produce per-frame feature vectors. Write to `live_features_cache` with `source_tag='vod_replay'`.
6. `tools/vod_pipeline.py`: orchestrator. Takes a date range, iterates, parallelizes 2-4 games at a time (CPU-bound CV is the bottleneck).
7. Manual review CLI for VODs that fail (no audio, missing frames, broadcast layout changed): `python -m loltrader.tools.review_vod {game_id}` shows what went wrong.

**Dependencies:** Phase 4, Phase 5. Can run in parallel with Phase 7-9 once Phase 5 is done.
**Deliverable:** ~150k feature-vector rows in `live_features_cache` covering LCK 2024–2026 YTD. Per-game extraction success rate ≥90%.
**Acceptance:** Mirror-augmented dataset has even class balance. Spot-check 10 random rows: features look reasonable for the game state at that timestamp.

---

## Phase 7: Live model — training + calibration + uncertainty (7–10 days)

**Goal:** XGBoost classifier with per-phase calibration and bootstrap ensemble, ready for live serving.

**Tasks:**
1. `src/loltrader/model/live/train.py`: trains single XGBoost model on Phase 6 dataset. Hyperparameters: start from v1's best_params.json as seed, re-run Optuna TPE (50 trials) on the live dataset.
2. **Group-by-match walk-forward CV**: folds time-ordered, all frames from a single match stay in same fold. Reuse v1's CV harness with adapted grouping.
3. **Per-phase calibration**: 4 isotonic calibrators for game-time buckets [0,10], (10,20], (20,30], (30,∞) minutes. Each fit on its phase-subset of training data.
4. **Bootstrap ensemble**: 30 XGBoost models trained on resampled folds. At inference time: predict from all 30, return mean + 10th/90th percentiles for uncertainty.
5. **Temporal variance contraction**: at inference, take last-5-frame predictions, compute variance across them. Combine with ensemble variance into single `uncertainty_width` value.
6. `src/loltrader/model/live/derive.py`: derived market probabilities. `P(KXLOLGAME yes) = direct`. `P(KXLOLMAP yes) = direct at map start`. `P(KXLOLTOTALMAPS) = recursive composition` per spec §8.
7. `src/loltrader/model/live/serve.py`: `class LiveModel`: `predict(feature_vec) → LivePrediction(p_yes, p10, p90, uncertainty_width)`. Loads pickle from `models/v2_latest.pkl`.
8. Tests:
   - Per-phase Brier ≤ 0.22 in each phase bucket on held-out (spec §8 target)
   - ECE ≤ 0.04 in each phase bucket
   - Derived KXLOLTOTALMAPS probabilities sum to 1.0 across all outcomes
   - Mirror invariance: model output on mirrored input = 1 - model output on original

**Dependencies:** Phase 5, Phase 6.
**Deliverable:** `models/v2_latest.pkl` artifact + `models/v2_YYYY-MM-DD_cv_report.md` with per-phase Brier/ECE plots.
**Acceptance:** All §8 target metrics pass on held-out games. CV report shows monotone calibration curves in each phase bucket.

---

## Phase 8: Live trader (5–7 days)

**Goal:** Per-frame decision loop, action thresholds, anti-churn, sizing — submitting orders to Kalshi (or paper).

**Tasks:**
1. `src/loltrader/trader/live/loop.py`: long-running process. Subscribes to "new frame" signal (poll `live_frames` for newer than last-seen frame_ts; could upgrade to SQLite triggers later).
2. Per-frame: load features → model → derive market probabilities → fetch latest Kalshi book (from v1's WS cache) → evaluate per spec §9.2 thresholds.
3. **OPEN/ADD/HOLD/CLOSE/REVERSE state machine** per market. Tracks edge-at-entry, time-since-last-action, position size.
4. **Anti-churn guards** (spec §9.4): min-hold 30s after OPEN, edge-confirmation across 2 consecutive frames for OPEN/REVERSE, per-game trade cap of 8 actions.
5. **Sizing** (spec §9.5, corrected formula): `size_v2 = size_v1 * max(0, 1 - 2 * uncertainty_width)`. Uses v1's Kelly-fraction sizing as base.
6. **Concurrent-game accounting** (spec §11.6): reads `risk_state` row before every OPEN/ADD. Atomically updates on fills.
7. **Paper mode**: `live_trading: false` config flag bypasses Kalshi order endpoints, writes simulated fills to `paper_trades` table at displayed ask/bid (no improvement — spec §12.2 conservative slippage).
8. **Live mode**: `live_trading: true` calls Kalshi REST order endpoints from v1's client.
9. Heartbeat + KILL_SWITCH check before every action.
10. Tests:
    - Simulated frame stream → assert OPEN fires only when thresholds met
    - Edge-confirmation: assert OPEN doesn't fire on 1-frame transient
    - Sizing: assert width=0 → full Kelly, width=0.5 → zero
    - Concurrent-game: assert second game's OPEN throttles when first game has saturated exposure

**Dependencies:** Phase 7. Phase 1 risk_state table.
**Deliverable:** Trader runs against live LCK game in paper mode, writes `paper_trades` rows. Decision log visible in Streamlit UI extension.
**Acceptance:** During a full LCK game (paper), trader produces 0–8 actions per market, no anti-churn violations, every decision has full provenance (frame_id, feature_vec, model_output, book_state at decision).

---

## Phase 9: Risk + ops layer (5–7 days)

**Goal:** Kill conditions, watchdog, pager, Task Scheduler, sleep config, logging. Production-ready autonomous operation.

**Tasks:**
1. `src/loltrader/risk/gates_live.py`: extends v1's `gates.py` with all spec §11.2 live-specific kills:
   - Per-game P&L stop (−15%)
   - Latency stop (decision lag >10s for 3 frames)
   - Model-market divergence stop (>40¢ for >2min on liquid market)
   - Calibration-drift watchdog (rolling-50-trade Brier vs train_brier + 0.03)
2. `tools/watchdog.py`: monitors heartbeat files for `game_discovery`, `livestats_poller`, `cv_pipeline`, `live_trader`. Restarts processes on missed heartbeat >30s. Halt + page on 3 restarts in 5 min.
3. `src/loltrader/notify/pager.py`: Twilio SMS + SMTP email. Rate-limited (max 1 page/min per trigger type). Triggers: any halt condition, 3-restart event, missing heartbeat >2 min.
4. `src/loltrader/logging.py`: structured JSONL logger per spec §11.7. Per-process daily file. Gzip after 7d, delete after 90d.
5. Windows Task Scheduler XMLs in `ops/scheduled_tasks/`:
   - `loltrader-discovery.xml` — `game_discovery.py` at boot, restart-on-fail
   - `loltrader-watchdog.xml` — `watchdog.py` at boot
   - `loltrader-morning-report.xml` — daily 8am PDT
6. Sleep config docs: `docs/superpowers/specs/operations/windows-setup.md` documents PowerCfg time-based scheme, wake timers, BIOS auto-restart.
7. `tools/morning_report.py`: writes `data/reports/YYYY-MM-DD.md`. Sections: games traded, P&L per game, halts triggered, calibration drift, disputed markets, any anomalies (>3σ on per-game P&L).
8. Disk-cleanup task: monthly compress 30-day-old PNGs, delete 90-day-old logs.

**Dependencies:** Phase 8. Can interleave starting from Phase 7.
**Deliverable:** Full ops stack running. Pager fires on a manually-triggered halt condition. Morning report renders.
**Acceptance:** Kill `livestats_poller` manually → watchdog restarts within 30s. Trigger halt → Twilio SMS arrives within 60s. Run for one LCK weekend; morning reports generated daily.

---

## Phase 10: Stage 1 validation (VOD replay) (~2 weeks)

**Goal:** Hit spec §12.1 pass criteria on held-out historical LCK games.

**Tasks:**
1. Re-run Phase 6 VOD pipeline through the now-complete CV extractors + feature assembler + model serving.
2. Holdout split: last 50 LCK games of dataset reserved for stage-1 evaluation; never used in training.
3. Per-holdout-game: replay frame-by-frame, model.predict at each frame, compute per-phase Brier, compute ECE, compute CV uptime, log OCR-vs-livestats divergence.
4. Reports: `data/reports/stage1/{run_id}/summary.md` + per-game timelines.
5. Iterate on failures: if Brier > 0.20 in a phase bucket, investigate feature drift, retrain. If CV uptime <90%, upgrade frame classifier to small CNN. If OCR divergence >2%, switch to PaddleOCR or expand region calibration to handle resolution variants.
6. Calibrate the OCR-vs-livestats watchdog threshold (spec §17 #12): histogram divergence across all stage-1 frames, set production watchdog at 99.5th percentile.

**Dependencies:** Phases 1–9 all green.
**Deliverable:** Stage-1 report demonstrating all §12.1 pass criteria met.
**Acceptance:**
- Per-frame Brier ≤ 0.20 averaged across full game ✓
- Per-phase Brier ≤ 0.22 in any bucket ✓
- CV uptime ≥ 90% across frames ✓
- OCR-livestats numerical disagreement <2% of frames, <5% magnitude ✓

If any fail: iterate on the specific failure mode before unlocking Phase 11.

---

## Phase 11: Stage 2 validation (paper-trading live) (~4 weeks)

**Goal:** 30 LCK games paper-traded with the full stack, hit spec §12.2 pass criteria.

**Tasks:**
1. **Critical pre-flight: empirical latency measurement** (spec §17 #11). For the first 3 LCK games in stage 2: user reads on-stream in-game clock at 3 random moments per game; bot logs livestats and CV frame timestamps at the same wall-clock moments. Update spec §10.6 with actual numbers. If CV lag turns out to exceed assumptions by >5s, pause stage 2 and investigate.
2. Set `live_trading: false`. Discovery + poller + CV + trader all run during LCK windows.
3. After every 5 games, user does decision audit (manual review of 20 random decisions). Document failures or unreasonable actions.
4. Continuous: track rolling Brier, per-phase Brier, simulated P&L distribution.
5. Halt conditions exercised in paper: any halt that fires gets investigated. False alarms get tuned (or threshold widened); true positives get treated as learning.
6. **Calibrate action thresholds**: after 15 games of paper data, sweep edge thresholds (3¢/5¢/7¢ for OPEN, etc.) on the recorded book history to find the EV-maximizing values. Update `config.py`.
7. After 30 games: final report. Spec §12.2 criteria check.

**Dependencies:** Phase 10 passed.
**Deliverable:** `data/reports/stage2/final.md` with 30-game audit, calibration plots, simulated P&L distribution.
**Acceptance:**
- Decision audit: 100 random sampled decisions look reasonable ✓
- Per-phase Brier within ±0.02 of stage-1 numbers ✓
- Positive simulated EV across ≥20 of 30 games ✓
- No unaddressed crashes; no false-alarm halts in last 10 games ✓

Pass → unlock Stage 3 (separate gate; not a build phase). Fail → iterate on the specific failure mode, do another 30-game run.

---

## Cross-cutting concerns (not phases, but always-on)

- **Type hints + ruff + mypy** clean throughout. v1 baseline carried forward.
- **Tests run on every commit.** No phase is "done" until tests are green.
- **Spec §10 / §17 stay live.** When we measure something or close an open question, update the spec immediately. Spec drift is how v1 ended up with the "data freshness" mistake we caught in §1.
- **Daily journal** in `docs/build/v2/journal/YYYY-MM-DD.md` — what shipped today, what blocked us, what we learned. Short notes; useful for retrospectives.
- **One PR per phase** (or per significant task within a phase). Easier to review and revert.

---

## Risk register

Risks that could blow up the timeline:

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Riot livestats retention <30 days (spec §17 #1) | Medium | Stage 1 needs full CV-only flow earlier | Test in Phase 1; if true, defer livestats join until live |
| LCK broadcast layout changes mid-build | Low | Phase 4 region calibrations break | Versioned region configs; per-patch quick recalibration runbook |
| OCR accuracy too low even with PaddleOCR | Low | CV-primary architecture untenable | Fall back to livestats-primary with size haircut accepting the freshness penalty |
| Stage 1 Brier > 0.22 in a phase bucket | Medium | Stage 2 blocked | Iterate on features; consider per-phase models instead of per-phase calibration |
| Stage 2 EV negative | Medium | Real-money gate not unlocked | Investigate adverse selection (spec §17 #9), tune thresholds, possibly retrain with stricter calibration |
| Single-machine SPOF causes overnight loss (spec §17 #8) | Low | Real-money exposure | Stay paper-only until we have a HA story, OR keep real-money caps small enough that one outage doesn't matter |

---

**End of plan. Awaiting user review.**
