"""Multiple-testing tools: stationary bootstrap, Hansen SPA, CSCV PBO, bootstrap Sharpe CI and the IS/OOS split."""

from __future__ import annotations

import numpy as np
import pytest

from backtest.multiple_testing import (
    block_bootstrap_sharpe_ci,
    default_block,
    pbo_cscv,
    spa_test,
    split_sharpe,
    stationary_bootstrap_indices,
)


def _noise(t: int = 750, k: int = 10, seed: int = 11) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, 0.01, size=(t, k))


def test_default_block() -> None:
    assert default_block(1000) == 10.0
    assert default_block(750) == 9.1
    assert default_block(1) == 1.0


def test_bootstrap_indices_shape_range_and_mean_block_length() -> None:
    n, block = 200_000, 10.0
    idx = stationary_bootstrap_indices(n, block, np.random.default_rng(3))
    assert idx.shape == (n,) and idx.min() >= 0 and idx.max() < n
    continues = (idx[1:] == (idx[:-1] + 1) % n)
    blocks = 1 + int((~continues).sum())
    assert n / blocks == pytest.approx(block, rel=0.05)
    iid = stationary_bootstrap_indices(1000, 1.0, np.random.default_rng(3))
    assert iid.shape == (1000,)
    assert stationary_bootstrap_indices(0, 5.0, np.random.default_rng(0)).shape == (0,)


def test_spa_on_pure_noise_does_not_reject() -> None:
    out = spa_test(_noise(), n_boot=1000, seed=0)
    assert out["p_consistent"] > 0.05
    assert out["T"] == 750 and out["K"] == 10 and out["n_boot"] == 1000
    assert out["p_lower"] <= out["p_consistent"] <= out["p_upper"]


def test_spa_finds_a_column_with_a_strong_drift() -> None:
    x = _noise(seed=5)
    x[:, 6] = np.random.default_rng(99).normal(0.002, 0.01, size=750)
    out = spa_test(x, n_boot=1000, seed=0)
    assert out["best_index"] == 6
    assert out["p_consistent"] < 0.05
    assert out["p_lower"] <= out["p_consistent"] <= out["p_upper"]


def test_spa_is_deterministic_for_a_seed() -> None:
    x = _noise(t=300, k=4)
    assert spa_test(x, n_boot=300, seed=7) == spa_test(x, n_boot=300, seed=7)


def test_spa_survives_a_constant_column() -> None:
    x = _noise(t=200, k=3)
    x[:, 1] = 0.0
    out = spa_test(x, n_boot=200, seed=0)
    assert 0.0 <= out["p_consistent"] <= 1.0 and out["t_stat"] >= 0.0 and out["best_index"] != 1


def test_pbo_on_noise_is_about_one_half() -> None:
    out = pbo_cscv(_noise(t=640, k=10, seed=21), S=16)
    assert out is not None and out["n_combinations"] == 12870 and out["S"] == 16 and out["K"] == 10 and out["T"] == 640
    assert 0.3 <= out["pbo"] <= 0.7


def test_pbo_is_low_when_one_column_dominates() -> None:
    x = _noise(t=640, k=8, seed=22)
    x[:, 3] += 0.004
    out = pbo_cscv(x, S=16)
    assert out is not None and out["pbo"] < 0.2


def test_pbo_drops_the_remainder_from_the_start_and_refuses_small_inputs() -> None:
    x = _noise(t=645, k=4, seed=23)
    assert pbo_cscv(x, S=16) == pbo_cscv(x[5:], S=16) | {"T": 645}
    assert pbo_cscv(_noise(k=1), S=16) is None
    assert pbo_cscv(_noise(t=31, k=3), S=16) is None


def test_sharpe_ci_contains_the_sample_sharpe_and_is_deterministic() -> None:
    x = np.random.default_rng(31).normal(0.0008, 0.01, size=500)
    ci = block_bootstrap_sharpe_ci(x, periods_per_year=365, n_boot=1000, seed=0)
    assert ci is not None and ci["ci_lower"] <= ci["sharpe"] <= ci["ci_upper"]
    assert ci["sharpe"] == pytest.approx(x.mean() / x.std(ddof=1) * np.sqrt(365))
    assert ci == block_bootstrap_sharpe_ci(x, periods_per_year=365, n_boot=1000, seed=0)
    ci252 = block_bootstrap_sharpe_ci(x, periods_per_year=252, n_boot=1000, seed=0)
    assert ci252["sharpe"] == pytest.approx(ci["sharpe"] * np.sqrt(252 / 365))


def test_sharpe_ci_is_none_without_variance() -> None:
    assert block_bootstrap_sharpe_ci([0.0] * 100, periods_per_year=365) is None
    assert block_bootstrap_sharpe_ci([0.01], periods_per_year=365) is None


def test_split_sharpe_uses_the_last_quarter_as_oos() -> None:
    x = np.concatenate([np.random.default_rng(1).normal(0.002, 0.01, 300), np.random.default_rng(2).normal(-0.002, 0.01, 100)])
    out = split_sharpe(x, periods_per_year=365)
    assert out["is_days"] == 300 and out["oos_days"] == 100
    assert out["is_sharpe"] > 0 > out["oos_sharpe"]
    assert out["is_sharpe"] == pytest.approx(x[:300].mean() / x[:300].std(ddof=1) * np.sqrt(365))
    assert split_sharpe([0.0] * 40, periods_per_year=252)["oos_sharpe"] is None
