"""``alphakeel_pack``: explicit-only marker source for frozen AlphaKeel data packs.

An AlphaKeel pack holds point-in-time top-of-book scans, funding observations and official settlements, not OHLCV bars.
Turning those into candles would silently change what the data means, so this loader refuses OHLCV requests with an
instruction instead of fabricating bars or falling back to another source. Strategies read the pack through the
``alphakeel_research`` package (``Pack`` / ``Pit``) or the ``alphakeel_research`` agent tool.

``is_available`` only reports whether a pack directory is configured (``ALPHAKEEL_PACK_DIR``) and the client package is
importable; it never touches the network.
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd

from backtest.loaders.base import NoAvailableSourceError
from backtest.loaders.registry import register
from src.config.accessor import get_env_value


def _pack_dir() -> str:
    # Un-cached pass-through so a pack directory set at runtime is seen without a restart.
    return get_env_value("ALPHAKEEL_PACK_DIR", "").strip()

REFUSAL = (
    "alphakeel_pack carries frozen AlphaKeel scans (quotes, funding, settlements), not OHLCV bars, and never falls back "
    "to another source. Read it with alphakeel_research.Pack/Pit (see agent/alphakeel_research/README.md) or the "
    "alphakeel_research tool."
)


@register
class DataLoader:
    name = "alphakeel_pack"
    markets = {"crypto"}  # reachability only (see registry _NO_NETWORK_FALLBACK_SOURCES)
    requires_auth = False

    def is_available(self) -> bool:
        d = _pack_dir()
        if not d or not os.path.isfile(os.path.join(d, "lock.json")):
            return False
        try:
            import alphakeel_research  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def open_pack(self) -> Any:
        """The configured pack, verified (every object hash checked on read)."""
        if not self.is_available():
            raise NoAvailableSourceError("alphakeel_pack: no exported pack directory is configured (ALPHAKEEL_PACK_DIR)")
        from alphakeel_research.packfile import Pack

        return Pack.from_directory(_pack_dir())

    def fetch(self, codes: list[str], start_date: str, end_date: str, *, interval: str = "1D",
              fields: list[str] | None = None) -> dict[str, pd.DataFrame]:
        raise NoAvailableSourceError(REFUSAL)
