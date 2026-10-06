"""AlphaKeel-convention performance statistics, so Vibe-Trading numbers can be put next to AlphaKeel's (review D-P2-8).

Vibe-Trading's ``calc_metrics`` annualises bar returns of an equity curve with ``sqrt(bars_per_year)``; AlphaKeel computes
its statistics on **UTC-daily** returns = daily P&L / a fixed capital base, annualised with ``sqrt(365)``, and gates a
result with PSR/DSR/PBO using the number of trials. The two are not comparable (review D §6), so a hand-off carries the
AlphaKeel-convention numbers computed here. Specification: ``docs/audits/2026-10-06-review/D-stats/recompute.py`` (the
independent re-computation whose reference points equal AlphaKeel's ``tests/p3_stats.rs``) and, for the DSR variance floor
and the effective sample size, ``tests/fixtures/stats/r2_reference.py`` of the R2 statistics fix.

Rules that differ from ``calc_metrics`` on purpose:
  * Sharpe: mean / sample std (n − 1) per day, ``× sqrt(365)``; **None** when there are fewer than 2 days or no variance
    (never ``mean / (std + 1e-10)``, which turns a flat series into an astronomically large ratio).
  * Sortino (Sortino & Price): downside deviation ``sqrt(mean(min(r, 0)²))`` over **all** n days, target 0; None when it is 0.
  * Skewness / kurtosis: population moments, kurtosis non-excess (normal = 3) — the γ4 that PSR's formula expects.
  * PSR(SR*) = Φ((SR − SR*)·sqrt(T − 1) / sqrt(1 − γ3·SR + (γ4 − 1)/4·SR²)); None when the denominator is not positive.
  * DSR = PSR(SR0) with SR0 = sqrt(V)·((1 − γ)Φ⁻¹(1 − 1/N) + γΦ⁻¹(1 − 1/(N·e))), N = trials, V = max(cross-variant
    variance of per-day Sharpes, the estimator's own variance (1 − γ3·SR + (γ4 − 1)/4·SR²)/(T_eff − 1)), T_eff from a
    Newey–West long-run variance (Bartlett, L = ⌊4·(n/100)^(2/9)⌋), T_eff = n·min(1, γ0/LRV).
  * Stationary-bootstrap block length rule used for intervals: max(2, ⌈n^(1/3)⌉) days.
  * PBO: CSCV (Bailey, Borwein, López de Prado & Zhu 2017) over S blocks.
Only the standard library is used (``statistics.NormalDist``), like the specification.
"""

from __future__ import annotations

import itertools
import math
import statistics
from typing import Sequence

import pandas as pd

ND = statistics.NormalDist()
EULER_GAMMA = 0.5772156649015329
DAYS_PER_YEAR = 365


def _mean(x: Sequence[float]) -> float:
    return sum(x) / len(x)


def _cdf(z: float) -> float:  # tail-exact Φ
    return 0.5 * math.erfc(-z / math.sqrt(2))


def daily_returns(equity: pd.Series, capital_base: float, initial_equity: float) -> list[float]:
    """UTC-daily returns = (equity at the day's last bar − previous day's) / ``capital_base``; day 0 starts from
    ``initial_equity``. ``capital_base`` is explicit (AlphaKeel: the deposit shared by every variant) — never inferred."""
    if capital_base <= 0:
        raise ValueError("capital_base must be positive")
    if equity.empty:
        return []
    idx = pd.DatetimeIndex(equity.index)
    idx = idx.tz_convert("UTC") if idx.tz is not None else idx.tz_localize("UTC")
    eod = pd.Series(equity.to_numpy(dtype=float), index=idx).groupby(idx.normalize()).last()
    prev = [float(initial_equity)] + eod.tolist()[:-1]
    return [(e - p) / capital_base for e, p in zip(eod.tolist(), prev)]


def sharpe(x: Sequence[float]) -> float | None:
    if len(x) < 2:
        return None
    sd = statistics.stdev(x)
    return None if sd == 0 else _mean(x) / sd


def sortino_annualised(x: Sequence[float]) -> float | None:
    if not x:
        return None
    dd = math.sqrt(sum(min(v, 0.0) ** 2 for v in x) / len(x))
    return None if dd == 0 else _mean(x) / dd * math.sqrt(DAYS_PER_YEAR)


def moments(x: Sequence[float]) -> tuple[float, float] | None:
    """(skewness, non-excess kurtosis), population moments; None without variance."""
    if len(x) < 2:
        return None
    m = _mean(x)
    n = len(x)
    m2 = sum((v - m) ** 2 for v in x) / n
    if m2 == 0:
        return None
    m3 = sum((v - m) ** 3 for v in x) / n
    m4 = sum((v - m) ** 4 for v in x) / n
    return m3 / m2 ** 1.5, m4 / m2 ** 2


def psr(sr: float, sr_star: float, t: float, g3: float, g4: float) -> float | None:
    d2 = 1 - g3 * sr + (g4 - 1) / 4 * sr * sr
    if d2 <= 0 or t <= 1:
        return None
    return _cdf((sr - sr_star) * math.sqrt(t - 1) / math.sqrt(d2))


def sr0(var_sr: float, n_trials: int) -> float:
    if n_trials < 2 or var_sr <= 0:
        return 0.0
    return math.sqrt(var_sr) * ((1 - EULER_GAMMA) * ND.inv_cdf(1 - 1 / n_trials)
                                + EULER_GAMMA * ND.inv_cdf(1 - 1 / (n_trials * math.e)))


def _autocov(x: Sequence[float], k: int) -> float:
    m = _mean(x)
    n = len(x)
    return sum((x[t] - m) * (x[t - k] - m) for t in range(k, n)) / n


def effective_sample_size(x: Sequence[float]) -> float:
    """T_eff = n·min(1, γ0/LRV) with a Newey–West (Bartlett) long-run variance; positive autocorrelation shrinks it."""
    n = len(x)
    lag = math.floor(4 * (n / 100) ** (2 / 9))
    g0 = _autocov(x, 0)
    lrv = g0 + sum(2 * (1 - k / (lag + 1)) * _autocov(x, k) for k in range(1, lag + 1))
    if g0 == 0 or lrv <= 0:
        return float(n)
    return n * min(1.0, g0 / lrv)


def dsr(x: Sequence[float], n_trials: int, variant_sharpes: Sequence[float] = ()) -> float | None:
    s = sharpe(x)
    mo = moments(x)
    if s is None or mo is None:
        return None
    g3, g4 = mo
    te = effective_sample_size(x)
    if te <= 1:
        return None
    cross = statistics.variance(list(variant_sharpes)) if len(variant_sharpes) >= 2 else 0.0
    floor = (1 - g3 * s + (g4 - 1) / 4 * s * s) / (te - 1)
    return psr(s, sr0(max(cross, floor), n_trials), te, g3, g4)


def block_length(n: int) -> int:
    return max(2, math.ceil(n ** (1 / 3)))


def pbo_cscv(variant_returns: Sequence[Sequence[float]], s_blocks: int) -> float | None:
    """Probability of backtest overfitting by CSCV; None when it cannot be computed (fewer than 2 variants or blocks)."""
    n_var = len(variant_returns)
    if n_var < 2 or s_blocks < 2 or s_blocks % 2:
        return None
    t = min(len(r) for r in variant_returns)
    blk = t // s_blocks
    if blk < 2:
        return None
    blocks = [list(range(b * blk, (b + 1) * blk)) for b in range(s_blocks)]
    le0 = combos = 0
    for is_ in itertools.combinations(range(s_blocks), s_blocks // 2):
        oos = [b for b in range(s_blocks) if b not in is_]
        ii = [i for b in is_ for i in blocks[b]]
        oi = [i for b in oos for i in blocks[b]]
        is_sr = [sharpe([r[i] for i in ii]) for r in variant_returns]
        oos_sr = [sharpe([r[i] for i in oi]) for r in variant_returns]
        cand = [(v, -k) for k, v in enumerate(is_sr) if v is not None]
        if not cand:
            continue
        best = -max(cand)[1]
        o = [v if v is not None else -math.inf for v in oos_sr]
        rank = sum(1 for v in o if v < o[best]) + (sum(1 for v in o if v == o[best]) + 1) / 2
        w = rank / (n_var + 1)
        combos += 1
        le0 += math.log(w / (1 - w)) <= 0
    return le0 / combos if combos else None


def alphakeel_metrics(returns: Sequence[float], *, trials: int, variant_sharpes: Sequence[float] = ()) -> dict:
    """The AlphaKeel-convention statistics of a UTC-daily return series, with the trial count they depend on."""
    if trials < 1:
        raise ValueError("trials must be at least 1: it is the number of parameter sets tried for this result")
    x = list(returns)
    s = sharpe(x)
    mo = moments(x)
    return {
        "convention": "alphakeel.daily/1",
        "days": len(x),
        "mean_daily_return": _mean(x) if x else None,
        "sharpe_daily": s,
        "sharpe_annualised": None if s is None else s * math.sqrt(DAYS_PER_YEAR),
        "sortino_annualised": sortino_annualised(x),
        "skewness": None if mo is None else mo[0],
        "kurtosis_non_excess": None if mo is None else mo[1],
        "psr_vs_zero": None if (s is None or mo is None) else psr(s, 0.0, len(x), mo[0], mo[1]),
        "dsr": dsr(x, trials, variant_sharpes),
        "effective_days": effective_sample_size(x) if len(x) >= 2 else None,
        "bootstrap_block_days": block_length(len(x)) if x else None,
        "trials": {"count": int(trials)},
    }
