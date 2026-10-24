"""A negative specific variance must raise, not be read as zero idiosyncratic risk."""

from __future__ import annotations

import pandas as pd
import pytest

from src.quantlib.factormodel import factor_risk_decomposition


def _inputs():
    weights = {"A": 0.5, "B": 0.5}
    exposures = pd.DataFrame({"value": [1.0, 0.5]}, index=["A", "B"])
    factor_cov = pd.DataFrame({"value": [0.0625]}, index=["value"])
    return weights, exposures, factor_cov


def test_negative_specific_variance_raises() -> None:
    weights, exposures, factor_cov = _inputs()
    with pytest.raises(ValueError, match="specific_variances must be non-negative"):
        factor_risk_decomposition(weights, exposures, factor_cov, {"A": 0.04, "B": -0.04})


def test_rounding_noise_is_still_clipped() -> None:
    weights, exposures, factor_cov = _inputs()
    noisy = factor_risk_decomposition(weights, exposures, factor_cov, {"A": 0.04, "B": -1e-12})
    clean = factor_risk_decomposition(weights, exposures, factor_cov, {"A": 0.04, "B": 0.0})
    assert noisy.total_variance == pytest.approx(clean.total_variance)


def test_valid_specific_variances_add_their_risk() -> None:
    weights, exposures, factor_cov = _inputs()
    result = factor_risk_decomposition(weights, exposures, factor_cov, {"A": 0.04, "B": 0.04})
    assert result.specific_variance == pytest.approx(0.25 * 0.04 + 0.25 * 0.04)
