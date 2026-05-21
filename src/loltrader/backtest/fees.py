"""Kalshi fee model.

Per Kalshi's public docs, the "standard" fee schedule for sports and
general markets is:

    fee_dollars = ceil(0.07 * contracts * price * (1 - price))

where price is in dollars (0.01..0.99). The factor of price*(1-price)
peaks at the midpoint (0.5) and goes to 0 at the extremes, so trading
near 50/50 markets is more expensive than trading near 95/5 markets.

This is paid by the TAKING side (the one crossing the spread); makers
pay no fee. v1 assumes we always take (aggressive limit orders).
"""
from __future__ import annotations


def kalshi_fee_cents(price_cents: int, contracts: int) -> int:
    """Compute the Kalshi standard fee in cents.

    Formula: fee_dollars = 0.07 * contracts * price * (1 - price)
    where price is in dollars. We implement this in pure integer math to
    avoid float-precision artifacts (0.07 * 100 * 0.5 * 0.5 yielded
    1.7500000000000002 in float, which math.ceil rounded up to 176c
    instead of the correct 175c).

    Integer derivation:
        7 * contracts * price_cents * (100 - price_cents) / 10_000
        (multiply through: 0.07 = 7/100, price = cents/100, (1-price) = (100-cents)/100)

    Args:
        price_cents: trade price in cents (1..99).
        contracts: number of contracts traded.

    Returns: fee in integer cents (rounded up to the nearest cent).
    """
    if contracts <= 0:
        return 0
    if price_cents < 1 or price_cents > 99:
        return 0
    numerator = 7 * contracts * price_cents * (100 - price_cents)
    # Ceiling division: (n + d - 1) // d
    return (numerator + 9_999) // 10_000


def slippage_cents() -> int:
    """v1 slippage model: assume we pay 1c worse than the displayed price.

    A more sophisticated model would account for order-book depth and
    volume, but for v1 the displayed ask is usually 1-2c worse than
    midpoint anyway, and 1c slippage on top of that is conservative.
    """
    return 1
