"""Statistics helpers with no SciPy dependency.

Beta quantiles are needed to report a credible interval for the Bayesian
posterior.  Implemented via the regularised incomplete beta function's
continued fraction (Lentz's algorithm), then bisection on the CDF.
"""

from __future__ import annotations

import math

_EPS = 3.0e-12
_ITMAX = 300


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-300:
        d = 1e-300
    d = 1.0 / d
    h = d
    for m in range(1, _ITMAX + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return h


def beta_cdf(x: float, a: float, b: float) -> float:
    """Regularised incomplete beta function I_x(a, b) = P[X <= x] for X~Beta(a,b)."""
    if a <= 0 or b <= 0:
        raise ValueError("a and b must be positive")
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0

    ln_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    # log of x^a (1-x)^b / B(a,b); the two branches only differ by the /a vs /b
    log_front = a * math.log(x) + b * math.log(1.0 - x) - ln_beta
    front_a = math.exp(log_front) / a

    if x < (a + 1.0) / (a + b + 2.0):
        return front_a * _betacf(a, b, x)

    # symmetry: I_x(a, b) = 1 - I_{1-x}(b, a), and that branch divides by b
    front_b = front_a * a / b
    return 1.0 - front_b * _betacf(b, a, 1.0 - x)


def beta_quantile(q: float, a: float, b: float) -> float:
    """Inverse CDF of the Beta distribution, by bisection."""
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0, 1]")
    if q <= 0.0:
        return 0.0
    if q >= 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if beta_cdf(mid, a, b) < q:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-9:
            break
    return (lo + hi) / 2.0


def beta_mean(a: float, b: float) -> float:
    return a / (a + b)


def beta_variance(a: float, b: float) -> float:
    total = a + b
    return (a * b) / (total * total * (total + 1.0))


def normal_cdf(x: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    if sigma <= 0:
        raise ValueError("sigma must be positive")
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


__all__ = ["beta_cdf", "beta_quantile", "beta_mean", "beta_variance", "normal_cdf"]
