"""Multiple-comparison tools for a set of daily-return series of competing strategy versions (numpy only, seeded).

* ``stationary_bootstrap_indices`` / ``default_block``: Politis & Romano (1994) stationary bootstrap.
* ``spa_test``: Hansen (2005) Superior Predictive Ability test of "no strategy has a positive mean" (zero benchmark), with the
  lower / consistent / upper p-values.
* ``pbo_cscv``: probability of backtest overfitting by combinatorially symmetric cross-validation (Bailey, Borwein,
  Lopez de Prado & Zhu 2015), Sharpe as the metric.
* ``annual_sharpe``, ``block_bootstrap_sharpe_ci`` and ``split_sharpe``: the annualised Sharpe of one series, its
  percentile CI and its in-sample / out-of-sample Sharpe.

Every function is deterministic for a fixed ``seed``. Nothing here reads files or the network.
"""

from __future__ import annotations

import itertools
import math
from typing import Any

import numpy as np


def default_block(n: int) -> float:
    """Mean block length for ``n`` observations: ``n ** (1/3)`` to one decimal, at least 1."""
    return max(1.0, round(float(n) ** (1.0 / 3.0), 1))


def stationary_bootstrap_indices(n: int, mean_block: float, rng: np.random.Generator) -> np.ndarray:
    """``n`` indices in ``[0, n)`` of one stationary-bootstrap resample: each position starts a new block (uniform start)
    with probability ``1 / mean_block`` and otherwise continues the previous index + 1 (wrapping around)."""
    if n <= 0:
        return np.zeros(0, dtype=np.int64)
    p = 1.0 / max(float(mean_block), 1.0)
    starts = rng.integers(0, n, size=n)
    new = rng.random(n) < p
    new[0] = True
    pos = np.arange(n)
    block_start = np.maximum.accumulate(np.where(new, pos, 0))
    base = np.where(new, starts, 0)[block_start]
    return ((base + pos - block_start) % n).astype(np.int64)


def _clean(perf: Any) -> np.ndarray:
    a = np.asarray(perf, dtype=float)
    if a.ndim == 1:
        a = a[:, None]
    return a


def spa_test(perf: np.ndarray, *, n_boot: int = 2000, mean_block: float | None = None, seed: int = 0) -> dict:
    """Hansen (2005) SPA of ``H0: max_k E[perf_k] <= 0`` for ``perf`` (T x K daily returns, positive = better).

    ``T_SPA = max(0, max_k sqrt(T) * mean_k / w_k)`` with ``w_k`` the bootstrap standard deviation of ``sqrt(T) * mean_k``.
    The null distribution resamples ``mean*_k - mean_k + mu_k`` with ``mu_k`` = ``min(mean_k, 0)`` (lower),
    ``mean_k * 1{sqrt(T) mean_k / w_k <= -sqrt(2 log log T)}`` (consistent: only clearly poor series keep their negative
    mean) or ``0`` (upper). Returns the three p-values, the index of the best studentised series and the statistic."""
    x = _clean(perf)
    t_len, k = x.shape
    block = float(mean_block) if mean_block is not None else default_block(t_len)
    rng = np.random.default_rng(seed)
    mean = x.mean(axis=0)
    boots = np.empty((n_boot, k))
    for b in range(n_boot):
        boots[b] = x[stationary_bootstrap_indices(t_len, block, rng)].mean(axis=0)
    root_t = math.sqrt(t_len)
    omega = (root_t * boots).std(axis=0, ddof=1)
    ok = omega > 1e-14
    safe = np.where(ok, omega, 1.0)
    t_k = np.where(ok, root_t * mean / safe, 0.0)
    t_spa = max(0.0, float(t_k.max()))
    threshold = math.sqrt(2.0 * math.log(math.log(t_len))) if t_len > 15 else 0.0
    mu = {"lower": np.minimum(mean, 0.0),
          "consistent": np.where(t_k <= -threshold, mean, 0.0),
          "upper": np.zeros(k)}
    p: dict[str, float] = {}
    for name, m in mu.items():
        z = root_t * (boots - mean + m) / safe
        z = np.where(ok, z, 0.0)
        stat = np.maximum(z.max(axis=1), 0.0)
        p[name] = float((stat > t_spa).mean())
    return {"p_consistent": p["consistent"], "p_lower": p["lower"], "p_upper": p["upper"], "best_index": int(np.argmax(t_k)),
            "t_stat": t_spa, "n_boot": int(n_boot), "mean_block": block, "T": int(t_len), "K": int(k)}


def _sharpe_columns(total: np.ndarray, total_sq: np.ndarray, n: int) -> np.ndarray:
    """Per-column (non-annualised) Sharpe from sums over ``n`` rows; 0 where the variance is zero."""
    mean = total / n
    var = (total_sq - n * mean ** 2) / (n - 1)
    ok = var > 1e-18
    return np.where(ok, mean / np.sqrt(np.where(ok, var, 1.0)), 0.0)


def pbo_cscv(perf: np.ndarray, *, S: int = 16) -> dict | None:
    """Probability of backtest overfitting by CSCV: split the T rows into ``S`` equal blocks (the remainder is dropped from
    the start), and for every choice of ``S/2`` blocks as in-sample take the in-sample-best series' rank among the K series on
    the remaining blocks, ``w = rank / (K + 1)`` (ties average), ``lambda = ln(w / (1 - w))``. ``pbo`` = share of
    ``lambda <= 0``. None when K < 2, S is odd or smaller than 2, or T < 2 * S."""
    x = _clean(perf)
    t_len, k = x.shape
    if k < 2 or S < 2 or S % 2 or t_len < 2 * S:
        return None
    size = t_len // S
    x = x[t_len - size * S:]
    blocks = x.reshape(S, size, k)
    b_sum, b_sq = blocks.sum(axis=1), (blocks ** 2).sum(axis=1)
    all_sum, all_sq = b_sum.sum(axis=0), b_sq.sum(axis=0)
    combos = np.array(list(itertools.combinations(range(S), S // 2)))
    n_is = size * (S // 2)
    is_sum, is_sq = b_sum[combos].sum(axis=1), b_sq[combos].sum(axis=1)
    sr_is = _sharpe_columns(is_sum, is_sq, n_is)
    sr_oos = _sharpe_columns(all_sum - is_sum, all_sq - is_sq, n_is)
    best = sr_is.argmax(axis=1)
    chosen = sr_oos[np.arange(len(combos)), best][:, None]
    rank = 1.0 + (sr_oos < chosen).sum(axis=1) + 0.5 * ((sr_oos == chosen).sum(axis=1) - 1)
    w = rank / (k + 1.0)
    lam = np.log(w / (1.0 - w))
    return {"pbo": float((lam <= 0).mean()), "n_combinations": int(len(combos)), "S": int(S), "K": int(k), "T": int(t_len)}


def _annual_sharpe(x: np.ndarray, periods_per_year: float) -> float | None:
    if len(x) < 2:
        return None
    sd = float(x.std(ddof=1))
    if not sd > 1e-14:
        return None
    return float(x.mean() / sd * math.sqrt(periods_per_year))


def annual_sharpe(returns: Any, periods_per_year: float) -> float | None:
    """Annualised Sharpe of a daily-return series; None with fewer than 2 returns or zero sample variance."""
    return _annual_sharpe(np.asarray(returns, dtype=float).ravel(), periods_per_year)


def block_bootstrap_sharpe_ci(returns: Any, *, periods_per_year: float, n_boot: int = 2000, mean_block: float | None = None,
                              conf: float = 0.95, seed: int = 0) -> dict | None:
    """Percentile stationary-bootstrap CI of the annualised Sharpe (``periods_per_year``: 365 crypto, 252 otherwise, chosen
    by the caller). None when there are fewer than 2 returns or the sample variance is zero."""
    x = np.asarray(returns, dtype=float).ravel()
    point = _annual_sharpe(x, periods_per_year)
    if point is None:
        return None
    n = len(x)
    block = float(mean_block) if mean_block is not None else default_block(n)
    rng = np.random.default_rng(seed)
    vals = np.empty(n_boot)
    for b in range(n_boot):
        s = _annual_sharpe(x[stationary_bootstrap_indices(n, block, rng)], periods_per_year)
        vals[b] = np.nan if s is None else s
    vals = vals[~np.isnan(vals)]
    if not len(vals):
        return None
    alpha = (1.0 - conf) / 2.0
    lo, hi = np.quantile(vals, [alpha, 1.0 - alpha])
    return {"sharpe": point, "ci_lower": float(lo), "ci_upper": float(hi), "conf": float(conf), "n_boot": int(n_boot),
            "mean_block": block, "n": int(n)}


def split_sharpe(returns: Any, *, oos_fraction: float = 0.25, periods_per_year: float) -> dict:
    """Annualised Sharpe of the first ``1 - oos_fraction`` of the series (in sample) and of the last ``oos_fraction``
    (out of sample); None for a part with fewer than 2 days or zero variance."""
    x = np.asarray(returns, dtype=float).ravel()
    n_oos = int(len(x) * oos_fraction)
    cut = len(x) - n_oos
    return {"is_sharpe": _annual_sharpe(x[:cut], periods_per_year), "oos_sharpe": _annual_sharpe(x[cut:], periods_per_year),
            "is_days": int(cut), "oos_days": int(n_oos)}
