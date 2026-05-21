"""Kill switches (spec section 9).

Three levels:
  - SOFT: stop opening new positions; hold existing. Auto-resumes when
    the triggering condition clears.
  - HARD: cancel any in-flight orders, hold positions. Manual restart only.
  - EMERGENCY: cancel + attempt to flatten positions. Post-mortem required.

For v1 paper trading there are no "in-flight" orders to cancel — the
trader just stops opening new positions. The kill state is consulted by
the trader's main loop.

Plus a file-based manual kill (`data/KILL_SWITCH`): if the file exists,
the bot soft-kills within 5 seconds. Lowest-tech, most reliable failsafe.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal


class KillLevel(Enum):
    NONE = "none"
    SOFT = "soft"
    HARD = "hard"
    EMERGENCY = "emergency"


@dataclass
class KillState:
    level: KillLevel
    reason: str = ""
    triggered_at: int = 0


def manual_killfile_present(kill_file: Path) -> bool:
    return kill_file.exists()


def evaluate_kill_state(
    *,
    daily_pnl_cents: int,
    session_pnl_cents: int,
    starting_bankroll_cents: int,
    last_data_seen_ts: int,
    now_ts: int,
    kill_file: Path,
    data_staleness_threshold_sec: int = 60,
    soft_drawdown_pct: float = 0.10,
    hard_drawdown_pct: float = 0.20,
    emergency_drawdown_pct: float = 0.30,
) -> KillState:
    """Compute the current kill state given session vitals.

    Priority (highest first):
      1. EMERGENCY: > emergency_drawdown_pct lost
      2. HARD: > hard_drawdown_pct lost OR data feed dead long
      3. SOFT: > soft_drawdown_pct daily lost OR data stale OR manual file
      4. NONE
    """
    soft_floor = -int(starting_bankroll_cents * soft_drawdown_pct)
    hard_floor = -int(starting_bankroll_cents * hard_drawdown_pct)
    emerg_floor = -int(starting_bankroll_cents * emergency_drawdown_pct)

    if session_pnl_cents < emerg_floor:
        return KillState(KillLevel.EMERGENCY, "session_drawdown_emergency", now_ts)
    if session_pnl_cents < hard_floor:
        return KillState(KillLevel.HARD, "session_drawdown_hard", now_ts)
    if (now_ts - last_data_seen_ts) > data_staleness_threshold_sec:
        return KillState(KillLevel.HARD, "data_feed_dead", now_ts)
    if daily_pnl_cents < soft_floor:
        return KillState(KillLevel.SOFT, "daily_drawdown_soft", now_ts)
    if manual_killfile_present(kill_file):
        return KillState(KillLevel.SOFT, "manual_killfile", now_ts)
    return KillState(KillLevel.NONE)
