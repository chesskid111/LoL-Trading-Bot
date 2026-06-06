# Comp Evaluation Engine — Design Spec

**Date:** 2026-06-06
**Status:** Draft for review
**Scope:** v1 of the comp-aware live win-probability engine that powers trade signal generation on Kalshi LoL markets.

---

## Goals

Build a calibrated, comp-aware win-probability engine that:

1. **Reads any pro LoL match's draft** and computes a structural fair-value win probability before the game starts.
2. **Updates the fair-value estimate live** as the game progresses, incorporating gold/kill/objective state, item completions, and the time-evolving strength of each comp.
3. **Outputs a calibrated probability with uncertainty band** so the risk manager can size positions properly.
4. **Surfaces edge vs Kalshi market price** so the dashboard can highlight trade opportunities.

The engine is the foundation of the trading bot's directional edge. Tonight's results validated the thesis: the market consistently underweights comp scaling and throw potential. This engine encodes that systematically.

## Non-goals (v1)

- **No CV pipeline.** Position data, cooldowns, HP/mana, and minimap state are deferred to Phase 2 (~3-4 weeks after this v1 ships).
- **No partner data feeds.** All sources are public (Riot livestats API, gol.gg, Oracle's Elixir).
- **No execution layer changes.** This is the model layer only; execution + risk wiring is a separate spec.
- **No fully autonomous trading.** Outputs signals and edge; human (currently) makes the trade decision via the dashboard.
- **No other esports.** LoL-only. CS2/Dota/Valorant deferred indefinitely.

## Architecture overview

Five layers, each independently testable and replaceable.

```
Champion picks (draft) ─┐
                        ▼
                  Layer 1: Champion profiles
                  - Hand-curated qualitative dimensions
                  - LLM-aggregated analyst priors
                  - Pro stats (gol.gg + Oracle's Elixir)
                        │
                        ▼
                  Layer 2: Comp aggregator
                  - Sums per-team dimension scores
                  - Archetype classifier
                  - Synergy lookups
                  - Player×champion comfort
                        │
                        ▼
                  Layer 3: Matchup evaluator
                  - Lane matchup winrates
                  - Comp archetype interactions
                  - Cross-over time estimator
                        │
                        ▼ (+ live game state)
                  Layer 4: Live state integrator
                  - Game state (live_frames)
                  - Per-player items + stats (live_frames_details)
                  - Time + objective state
                        │
                        ▼
                  Layer 5: Win-prob model
                  - XGBoost on Match-V5 timelines
                  - Calibrated via isotonic regression
                  - Outputs P(blue wins) + uncertainty
                        │
                        ▼
                  Compare to Kalshi → trade signal
```

---

## Data sources

| Source | Used for | Access | Update cadence |
|---|---|---|---|
| Riot livestats `/window` | Live team-state (gold, kills, towers, dragons, barons) | Public (30s embargo) | Per-game, every 2s |
| Riot livestats `/details` | Per-player items, stats, runes, abilities | Public (30s embargo) | Per-game, every 2s |
| Riot Match-V5 timelines | Training data for win-prob model | Public, rate-limited | Bulk pull per patch |
| gol.gg | Pro winrates, pickrates, lane matchups | Free, scrapeable | Per patch |
| Oracle's Elixir CSVs | Pro match-level data, raw stats | Free CSVs | Weekly |
| LLM analyst synthesis | Qualitative champion priors | API calls | Per patch |
| lolesports getLive / getEventDetails | Live draft + game discovery | Free, polling | Every 30s |

**Regions covered in v1:**
- **LCK, LPL, LCS, LEC** (primary — sufficient pro data volume)
- **LCP, LTA-S** (secondary — included where at least 50 games in last 6 months exist)
- **Worlds, MSI** when active

**Patch coverage:** Training data spans the last 6 months of patches. Roughly 13 patches at the current 2-week cadence. Approximate training set: ~5,000-8,000 pro games + ~500,000 Match-V5 timeline games for state-level features.

---

## Layer 1: Champion profiles

### File location

- `data/champion_profiles.json` — qualitative + composite
- `data/patch_stats.json` — auto-computed pro stats per patch
- `data/champion_aliases.json` — for legacy / regional name variations

### Schema

```json
{
  "Caitlyn": {
    "schema_version": 1,
    "patch": "26.10",
    "qualitative": {
      "scaling_early": 1,
      "scaling_mid": 2,
      "scaling_late": 3,
      "baron_dps_tier": 3,
      "peel_needs": 2,
      "peel_supply": 0,
      "split_push_threat": 1,
      "pick_threat": 0,
      "teamfight_score": 2,
      "engage_score": 0,
      "disengage_score": 0,
      "wave_clear": 3,
      "ult_impact": 1,
      "comfort_curve": "smooth",
      "primary_role": "bot",
      "secondary_roles": []
    },
    "pro_stats": {
      "pickrate_30d": 0.32,
      "banrate_30d": 0.18,
      "winrate_30d": 0.535,
      "priority_score": 7.8,
      "games_sampled": 41
    },
    "common_partners": ["Lulu", "Karma", "Yuumi"],
    "common_counters": ["Senna+Tahm", "Twitch", "Draven"],
    "data_sources": ["gol.gg/v1", "LS draft analysis 5/22"],
    "confidence": 0.85,
    "last_updated": "2026-06-04T03:00:00Z",
    "validation_flags": []
  }
}
```

### Qualitative dimensions

| Dimension | Range | Meaning |
|---|---|---|
| scaling_early | -3 to +3 | Strength at minute 0-15 |
| scaling_mid | -3 to +3 | Strength at minute 15-25 |
| scaling_late | -3 to +3 | Strength at minute 25+ |
| baron_dps_tier | 1-5 | Single-target sustained DPS for objectives |
| peel_needs | 0-3 | How much team protection this champion requires |
| peel_supply | 0-3 | Peel this champion provides to teammates |
| split_push_threat | 0-3 | Capability to threaten side lanes alone |
| pick_threat | 0-3 | Ability to set up picks via mobility/CC |
| teamfight_score | -3 to +3 | Strength in coordinated 5v5 |
| engage_score | 0-3 | Provides hard engage |
| disengage_score | 0-3 | Provides disengage / peel out |
| wave_clear | 0-3 | Sustained vs burst wave clearing |
| ult_impact | 0-3 | Strength of ultimate as a fight-deciding tool |
| comfort_curve | enum | "smooth" / "spike-2-item" / "spike-3-item" |

### Update process

**Two tracks:**

**Track A — Pro stats (fully automated, per patch):**

1. Cron job runs ~4 hours after patch goes live.
2. Scrape gol.gg for each champion's pro stats (pickrate/banrate/winrate) filtered to last 30 days, regional split per league.
3. Query Oracle's Elixir CSVs for the same period to validate.
4. Write to `data/patch_stats.json`.
5. Merge into `champion_profiles.json` (the `pro_stats` sub-block).

Estimated time: ~1 hour cron, no human input.

**Track B — Qualitative (LLM-assisted, per patch):**

We've committed to **LLM curation Option B**: build a prototype on 20 champions, validate accuracy, then commit to full automation if accuracy is acceptable.

**Prototype phase (week 1 of build):**

1. Pick 20 most-played champions across LCK/LPL/LCS/LEC current patch.
2. For each, query Claude/GPT-4 with web-search-enabled prompt asking for tier values across the 14 dimensions, with citations.
3. You spot-check the LLM output against your own intuition and any pro analyst content you've seen.
4. Score LLM accuracy: % of dimensions within 1 point of your reference value.
5. If accuracy ≥ 80% → proceed to full automation. If < 80% → refine prompt + retry, or fall back to manual.

**Production phase (after validation):**

1. Per patch, LLM runs on all ~170 champions.
2. LLM output validated against statistical disagreements with `patch_stats.json`.
3. Disagreements flagged for human review (~5-10 typically).
4. You spend ~30 min spot-checking flagged disagreements + adding personal overrides for high-conviction edges.
5. Approved values committed to `champion_profiles.json`.

**LLM prompt template** (per champion):

```
You are aggregating pro LoL meta analysis for patch {patch} for the {league} region.

Find recent (last 14 days) analysis about {champion} from:
- LS / Last Shadow YouTube
- Caedrel YouTube
- MonteCristo Twitter / podcast
- Reddit r/leagueoflegends weekly meta threads
- Pro broadcast analyst commentary
- Recent pro coach interviews

Output as JSON conforming to the schema:
{schema_for_qualitative_dimensions}

Each value must cite at least one source. Confidence < 0.5 means flag for manual review.
```

**Cost:** ~$0.03 per champion × 170 champions = ~$5 per patch. ~$10-15/month at biweekly patches.

### Validation

The system cross-checks LLM-derived qualitative scores against pro statistics:

```
If LLM says scaling_late = 3 for Champion X,
and Champion X's winrate-past-25-min < league-average by > 5%:
  → flag for human review
```

The threshold is tunable. Disagreements catch both LLM hallucinations and genuine meta shifts.

---

## Layer 2: Comp aggregator

### File location

- `src/loltrader/comp/aggregator.py`

### API

```python
@dataclass
class CompProfile:
    scaling_curve: dict[int, float]    # minute → composite score
    baron_dps_total: float
    peel_supply_total: float
    peel_demand_total: float            # sum of peel_needs
    split_push_threat: float
    pick_threat: float
    teamfight_score: float
    engage_score: float
    disengage_score: float
    wave_clear: float
    archetype: Literal["scaling", "teamfight", "pick", "balanced"]
    synergy_bonuses: list[str]
    win_condition: str
    confidence: float

def evaluate_comp(
    picks: list[ChampionPick],   # 5 picks with roles
    patch: str,
    players: list[str] | None = None,  # for player×champion overrides
) -> CompProfile:
    ...
```

### Synergy lookup

A hand-curated table of known strong combos (~50 entries). Examples:

```python
SYNERGY_BONUSES = {
    ("Lulu", "Lucian"): {"teamfight_score": +1, "scaling_late": +1},
    ("Maokai", "Sett"): {"engage_score": +1, "teamfight_score": +1},
    ("Karma", "Yasuo"): {"engage_score": +1},
    ...
}
```

Updates: ~per major patch or as new synergies emerge. Manually curated.

### Player×champion overrides

When a known carry is on a comfort pick, override the comp profile:

```python
PLAYER_COMFORT_OVERRIDES = {
    ("Faker", "Azir"): {"teamfight_score": +0.5, "wave_clear": +0.5, "comfort_curve": "smooth"},
    ("Chovy", "Akali"): {"pick_threat": +0.5, "kill_pressure": +0.5},
    ...
}
```

Sourced from gol.gg per-player stats. Auto-generated for players with ≥10 games on a specific champion in the last 90 days.

### Win-condition inference

Based on archetype + key picks, generates a string:

- "split push (Yorick) → 4-man teamfight Baron"
- "scaling 5v5 around Senna+Caitlyn"
- "early game pick comp, snowball bot lane"
- "late-game wombo around Karthus ult"

Used for dashboard display + LLM-readable context.

---

## Layer 3: Matchup evaluator

### File location

- `src/loltrader/comp/matchup.py`

### API

```python
def lane_matchup(
    champ_a: str, champ_b: str, role: str, patch: str
) -> tuple[float, float]:
    """Returns (winrate_a, confidence). Pro-filtered from gol.gg + Oracle's Elixir.
    Bayesian shrinkage for small samples; falls back to neutral 0.5 when <5 games."""

def comp_matchup(
    comp_a: CompProfile, comp_b: CompProfile, minute: int
) -> tuple[str, float]:
    """Returns (favored_team, edge_magnitude) for this matchup at this minute.
    e.g., ('B', 0.15) means comp B is 15% favored at this minute structurally."""

def crossover_minute(
    comp_a: CompProfile, comp_b: CompProfile
) -> int | None:
    """Estimates the minute at which comp dominance flips (if any).
    Returns None for matchups where no crossover is expected (one comp dominates throughout)."""
```

### Lane matchup data

Built from:

1. **gol.gg lane matchup data** — scraped per patch, filtered to pro region.
2. **Oracle's Elixir queries** — computed from raw match data as validation.
3. **Bayesian shrinkage** — sparse matchups (<5 games) regressed toward neutral or composite prior.

Storage: `data/lane_matchups.json` — refreshed per patch.

### Crossover detection

Two scaling curves over time. Crossover = minute at which (comp_b_scaling - comp_a_scaling) flips sign.

For matchups with crossover in [10, 35] minute window → tradeable cycle opportunity (Layer 4 will exploit).

For matchups where one comp dominates throughout → trade based on raw advantage, no cycle play.

---

## Layer 4: Live state integrator

### File location

- `src/loltrader/winprob/state.py`

### API

```python
def integrate_state(
    comp_eval: CompMatchupResult,
    frame: LiveFrame,
    details: list[LiveFrameDetail],
    minute: int,
) -> dict[str, float]:
    """Combines comp evaluation with current game state into a flat feature dict
    that the win-prob model consumes."""
```

### Feature categories

**State features (~15):**

```
gold_diff, kill_diff, tower_diff, inhib_diff
dragon_diff, soul_state, baron_state, baron_buff_time_remaining
minute, time_to_next_baron, time_to_next_dragon
```

**Comp features (~25):**

```
comp_a_scaling_at_t, comp_b_scaling_at_t
comp_a_baron_dps, comp_b_baron_dps
comp_a_squishiness, comp_b_squishiness  (sum of peel_needs - peel_supply)
comp_a_disengage, comp_b_disengage
comp_a_split_push_threat, comp_b_split_push_threat
archetype_diff (categorical)
synergy_bonus_a, synergy_bonus_b
```

**Team/player features (~15):**

```
team_a_glicko, team_b_glicko
team_a_conversion_rate, team_b_conversion_rate  (from leads)
team_a_comeback_rate, team_b_comeback_rate
player_a_avg_form, player_b_avg_form  (per role)
player_champion_comfort_score
team_h2h_recent  (last 5-10 games)
```

**Item-progression features (~10, NEW with details integration):**

```
comp_a_items_completed, comp_b_items_completed
comp_a_carry_progression  (specific item milestones)
comp_a_avg_components_to_completion
comp_a_mythic_components_built (legacy if mythic system)
```

**Interaction features (~10):**

```
gold_diff × time_remaining
gold_diff × comp_a_squishiness  (squishy comps convert leads worse)
minute × scaling_diff
baron_state × comp_a_baron_dps
```

Total: ~75 features feeding into Layer 5.

---

## Layer 5: Win-probability model

### File location

- `src/loltrader/winprob/model.py`
- `src/loltrader/winprob/train.py`
- `src/loltrader/winprob/calibrate.py`

### Architecture

**XGBoost classifier** with:

- ~75 features (from Layer 4)
- Binary target: blue team wins (1/0)
- 5-fold time-based CV (no leakage from future games into training)
- 10-member ensemble for uncertainty estimation

**Calibration:**

- Isotonic regression on holdout predictions
- Goal: predicted probability ≈ empirical frequency across all probability buckets
- Calibration plot validation per training run

### Relationship to existing v1 pre-game model

This Layer 5 model supersedes the existing `models/v1_latest.pkl` (95-feature pre-game XGBoost) for inference. The existing model's feature engineering for team/player ratings (Glicko, recent form, H2H, etc.) is reused as input to the new comp-aware model; the new model adds live state features and comp-aware composition features that the existing model lacks. After v1 ships, the existing model can be deprecated in favor of Layer 5 evaluated at minute 0.

### Training data

**Match-V5 timelines:** Pulled for the last 6 months of pro games across LCK/LPL/LCS/LEC/LCP/LTA-S where at least 50 games per league exist.

**Sampling:** At every minute of every game, generate a training example: (state at minute t, comp features, team/player features) → outcome.

A 30-minute game produces ~30 training examples. ~5,000 pro games × 25 minutes avg = ~125,000 training examples. Plus we can supplement with solo queue Match-V5 timelines (~500k+ available).

### Performance targets

- **Pre-game accuracy (no state info):** ≥ 70% on holdout
- **Mid-game accuracy (at minute 15):** ≥ 75%
- **Late-game accuracy (at minute 25+):** ≥ 85%
- **Brier score (calibration):** ≤ 0.20 pre-game, ≤ 0.10 mid+late
- **Calibration error (max bucket):** ≤ 5%

If pre-game Brier > 0.22 after calibration, the model is too noisy to trade pre-game value. Cycle trades may still work if mid-game Brier is below target.

### Output API

```python
@dataclass
class WinProbPrediction:
    p_blue: float           # calibrated probability
    p10: float              # 10th percentile of ensemble
    p90: float              # 90th percentile
    raw_p_blue: float       # uncalibrated, for diagnostics
    feature_contributions: dict[str, float]  # top features for this prediction
    confidence_band_width: float  # p90 - p10
```

The `confidence_band_width` feeds directly into the risk manager's sizing logic — wider band = smaller position.

---

## Build order (5 phases)

| Phase | Duration | Deliverable |
|---|---|---|
| 1. Champion profile bootstrap | 3 days | 20-champion LLM prototype + validation; full bootstrap if green |
| 2. Comp aggregator + matchup | 3 days | `evaluate_comp()` + `lane_matchup()` working |
| 3. Live state integrator | 2 days | `integrate_state()` produces feature dict from any frame |
| 4. Win-prob model | 5 days | Trained XGBoost + calibration + holdout validation |
| 5. API + dashboard wiring | 2 days | `/api/live_winprob` + WS push + dashboard edge column |

**Total: ~15 working days (3 weeks).**

Each phase is independently testable. Phases 1-3 produce useful artifacts even if Phase 4 reveals model issues — e.g., the comp evaluation alone can drive a dashboard column.

---

## Risk management integration

The model output feeds the risk manager (separate spec, but contract defined here):

```python
def size_position(prediction: WinProbPrediction, market_price: float, bankroll: float):
    edge = prediction.p_blue - market_price
    if abs(edge) < MIN_EDGE_THRESHOLD:    # 4¢
        return 0
    if prediction.confidence_band_width > MAX_BAND_WIDTH:  # 0.20
        return 0
    kelly = (edge / odds) * bankroll * KELLY_FRACTION  # half-Kelly
    kelly *= (1 - prediction.confidence_band_width) ** 2  # variance penalty
    return min(kelly,
               bankroll * MAX_PER_POSITION,
               bankroll * MAX_PER_GAME - current_game_exposure)
```

The model exposes the inputs (edge + band); the risk manager applies the policy. **Position size scales inversely with model uncertainty** — a wide band means smaller bet, even if edge is large.

---

## Testing strategy

**Unit tests:**

- Each layer has isolated unit tests
- Mock data for champion_profiles in tests
- Specific known matchups with expected outputs

**Integration tests:**

- Replay historical game (e.g., tonight's GEN/HLE) through the full pipeline
- Verify model output evolves sensibly over time
- Check that comp scaling differences manifest correctly

**Backtest framework:**

- Reusable replay of historical pro games
- Apply model + risk sizing rules → compute hypothetical P&L
- Validate: Brier score, max drawdown, win rate, average edge per trade
- Compare against naive baseline (market price = fair price)

**Calibration regression test:**

- Daily check that the model's predicted probability still matches empirical frequency
- If calibration drifts > 5% in any bucket → flag for retraining

---

## Maintenance burden

**Per patch (~biweekly):**

- ~1 hour automated (pro stats refresh, LLM curation run, lane matchup recomputation)
- ~30 minutes manual (spot-check LLM disagreements, validate against patch notes)

**Per major meta shift (~quarterly):**

- ~2-3 hours to retune scoring dimensions if a new champion class emerges
- Possible model retraining if patch-specific drift exceeds calibration tolerance

**Per new champion release:**

- ~30 minutes to add manual entry to `champion_profiles.json`
- LLM picks up the new champion automatically in next patch refresh

**Annually:**

- Full model retrain on rolling 12-month window
- Review feature importance, prune low-signal features
- Re-validate calibration

---

## Open risks

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| LLM hallucination on qualitative dimensions | Medium | Medium | Statistical validation + spot-check workflow |
| Sparse data for niche champion picks | High | Low | Bayesian shrinkage; default to neutral priors |
| Meta shifts mid-patch invalidate priors | Medium | Medium | 2-week patch cadence catches it; emergency recurate flag |
| Model overfits to specific team combos | Medium | Medium | Time-based CV; feature regularization (L1) |
| Calibration drift between training and production | Medium | High | Daily calibration regression test |
| Pro data scraping breaks (gol.gg HTML change) | Low | High | Multiple sources (gol.gg + Oracle's) provide redundancy |
| Riot Match-V5 API rate limits constrain training data | Medium | Medium | Pre-bulk-download; cache aggressively |
| Player roster changes mid-split | High | Low | Roster scraper detects sub-ins; player rating refreshed |
| New patch breaks pro data structure | Low | Low | Re-validate ETL per patch |

---

## Success criteria

This v1 ships successfully when:

1. **End-to-end works:** Pick any active LCK/LPL/LCS/LEC game; the system computes `P(blue wins)` with uncertainty band for any minute t of the game.
2. **Calibration passes:** Brier ≤ 0.20 pre-game, ≤ 0.10 mid-game on holdout.
3. **Backtest shows edge:** Hypothetical P&L on last 30 days of games is positive after fees, with Sharpe ≥ 0.5.
4. **Dashboard surfaces signals:** Each market on the dashboard has a model column showing fair value + edge vs ask.
5. **Risk manager protects:** No single trade exceeds the position cap; no game exceeds the game cap.

If all 5 are met, v1 is ready to trade real money in small size (capped at 1-2% bankroll per position).

---

## Out of scope / Phase 2

- **Computer vision:** Minimap tracking, HUD parsing, event detection (3-4 weeks after v1)
- **Position-based features:** Backline isolation alerts, pre-fight setup detection (requires CV)
- **Cooldown tracking:** Ult availability features (requires CV)
- **Cross-region multi-game arbitrage:** Trading patterns across regions
- **Other esports:** CS2, Valorant, Dota 2

Phase 2 starts after this v1 ships and has 30+ days of live trading data validating the edge.

---

## Decisions captured

From design conversation:

1. **LLM curation:** Option B (prototype on 20 champions, validate, then commit to full).
2. **Build scope:** Full 5-layer build over ~3 weeks (no MVP shortcut).
3. **Patch coverage:** Last 6 months of patches (~13 patches at biweekly cadence).
4. **Region coverage:** LCK, LPL, LCS, LEC primary; LCP, LTA-S secondary where data permits.

---

## Next steps after this spec is approved

1. Invoke `writing-plans` skill to produce phase-by-phase implementation plan.
2. Phase 1 (champion profiles bootstrap) kicks off first.
3. Each phase has its own PR; integration tests gate merges.
4. After all 5 phases ship, real-money trading begins with strict position caps; volume increases incrementally based on observed performance.
