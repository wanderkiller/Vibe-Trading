"""AlphaKeel-convention statistics (review D-P2-8): reference points equal AlphaKeel's own tests.

Expected values are the reference values of AlphaKeel ``tests/p3_stats.rs`` as re-computed independently by the review's
``D-stats/recompute.py`` (§1), and the R2 statistics fix's ``tests/fixtures/stats/r2_reference.py`` (DSR variance floor and
effective sample size). They are not produced by the module under test.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from backtest import alphakeel_metrics as akm

X = [0.01, -0.02, 0.03, 0.0, 0.02]
M64 = (1 << 64) - 1


class _Rng:  # SplitMix64, the sequence of alphakeel::fidelity::stats::Rng
    def __init__(self, seed):
        self.s = seed & M64

    def f(self):
        self.s = (self.s + 0x9E3779B97F4A7C15) & M64
        z = self.s
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & M64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & M64
        return ((z ^ (z >> 31)) >> 11) / float(1 << 53)


def _noisy(rng, n, mu, sd):
    return [mu + sd * (sum(rng.f() for _ in range(12)) - 6.0) for _ in range(n)]


def test_reference_points_match_alphakeel():
    assert akm.sharpe(X) == pytest.approx(0.41590019592802907, abs=1e-13)
    assert akm.sharpe(X) * math.sqrt(365) == pytest.approx(7.945762086492089, abs=1e-11)
    assert akm.sortino_annualised(X) == pytest.approx(17.08800749063506, abs=1e-10)
    g3, g4 = akm.moments(X)
    assert g3 == pytest.approx(-0.3958703373438178, abs=1e-13) and g4 == pytest.approx(1.9945215485756027, abs=1e-13)
    assert akm.sr0(1.0, 100) == pytest.approx(2.5306028932016846, abs=1e-9)
    assert akm.sr0(0.0004, 10) == pytest.approx(0.031491966026915, abs=1e-10)
    assert akm.psr(0.0866, 0, 250, -0.5, 4.0) == pytest.approx(0.9089431597820053, abs=1e-10)
    assert akm.psr(0.1, 0.05, 100, 0, 3) == pytest.approx(0.6901426136182268, abs=1e-10)
    assert akm.psr(2.0, 0, 100, 5, 3) is None  # degenerate denominator
    assert akm.pbo_cscv([[0.02, 0.01, -0.01, -0.02], [-0.02, -0.01, 0.01, 0.02]], 2) == pytest.approx(1.0)
    bb = [0.01, -0.02, 0.03, 0.0, -0.01, 0.02, 0.01, -0.01]
    assert akm.pbo_cscv([[v + 0.02 for v in bb], bb], 4) == pytest.approx(0.0)
    assert [akm.block_length(n) for n in (30, 34, 90, 150, 365)] == [4, 4, 5, 6, 8]


def test_dsr_uses_the_variance_floor_and_the_trial_count():
    """The review's counterexample: two near-identical variants made DSR ~0.97 even with 100 000 trials."""
    core = _noisy(_Rng(2026), 150, 0.00018, 0.001)
    v2 = [u + 0.000005 for u in core]
    s = [akm.sharpe(core), akm.sharpe(v2)]
    assert akm.effective_sample_size(v2) == pytest.approx(150.0)
    assert akm.dsr(v2, 2, s) == pytest.approx(0.9362548486081268, abs=1e-9)
    assert akm.dsr(v2, 1000, s) == pytest.approx(0.11289166609562647, abs=1e-9)
    assert akm.dsr(v2, 100000, s) == pytest.approx(0.009462634946427831, abs=1e-9)


def test_flat_or_tiny_series_give_explicit_none_not_huge_ratios():
    """VT's calc_metrics divided by (std + 1e-10): a flat series gave Sortino 7.6e8 on X-like data (review D §6)."""
    m = akm.alphakeel_metrics([0.001] * 10, trials=3)
    assert m["sharpe_annualised"] is None and m["sortino_annualised"] is None and m["dsr"] is None and m["skewness"] is None
    assert akm.alphakeel_metrics([0.01], trials=1)["sharpe_daily"] is None
    with pytest.raises(ValueError, match="trials"):
        akm.alphakeel_metrics(X, trials=0)
    full = akm.alphakeel_metrics(X, trials=480)
    assert full["trials"] == {"count": 480} and full["convention"] == "alphakeel.daily/1"
    assert full["sortino_annualised"] == pytest.approx(17.08800749063506, abs=1e-10)


def test_daily_returns_group_by_utc_day_over_an_explicit_capital_base():
    idx = pd.to_datetime(["2025-01-01 01:00", "2025-01-01 23:00", "2025-01-02 12:00", "2025-01-03 00:00"])
    eq = pd.Series([10_050.0, 10_100.0, 10_040.0, 10_070.0], index=idx)
    assert akm.daily_returns(eq, 1_000.0, 10_000.0) == pytest.approx([0.1, -0.06, 0.03])
    with pytest.raises(ValueError, match="capital_base"):
        akm.daily_returns(eq, 0.0, 10_000.0)


def test_mean_is_exactly_rounded_so_flat_series_has_no_moments():
    # plain sum() of ten 0.001 is 0.0010000000000000002 on Python 3.11 (3.12 compensates) -> fake 1e-38 variance
    assert akm._mean([0.001] * 10) == 0.001
    assert akm.moments([0.001] * 10) is None


# --- protocol 3.0 (AlphaKeel WP14a): ports of alphakeel_util::stats, with AlphaKeel's own test cases ---------------------

D_MS = akm.MS_PER_DAY


def test_entries_distinct_counts_rounds_and_open_entries_held_at_least_seven_days():
    end = 100 * D_MS
    # the same pair opened and closed twice counts twice + open 8 days + open 3 days (not counted) = 3 (AK entries_tests)
    entries = [(D_MS, 2 * D_MS), (3 * D_MS, 4 * D_MS), (end - 8 * D_MS, None), (end - 3 * D_MS, None)]
    assert akm.entries_distinct(entries, end) == 3
    assert akm.entries_distinct([(end - 7 * D_MS, None)], end) == 1  # exactly 7 days counts
    assert akm.entries_distinct([(end - 7 * D_MS + 1, None)], end) == 0  # 1 ms short does not
    assert akm.counted_entries([(0, None)], None) == []  # no window end: an open entry never counts


def test_median_hold_days_rounds_up_floors_at_one_and_uses_the_counted_entries():
    assert akm.median_hold_days([]) is None and akm.median_hold_days([1]) == 1
    assert akm.median_hold_days([D_MS, 3 * D_MS, 10 * D_MS]) == 3 and akm.median_hold_days([D_MS, 2 * D_MS]) == 2
    assert akm.median_hold_days([3 * D_MS + 1]) == 4
    end = 100 * D_MS
    e = [(0, 2 * D_MS), (end - 8 * D_MS, None), (10 * D_MS, 14 * D_MS)]
    assert akm.median_hold_days_of_entries(e, end) == 4
    assert akm.median_hold_days_of_entries([*e, (end - 3 * D_MS, None)], end) == 4  # a short open entry is not counted
    assert akm.median_hold_days_of_entries([(end - 3 * D_MS, None)], end) is None


def _ak_noisy(rng, n, mean, sd):  # left-fold sum, as Rust's Iterator::sum
    out = []
    for _ in range(n):
        acc = 0.0
        for _k in range(12):
            acc += rng()
        out.append(mean + sd * (acc - 6.0))
    return out


def test_politis_white_block_matches_alphakeel_reference_values():
    e = _ak_noisy(_Rng(77).f, 200, 0.0, 0.001)
    prev, ar1 = 0.0, []
    for v in e:
        prev = 0.5 * prev + v
        ar1.append(0.0003 + prev)
    assert akm.politis_white_block(ar1) == pytest.approx(3.3672729861992985, rel=1e-10)  # AK p3_stats r2 reference
    assert akm.politis_white_block(_ak_noisy(_Rng(5).f, 120, 0.0, 0.001)) == 1.0
    assert akm.politis_white_block([0.001] * 200) is None and akm.politis_white_block([0.001, 0.002, 0.0]) is None
    assert akm.cbrt(1.269021935959626) == 1.0826540622297138  # correctly rounded, as Rust's f64::cbrt (glibc gives ...136)
    assert akm.block_length(27) == 3 and akm.block_length(64) == 4  # perfect cubes are not rounded up


def test_v3_block_is_the_larger_of_politis_white_and_the_hold_median_and_never_truncated():
    x = [math.sin(i * 1.3) for i in range(60)]
    pw = math.ceil(akm.politis_white_block(x))
    assert akm.block_cap(60) == 20
    assert akm.v3_block(x, None) == (pw, None) and akm.v3_block(x, 1) == (pw, None)
    assert akm.v3_block(x, 20) == (max(pw, 20), None)
    assert akm.v3_block(x, 21) == (None, {"median_hold_days": 21, "days": 60, "block_cap": 20})
    assert akm.v3_block([0.1, -0.1, 0.2], None)[0] is None  # too short: the fallback max(2, ...) exceeds the cap 1
