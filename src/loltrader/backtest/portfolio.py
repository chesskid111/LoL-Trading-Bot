"""Portfolio: position sizing (fractional Kelly + caps), exposure tracking,
PnL accounting.

All monetary values are integer cents to avoid float drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Side = Literal["YES", "NO"]


@dataclass
class Position:
    market_ticker: str
    match_id: int
    side: Side
    contracts: int
    entry_price_cents: int      # what we paid per contract
    entry_fee_cents: int        # Kalshi fee on the buy
    entry_date: str             # ISO date of entry
    model_prob: float           # model's p(YES) at entry
    market_implied: float       # market's implied p(YES) at entry (yes_ask/100)
    edge: float                 # at entry: signed edge (positive for YES, positive when buying NO)
    p10: float
    p90: float
    # Filled at settlement:
    exit_value_cents: int | None = None    # 100 if our side won, 0 if not
    pnl_cents: int | None = None           # final realized PnL
    settled_date: str | None = None

    @property
    def cost_basis_cents(self) -> int:
        """Total dollars laid out on this position (price + fee)."""
        return self.contracts * self.entry_price_cents + self.entry_fee_cents

    def settle(self, yes_won: bool, settled_date: str) -> int:
        """Mark the position settled. Returns realized PnL in cents."""
        if self.side == "YES":
            exit_per_contract = 100 if yes_won else 0
        else:  # NO
            exit_per_contract = 0 if yes_won else 100
        revenue = self.contracts * exit_per_contract
        pnl = revenue - self.cost_basis_cents
        self.exit_value_cents = exit_per_contract
        self.pnl_cents = pnl
        self.settled_date = settled_date
        return pnl


@dataclass
class Portfolio:
    starting_bankroll_cents: int
    kelly_fraction: float = 0.25
    max_position_pct: float = 0.05            # 5% of bankroll per market
    max_total_exposure_pct: float = 0.20      # 20% of bankroll across all open

    bankroll_cents: int = field(init=False)
    open_positions: list[Position] = field(default_factory=list)
    closed_positions: list[Position] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.bankroll_cents = self.starting_bankroll_cents

    @property
    def total_pnl_cents(self) -> int:
        return sum(p.pnl_cents or 0 for p in self.closed_positions)

    @property
    def open_exposure_cents(self) -> int:
        return sum(p.cost_basis_cents for p in self.open_positions)

    def kelly_size_contracts(
        self,
        side: Side,
        model_prob: float,
        price_cents: int,
    ) -> int:
        """Fractional Kelly position size in whole contracts.

        For a binary contract:
            payout if win = (1 - price)        # net gain per $1 contract
            payout if lose = -price            # net loss per $1 contract
            f* = (p * b - q) / b
            where b = payout_win / payout_loss (decimal odds)
                  p = our probability of winning
                  q = 1 - p

        For buying YES: p = model_prob, win payout = 1 - price_cents/100.
        For buying NO:  p = 1 - model_prob, win payout = price_cents/100.
        """
        price = price_cents / 100.0
        if side == "YES":
            p = model_prob
            payout_win = 1.0 - price
        else:
            p = 1.0 - model_prob
            payout_win = price
        if payout_win <= 0:
            return 0
        f_full = (p * payout_win - (1 - p) * (1.0 - payout_win)) / payout_win
        if f_full <= 0:
            return 0  # negative or zero Kelly = no edge
        f = f_full * self.kelly_fraction
        # Convert bankroll fraction to dollar cost, then to contracts at the
        # cost-per-contract = price_cents (we pay price per contract upfront).
        dollar_size_cents = int(self.bankroll_cents * f)
        return max(0, dollar_size_cents // max(price_cents, 1))

    def can_open(
        self,
        side: Side,
        contracts: int,
        price_cents: int,
        entry_fee_cents: int,
    ) -> tuple[bool, str]:
        """Risk gates. Returns (ok, reason_if_not)."""
        cost = contracts * price_cents + entry_fee_cents
        if contracts <= 0:
            return False, "zero_contracts"
        if cost > self.bankroll_cents:
            return False, "insufficient_balance"
        per_market_cap = int(self.starting_bankroll_cents * self.max_position_pct)
        if cost > per_market_cap:
            return False, "exceeds_per_market_cap"
        total_cap = int(self.starting_bankroll_cents * self.max_total_exposure_pct)
        if self.open_exposure_cents + cost > total_cap:
            return False, "exceeds_total_exposure_cap"
        return True, ""

    def cap_contracts(
        self,
        side: Side,
        contracts: int,
        price_cents: int,
        entry_fee_cents_for: callable,
    ) -> tuple[int, str]:
        """Reduce a desired contract count to fit within caps.
        Returns (allowed_contracts, reason). entry_fee_cents_for(n) lets
        the caller recompute fees for the trimmed size."""
        per_market_cap = int(self.starting_bankroll_cents * self.max_position_pct)
        total_cap = int(self.starting_bankroll_cents * self.max_total_exposure_pct)
        remaining_total = total_cap - self.open_exposure_cents
        # Iterate down from desired to find max feasible (could solve in closed form
        # but contracts is small enough that linear scan is fine).
        for n in range(contracts, 0, -1):
            cost = n * price_cents + entry_fee_cents_for(n)
            if cost <= per_market_cap and cost <= remaining_total and cost <= self.bankroll_cents:
                return n, "ok"
        return 0, "no_feasible_size"

    def open_position(self, pos: Position) -> None:
        self.open_positions.append(pos)
        self.bankroll_cents -= pos.cost_basis_cents

    def settle_position(self, pos: Position, yes_won: bool, settled_date: str) -> int:
        pnl = pos.settle(yes_won, settled_date)
        # Move from open to closed
        if pos in self.open_positions:
            self.open_positions.remove(pos)
        self.closed_positions.append(pos)
        # Receive the payout
        revenue = pos.contracts * (pos.exit_value_cents or 0)
        self.bankroll_cents += revenue
        return pnl

    def equity_cents(self) -> int:
        """Total equity = bankroll + value of open positions (marked at cost)."""
        return self.bankroll_cents + self.open_exposure_cents
