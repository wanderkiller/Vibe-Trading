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
bbo from `ts_recv_ms`, settlements from `available_at`, instrument metadata = the last snapshot whose snapshot day is not
after `as_of`); it cannot drive engine runs (`capability.unsupported`). Market objects are per venue *and* market
(`market/<table>/<venue>/<market>/part-N`: spot and perp can share a symbol); quantities are base units (OKX perpetual
contract counts are converted by the service with the snapshot's `contract_value`).

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
- `ir_policy/strategy.py`: a generic `PolicyHost` script. Parameters `{"ir": <document>, "qty_dp": 8}`; entries become two
  `limit_ioc` orders (long buys at its ask, short sells at its bid, `notional_per_leg / long.ask` rounded half-even to 8
  places, same qty on both legs, README §5a); exits become reduce-only orders for the engine's real positions of that
  pair (for the entry's IR quantity), at THIS frame's touch, and only while both legs have a tradable quote in this frame
  (README §5a: a quote row with `bbo = true`, sane numbers, and a quote time -- `quote_ts`, else the venue's fetch time --
  within the pack's `engine.max_quote_age_ms`; otherwise the pair is held, `max_hold`/`max_loss`/`take_profit` included,
  until a frame quotes both; an older quote is never used). Intents are named as
  AlphaKeel's Rust `ir_strategy` run names them (README §5a): `n<trade>-<L|S>-<open|close>`, `position_ref p<trade><L|S>`,
  `pair_id pair-<trade>`, `trade` counting from 1 only the entries actually submitted (an entry with a `bbo = false` or
  invalid-quote leg, or a zero quantity, is not submitted and takes no number). State (`positions`, `cooldowns`, `next_trade`) is reconciled with
  `ctx["positions"]` every step. A pair's `net_now` is the sum of its legs' `ctx["positions"][].net_now` (the engine's
  per-leg close-now net; `null` if either leg's is), so `exit.max_loss` / `exit.take_profit` fire on the engine's state.
  The local simulator gives the same per-leg `net_now` (same formula, null rule and decimal text) as the service's session.
  Only `funding_estimate: "predicted"` is served (`last_settled` is refused at `initialize`).
- `examples/ir/strategy.json`: an example for the fixture pack (Binance vs OKX BTC perpetuals; enters on the first frame,
  leaves on `max_hold_hours: 1` at the last). It is also the template for new IRs: its entry conditions carry
  `quote_age_ms <= 5000` and `leg_skew_ms <= 3000`, AlphaKeel's runtime defaults (`max_quote_age_ms`,
  `max_leg_skew_ms`). IR frames are not scored, so these gates bind an IR's entries only when the IR writes them; keep
  them (or tighter) in every IR. `ir export` adds a `provenance.warnings` line when either bound is missing or looser,
  the same rule as AlphaKeel's `ir.quote_age_unbounded` / `ir.leg_skew_unbounded` / `*_looser` runtime warnings.

Two commands against the research service:

```bash
python -m alphakeel_research ir validate --ir strategy.json [--pack PACK_ID]
python -m alphakeel_research ir review   --pack PACK_ID --ir strategy.json [--seed N] [--profile profile.json] --out DIR
```

`ir validate` is AlphaKeel's own check (`POST /validate/ir`: `valid`, `strategy_id`, `strategy_sha256`, `problems[]`
with `ir.*` codes; exit 2 when invalid). `ir review` (also `workflow.ir_review(...)`, which returns the same dict) runs
the IR (a) in Python: `ir_policy` under the local simulator with the execution profile the service renders for
`ir_strategy`, evidence in `DIR`; (b) in AlphaKeel's Rust evaluator: a `mode: "ir_strategy"` run on the same pack;
(c) registers (a) as an external `ir_strategy` run and asks for the layered comparison. A non-null `sizing.leverage` is
passed as the profile's `leverage_perp` (both sides use it; a contradicting `--profile` value is refused), and when the
engine's quantity precision cap (README §5a) binds, the local run is repeated with the engine's per-trade quantity. It prints the layer table,
`first_difference`, `pass_scope` and both sides' opened/closed/realized, writes `DIR/reconciliation.json` (run, comparison
and pack ids, pack digest, `strategy_id`, `strategy_sha256`, layers, `first_difference`) and ends with the fixed line
"Decision (paper or not) is yours; this report does not approve anything." It gives no verdict of its own.

## Long-history IR research: `ir backtest` and `ir export`

The scan archive (real top of book, predicted funding) only starts 2026-10. For research over AlphaKeel's long datasets
(1-minute trade klines and official funding settlements, 2019 onwards, nine venues) there is a separate backtest on a
**dataset pack**:

```bash
python -m alphakeel_research ir backtest --ir strategy.json --start-ms N --end-ms N --instruments inst.json \
    [--step-minutes 2] [--fees fees.json] [--trials N [--trials-evidence trials.csv]] [--capital-base X] \
    [--accept-partial] [--pack-dir DIR] [--verbose] --out RUN
python -m alphakeel_research ir export --ir strategy.json --run RUN [--research-credential] --out EXPORT
```

`inst.json` is `[{"venue", "market", "symbol", "base", "quote"}]` (dataset packs leave base/quote null in their instruments
table, so declare them); only instruments on the IR's venues and markets are used. `ir backtest`
(`workflow.ir_backtest`, `ir_backtest.py`) freezes — or, the request key being content-derived, reuses — a dataset pack
with `kline_1m` (trade) + funding for those instruments over `[start − 1 day, end)` (the extra day feeds the 24 h volume and
the last settlement), checks it is the pack asked for, then at every instant `start + k·step` that has data builds the
**dataset view** (`ir_views.dataset_frame_pairs`, `view = "kline_close_synthetic_bbo"`) and calls `ir.decide` with the same
state handling as `ir_policy` (trade numbering, §5a qty and hold rules, cooldown from the instant after a close). The view's
approximations, written into every run card:

- touch `bid = ask = close` of the last 1m bar closed at the instant; `bbo = true` is **synthetic** (spread 0, never
  `price_estimated`); `mark`/`funding_px` = that close; a leg whose last bar is older than 5 minutes is not in the frame;
- `funding_rate` = the last **official settlement** visible at the instant (`available_at`, or time + the pack's assumed
  delay): a `last_settled` proxy for the predicted rate a live scan uses. The IR's `assumptions.funding_estimate` stays
  `predicted`; this is the view's approximation, not a change of the strategy. `interval_hours` from that settlement
  (unknown → the perp is left out), `next_funding_ms` = settlement + interval;
- `volume_quote` = 24 h sum over 1440 bars (null if any is missing); `quote_ms` = the bar's close time;
- `taker_fee` from `--fees` (`{venue: {"perp": "0.0005", "spot": …}}`); default AlphaKeel's public lowest-tier perp taker
  0.0005 on every venue (stated as an assumption); spot legs need a spot fee and spot klines (otherwise only cross_perp).

Execution is a small separate Decimal simulator (`ir_backtest.Sim`; `sim.Simulator` is bound to scan packs and the engine
profile): complete fills at the touch, `q8(qty × price × taker_fee)` per fill, funding at each official settlement time on
open positions `q8(qty × mark × rate)` paid by longs / received by shorts (mark = the last close at that time), equity =
realized + open pairs valued close-now; after the run every trade's fills, fees and funding are redone at the quantity
AlphaKeel's engine executes (README §5a precision cap; `ir_qty` on the trade record when it differs, usually never).
**No margin, leverage or liquidation model**, no slippage beyond the synthetic touch,
USDT legs only. Output in `RUN`: `run_card.json` (`vibe-trading.ir-backtest-run-card/1`: strategy id, pack id/digest/dataset
versions, window, step, view + approximations, simulator conventions, fee and capital-base assumptions, `data_audit`
(`alphakeel_b2`, auditable), counts, realized/funding/fees, max drawdown, `alphakeel_metrics` on UTC-daily returns over the
capital base with `trials`), `trades.jsonl`, `equity.jsonl`, `decisions.jsonl` (per instant: entries/exits/held/rejected
count; every rejection with `--verbose`). Rows are streamed (klines are spilled per instrument and read back in order),
so memory does not grow with the window.

`ir export` (`workflow.ir_export`) writes `EXPORT/strategy.json`: the IR **unchanged** plus `strategy_id` and `provenance`
`{tool: "vibe-trading", run_id, window {start, end} (UTC days of the backtest window), data_window, trials {count,
evidence}, classification, data_audit, data_view, approximations, backtest {pack_id, pack_sha256, dataset_versions, start_ms,
end_ms, step_minutes, audit}, research_credential?}`, the trials evidence under `EXPORT/evidence/` (a run without
`--trials` is refused; `--trials 1` alone generates a one-line `trials.jsonl`) and `evidence/ir-backtest-audit.json` (run
card + AlphaKeel-convention metrics). `--research-credential` records `Client.check()`'s credential and applies the same
data-horizon rule as the hand-off (`handoff.check_research_credential`). A non-auditable run card is classified
`exploratory` in provenance only; the definition (and so the id) never changes, and the export is re-loaded to prove it.
AlphaKeel imports the file into its registry (`strategy_ir::Ir::parse` + `validate`).

**The long-history run is research evidence, not reconciliation input.** Reconciliation with AlphaKeel's evaluator is
`ir review` on a scan pack (`reconciliation.json`, accepted by `arb strategy reconcile --from-file`).

The agent tool exposes the four IR commands as actions `ir_validate`, `ir_review`, `ir_backtest`, `ir_export`.

## Factor research on a crypto cross-section: the `alphakeel:` alpha-bench universe

The alpha zoo's bench universes are `csi300`, `sp500` and the single-asset `btc-usdt`; `alphakeel:<spec>` adds a crypto
cross-section built from a dataset pack (`src/factors/alphakeel_universe.py`, patch group `alpha-bench-universe`). The
spec is a JSON file (or the JSON object inline after `alphakeel:`; relative paths are resolved against the file's
directory):

```json
{"schema": "vibe-trading.alphakeel-universe/1",
 "pack_dir": "pack",
 "instruments": [{"venue": "binance", "market": "perp", "symbol": "BTCUSDT", "base": "BTC"},
                 {"venue": "binance", "market": "perp", "symbol": "ETHUSDT", "base": "ETH"}],
 "interval": "1d", "ir": "strategy.json", "excluded_bases": [], "funding": true}
```

```bash
vibe-trading alpha bench --zoo academic --universe alphakeel:/path/universe.json --period 2025-01-01/2025-07-20
vibe-trading alpha compare academic_illiq academic_smb --universe alphakeel:/path/universe.json --period 2025-01-01/2025-07-20
```

The agent's `alpha_bench` tool (and MCP `alpha_bench`) take the same `universe` string; the web UI's REST bench does not
(it accepts no file path).

- **Pack**: `pack_dir` = an exported pack (`Pack.export`, e.g. `ir review`'s `DIR/pack`; no network); `pack_id` = a
  frozen pack opened through the research service; neither = freeze a dataset pack for exactly the period
  (`[first day 00:00, last day + 1 d)`, `kline_1m` trade + funding for perps, strict coverage unless
  `"accept_partial": true`) with `ALPHAKEEL_RESEARCH_URL` / `_TOKEN(_FILE)`, keyed by request + UTC day like `ir
  backtest`. The pack must be a versioned dataset pack holding every instrument over the whole period; anything else is
  refused (no other source is used).
- **Roster**: `instruments` with a declared `base` (dataset packs leave it null), one instrument per base (a column is a
  base). Filter: `bases` (allow-list) and `excluded_bases`, the IR's rule (non-empty, upper-case, distinct); with `ir`,
  the IR's `universe.bases` intersects the allow-list and its `excluded_bases` add up, so the factor is researched on
  exactly the bases the strategy may enter (`_meta.allowed_bases_without_instrument` lists allowed bases nobody
  declared). At least two bases must remain.
- **Panel**: `open high low close volume amount vwap` (volume in base units, amount = quote volume, vwap = amount / volume),
  one row per complete UTC bar (`1h`, `4h`, `1d`; a bucket with a missing minute is NaN, never filled), labelled with the
  bar's open time. Perps add `funding_rate` (last official settlement visible at the bar close, per settlement, not
  normalised), `funding_interval_hours` and `funding_sum` (rates that became visible during the bar).
- **Point in time**: a row holds only what is visible at its bar close (`open + interval - 1 ms`): klines from
  `close_time_ms` (checked `= t + 59 999`), settlements from `available_at_ms` or settlement time + the lock's
  `funding.assumed_delay_ms` (>= 60 s, the `Pit` rule). The bench's forward return is the next bar's close over this one.
  The roster is the researcher's declared list, not a point-in-time index membership: `_meta.survivorship_bias` is
  `true` (pick the list by a rule fixed before the period, e.g. the IR's `bases`). `_meta` also records the pack id and
  digest, dataset versions, interval and the filter.

Tests: `agent/tests/test_alphakeel_*.py` (install `.[dev,alphakeel-dev]`). Without the fixture server the cross-process
tests skip; AlphaKeel's cross-project CI runs them with `ALPHAKEEL_REQUIRE_FIXTURE=1`, where a missing binary or tool
fails instead, against the Vibe-Trading commit pinned in AlphaKeel's `ci/vibe-trading.ref`.
