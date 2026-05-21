"""Glicko-2 rating algorithm.

Standard reference: http://www.glicko.net/glicko/glicko2.pdf

Internal state per team is (mu, phi, sigma) on Glicko-2's scaled units.
External "human-readable" ratings are r = mu * 173.7178 + 1500 and
RD = phi * 173.7178.

We treat each game as a rating period of one (i.e., the team's rating
updates immediately after each game). For multi-game series, this gives
the model independent observations rather than collapsing them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# System defaults (per Glickman's paper)
DEFAULT_RATING = 1500.0
DEFAULT_RD = 350.0
DEFAULT_VOLATILITY = 0.06
TAU = 0.5           # system constant; smaller = ratings change less in volatile periods
EPSILON = 1e-6      # convergence tolerance for sigma update
SCALE = 173.7178    # rating -> internal scale conversion


@dataclass
class GlickoState:
    """A team's Glicko-2 internal state."""
    mu: float
    phi: float
    sigma: float

    @classmethod
    def default(cls) -> "GlickoState":
        return cls(
            mu=0.0,                       # = (1500 - 1500) / 173.7178
            phi=DEFAULT_RD / SCALE,
            sigma=DEFAULT_VOLATILITY,
        )

    @property
    def rating(self) -> float:
        return self.mu * SCALE + 1500.0

    @property
    def rd(self) -> float:
        return self.phi * SCALE


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _expected_score(mu: float, mu_j: float, phi_j: float) -> float:
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def _new_sigma(phi: float, sigma: float, v: float, delta: float) -> float:
    """Update sigma via Illinois iteration (Glickman p.4)."""
    a = math.log(sigma * sigma)
    A = a
    if delta * delta > phi * phi + v:
        B = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        while _f(a - k * TAU, delta, phi, v, a) < 0:
            k += 1
        B = a - k * TAU
    fa = _f(A, delta, phi, v, a)
    fb = _f(B, delta, phi, v, a)
    while abs(B - A) > EPSILON:
        C = A + (A - B) * fa / (fb - fa)
        fc = _f(C, delta, phi, v, a)
        if fc * fb <= 0:
            A, fa = B, fb
        else:
            fa /= 2.0
        B, fb = C, fc
    return math.exp(A / 2.0)


def _f(x: float, delta: float, phi: float, v: float, a: float) -> float:
    ex = math.exp(x)
    num = ex * (delta * delta - phi * phi - v - ex)
    denom = 2.0 * (phi * phi + v + ex) * (phi * phi + v + ex)
    return num / denom - (x - a) / (TAU * TAU)


def update(
    state: GlickoState,
    opponents: list[GlickoState],
    scores: list[float],
) -> GlickoState:
    """Apply Glicko-2 rating update for a team that played one or more
    games in this period.

    Args:
        state: the team's current GlickoState (pre-update).
        opponents: list of opponents' GlickoStates at game time.
        scores: team's score against each opponent (1.0 win, 0.5 draw, 0.0 loss).

    Returns: the team's updated GlickoState.
    """
    if not opponents:
        # Period with no games: only phi increases due to volatility
        new_phi = math.sqrt(state.phi ** 2 + state.sigma ** 2)
        return GlickoState(mu=state.mu, phi=new_phi, sigma=state.sigma)

    # Step 3: compute v (variance of the rating based on game outcomes)
    v_inv = 0.0
    for opp, _ in zip(opponents, scores, strict=True):
        g_phi_j = _g(opp.phi)
        e = _expected_score(state.mu, opp.mu, opp.phi)
        v_inv += g_phi_j * g_phi_j * e * (1.0 - e)
    v = 1.0 / v_inv

    # Step 4: compute delta (estimated improvement)
    delta_sum = 0.0
    for opp, s in zip(opponents, scores, strict=True):
        g_phi_j = _g(opp.phi)
        e = _expected_score(state.mu, opp.mu, opp.phi)
        delta_sum += g_phi_j * (s - e)
    delta = v * delta_sum

    # Step 5: new volatility
    new_sigma = _new_sigma(state.phi, state.sigma, v, delta)

    # Step 6: pre-period RD
    phi_star = math.sqrt(state.phi ** 2 + new_sigma ** 2)

    # Step 7-8: new phi and mu
    new_phi = 1.0 / math.sqrt(1.0 / (phi_star ** 2) + 1.0 / v)
    new_mu = state.mu + new_phi ** 2 * delta_sum

    return GlickoState(mu=new_mu, phi=new_phi, sigma=new_sigma)
