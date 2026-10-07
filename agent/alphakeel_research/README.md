# alphakeel_research

Client for AlphaKeel's **offline** research-backtest service. It freezes AlphaKeel market data into a verified pack,
runs an independent Decimal backtest locally, and has AlphaKeel's Rust/NautilusTrader engine execute the same intents (or
drive the same policy step by step under the engine's real state) and compare every layer. It never connects to an
exchange and never places orders. Full description, rules and limits: AlphaKeel `docs/37-research-backtest-integration.md`.

```bash
pip install ./agent/alphakeel_research            # or '.[fast,sql]' for zstandard and DuckDB
export ALPHAKEEL_RESEARCH_URL=http://127.0.0.1:8731
export ALPHAKEEL_RESEARCH_TOKEN_FILE=~/.vibe-trading/alphakeel.token   # created with `arb research-service token create`
python -m alphakeel_research check
```

| Task | Command |
| --- | --- |
| freeze / read data | `freeze --start-ms N --end-ms N`, `read --pack ID --venue V --symbol S --start-ms N --end-ms N [--as-of-ms N]` |
| dataset pack (funding history 2019-, 9 venues; 1m klines / bbo; no scan frames) | `freeze --start-ms N --end-ms N --instruments inst.json [--market-tables kline_1m,bbo] [--price-kinds trade] [--funding none]`, then `read --table settlements\|klines\|bbo` |
| Python backtest + intent review | `review --pack ID --intents intents.json` |
| policy under both state sources | `policy --pack ID --strategy DIR --instruments instruments.json --parameters params.json --seed N` |
| AlphaKeel native strategy | `native --pack ID --params handoff-params.json` |
| compare two runs | `compare --a RUN --b RUN --mode fixed_intent_replay\|python_policy\|native_strategy` |
| fault-injection demo | `inject --pack ID --intents intents.json --fault fee\|funding-sign` |

`examples/walkthrough.sh` runs all of them against AlphaKeel's offline fixture server (a separate process; set
`ALPHAKEEL_FIXTURE_SERVER` or `ALPHAKEEL_REPO`). A pass means
the engine execution and accounting of the submitted intents agree; it does not certify strategy logic. The policy sandbox
is process isolation with a scrubbed environment and resource limits, not a security boundary: do not run untrusted code.

Inside Vibe-Trading the same operations are available as the `alphakeel_research` agent tool; the `alphakeel_pack` data
source only marks pack-backed data and refuses OHLCV requests (there is no fallback to another source).

## Dataset packs, the `alphakeel_b2` loader and the hold-out

AlphaKeel's scan archive is short (it starts 2026-10-01); its funding history in B2 starts in 2019 (nine venues) and its
market dataset holds 1-minute klines, bbo, depth and instrument metadata. A **dataset pack** (`dataset` in the pack
request, `Client.dataset_pack_request(...)`) freezes any window from those versioned datasets without scan frames:
versions are pinned by the service at acceptance, a gap in any instrument/table is an error (`data.coverage_partial`)
unless you opt into `accept_partial`, and `pack_id` is the digest of the frozen content. A dataset pack serves research
reads (`Pack.settlements/klines/bbo/instrument_meta`, `Pit.*` with point-in-time visibility: klines from `close_time_ms`,
bbo from `ts_recv_ms`, settlements from `available_at`); it cannot drive engine runs (`capability.unsupported`).

The **`alphakeel_b2` loader** (`backtest/loaders/alphakeel_b2_loader.py`) wraps this for Vibe-Trading: `fetch()` builds
OHLCV from `kline_1m` (1m...1D, complete bars only) and `fetch_funding()` returns official funding settlements for all nine
venues as exact `Decimal`. It talks only to the research service (no object-store credentials, no exchange), verifies the data
lock digest and every object it downloads, checks the pack matches the request, tags each frame with
`df.attrs["alphakeel_b2"]` (pack id and digest, dataset versions and snapshot hashes) and records it for the run card. It is
explicit-only (never in an `auto` chain) and never falls back to another source. Codes: `venue:market:SYMBOL`
(`okx:perp:BTC-USDT-SWAP`) or `BTC-USDT` / `BTC-USDT-PERP` on `ALPHAKEEL_B2_VENUE` (default binance; venues whose symbols are
BASEQUOTE only). Data pulled live through `ccxt` stays available but is marked **non-auditable** in `run_card.json`
(`data_audit`) because nothing pins it to a dataset version.

**Hold-out**: the service keeps the most recent N days (per credential, default 60; `arb research-service token create
--holdout-days`) invisible so AlphaKeel's own re-checks stay out-of-sample. A request whose `end_ms` reaches into it is
refused with `data.holdout` (the message states the latest allowed `end_ms`; `Client.holdout()` returns it). Do not try to
work around it; ask for an earlier window.

```bash
python -m alphakeel_research freeze --start-ms 1672531200000 --end-ms 1704067200000 \
    --instruments inst.json --market-tables kline_1m --out lock.json      # inst.json: [{"venue":"binance","market":"perp","symbol":"BTCUSDT"}]
```

**Policy sandbox network isolation**: `PolicyHost` tries to start the strategy in its own network namespace (`unshare -n`,
with uid drop when run as root; `-Urn` for unprivileged users) and only reports `network_blocked: true` after a probe run
under that exact launch command proved there is no route (only loopback, `connect()` gets ENETUNREACH). When the host
cannot do it (no `unshare`, no privilege, user namespaces disabled, container without CAP_SYS_ADMIN) it reports
`network_blocked: false` and the reason in `network_isolation`. Set `ALPHAKEEL_REQUIRE_NETNS=1` to refuse to start without it.

Contract: `contract/` is a pinned copy of AlphaKeel's OpenAPI, error codes and shared test vectors (`PIN` holds their
hashes); `models.py` is generated from it (`gen_models.sh`), never edited by hand. `Client.check()` (and the
`alphakeel_b2` loader before it freezes anything) compares the service's `/whoami` `contract_pin` with `PIN` entry by entry
and refuses a different contract build (`contract.pin_mismatch`).

Point in time: a strategy receives only `ctx.pit` (`Pit`), never the unfiltered pack. `Pit` hides quotes whose exchange
`quote_ts` is after `as_of`, settlements before `available_at` (or settlement time + the lock's `assumed_delay_ms`, at least
60 s; a lock below that is refused), klines before their close (`close_time_ms` must be `t + 59999`), future listings and
delistings — the same rules as the service's `/slice?as_of_ms=`.

Hand-off to AlphaKeel: `alphakeel_research.handoff.write_handoff(...)` writes `params.json` (with the required
`source.window` and `trials {count, evidence}`) and `evidence/handoff-audit.json` (AlphaKeel-convention statistics from
`backtest.alphakeel_metrics`, the run card's data audit). A run whose data is not auditable (`data_audit.auditable` false,
e.g. ccxt) is classified **exploratory**.

## Strategy IR

A strategy can also be a declarative document, `alphakeel.strategy-ir/1` (`strategy.json`): the single definition of a
cross-venue carry strategy that Vibe-Trading researches and AlphaKeel runs. `contract/strategy-ir/` is a pinned copy of
AlphaKeel's spec (`README.md` is the source of truth), `schema.json` and shared gold vectors, exported together with the
research contract: their hashes are in the one `contract/PIN` (keys `strategy-ir/...`), so `Client.check()` also refuses
a service built from another IR spec. This package is an independent implementation of that text, checked against the
same vectors as the Rust crate.

- `ir.py` (standard library only): `load(text_or_dict)` validates (§1–§3) and raises `IrError` with a rule-group code
  (`ir.shape`, `ir.schema`, `ir.kind`, `ir.invalid`, `ir.parameter_path`, `ir.parameter_mismatch`,
  `ir.strategy_id_mismatch`); `validate(doc)` returns the problem as a list instead; `strategy_id(doc)` is
  `ir-` + sha256 of the canonical definition (provenance and `strategy_id` removed, `quote_ccys` default inserted);
  `compute_features(pair, assumptions, now_ms)` (§4, exact decimals, unavailable = `None`, invalid pair raises
  `PairInvalid`); `decide(ir, frame, state, now_ms)` (§5) returns `{exits, entries, rejected}` in the spec's order.
- `ir_views.py`: `scan_frame_pairs(pit, frame_index, now_ms)` enumerates one scan frame's pairs from the raw pack tables
  (§4.1a: selected contracts quoted in this frame, same-frame observations, fee table, screener-style ids, sorted).
- `ir_policy/strategy.py`: a generic `PolicyHost` script. Parameters `{"ir": <document>, "qty_dp": 6}`; entries become two
  `limit_ioc` orders (long buys at its ask, short sells at its bid, `notional_per_leg / long.ask` rounded down, same qty);
  exits become reduce-only orders for the engine's real positions of that pair, at the touch. Intents are named as
  AlphaKeel's Rust `ir_strategy` run names them (README §5a): `n<trade>-<L|S>-<open|close>`, `position_ref p<trade><L|S>`,
  `pair_id pair-<trade>`, `trade` counting from 1 only the entries actually submitted (an entry with a `bbo = false` leg
  is not submitted and takes no number). State (`positions`, `cooldowns`, `next_trade`) is reconciled with
  `ctx["positions"]` every step. A pair's `net_now` is the sum of its legs' `ctx["positions"][].net_now` (the engine's
  per-leg close-now net; `null` if either leg's is), so `exit.max_loss` / `exit.take_profit` fire on the engine's state.
  The local simulator gives the same per-leg `net_now` (same formula, null rule and decimal text) as the service's session.
  Only `funding_estimate: "predicted"` is served (`last_settled` is refused at `initialize`).
- `examples/ir/strategy.json`: an example for the fixture pack (Binance vs OKX BTC perpetuals; enters on the first frame,
  leaves on `max_hold_hours: 1` at the last).

Two commands against the research service:

```bash
python -m alphakeel_research ir validate --ir strategy.json [--pack PACK_ID]
python -m alphakeel_research ir review   --pack PACK_ID --ir strategy.json [--seed N] [--profile profile.json] --out DIR
```

`ir validate` is AlphaKeel's own check (`POST /validate/ir`: `valid`, `strategy_id`, `strategy_sha256`, `problems[]`
with `ir.*` codes; exit 2 when invalid). `ir review` (also `workflow.ir_review(...)`, which returns the same dict) runs
the IR (a) in Python: `ir_policy` under the local simulator with the execution profile the service renders for
`ir_strategy`, evidence in `DIR`; (b) in AlphaKeel's Rust evaluator: a `mode: "ir_strategy"` run on the same pack;
(c) registers (a) as an external `ir_strategy` run and asks for the layered comparison. It prints the layer table,
`first_difference`, `pass_scope` and both sides' opened/closed/realized, writes `DIR/reconciliation.json` (run, comparison
and pack ids, pack digest, `strategy_id`, `strategy_sha256`, layers, `first_difference`) and ends with the fixed line
"Decision (paper or not) is yours; this report does not approve anything." It gives no verdict of its own.

Tests: `agent/tests/test_alphakeel_*.py` (install `.[dev,alphakeel-dev]`). Without the fixture server the cross-process
tests skip; AlphaKeel's cross-project CI runs them with `ALPHAKEEL_REQUIRE_FIXTURE=1`, where a missing binary or tool
fails instead, against the Vibe-Trading commit pinned in AlphaKeel's `ci/vibe-trading.ref`.
