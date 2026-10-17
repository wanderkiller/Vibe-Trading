"""The one way a signal engine uses a factor-library alpha.

A signal engine that wants a factor-library alpha (``gtja191_010``,
``alpha101_017``, ...) calls ``zoo_factor("<id>", data_map)`` and gets the
library's own implementation. A rule the engine writes itself, even one inspired
by a library alpha, must not carry the library id or a paper's alpha name: the
engine screen records the ``zoo_factor`` calls as ``zoo_factors_used`` and lists
any library name in the code that is not backed by such a call.
"""

from __future__ import annotations

import pandas as pd


def zoo_factor(alpha_id: str, data_map: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Compute a factor-library alpha over the backtest's per-symbol OHLCV frames.

    Args:
        alpha_id: Registry id, e.g. ``"gtja191_010"``.
        data_map: Backtest loader output, ``code -> OHLCV frame``.

    Returns:
        The alpha as ``dates x symbols``.

    Raises:
        KeyError: ``alpha_id`` is not in the library.
        SkipAlpha: the data lacks a column the alpha needs.
        RegistryError: the alpha failed to import or compute.
    """
    from backtest.runner import _build_price_panel
    from src.factors.registry import get_default_registry

    return get_default_registry().compute(alpha_id, _build_price_panel(data_map))
