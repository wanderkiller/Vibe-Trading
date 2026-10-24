"""A post-event window must not pull the event row into the estimation sample."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.quantlib.eventstudy import estimate_market_model, event_study


@pytest.fixture
def shocked_frame():
    index = pd.bdate_range("2023-01-02", periods=400)
    rng = np.random.default_rng(0)
    market = pd.Series(rng.normal(0.0, 0.01, len(index)), index=index)
    returns = pd.DataFrame({"A": market + rng.normal(0.0, 0.01, len(index))}, index=index)
    position = 300
    returns.iloc[position, 0] += 0.5  # the event-day reaction
    return returns, market, index, position


@pytest.mark.parametrize("event_window", [(15, 20), (1, 5), (11, 30)])
def test_post_event_window_fits_on_rows_before_the_event(shocked_frame, event_window) -> None:
    returns, market, index, position = shocked_frame
    gap, window = 10, 120
    result = event_study(
        returns, market, [("A", index[position])], event_window=event_window,
        estimation_window=window, estimation_gap=gap,
    )
    end = position - gap
    expected = estimate_market_model(
        returns["A"].iloc[end - window:end], market.iloc[end - window:end]
    )
    assert result.events[0].fit.residual_std == pytest.approx(expected.residual_std)


def test_pre_event_windows_keep_their_estimation_sample(shocked_frame) -> None:
    returns, market, index, position = shocked_frame
    gap, window, start = 10, 120, -5
    result = event_study(
        returns, market, [("A", index[position])], event_window=(start, 5),
        estimation_window=window, estimation_gap=gap,
    )
    end = position + start - gap
    expected = estimate_market_model(
        returns["A"].iloc[end - window:end], market.iloc[end - window:end]
    )
    assert result.events[0].fit.residual_std == pytest.approx(expected.residual_std)


@pytest.mark.parametrize("model", ["market", "market_adjusted", "mean_adjusted"])
def test_zero_gap_excludes_event_and_later_returns(shocked_frame, model) -> None:
    returns, market, index, position = shocked_frame
    before = event_study(returns, market, [("A", index[position])],
                         event_window=(1, 5), estimation_gap=0, model=model)
    shocked = returns.copy()
    shocked.iloc[position:position + 6, 0] += 10
    after = event_study(shocked, market, [("A", index[position])],
                        event_window=(1, 5), estimation_gap=0, model=model)
    assert before.events[0].fit == after.events[0].fit
    assert after.events[0].car - before.events[0].car == pytest.approx(50)
