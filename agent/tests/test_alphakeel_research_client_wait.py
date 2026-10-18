"""wait_pack times out on a stall, not on total time; cli._load takes inline JSON; freeze drops `base` from instruments."""
import pytest

from alphakeel_research import cli, client as client_mod
from alphakeel_research.errors import ApiError


def _client(monkeypatch):
    monkeypatch.setenv("ALPHAKEEL_RESEARCH_TOKEN", "test-token")
    now = [0.0]
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: now[0])
    c = client_mod.Client("http://127.0.0.1:1", sleep=lambda s: now.__setitem__(0, now[0] + s))
    return c, now


def test_wait_pack_times_out_on_stall_not_total_time(monkeypatch):
    c, now = _client(monkeypatch)
    c.pack = lambda job: ({"status": "ready"} if now[0] > 3000 else {"status": "running", "phase": "build", "progress": int(now[0] // 100), "total": 100})
    assert c.wait_pack("j", timeout=900, poll=50)["status"] == "ready"  # >900s overall, but always advancing
    now[0] = 0.0
    c.pack = lambda job: {"status": "running", "phase": "build", "progress": 5 if now[0] > 100 else int(now[0] // 10), "total": 100}
    with pytest.raises(ApiError) as e:
        c.wait_pack("j", timeout=900, poll=50)
    assert e.value.code == "service.unavailable" and e.value.retryable
    assert "no progress for 900s" in str(e.value)


def test_load_parses_inline_json_list():
    assert cli._load('  [{"venue": "binance", "market": "spot", "symbol": "BTCUSDT"}]') == [{"venue": "binance", "market": "spot", "symbol": "BTCUSDT"}]
    with pytest.raises(ApiError) as e:
        cli._load("[oops")
    assert e.value.code == "request.invalid"


def test_freeze_drops_base_from_instruments(monkeypatch, tmp_path):
    seen = {}

    class Fake:
        def dataset_pack_request(self, **kw):
            seen.update(kw)
            return {}

    def stop(*a, **k):
        raise ApiError("request.invalid", "stop")

    monkeypatch.setattr(cli, "Client", lambda url=None: Fake())
    monkeypatch.setattr(cli.workflow, "freeze_pack", stop)
    inst = '[{"venue": "binance", "market": "perp", "symbol": "BTCUSDT", "base": "BTC", "quote": "USDT"}]'
    cli.main(["--cache", str(tmp_path), "freeze", "--start-ms", "0", "--end-ms", "1", "--instruments", inst])
    assert seen["instruments"] == [{"venue": "binance", "market": "perp", "symbol": "BTCUSDT"}]


@pytest.mark.parametrize("bad", ['[1]', '{"venue": "binance", "market": "spot", "symbol": "X"}', '["BTCUSDT"]'])
def test_freeze_rejects_non_list_instruments(monkeypatch, tmp_path, capsys, bad):
    monkeypatch.setattr(cli, "Client", lambda url=None: object())
    assert cli.main(["--cache", str(tmp_path), "freeze", "--start-ms", "0", "--end-ms", "1", "--instruments", bad]) == 1
    err = __import__("json").loads(capsys.readouterr().out)["error"]
    assert err["code"] == "request.invalid" and err["field"] == "instruments"


def test_inline_fees_source_is_labelled():
    import hashlib
    assert cli._is_inline(' {"a": 1}') and cli._is_inline("[1]") and not cli._is_inline("fees.json") and not cli._is_inline(None)
    v = ' {"taker": "0.0005"} '
    assert cli._source_label(v) == "inline:sha256=" + hashlib.sha256(v.strip().encode()).hexdigest()[:12]
    assert cli._source_label("fees.json") == "fees.json" and cli._source_label(None) is None
