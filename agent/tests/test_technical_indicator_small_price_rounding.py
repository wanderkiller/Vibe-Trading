"""Sub-penny assets must not have their Bollinger/MACD output rounded to zero."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.tools.technical_indicator_tool import _compute_bollinger, _compute_macd


@pytest.mark.parametrize("scale", [1.2e-12, 1.2e-20, 1.2e-100])
def test_indicators_preserve_significant_digits_below_fifteen_decimals(scale) -> None:
    close = _series(scale)
    bands = _compute_bollinger(close)
    macd = _compute_macd(close)
    middle = close.rolling(20).mean().iloc[-1]
    line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    signal = line.ewm(span=9, adjust=False).mean()
    assert bands["middle"] == pytest.approx(middle, rel=5e-4, abs=0)
    for key, value in {"macd_line": line.iloc[-1], "signal_line": signal.iloc[-1],
                       "histogram": (line - signal).iloc[-1]}.items():
        assert macd[key] == pytest.approx(value, rel=5e-4, abs=0)


@pytest.mark.parametrize("reference", [0.0, 1.0, 1e-5])
def test_small_component_is_not_erased_by_larger_or_zero_reference(reference) -> None:
    from src.tools.technical_indicator_tool import _round_keeping_precision

    assert _round_keeping_precision(-1.23456e-12, 4, reference) == pytest.approx(
        -1.23456e-12, rel=5e-4, abs=0
    )


def _series(scale: float) -> pd.Series:
    rng = np.random.RandomState(0)
    return pd.Series(scale * np.cumprod(1 + rng.normal(0, 0.02, 80)))


def test_bollinger_keeps_precision_for_sub_penny_price() -> None:
    bands = _compute_bollinger(_series(1.2e-5))
    assert bands is not None
    assert bands["lower"] > 0
    assert bands["lower"] < bands["middle"] < bands["upper"]


def test_macd_keeps_precision_for_sub_penny_price() -> None:
    macd = _compute_macd(_series(1.2e-5))
    assert macd is not None
    assert macd["macd_line"] != 0.0
    assert macd["signal_line"] != 0.0


def test_ordinary_price_rounding_is_unchanged() -> None:
    close = _series(150.0)
    bands = _compute_bollinger(close)
    macd = _compute_macd(close)
    assert bands is not None and macd is not None
    assert all(round(v, 2) == v for v in bands.values())
    assert all(round(v, 4) == v for v in macd.values())
