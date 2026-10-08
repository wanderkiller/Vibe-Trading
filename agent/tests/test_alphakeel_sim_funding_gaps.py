"""The independent simulator settles funding boundaries a scan gap skipped (same rule as AlphaKeel's engine feed).

Each scan reports only the next settlement boundary. A 1h contract scanned at 07:55 and then at 09:05 never reports 09:00;
before the fix the simulator (and the engine) booked 08:00 only, so a settlement silently disappeared from PnL. These are
pure unit tests over an in-memory pack (no AlphaKeel server needed).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from alphakeel_research.errors import ApiError
from alphakeel_research.packfile import Frame, Inst, Instrument, Observation, Quote, Settlement
from alphakeel_research.sim import HOUR_MS, Simulator, funding_feed_plan

H = HOUR_MS
BASE = 1_699_977_600_000  # 2023-11-14 16:00 UTC, 8h aligned
BIN = Inst("binance", "perp", "BTCUSDT")
D = Decimal


class FakePack:
    def __init__(self, obs, official=()):
        """``obs``: (time, rate, interval hours, venue next boundary) per frame; ``official``: (boundary, rate)."""
        self._frames = [Frame(i, i + 1, t, t, "h", False, {"binance": {"ok": True, "fetched_ms": t}}, None, False) for i, (t, *_) in enumerate(obs)]
        self._obs = obs
        self._official = list(official)

    def fees(self):
        return {"engine": {"max_quote_age_ms": 600_000, "maint_margin": None}, "fees": {"binance": {"perp": "0.0005", "spot": "0.001"}}}

    def instruments(self):
        return {BIN: Instrument("binance", "perp", "BTCUSDT", "BTC", "USDT", "USDT", "linear", None, "ok", None, None, "ok", 0, 0, True)}

    def frames(self, include_warmup=False):
        return self._frames

    def note_frames(self, t0, t1):
        pass

    def quotes(self, inst, t0, t1):
        return [Quote(f.t, D(100), D(100), True, D(100), None, f.t, f.i) for f in self._frames]

    def observations(self, inst, t0, t1):
        return [Observation(t, D(r), h, n, D(100), "mark", i) for i, (t, r, h, n) in enumerate(self._obs)]

    def settlements(self, inst, t0, t1):
        return [Settlement(t, "BTCUSDT", D(r), "regular", None, "ok", "USDT", "linear", "x", None, t, None, "0" * 64, True) for t, r in self._official]

    def accesses(self):
        return []


def profile(official=False, strict=True):
    return {"account": {"starting_balance_per_venue": "10000", "leverage_perp": "1", "same_instant_margin": "cumulative_initial_margin"},
            "funding": {"mode": "official_settlement" if official else "estimate", "strict": strict},
            "quotes": {"max_age_ms": 600_000, "fill_future_tolerance_ms": 0}, "decision_clock": {"history_len": 1}}


def run(obs, *, official=(), prof=None, hold=True):
    pack = FakePack(obs, official)
    sim = Simulator(pack, prof or profile(), [BIN], run_id="gap")
    t0 = obs[0][0]
    buy = {"intent_id": "a", "decision_time_ms": t0, "instrument": BIN.ref(), "side": "buy", "qty": "1", "order_type": "market",
           "limit_price": None, "reduce_only": False, "position_ref": "p"}
    out = sim.run(lambda k, t, view: [buy] if (hold and k == 0) else [])
    return out, sim


def settled(out):
    return [(e["data"]["boundary_ms"], e["data"]["amount"], e["data"]["rate_source"]) for e in out.events if e["kind"] == "funding"]


def test_a_70_minute_scan_gap_on_a_1h_contract_settles_both_boundaries():
    b8 = BASE + 16 * H
    out, _ = run([(b8 - 5 * 60_000, "0.001", 1, b8), (b8 + H + 5 * 60_000, "0.002", 1, b8 + 2 * H), (b8 + H + 10 * 60_000, "0.002", 1, b8 + 2 * H)])
    # 09:00 was never reported: the last observed predicted rate 0.001, flagged as an estimate
    assert settled(out) == [(b8, "-0.1", "predicted_estimate"), (b8 + H, "-0.1", "predicted_estimate")]
    assert out.result["by_currency"]["USDT"]["funding"] == "-0.2"
    assert "unobserved_funding_gaps" not in out.result["data_quality"]


def test_an_8h_contract_with_a_gap_longer_than_8h_settles_the_skipped_boundary():
    b0 = BASE + 8 * H
    out, _ = run([(b0 - 60_000, "0.001", 8, b0), (b0 + 9 * H, "0.003", 8, b0 + 16 * H)])
    assert [b for b, *_ in settled(out)] == [b0, b0 + 8 * H]


def test_an_ambiguous_gap_is_counted_not_guessed_and_fails_a_strict_official_run():
    b0 = BASE + 8 * H
    obs = [(b0 - 60_000, "0.001", 8, b0), (b0 + 7 * H, "0.002", 4, b0 + 8 * H)]  # 8h -> 4h across the gap: was 12:00 settled?
    out, _ = run(obs)
    assert [b for b, *_ in settled(out)] == [b0]
    assert out.result["data_quality"]["unobserved_funding_gaps"] == 1
    with pytest.raises(ApiError) as e:
        run(obs, official=[(b0, "0.001")], prof=profile(official=True, strict=True))
    assert e.value.code == "data.funding_missing" and "scan gap" in str(e.value)
    # misaligned (same interval, new boundary off the old schedule) is not guessed either
    b8 = BASE + 16 * H
    out, _ = run([(b8 - 5 * 60_000, "0.001", 1, b8), (b8 + H + 40 * 60_000, "0.002", 1, b8 + 2 * H + 30 * 60_000)])
    assert [b for b, *_ in settled(out)] == [b8] and out.result["data_quality"]["unobserved_funding_gaps"] == 1
    # a gap on a contract that is not held does not count
    out, _ = run(obs, hold=False)
    assert "unobserved_funding_gaps" not in out.result["data_quality"]


def test_official_mode_uses_the_official_rate_for_the_skipped_boundary():
    b8 = BASE + 16 * H
    obs = [(b8 - 5 * 60_000, "0.001", 1, b8), (b8 + H + 5 * 60_000, "0.002", 1, b8 + 2 * H)]
    out, sim = run(obs, official=[(b8, "0.0011"), (b8 + H, "0.0007")], prof=profile(official=True))
    assert settled(out) == [(b8, "-0.11", "official_settlement"), (b8 + H, "-0.07", "official_settlement")]
    assert sim.estimate_fallbacks == 0
    # no official event for the skipped boundary: strict fails exactly like any other estimate fallback
    with pytest.raises(ApiError) as e:
        run(obs, official=[(b8, "0.0011")], prof=profile(official=True, strict=True))
    assert e.value.code == "data.funding_missing" and "1 settlement(s)" in str(e.value)
    out, _ = run(obs, official=[(b8, "0.0011")], prof=profile(official=True, strict=False))
    assert settled(out) == [(b8, "-0.11", "official_settlement"), (b8 + H, "-0.1", "predicted_estimate")]


def test_the_plan_feeds_every_skipped_boundary_once_from_the_last_observation():
    b8 = BASE + 16 * H
    extra, gaps = funding_feed_plan([(0, b8 - 5 * 60_000, 1, b8), (1, b8 + 3 * H + 5 * 60_000, 1, b8 + 4 * H)])
    assert extra == {0: [b8 + H, b8 + 2 * H, b8 + 3 * H]} and gaps == []
    # an interval change seen promptly (no boundary passed unobserved) is not a gap
    assert funding_feed_plan([(0, b8 - 5 * 60_000, 1, b8), (1, b8 + 5 * 60_000, 8, b8 + 8 * H)]) == ({}, [])
    # consecutive scans: nothing extra
    assert funding_feed_plan([(0, b8 - 5 * 60_000, 1, b8), (1, b8 + 5 * 60_000, 1, b8 + H)]) == ({}, [])
