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
  * Stationary-bootstrap block length rule used for intervals: max(2, ⌈n^(1/3)⌉) days (protocol <= 2.8); protocol 3.0:
    max(⌈Politis–White⌉, median holding days), see :func:`v3_block`.
  * PBO: CSCV (Bailey, Borwein, López de Prado & Zhu 2017) over S blocks.
Only the standard library is used (``statistics.NormalDist``), like the specification.
"""

from __future__ import annotations

import itertools
import math
import random
import statistics
from fractions import Fraction
from typing import Sequence

import pandas as pd

ND = statistics.NormalDist()
EULER_GAMMA = 0.5772156649015329
DAYS_PER_YEAR = 365


def _mean(x: Sequence[float]) -> float:
    return math.fsum(x) / len(x)  # exact-rounded: plain sum() differs across Python 3.11/3.12


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
    m2 = math.fsum((v - m) ** 2 for v in x) / n
    if m2 == 0:
        return None
    m3 = math.fsum((v - m) ** 3 for v in x) / n
    m4 = math.fsum((v - m) ** 4 for v in x) / n
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


def dsr_inputs(x: Sequence[float], n_trials: int, variant_sharpes: Sequence[float] = ()) -> dict | None:
    """Everything :func:`dsr` feeds into ``psr(sharpe, sr0(var_sr_used, trials), effective_days, skew, kurt)``; None when
    the DSR is not defined. ``var_sr_used = max(cross-variant variance of per-day Sharpes, the estimator's own variance)``.
    Published in ``robustness.json`` so a reader (AlphaKeel's G1 precheck) can recompute the DSR with another trial count."""
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
    return {"sharpe_daily": s, "skewness": g3, "kurtosis_non_excess": g4, "effective_days": te, "var_sr_used": max(cross, floor),
            "trials": int(n_trials)}


def dsr(x: Sequence[float], n_trials: int, variant_sharpes: Sequence[float] = ()) -> float | None:
    i = dsr_inputs(x, n_trials, variant_sharpes)
    if i is None:
        return None
    return psr(i["sharpe_daily"], sr0(i["var_sr_used"], n_trials), i["effective_days"], i["skewness"], i["kurtosis_non_excess"])


def cbrt(a: float) -> float:
    """Correctly rounded cube root, as Rust's ``f64::cbrt`` (glibc's ``cbrt`` behind ``math.cbrt`` can be 1 ulp off, e.g.
    1.269021935959626): ``math.cbrt`` corrected against the exact midpoints to its neighbours."""
    if a < 0:
        return -cbrt(-a)
    y = math.cbrt(a)
    if a == 0 or not math.isfinite(y):
        return y
    fa = Fraction(a)
    while True:
        up = math.nextafter(y, math.inf)
        if ((Fraction(y) + Fraction(up)) / 2) ** 3 < fa:
            y = up
            continue
        down = math.nextafter(y, 0.0)
        if ((Fraction(y) + Fraction(down)) / 2) ** 3 > fa:
            y = down
            continue
        return y


def block_length(n: int) -> int:
    """Protocol <= 2.8 mean block max(2, ceil(n^(1/3))) (AlphaKeel ``ProtocolBootstrap::mean_block``, ``f64::cbrt``:
    ``n ** (1/3)`` is 3.0000000000000004 for n = 27 and would round a perfect cube up)."""
    return max(2, math.ceil(cbrt(n)))


# ---------------------------------------------------------------------------------------------------------------------
# Protocol 3.0 (AlphaKeel WP14a): distinct entries, median holding days and the mean block of 1.8 / 3.4.
# Line-by-line ports of AlphaKeel ``alphakeel_util::stats`` (``counted_entries``, ``entries_distinct``, ``median_hold_days``,
# ``median_hold_days_of_entries``, ``politis_white_block``, ``block_cap``, ``ProtocolBootstrap::v3_block``).
# ---------------------------------------------------------------------------------------------------------------------

MS_PER_DAY = 86_400_000
#: an entry still open at the window end counts (entries and median hold) only when held at least this long (protocol 3.0)
OPEN_ENTRY_MIN_HOLD_MS = 7 * MS_PER_DAY


def counted_entries(entries: Sequence[tuple[int, int | None]], end_ms: int | None) -> list[tuple[int, int]]:
    """The entries protocol 3.0 counts, as ``(open_ms, close_ms or end_ms)``: every closed one; one still open at ``end_ms``
    only when ``end_ms - open_ms >= 7 days`` (``end_ms`` None = never). Input: one ``(open_ms, close_ms or None)`` per entry
    (round): the same pair opened and closed twice is two entries."""
    out = []
    for open_ms, close_ms in entries:
        if close_ms is not None:
            out.append((open_ms, close_ms))
        elif end_ms is not None and end_ms - open_ms >= OPEN_ENTRY_MIN_HOLD_MS:
            out.append((open_ms, end_ms))
    return out


def entries_distinct(entries: Sequence[tuple[int, int | None]], end_ms: int) -> int:
    return len(counted_entries(entries, end_ms))


def median_hold_days(holds_ms: Sequence[int]) -> int | None:
    """Median holding time in days, rounded up, at least 1 (even count: mean of the two middle ones); None when empty."""
    if not holds_ms:
        return None
    v = sorted(max(0, int(h)) for h in holds_ms)
    n = len(v)
    twice = v[n // 2] * 2 if n % 2 == 1 else v[n // 2 - 1] + v[n // 2]
    return max(1, (twice + 2 * MS_PER_DAY - 1) // (2 * MS_PER_DAY))


def median_hold_days_of_entries(entries: Sequence[tuple[int, int | None]], end_ms: int | None) -> int | None:
    return median_hold_days([c - o for o, c in counted_entries(entries, end_ms)])


def _seq_sum(xs) -> float:
    acc = 0.0  # left fold like Rust's Iterator::sum (Python 3.12's sum() of floats is compensated)
    for v in xs:
        acc += v
    return acc


def _ak_mean(x: Sequence[float]) -> float:
    return _seq_sum(x) / len(x)


def _ak_autocov(x: Sequence[float], k: int) -> float | None:
    m = _ak_mean(x)
    return _seq_sum((x[t] - m) * (x[t - k] - m) for t in range(k, len(x))) / len(x) if k < len(x) else None


def block_cap(n: int) -> int:
    """Mean block cap ceil(min(3·sqrt(n), n/3)) (Politis–White's own cap and the protocol 3.0 block cap)."""
    return math.ceil(min(3.0 * math.sqrt(n), n / 3))


def politis_white_block(x: Sequence[float]) -> float | None:
    """Politis & White (2004) optimal mean block of the stationary bootstrap with the Patton, Politis & White (2009)
    correction (AlphaKeel ``politis_white_block``); None when unavailable (too short, no variance, long-run variance <= 0)."""
    n = len(x)
    kn = max(5, math.ceil(math.sqrt(math.log10(max(n, 2)))))
    m_max = math.ceil(math.sqrt(n)) + kn
    if n < 2 * (m_max + 1):
        return None
    g0r = _ak_autocov(x, 0)
    m = _ak_mean(x)
    if g0r is None or not (math.isfinite(g0r) and g0r > 0 and math.sqrt(g0r) > 1e-12 * abs(m)):
        return None
    acv = [_ak_autocov(x, k) for k in range(m_max + kn + 1)]
    if any(a is None for a in acv):
        return None
    cv = 2.0 * math.sqrt(math.log10(n) / n)
    mhat = next((mm for mm in range(m_max + 1) if all(abs(acv[mm + k] / g0r) < cv for k in range(1, kn + 1))), None)
    big_m = m_max if mhat is None else min(2 * mhat, m_max)
    if big_m == 0:
        return 1.0

    def lam(t: float) -> float:
        a = abs(t)
        return 1.0 if a <= 0.5 else (2.0 * (1.0 - a) if a <= 1.0 else 0.0)

    g, g0 = 0.0, acv[0]
    for k in range(1, big_m + 1):
        w = lam(k / big_m)
        g += 2.0 * w * k * acv[k]
        g0 += 2.0 * w * acv[k]
    if g0 <= 0 or not math.isfinite(g0):
        return None
    d_sb = 2.0 * g0 * g0
    b = cbrt(2.0 * g * g / d_sb) * cbrt(n)
    return min(max(b, 1.0), float(block_cap(n)))


def v3_block(returns: Sequence[float], median_hold: int | None) -> tuple[int | None, dict | None]:
    """Protocol 3.0 mean block (1.8 / 3.4, threshold ``block = max_politis_white_median_hold``): max(ceil(Politis–White)
    (unavailable: :func:`block_length`), median holding days (>= 1; None = no hold data: Politis–White alone)).
    ``(block, None)``, or ``(None, {"median_hold_days", "days", "block_cap"})`` when it exceeds :func:`block_cap` (the
    sample cannot cover the holding period: FAIL, never truncated)."""
    n = len(returns)
    pw = politis_white_block(returns)
    pw_i = max(1, math.ceil(pw)) if pw is not None else block_length(n)
    hold = 0 if median_hold is None else max(1, int(median_hold))
    block, cap = max(pw_i, hold), block_cap(n)
    if block > cap:
        return None, {"median_hold_days": 0 if median_hold is None else int(median_hold), "days": n, "block_cap": cap}
    return block, None


def stationary_bootstrap_mean_ci(x: Sequence[float], *, seed: int, resamples: int = 2000, confidence: float = 0.95,
                                 block: int | None = None) -> dict | None:
    """Politis–Romano stationary bootstrap of the mean daily return: each resample walks the series circularly from a
    uniform random start, jumping to a new uniform start with probability 1/``block`` before every next draw (geometric
    block lengths, mean ``block`` = :func:`block_length` by default). Percentile interval (nearest rank). ``random.Random(seed)``
    makes it deterministic. None with fewer than 2 points. (``quantlib.timeseries.bootstrap_statistic`` is the i.i.d.
    bootstrap; daily P&L of a carry book is autocorrelated, which an i.i.d. resample understates.)"""
    n = len(x)
    if n < 2 or resamples < 1 or not 0 < confidence < 1:
        return None
    blk = block or block_length(n)
    p = 1.0 / blk
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        i = rng.randrange(n)
        acc = [x[i]]
        for _k in range(n - 1):
            i = rng.randrange(n) if rng.random() < p else (i + 1) % n
            acc.append(x[i])
        means.append(math.fsum(acc) / n)
    means.sort()
    a = (1 - confidence) / 2
    lo = means[max(0, math.ceil(a * resamples) - 1)]
    hi = means[min(resamples - 1, math.ceil((1 - a) * resamples) - 1)]
    return {"method": "stationary_bootstrap/politis-romano", "statistic": "mean_daily_return", "mean": _mean(x), "ci_lower": lo,
            "ci_upper": hi, "confidence": confidence, "resamples": resamples, "block_days": blk, "seed": seed}


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
