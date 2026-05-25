# LoL Trading Bot — v2 Design Spec (Live In-Game Trading)

**Date:** 2026-05-24
**Owner:** mil197@ucsd.edu
**Status:** Draft, awaiting user review
**Supersedes (for live trading):** [2026-05-19-lol-trading-bot-design.md](2026-05-19-lol-trading-bot-design.md) (v1) — v1 remains valid for pre-match/draft and stays as the offline-trained component v2 builds on
**Target ship:** v2 paper-trading live in ~10–12 weeks (full-time, AI-assisted)

---

## 0. Why v2 exists (the pivot)

v1 shipped a calibrated pre-match + draft model (95 features, CV Brier 0.197 clearing the 0.20 spec gate). Empirically, **pre-match trading on Kalshi LoL markets has two structural problems v1 can't escape**:

1. **Heavy-underdog bets are fragile.** A calibrated model that says KT has a 25% chance against Gen.G is mathematically correct on average, but the Kalshi sample size per matchup is too small to bank on long-run convergence within a single split. Edges are 5–8¢; one outlier week wipes out months of expected value.
2. **The information surface pre-match is small.** Once teams are public and lines are set, you're competing on calibration alone against a market that's already integrated public information. The asymmetric advantage we *want* to exploit — reading the game as it unfolds — is unavailable to the v1 architecture.

v2 moves trading into the live window where (a) far more decisions per unit time, (b) far more information per decision, and (c) most participants have worse data than we will. The v1 model becomes a prior for the live model; v1 features remain in the feature vector.

## 1. Edge hypothesis (v2 North Star)

### What we are NOT betting on (corrected after empirical review)

The original v2 draft claimed an edge from "data freshness" because Riot's livestats API gives "free, fast" data. **That was wrong.** Empirical findings (§10):

- Riot's livestats API minimum delay (30s LCS, 45s LCK) is **deliberately calibrated to match broadcast delay**. The API is intentionally throttled so betting/data services don't gain a structural edge over stream viewers. We are not ahead of viewers on raw data freshness — we're approximately at parity, sometimes slightly behind.
- An engaged Kalshi trader watching the broadcast goes eyeball-to-fill in ~2.5–5s. Our CV→model→Kalshi pipeline goes event-on-screen-to-fill in ~2–5s. **We are at parity with engaged sharp viewers on reaction speed, not faster.**
- There is no free way to be ahead of broadcast. Paid feeds (GRID/PandaScore/Bayes) can be faster but cost €500–€2000+/mo and prohibit betting on cheap tiers — explicitly out of scope.

### Where the edge actually lives

If raw speed and freshness aren't a moat against engaged sharps, profitability depends entirely on:

1. **Synthesis quality.** A sharp viewer sees "LYON took soul, bet up." We compute "P(LYON wins) shifted from 48% → 61% based on calibrated state-space history; optimal action is BUY_YES below $0.61 at size N." Better directional accuracy + better sizing.
2. **Coverage breadth.** A sharp trades the events they personally react to (~20–40/game). We trade *every* state change across *every* market (KXLOLGAME + KXLOLMAP + KXLOLTOTALMAPS) on every game.
3. **No emotional bias / anchoring.** Sharps over-weight their team, tilt after losses, anchor on opening prices. We don't.
4. **24/7 capacity.** A sharp does ~3 hours/night. We do every game in every window.
5. **Edge against the non-sharp half of the book.** A meaningful share of Kalshi LoL volume comes from casual viewers (slower reactions, worse synthesis) and from fundamental traders not watching live. We extract from them, not from the sharps.

**The honest hypothesis:** *A model trained on broadcast-derived numerical data + CV-derived positional features + the v1 pre-match prior, executed automatically with no emotional bias and full multi-market coverage, will produce positive EV against the Kalshi LoL book by beating non-sharp participants and matching (but rarely outpacing) sharps.*

### What this implies for the spec

- **Calibration matters more than speed.** Stage 2 / stage 3 validation gates on Brier and ECE are the load-bearing checks, not latency.
- **CV is not just for positional features.** It's also our *stream-speed numerical source* — fresher than the livestats API by ~10–15s. See §6 for revised data-layer architecture.
- **There is no fallback moat.** If the model isn't actually good, no structural advantage saves us. v1's calibration discipline carries forward as a non-negotiable.

What this is **not:** sub-second arbitrage, paid-data dependent, or a research-grade teamfight evaluator. We're a calibration-and-coverage edge applied to in-game windows.

## 2. Trading style and scope

- **Style:** autonomous live in-game trading. Active position management (open / add / hold / close / reverse based on edge evolution), not buy-and-hold.
- **Leagues (v2.0):** LCK only. Single league reduces broadcast-delay variance, stream-quality variance, and ad-pattern variance. Expansion to LPL/LEC/LCS deferred to v2.1+ after LCK paper-trades positively for ≥30 games.
- **Markets traded (all three, in parallel):**
  - `KXLOLGAME` — single map / single game winner. Highest signal-per-time; resolves in 25–40 min.
  - `KXLOLMAP` — which map of the series. Updates only between maps.
  - `KXLOLTOTALMAPS` — series score outcomes (3–0, 3–1, 3–2, etc.). Derived mathematically from per-map predictions + current series state.
- **Trading window:** kickoff → game-end resolution candle (we exit before settlement to avoid the spread on resolution). No pre-match trading in v2 (v1 remains available for that if reinstated separately).

## 3. Capital and risk parameters (unchanged from v1 spec §3)

Same bankroll structure as v1 carries forward, with **live-trading-specific haircut** on sizing (see §9.5):
- **Bankroll:** $500–$2,000 risk capital
- **Max position per market:** 5% of bankroll
- **Max total exposure at any time:** 20% of bankroll across correlated markets on the same match **and across any concurrent matches** (see §11.6 for concurrent-game accounting)
- **Daily stop-loss:** −10% triggers soft kill
- **Session stop-loss:** −30% triggers emergency kill
- **Per-game P&L stop:** −15% of starting bankroll on a single game closes all positions in that game (new in v2, see §11.2)
- **Sizing:** 0.25× Kelly steady-state, ramping from 0.10× Kelly for the first month after the go-live gate clears
- **Live size haircut:** `size_live = size_v1 * (1 - 2 * uncertainty_band_width)` (see §9.5)

## 4. Roadmap (v2-specific)

| Phase | Ships | Estimated duration |
|---|---|---|
| **v2.0** | LCK live trading paper-only, all 3 market types, CV + livestats hybrid | 10–12 weeks |
| **v2.1** | Real money micro ($50/game cap) on LCK after stage-2 validation passes | 6 weeks after v2.0 |
| **v2.2** | Real money at full v1 sizing on LCK | 4 weeks after v2.1 |
| **v2.3** | Expand to LPL/LEC/LCS one at a time, each with its own validation gate | 4–6 weeks per league |

Total to LCK at full sizing: ~5–6 months from v2.0 kickoff.

---

## 5. Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                         LOCAL WIN11 MACHINE                            │
│                                                                        │
│  Always-on supervisors:                                                │
│    game_discovery.py  ── polls Riot persisted API /30s                 │
│    watchdog.py        ── heartbeat monitor, restarts crashed children  │
│                                                                        │
│  Game-driven children (spawned by game_discovery on live LCK game):    │
│                                                                        │
│     livestats_poller.py ── REST /window/{gameId} every 2s, delay=45s   │
│             │                                                          │
│             ▼                                                          │
│     ┌─────────────────────────────────────────────────────┐            │
│     │            SQLite DB (data/lol.db, WAL mode)        │            │
│     │            v1 schema + 008_v2_live_data.sql         │            │
│     └─────────┬──────────────────────────────────┬────────┘            │
│               ▲                                  │                     │
│               │                                  │                     │
│     cv_pipeline.py ── streamlink+ffmpeg+OpenCV   │                     │
│       │  Twitch HLS via session cookie           │                     │
│       │  Frame classifier → region extractors    │                     │
│       │  Minimap dot tracking + OCR              │                     │
│       │                                          ▼                     │
│       │                              live_trader.py                    │
│       │                                  │                             │
│       │                                  │ uses model + features       │
│       │                                  │ submits orders              │
│       │                                  ▼                             │
│       │                            Kalshi REST (orders) + WS (book)    │
│       │                                                                │
│       └──→ frame snapshots → data/cv_frames/ (PNG, 30d retention)      │
│                                                                        │
│  live_ui.py (Streamlit) — on-demand, read-only dashboard               │
│  pager.py            — Twilio SMS / email on halt conditions           │
└────────────────────────────────────────────────────────────────────────┘
```

**Components:**
- `game_discovery.py` — polls `persisted/gw/getLive` every 30s. On detecting a live LCK game, spawns poller/CV/trader subprocesses for that gameId. Cleans up on game end.
- `livestats_poller.py` — REST polls `feed.lolesports.com/livestats/v1/window/{gameId}?startingTime=<now − 45s>` every 2s. Writes raw frame JSON + parsed numerical features to SQLite.
- `cv_pipeline.py` — `streamlink twitch.tv/lck --hls-live-restart` piped to ffmpeg piped to OpenCV. Per-frame: classify (in-game / studio / replay / ad), extract regions (minimap, scoreboard, items), OCR text, template-match icons. Writes positional/qualitative features to SQLite. Drops out gracefully on ad frames or stream loss.
- `live_trader.py` — on each new frame write (DB trigger / poll), recompute features → model → derived market probabilities → for each linked market: evaluate OPEN/ADD/HOLD/CLOSE/REVERSE per §9.2 thresholds. Submits orders via Kalshi REST, reads book via Kalshi WS (already built in v1).
- `live_ui.py` — Streamlit dashboard (read-only). Adds to v1's UI: current frame timestamp, CV uptime indicator, model prob vs market book chart per active market, per-game P&L gauge.
- `watchdog.py` — every 10s, checks heartbeat files for each child process. Restarts on missed heartbeat >30s. Pages user on 3 restarts in 5 min.
- `pager.py` — Twilio SMS + SMTP email. Triggered by watchdog and by halt conditions in §11.

**Transport choice:** REST polling for Riot (no WS endpoint exists; 45s broadcast delay dwarfs the ~1s saved by hypothetical push), WebSocket for Kalshi book (sub-second changes matter). See §10.3.

---

## 6. Live data layer

**Data source priority (further revised after Phase 4 empirical findings — see §10.7):**

| Field type | Primary source | Notes |
|---|---|---|
| **Numerical state** (gold, kills, towers, dragons, barons, inhibs) | **Riot livestats** | 30-45s broadcast-calibrated delay; reliable; complete |
| **Positional / qualitative** (minimap positions, item builds, vision) | **CV from broadcast** | Not available in livestats at all |
| **Game timer** | Either (timer is in livestats; can also OCR clean broadcast clock) | Both usable when sane |

The original spec stance was "CV primary for everything numerical, livestats as cross-check." Phase 4 calibration revealed Tesseract OCR on the LCK broadcast font achieves only ~50-70% accuracy with frequent gross errors (e.g. 244K read instead of 2.4K). Pulling in a heavyweight engine (EasyOCR / PaddleOCR) for ~10-15s freshness gain was judged not worth the dep weight + setup complexity.

**The real value of CV is positional features that no API provides** — minimap dot tracking, item builds per player, vision-control estimation. Livestats covers numerical state more reliably than CV-OCR ever could on the current font.

CV-OCR remains useful as an optional cross-check (§6.3 watchdog still applies when OCR returns parseable values) but is not load-bearing.

### 6.1 Riot livestats (numerical, primary)

- **Endpoint:** `https://feed.lolesports.com/livestats/v1/window/{gameId}?startingTime=<UTC ISO timestamp aligned to 10s boundary>`
- **Auth:** public API key `0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z` (widely known, no rate-limit penalty)
- **Cadence:** 10s frame interval enforced server-side. Poll at 2s to minimize wait between frame-becomes-available and our fetch (so worst-case added latency is ~2s, average ~1s). **New decisions fire on new frames (so ~every 10s), not every poll.**
- **Adaptive delay:** at game start, run `probe_minimum_delay()` (already implemented in `research/live_lol_demo.py`) to find the actual broadcast delay for this stream. Cache the value for the duration of the game. Empirically LCK = 45s (verified 2026-05-24, see §10.1). Safety rails: minimum 30s (below which probe wouldn't have data anyway), maximum 600s (above which data is too stale to trade on — halt if probe returns >600s).
- **Data extracted per frame:** `rfc460Timestamp`, `gameState`, blueTeam and redTeam `totalGold`, `totalKills`, `towers`, `inhibitors`, `dragons` (list with elemental types), `barons`, plus per-participant champion / gold / kills / items / level (used for feature §7.B).
- **Schema:** `live_frames` table (frame_id PK, game_id, frame_ts_unix, raw_json BLOB, parsed JSON fields…) — migration `008_v2_live_data.sql`.
- **Game start timestamp caching:** when `game_discovery` first sees a game in `state=in_game`, persist that frame's `rfc460Timestamp` to a `games_live` table (game_id PK, game_start_ts_unix, first_seen_ts_unix). All in-game-clock calculations downstream read from this cached value — **never re-probed retrospectively**, because the window endpoint only serves a recent slice and walking back via binary search is wasteful and produces drift between runs (verified empirically 2026-05-24: a naive linear-scan heuristic gave game-times off by ~10 minutes). The cache is set-once per game.

### 6.2 Computer vision (positional features, Twitch broadcast)

CV's role in v2 (refined after Phase 4 empirical findings):
1. **Positional / qualitative source (the v1-to-v2 differentiator):** minimap dot tracking, vision-control estimation, item-build progression, lane-state inference — features that are not in any free API at any speed. THIS is where CV is load-bearing.
2. **Optional numerical cross-check via OCR:** when Tesseract returns parseable values for gold/timer, feed them to the OCR-vs-livestats watchdog (§6.3) as one more sanity check. Not relied upon for trading decisions.

OCR scope decision: gold + game_timer are the only fields with text big enough to OCR reliably (~50-70% of frames; Tesseract struggles with the LCK font's "2"/"9" digits and gold-coin icons get misread as "0"). Kills/towers/dragons/barons/inhibitors are too small to OCR cleanly and come from livestats. This is documented in [src/loltrader/cv/regions.py](../../src/loltrader/cv/regions.py).

If a future LCK season uses a more OCR-friendly font, or we adopt PaddleOCR / EasyOCR (spec §17 #4), revisit and potentially restore CV-numerical primacy.

- **Source:** `twitch.tv/lck` — user is subscribed to this channel, so ad-free playback works for user's session cookie.
- **Auth:** browser session cookie `auth-token` stored in `data/twitch_creds.json` (gitignored, file ACL restricted to user). Revoke from "Disconnect all sessions" if leaked.
- **Pipeline:** `streamlink twitch.tv/lck source --hls-live-restart --twitch-low-latency -O` → `ffmpeg -i pipe:0 -vf "fps=1" -f image2pipe -vcodec png -` → OpenCV reads PNG frames at 1 fps. The `--twitch-low-latency` flag drops viewer-side delay from ~20s to ~5s when LCK's broadcast supports it, narrowing the gap between us and game truth.
- **Frame classifier:** small CNN or template-difference heuristic. Classes: `in_game`, `studio`, `replay`, `ads`, `unknown`. Only `in_game` frames feed downstream extractors.
- **Region extractors:**
  - **Minimap:** bottom-right ~250x250 px region. Template-match team-coded champion icons. Output: list of `(team, champion, x, y)` per frame.
  - **Scoreboard top-bar:** OCR (Tesseract or PaddleOCR) on the gold/timer/score region. Used as cross-check against livestats (catches OCR errors via disagreement).
  - **Item slots:** template-match item icons against item icon library. Output: per-champion item list. Used for build-stage features (§7.C).
- **Output schema:** `cv_frames` table (frame_id, game_id, frame_ts_unix, classifier_class, regions_extracted JSON, minimap_dots JSON, items JSON, ocr_text JSON, confidence FLOAT).

### 6.3 Graceful degradation

CV is primary, livestats is the safety net. Failure semantics reflect that priority:

- **CV degrades (class != in_game / confidence < 0.5 for >3 consecutive frames):** fall back to livestats for numerical features; positional/qualitative features → NaN (XGBoost handles natively). Apply 30% size haircut while degraded and flag in UI. We're now operating on stale-by-~10–15s data, so caution is warranted.
- **Livestats degrades (poller fails >60s) AND CV is healthy:** keep trading on CV-only numerical + positional. No size haircut — CV is our primary source. Note in UI that cross-check is unavailable.
- **Both degrade simultaneously:** hard halt all trading on that game; close to flat at next available book.
- **OCR-vs-livestats disagreement watchdog:** when both sources are healthy, continuously compare. If OCR'd gold disagrees with livestats gold by >5% for >3 consecutive 10-second windows, OCR is suspect → fall back to livestats numerical + apply size haircut + flag for manual review. (See §12.1 stage-1 calibration of this threshold.)

---

## 7. Features (220 total target)

Categories:
- **A. Live numerical** (~50 features): gold diff, gold diff per minute, kills diff, towers/inhibs/dragons/barons diff, jungle objective tempo (time-since-last-objective), CS diff at last frame, recent gold delta (last 30s / 60s / 120s windows).
- **B. Live per-participant numerical** (~40 features): per-role gold lead, per-role level diff, per-role item slot count, per-role recent-kill participation.
- **C. CV positional / macro** (~60 features): minimap-derived team grouping (how close are 4+ champs?), vision control (count of estimated wards in each jungle quadrant), side-lane pressure indicators, recall states (champs returning to fountain), item-build-stage progression.
- **D. Champion/draft features** (~25 features, reused from v1): champion-pair synergies, champion-vs-champion matchups, draft win-conditions.
- **E. Team / roster strength** (~20 features, reused from v1): Glicko-2 ratings, head-to-head history, recent form, roster-change phi widening, region/league strength.
- **F. Pre-match prior** (~25 features): v1 model's output probability, v1 model's calibrated confidence band, v1 feature subset (top-25 by SHAP importance).

**Mirror augmentation:** for each training row, add a mirrored row with team_a / team_b swapped and label flipped. Free 2× data, removes side-bias.

**Training data construction:**
1. VOD-replay phase 1: for every completed LCK game in 2024-2025, pull the gameId, fetch all historical livestats frames (Riot stores them for ~30 days; older games we re-derive from broadcast VOD via CV).
2. CV phase: run cv_pipeline.py against VOD downloads (yt-dlp from official LCK YouTube). Generate CV feature rows aligned to livestats frame timestamps.
3. Join: per-frame feature vector keyed on (game_id, frame_ts).
4. Label: did blue side win this game? (1 / 0)
5. Output: ~150k training rows across ~500 historical LCK games × ~300 frames per game.

---

## 8. Live model

- **Architecture:** single XGBoost classifier predicting `P(team_a wins this map | features_at_frame_t)`. Series and total-maps probabilities derived mathematically from per-map predictions + current series score.
- **Per-phase calibration:** 4 separate isotonic calibrators, one each for game-time buckets `[0,10]`, `(10,20]`, `(20,30]`, `(30,∞)` minutes. Early-game probabilities calibrate differently than closeout-stage probabilities.
- **Uncertainty quantification:** bootstrap ensemble of 30 models trained on resampled folds + temporal variance across the last 5 frames (variance contracts as game proceeds; low variance = high confidence).
- **Cross-validation:** group-by-match walk-forward. Folds are time-ordered; frames from the same game must stay in the same fold (no intra-game leakage).
- **Target metrics:**
  - Per-frame Brier ≤ 0.20 averaged across all phases
  - Per-phase Brier ≤ 0.22 in any single phase bucket
  - Calibration plot ECE ≤ 0.04 in each phase bucket
- **Derived market probabilities:**
  - `P(KXLOLGAME yes for team A this map) = model output`
  - `P(KXLOLMAP yes for team A map N) = model output evaluated against map N's draft/state at start of map`
  - `P(KXLOLTOTALMAPS 3-1 for team A | currently 1-0) = P(A wins next 2) + P(A loses next 1, wins next 2)` — recursive per-map composition
- **Retraining cadence:**
  - **Patch boundaries** (every ~2 weeks for LoL): retrain only if patch notes touch ≥3 frequently-played champions or ≥1 system change (items, dragons, gold). Otherwise skip.
  - **Split boundaries** (LCK spring/summer): always retrain. Roster changes, meta shifts, and accumulated new data justify a fresh model.
  - **Calibration-drift trigger:** if rolling-50 Brier exceeds `train_brier + 0.02` (one tick before the §11.2 halt threshold of +0.03), schedule retrain at next no-game window. Doesn't auto-halt; gives lead time.
  - Every retrain produces a `models/v2_YYYY-MM-DD.pkl` artifact + a CV report. Old models retained for rollback. `models/v2_latest.pkl` is a symlink to the active one.

---

## 9. Live trading logic

### 9.1 Per-frame decision loop

Riot emits a new frame every 10s. The poller fetches at 2s cadence to catch each new frame within ~1s of availability. **The decision loop fires on each new frame** (i.e. ~every 10s, not every poll). On each new frame: recompute features → one model call → derive all three market probabilities → for each linked market: fetch latest Kalshi book → decide OPEN / ADD / HOLD / CLOSE / REVERSE.

CV frames arrive at 1 fps (10× the livestats rate). They're aggregated to the nearest livestats frame timestamp before feature computation, so the decision loop remains driven by livestats arrivals.

### 9.2 Action thresholds

| Action | Trigger |
|---|---|
| **OPEN** (no position) | edge ≥ 5¢ AND p10–p90 width ≤ 25¢ |
| **ADD** (same direction) | edge widened by ≥ 3¢ since entry AND total exposure < per-market cap |
| **HOLD** | edge in `[−2¢, entry_edge]` |
| **CLOSE** | edge within 1¢ of fair OR edge inverted by ≥ 3¢ |
| **REVERSE** | opposite-direction edge ≥ 5¢ AND uncertainty band acceptable |

Threshold values are **placeholders** to be calibrated from paper-trading data (§11.3). Asymmetric (5¢ open, 1¢ close) to favor exits over entries.

### 9.3 Market-type specifics

- **KXLOLGAME:** direct from per-map model. ~5–15 decisions per game expected.
- **KXLOLMAP:** updates only on map-end events (between maps).
- **KXLOLTOTALMAPS:** derived. Recomputed after each map end. Carrying positions between maps allowed only if edge ≥ 3¢ at map-end.

### 9.4 Anti-churn guards

- **Min-hold:** ≥ 30s after OPEN before any further action on that position.
- **Frame-staleness kill:** halt all new actions if poller hasn't written a frame in ≥ 60s.
- **Edge-confirmation:** OPEN and REVERSE require trigger condition to hold across 2 consecutive frames (~4s). CLOSE fires immediately.
- **Per-game trade cap:** max 8 actions per game across all linked markets.

### 9.5 Sizing

```
size_v2 = size_v1 * max(0, 1 - 2 * uncertainty_width)
```
where `uncertainty_width = p90 - p10` from bootstrap ensemble. Width 0 → full size. Width 0.1 → 80% size. Width 0.25 → 50% size. Width 0.4 → 20% size. Width ≥ 0.5 → no trade.

---

## 10. Verified empirical findings (2026-05-24)

### 10.1 LCK broadcast delay = 45s

Ran `research/live_lol_demo.py` against the live Gen.G Esports vs DN SOOPers game (gameId 115548128962971949). Probed delays from 30s to 600s. Minimum working delay: **45 seconds**. LCP (also probed, Fukuoka SoftBank HAWKS vs Relove Deep Cross Gaming, gameId 115570683341512194): **30 seconds**. Both well within tradable range.

### 10.2 Frame structure validated

Every frame contains the full numerical state we expected: `rfc460Timestamp`, `gameState`, `blueTeam/redTeam.{totalGold, totalKills, towers, inhibitors, dragons, barons}`, plus per-participant details. Sample observed: Gen.G mid-late game leading by 9,139g with 2 chemtech drakes and 1 baron. Data is consumable as-is for feature §7.A.

### 10.3 Transport decision: REST for Riot, WS for Kalshi

The 45s broadcast delay is the dominant latency floor and is immovable. REST polling at 2s adds at most ~2s average wait; a hypothetical WebSocket would save ~1–2s. Not worth the fragility of reverse-engineering an undocumented endpoint. Kalshi book changes happen many times per second, so WebSocket there is correct (already built in v1).

### 10.4 LCS also tradable (30s delay)

Probed live during a Team Liquid vs LYON Game 3 (LCS, gameId 115564793879469297). Minimum working delay: **30 seconds**. Faster than LCK (45s), consistent with a Western broadcast pipeline. Validates that v2.3+ expansion to LCS will not hit a delay-floor surprise.

### 10.5 In-game-clock caching, not retrospective probing

Building a "what's the current in-game minute?" function via retrospective probing of the livestats window endpoint produces ~10-minute errors when implemented as a naive linear scan over coarse time candidates (verified during initial implementation). The window endpoint serves only ~10-second-wide slices, so the earliest in_game frame in any returned slice is not the actual game start — just the earliest frame in the most-recent serving window. A binary search across the full game-length range nails the true game start in ~10 API calls, but the production system should avoid the issue entirely by **caching game-start on first detection** (see §6.1). The takeaway baked into the spec: never retroactively probe for state we could have captured at first observation.

### 10.6 Data-freshness reality check

Riot livestats minimum delays (30s LCS, 45s LCK) are calibrated to match broadcast delays. This means **the livestats API does not give us a data-freshness edge over stream viewers**. CV from the broadcast itself runs at viewer-speed (~15–25s behind game truth, vs livestats' ~30–45s), making CV faster than the API by ~10–15s. This finding initially drove the v2 architecture to reorganize CV as primary numerical source — but see §10.7 for the further revision after empirical OCR calibration.

### 10.7 OCR reliability is the binding constraint

Phase 4 calibration measured Tesseract OCR against the LCK broadcast font across 20 known-in_game frames at varied gold values:

- **blue_gold: 50% parseable, frequent gross errors** (e.g. "39.91" parsed when actual value was ~5000)
- **red_gold: 55% parseable, similar error mode**
- **game_timer: 70% parseable** (the timer's larger font + simpler "M:SS" pattern is easier)

Tesseract's English model misreads the LCK font's stylized "2" and "9" digits as "e<" and "I" respectively. The gold-coin icon is misread as "0". Pulling in a heavyweight OCR engine (EasyOCR / PaddleOCR, ~500MB models) would likely improve this but adds dep weight + setup complexity for marginal benefit (the freshness gain over livestats was already only ~10-15s).

**Architectural consequence (§6 updated):**
- Numerical state (gold/kills/towers/etc) → **livestats primary**, not CV
- Positional features (minimap/items/vision) → **CV primary** (the real v2 differentiator)
- CV-OCR remains useful as an opportunistic cross-check via the watchdog in §6.3 but is not load-bearing

This is honest engineering, not a CV failure. CV does what it's good at (extracting structure from pixels that aren't in any API); livestats does what it's good at (parsed numerical state).

---

## 11. Risk and safety (live-specific)

### 11.1 Failure modes

| Failure | Detection | Response |
|---|---|---|
| Poller stalls | No new frame >60s | HALT new openings; hold positions; resume on 2 fresh frames |
| CV drops out | class != in_game / confidence < 0.5 for >3 frames | Numerical-only mode + 30% size haircut + UI flag |
| Game state paused | `gameState != in_game` | Freeze decisions; pauses can last 20+ min in pro play |
| Book moves during decision | Quote drift >3¢ between decision time and submit | Cancel order, don't chase |
| Resolution dispute (chronobreak) | Match-end + new game same teams within 30 min | Mark positions `disputed`, halt market |
| Cross-game contamination | Concurrent LCK games | Isolated state machine per gameId, never cross signals |

### 11.2 Live kill conditions (auto-halt new openings)

In addition to v1 session-level gates:
- **Per-game P&L stop:** −15% of starting bankroll on one game → close all positions in that game; no new openings for that game.
- **Latency stop:** poller→decision latency >10s for 3 consecutive frames → halt.
- **Model-market divergence stop:** model and market diverge by >40¢ for >2 min with ≥$500 depth at top of book → halt and require manual review.
- **Calibration drift watchdog:** rolling Brier across last 50 closed live trades > `train_brier + 0.03` → halt.

### 11.3 Overnight autonomous operation

LCK plays 1–3 AM PDT. User is asleep.
- **Pager:** Twilio SMS + SMTP email on any halt condition. Silence = healthy.
- **Conservative defaults overnight:** auto-tighten size caps to 50% of daytime values between 11pm–7am PDT.
- **Daily morning summary:** `data/reports/YYYY-MM-DD.md` written at 8 AM PDT. Games traded, P&L, halts triggered, calibration drift, disputed markets. One page.
- **No new openings within 60s of `state=ended`** — resolution candle is too volatile.

### 11.4 Position carrying

- **KXLOLGAME:** must close before `state=ended` event. Never carry to settlement.
- **KXLOLTOTALMAPS:** allowed between maps only if edge at map-end ≥ 3¢. Otherwise close before next map.
- **KXLOLMAP:** closes naturally with the map it's tied to.

### 11.5 Process supervision

- Windows Task Scheduler runs `game_discovery.py` and `watchdog.py` at boot with restart-on-failure (3 attempts, 60s between).
- Heartbeat file per child process, touched every 10s. Missing >30s → watchdog restarts.
- 3 crash-restarts within 5 min → halt + page (don't infinite-loop a broken process).
- `data/KILL_SWITCH` file checked before every action (already wired in v1).

### 11.6 Concurrent-game accounting

LCK regularly has 2 series live in parallel (e.g. weekend playoffs). Bankroll caps apply *across* concurrent games, not per-game:

- The 20% **max total exposure** from §3 sums across every open position in every active game.
- The 5% **max position per market** stays per-market.
- If game A is using 18% exposure and game B starts, game B can open new positions worth at most 2% total before hitting the cap → new openings throttled, existing positions preserved.
- The −10% daily and −30% session stops sum P&L across all concurrent games.
- The −15% **per-game P&L stop** is independent per game.

Implementation: a single `risk_state` row in SQLite tracks current cross-game exposure, updated atomically on every fill. Trader reads it before any OPEN/ADD decision.

### 11.7 Logging

- Each child process writes JSONL to `data/logs/{process_name}/{YYYY-MM-DD}.jsonl`.
- Levels: DEBUG (CV intermediate steps), INFO (decisions, fills), WARN (degraded modes), ERROR (halt triggers, exceptions).
- Rotation: daily, gzip after 7 days, delete after 90 days.
- Structured fields: `ts`, `level`, `process`, `game_id` (if applicable), `event_type`, `payload`.
- A single `tail -F` of `data/logs/*/today.jsonl` gives a unified live view (use `jq` for filtering).

---

## 12. Testing and validation

Three-stage pipeline. Each must pass before next is enabled.

### 12.1 Stage 1: VOD replay (offline)

Stage 1 has internal ordering that the spec must call out explicitly to avoid a chicken-and-egg trap:

1. **Extract** features from VODs + livestats (no model needed yet — just CV pipeline + numerical parsing).
2. **Train** the model on the extracted feature dataset (group-by-match walk-forward CV).
3. **Evaluate** on held-out games — this is what the "stage 1 pass criteria" refer to.

Concretely:
- For every completed LCK game in 2024–2025: download VOD (yt-dlp from official LCK YouTube) and pull historical livestats frames (subject to retention window — see §17 #1).
- Run CV pipeline on VOD as if live. Cross-check OCR'd gold against livestats gold.
- Join → feature table → train model with held-out validation games.
- Score per-phase Brier + ECE on held-out games.
- **Pass criteria (on the held-out evaluation games):**
  - Per-frame Brier ≤ 0.20 averaged across full game
  - CV uptime ≥ 90% across frames
  - OCR-livestats numerical disagreement on <2% of frames, <5% magnitude when it occurs
- **Duration:** ~2 weeks offline work (CV extraction is the long pole; training is ~hours).

### 12.2 Stage 2: Paper-trading live

- Full live stack runs against real LCK games. `live_trading: false` config flag. Trader computes decisions and writes to `paper_trades`; never calls Kalshi order endpoints.
- Fee math uses actual book at decision time — simulated P&L is realistic.
- **Slippage assumption in paper mode:** fills modeled at the displayed ask (for BUY_YES) or bid (for BUY_NO) at decision-time, no improvement, no walking the book. This is **conservative** — real fills sometimes get better prices but never reliably so. Stage 3 measures whether realized fills match this assumption.
- **Pass criteria after 30 games:**
  - Decision audit (you, manual): 100 random sampled decisions each look reasonable for the game state
  - Per-phase Brier within ±0.02 of stage-1 numbers
  - Positive simulated EV across ≥ 20 of the 30 games
  - No unaddressed crashes, no halts that turn out to be false alarms
- **Also calibrate §9.2 action thresholds from this stage's data.**
- **Duration:** ~4 weeks (one LCK split + group stage).

### 12.3 Stage 3: Micro-real-money

- `max_position_value_cents: 5000` ($50/game). `daily_loss_cap_cents: 20000` ($200/day).
- 50 LCK games.
- **Critical comparison:** realized P&L vs simulated P&L from stage 2 should match within ±25%. If less, slippage/fee model is broken.
- **Pass criteria:** within-band P&L match + no operational incidents + calibration holds.
- **Duration:** ~6 weeks.

### 12.4 Continuous validation post-launch

- Rolling 50-trade Brier as a halt trigger (§11.2)
- Weekly calibration plot in morning report
- Anomaly detector on per-game P&L (>3σ from rolling mean → flag)
- Pure-control check: every Nth game runs trader in `paper_only: true` even when live is on. Drift between paper and live decisions → investigate.

### 12.5 What we deliberately won't do

- **No simulated-live backtesting.** Lab conditions don't reproduce broadcast delay, ads, CV noise.
- **No cross-league validation transfer.** Each league validates separately.
- **No calendar-date gating.** Metric-driven only.

---

## 13. Operations

### 13.1 Process activation model

Not 24/7 hot — **game-driven cold start**. `game_discovery` is always-on. When it detects a live LCK game, it spawns poller/CV/trader for that gameId. Cleanup on game end. Saves ~30% sustained CPU when no game is live.

### 13.2 Windows Task Scheduler

- `loltrader-discovery`: runs `game_discovery.py` at system boot, restart-on-failure (3 attempts, 60s between)
- `loltrader-watchdog`: runs `watchdog.py` at boot, same restart policy
- `loltrader-morning-report`: daily at 8:00 AM PDT

Game-driven processes are subprocess children of `game_discovery`, not scheduled tasks, so they inherit the supervision tree.

### 13.3 Sleep / wake config

- Sleep: disabled during 11 PM–4 AM PDT (PowerCfg time-based scheme)
- Wake timers: enabled
- BIOS auto-restart on power loss

Documented in `docs/superpowers/specs/operations/windows-setup.md`.

### 13.4 Disk budget

~33 MB per game (frame PNGs + JSON + decision rows). LCK plays 5 match-days/week × 2–3 series/day × ~2 games/series = **roughly 20–30 games/week**, so **~30 GB/year** (revised up from initial 9 GB/year estimate).
- Frame PNGs: 30-day retention, then compressed-tar archive
- SQLite rows: keep forever
- Streamlink raw mpegts (if recorded for debug): 7-day retention

Provisioning: dedicate at least 100 GB of free space on the data drive before stage 2 kicks off. Cleanup runs monthly via scheduled task.

### 13.5 Network failure responses

| Failure | Response |
|---|---|
| Twitch drops | CV → NaN, numerical-only mode |
| Riot livestats drops | Halt openings, hold positions, resume on 2 fresh frames |
| Kalshi REST drops | Halt openings; existing orders unaffected |
| Kalshi WS drops | Auto-reconnect (v1). Down >30s → halt openings. |
| Home internet drops | All of above. Watchdog detects, pager fires SMS via phone cell signal. |

### 13.6 Pager

- Primary: Twilio SMS to user phone. Twilio is paid (~$0.008/SMS in US); ~5–20 alerts/month expected = <$0.50/mo. Initial $15 trial credit covers months of testing.
- Backup: SMTP email via configured mail account (free).
- Triggers: any halt condition (§11.2), any 3-restart event (§11.5), missing heartbeat from any child >2 min.
- **Rate limit:** max 1 page per minute per trigger type. Prevents pager storms when a process is flapping.

### 13.7 Update / deploy workflow

- Never deploy mid-game.
- Update between matches (LCK has 30-min breaks).
- `git pull` → restart watchdog. Watchdog brings down children, restarts on new code.
- Smoke test: next `game_discovery` cycle should pick up the next live game within 60s.
- Rollback: `git checkout <last-known-good-tag>` + restart. Every deploy gets a tag.
- Schema migrations / model retrains require a no-active-games window (LCK off-days).

### 13.8 Typical user-day

- **~11 PM PDT:** glance at Streamlit dashboard. Check discovery sees tomorrow's schedule, no halts, bankroll where expected. Close browser.
- **Overnight:** machine runs unattended. Silence = healthy.
- **~8 AM PDT:** open `data/reports/YYYY-MM-DD.md`. One page. Decide if action needed.

Target: ~10 min/day of attention once stable. More during stages 1–2; less after.

---

## 14. Twitch authentication

- User has personal Twitch account with active LCK channel subscription → ad-free playback on user-authenticated requests.
- **Auth method (v2.0):** browser session cookie `auth-token` from logged-in Twitch session. Stored at `data/twitch_creds.json` (gitignored, file ACL restricted).
- **Why cookie over OAuth dev-console token for v2.0:** known-good path. streamlink + Twitch HLS reliably delivers sub-channel ad-free streams via session cookie. OAuth-token-based ad-free is theoretically equivalent but unverified on current Twitch API.
- **Revocation:** Twitch settings → "Disconnect all sessions" → re-login → re-export cookie.
- **Migration path (v2.1+):** if cookie security becomes a real concern, generate a zero-scope OAuth token via Twitch dev console after A/B-verifying it delivers ad-free LCK playback. Less blast radius if leaked.

---

## 15. Schema migrations introduced in v2

| Migration | Purpose |
|---|---|
| `008_v2_live_frames.sql` | `live_frames` (raw + parsed Riot livestats JSON per frame) |
| `009_v2_cv_frames.sql` | `cv_frames` (CV extractor outputs, classifier class, regions, OCR text, confidence) |
| `010_v2_live_decisions.sql` | `live_decisions` extends v1 `decisions` with frame_id, model_uncertainty_width, action_type (OPEN/ADD/HOLD/CLOSE/REVERSE) |
| `011_v2_live_features.sql` | `live_features_cache` per-frame feature vector snapshot for reproducibility |

All migrations are additive; v1 schema untouched.

---

## 16. Out of scope (v2.0)

- LPL, LEC, LCS, MSI, Worlds, First Stand, EWC, ENC — deferred to v2.3+
- Pre-match trading (v1 covers this if reinstated separately)
- Sub-30s data (paid feeds like GRID/PandaScore)
- A research-grade teamfight outcome model (we use position+state features, not a learned teamfight evaluator)
- Mobile / remote operation
- Cloud deployment

---

## 17. Open questions / TODOs before implementation kicks off

1. Verify Riot livestats historical retention window. We need ≥30 days for stage-1 VOD replay; ideally 1+ year. **Action:** probe with a 30-day-old gameId. If retention is short, stage 1 has to rely entirely on CV-derived numerical features (OCR'd scoreboard), and we lose ground-truth cross-checking.
2. Verify yt-dlp can pull official LCK VODs at >=720p reliably. **Action:** test on 3 recent games. If 720p only, resolution-scale issues may affect template-matching accuracy.
3. Decide frame-classifier approach: small CNN (more accurate, requires training data) vs template-difference heuristic (zero training, lower accuracy). **Recommend:** start with template heuristic; upgrade to CNN if stage-1 CV uptime <90%.
4. OCR engine: Tesseract (simpler) vs PaddleOCR (more accurate, GPU-friendly). **Recommend:** Tesseract first; PaddleOCR if disagreement rate >2%.
5. Confirm Twilio account setup + budget for expected pager volume (~$1/mo realistic).
6. Confirm Windows BIOS auto-restart-on-power-loss is available on user's machine.
7. **Frame ordering robustness:** livestats can theoretically return frames out-of-order if poller is delayed. Schema should key on `(game_id, frame_ts)` UNIQUE constraint and the poller should drop duplicates. **Action:** decide write-strategy (UPSERT vs INSERT OR IGNORE).
8. **Single-machine SPOF acknowledged.** The entire system runs on one Windows box. Network outage, hardware failure, or user-locked-out-of-machine all cause complete downtime. No HA in v2.0 — accepted risk; revisit in v2.x if a real-money loss can be tied to it.
9. **Adverse selection in live markets:** when we want to take, market makers may pull quotes seeing edge move against them. Stage 3 will measure this empirically. **Open:** no mitigation strategy yet; if stage 3 shows we only get filled when we're wrong, the action thresholds in §9.2 need re-tuning (likely larger required edges).
10. **Will v1's pre-match prior actually help?** §8 adds 25 features from v1 output. If v1's CV Brier 0.197 doesn't survive recalibration into the live setting, those features are noise. **Action:** SHAP audit after stage 1 — if v1 prior contributes <2% feature importance, drop it.
11. **Measure end-to-end latency empirically.** During the next live LCK/LCS game, perform synced comparison: user reads on-stream in-game clock, we pull livestats and CV frame timestamps at the same wall-clock moment. Compute (CV lag vs stream), (livestats lag vs CV), (livestats lag vs stream). 3–5 samples through the game gives variance bands. **Action:** must be done before any real-money trading; cannot rely on assumed 15–25s broadcast delay. Then revise §1 / §6 numbers if reality differs.
12. **OCR vs livestats numerical disagreement threshold.** §6.3 sets a 5%-divergence-for-3-windows watchdog. Real threshold needs stage-1 calibration: if OCR is normally within 0.5% of livestats then 5% is permissive; if OCR is noisy at 2% baseline then 5% triggers constantly. **Action:** after stage 1 VOD-replay, histogram OCR-vs-livestats divergence and set the watchdog at the 99.5th percentile of the baseline distribution.
13. **`--twitch-low-latency` actually works on LCK/LCS broadcasts?** The streamlink flag is best-effort and depends on the channel's own transcoding settings. **Action:** verify during stage 1 by measuring CV frame timestamps before/after enabling the flag.

---

**End of spec. Awaiting user review.**
