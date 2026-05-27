# LoL Trading Bot v2.0a — Decision-Support Tool

**Date:** 2026-05-26
**Supersedes:** [2026-05-24-lol-trading-bot-v2-plan.md](2026-05-24-lol-trading-bot-v2-plan.md) (the autonomous-bot plan)
**Status:** Pivot — replaces the autonomous Brier-gated build

---

## What changed and why

The original v2 plan aimed for fully autonomous trading. After ~6 weeks of work + a 38-game backtest, we hit these realities:

1. **User is already profitable manually.** Today's session: $130 won on two trades in one match (G2 vs MKOI). User's intuition catches qualitative factors (comp scaling, comebacks, shutdowns) that pure numerical models miss.
2. **Pure-numerical bot can't match user's intuition** on the trades that matter most (e.g., the G2 -10k gold comeback trade — bot would have priced G2 at 8% to win; reality was much higher).
3. **The Brier-test approach was theater** — neither pass nor fail cleanly tells us to ship or stop. The actual edge test requires Kalshi book history we don't have.
4. **Full autonomy is a 12-24 month arc.** Realistic for v3+, not v2.

**The pivot:** Build a **decision-support tool** that makes the user faster + more disciplined at the manual trading they're already doing profitably. Add autonomy incrementally as the tool proves itself.

---

## Goal

Tool that lets the user:
- Monitor multiple live LCK markets simultaneously
- See game state + Kalshi book + edge math in one view (no app-switching)
- Execute trades with one click instead of friction-filled Kalshi UI
- Track P&L across sessions
- Get alerts when edges develop in games they're not actively watching

**Success criteria:** User hits $100+/game target more consistently across more games, with less manual data-tracking overhead.

**Not a goal (yet):** Autonomous trading. The user makes every decision in v2.0a.

---

## What's reusable from the old build

| Component | Status | Reuse plan |
|---|---|---|
| Live livestats poller | Built (Phase 2) | Use as-is for game state |
| Game discovery | Built (Phase 2) | Use as-is for finding live games |
| Schema (live_frames, games_live, decisions, paper_trades) | Built (Phases 1-2) | Use as-is |
| Kalshi REST + WS client | Built (v1) | Use as-is for book + execution |
| Streamlit UI skeleton | Built (v1) | Extend with live-trading view |
| CV pipeline (Phases 3-4) | Built but dormant | Not used in v2.0a. Reserved for v3+ when autonomy needs richer features. |
| Twitch auth + creds | Built | Not needed for v2.0a. Reserved for CV-driven future. |
| 38-game historical dataset | Extracted | Reserved for future model training when we add autonomy |

**Net new code for v2.0a is small** — most infrastructure already exists.

---

## Phase A: Live dashboard MVP (~3-5 days)

**Goal:** Single Streamlit page showing all info needed to make a trade decision.

**Tasks:**
1. New view in `src/loltrader/ui/app.py` (or `live_dashboard.py`):
   - **Active games panel:** for each currently-live LCK game show:
     - Game timer + scoreboard timer (in-game clock)
     - Team logos + names (blue + red)
     - Current state: gold, kills, towers, dragons (with elements), barons, inhibs
     - Recent events: last kill cluster (kills in last 60s), last objective taken
   - **Kalshi markets panel:** matched markets for this game
     - KXLOLGAME-{team} YES bid/ask, last trade
     - Implied probability (yes_ask/100)
     - Spread, volume
   - **Edge panel:**
     - User-input "your read" probability slider
     - Edge = your_prob - implied_prob
     - Suggested action (BUY YES / BUY NO / HOLD) based on edge sign and magnitude
2. Auto-refresh every 5s (Streamlit's `st.autorefresh` or `st.empty()`-based polling).
3. Reuse existing `_get_conn()` + `_open_positions()` + `_session_status()` from v1 app.
4. Add new `src/loltrader/ui/live_view.py` for the live-game-specific rendering.

**Dependencies:** Existing livestats poller + Kalshi WS client.
**Deliverable:** Open dashboard during a live LCK game → see game state + market book + edge math in one view.
**Acceptance:** Watch a real LCK game with the dashboard open; everything refreshes within 5s; data matches what you see on Twitch (modulo broadcast delay).

---

## Phase B: One-click trade execution (~2-3 days)

**Goal:** Execute trades from the dashboard without switching to Kalshi UI.

**Tasks:**
1. Add Kalshi write-mode client wiring (currently we have read-only).
   - Load creds with `scope="write"` from `data/kalshi_creds.json`
   - Single `place_order(ticker, side, count, price)` helper
2. UI: BUY YES / BUY NO buttons next to each market.
   - Quantity input (default = Kelly-sized based on user's bankroll + entry edge)
   - Price input (default = current ask for BUY YES, current bid for BUY NO)
   - Confirmation step before submit
3. Display order status + position state after submission.
4. Log every action to `decisions` + `paper_trades` tables (regardless of whether real or paper).

**Dependencies:** Phase A.
**Deliverable:** Click button → real order placed at Kalshi → confirmation shown → position appears in dashboard.
**Acceptance:** Place a $5 paper-money order, watch it through to fill, verify P&L tracks correctly.

---

## Phase C: P&L + session tracking (~1-2 days)

**Goal:** Always-on view of how the user is doing.

**Tasks:**
1. Session selector: "Today's session," "All time," "Last 7 days."
2. Per-session stats: open positions value, realized P&L, total trades, win rate, average trade size, max drawdown.
3. Per-game P&L breakdown (which games made/lost money).
4. Export to CSV for manual review.

**Dependencies:** Phase B.
**Deliverable:** Dashboard shows lifetime + session-specific P&L.

---

## Phase D: Multi-market monitoring + alerts (~3-5 days)

**Goal:** Don't miss opportunities in games you're not actively watching.

**Tasks:**
1. Configurable alerts:
   - Edge crosses threshold (e.g., "alert when edge > 15¢ on any active market")
   - State changes (kill clusters, baron taken, soul taken)
   - Concurrent games (you're watching A, alert for B)
2. Notification channels:
   - Browser notification (when dashboard is open)
   - Phone SMS via Twilio (when away)
   - Optional: desktop notification via Windows Toast
3. Background poller running independently of dashboard UI — alerts fire even if dashboard is closed (run as Windows scheduled task).

**Dependencies:** Phase A + Twilio account setup.
**Deliverable:** Phone buzzes when an edge develops in any LCK game.

---

## Phase E: Optional simple model layer (~3-5 days)

**Goal:** Show the user a baseline probability they can compare their gut to.

**Tasks:**
1. Train basic XGBoost on the 38 games we have, using just numerical features.
2. Add "Model says: X% | Your read: Y% | Market says: Z%" comparison row.
3. Track historical agreement vs disagreement between user, model, and market — gives empirical sense of when model is useful vs misleading.

**Note:** This is NOT a gate. The model is decoration; user decisions are the edge.

**Dependencies:** Phase A.
**Deliverable:** Probability comparison row in dashboard.

---

## Phase F: Selective auto-execution (LATER, after months of using A-E)

**Goal:** Bot starts auto-executing trades in patterns user has validated.

**Tasks:**
1. Pattern library: user defines auto-execute rules
   - "Auto-buy YES if edge > 15¢ AND lead > 5k gold AND game time > 30 min AND nexus exposed"
   - "Auto-sell if my position's edge inverts by 5¢ within 60s"
2. Bot fires those rules automatically.
3. User can override or disable any pattern instantly.

**Dependencies:** A-E shipped + months of empirical data about which patterns work.
**Deliverable:** Bot trades autonomously on pre-approved patterns; user handles novel situations.

---

## Phase G: Full autonomy (FAR LATER, ~12-24 months out)

This is the original v2 destination. We get here AFTER:
1. Tool is mature (A-E)
2. Several months of manual + selective-auto trading
3. Rich feature set (composition, items, vision, recent-fight outcomes)
4. Demonstrated bot decisions are at least as good as user's in well-defined scenarios

**Not in the v2.0a plan. Listed for context.**

---

## Cross-cutting concerns

- **Tests:** Each phase has integration tests. Real-money path has explicit kill-switch checks.
- **Existing CV pipeline + Twitch auth:** Stays dormant. Re-activated when v3+ feature additions need it.
- **Spec drift:** The 2026-05-24 spec is partially obsolete. Sections §1 (edge hypothesis), §6 (data layer), §9 (live trading logic) need rewriting to reflect "human-in-the-loop" architecture. Defer until A-D is built — then update spec to match reality.
- **Daily journal:** `docs/build/v2.0a/journal/YYYY-MM-DD.md` for each session's notes.

---

## Realistic timeline

| Phase | Effort | Cumulative |
|---|---|---|
| **A. Live dashboard MVP** | 3-5 days | Week 1 |
| **B. One-click execution** | 2-3 days | Week 1-2 |
| **C. P&L tracking** | 1-2 days | Week 2 |
| **D. Multi-market + alerts** | 3-5 days | Week 2-3 |
| **E. Simple model layer** | 3-5 days | Week 3 |
| **A-E total** | **~2-3 weeks** | |
| F. Selective auto-execution | Months of usage required | Quarter 2+ |
| G. Full autonomy | 12-24 months | Year 2 |

**A-E = usable, profitable tool in ~3 weeks.** Much faster than the 10-12 weeks of the autonomous plan.

---

## Decision boundaries

Stop building v2.0a if:
- After 30 days of using Phase A-D, user's win rate / profit per game doesn't improve over manual-only
- Maintenance burden of the tool exceeds time saved
- Better venues for trading become obvious

Continue + expand to F-G if:
- Tool consistently helps user hit $100+/game across multiple games per session
- Patterns become clear enough that auto-execution feels safe
- User wants overnight coverage (LCK overnight games are the key driver)

---

**End of plan. Building Phase A now.**
