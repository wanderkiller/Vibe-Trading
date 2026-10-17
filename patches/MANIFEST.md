# Local patch layer over upstream HKUDS/Vibe-Trading

This fork (`wanderkiller/Vibe-Trading`, branch `main`) is upstream
[HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) plus the patch groups below. The upstream commit the
series is based on is recorded in [`BASE`](BASE) (today `e1dbea8a`, upstream release 0.1.16, 2026-09-29).

* [`anchors.json`](anchors.json) is the machine-readable form of every group's files and anchors.
* [`check_patches.py`](check_patches.py) checks them (working tree, `--upstream <ref>`, `--tests`); the test
  `agent/tests/test_alphakeel_patch_layer.py` runs it on every test run.
* [`UPGRADE.md`](UPGRADE.md) is the procedure for moving the series onto a new upstream release.

**Anchor kinds.** `upstream`: upstream code a patch relies on (a signature, a literal, a call site); it must exist
upstream and in our tree. `hook`: our change inside an upstream file; it must exist in our tree. `preimage`: the
upstream code a patch replaces; it must be absent from our tree (otherwise a conflict was resolved back to upstream),
and in `--upstream` mode it must still be present upstream (otherwise upstream changed the code we replace and the
patch has to be re-read, not just re-applied). `derived`: a file generated from an upstream file plus an overlay block.
The anchor lists below mirror `anchors.json`, which is authoritative (and what the checker reads); the file lists
must match it exactly (the test enforces that).

**History shape.** The series is not linear today: `d9aa82d1`, `98e8248e` and `cc65301e` are merges. A
`git rebase --onto <new> e1dbea8a` linearises it and drops the merges; two pieces of content exist only in the
resolution of merge `98e8248e` and are lost by a plain rebase (verified by rebasing onto `e1dbea8a` itself):
`requirements-local-providers-lock.txt` and the `layout.close`/`layout.menu` keys in `frontend/src/i18n/locales/id.json`.
UPGRADE.md step 5 re-adds them; after the first upgrade the series is linear and this note can go.

| Group | Upstream files touched | Status |
|---|---|---|
| `frontend-mobile` | AgentAvatar, Agent page, index.html | small residue; upstream's own compact sidebar replaced our drawer |
| `local-deploy` | none (new files only; derived from `Dockerfile`) | local-only |
| `leaf-package` | none (new files only) | local-only |
| `loader-registry-hooks` | registry, loader_health, metrics tables, runner `_get_loader` | local-only (follows the leaf package); the runner guard is upstreamable |
| `alpha-bench-universe` | alpha_bench_tool, factors/cli_handlers | local-only (follows the leaf package) |
| `crypto-funding` | engines/_market_hooks, crypto, composite; ccxt_loader; loaders/base `resample_bars`; perpetual_evidence; routing tests; shadow-account fee source and the journal `fee_currency` | upstreamable as a bug-fix PR (see below) |
| `metrics-conventions` | metrics, validation | partly upstreamed already; rest upstreamable |
| `run-card-data-audit` | run_card, loaders/base (exposure hook), backtest_tool (one helper, two call sites), agent/loop (tool threads keep the caller's context) | local-only as written; a generic provenance hook is upstreamable |
| `alphakeel-handoff` | api_server (one hook), RunDetail (one import, one element), api.ts (one `export`) | local-only (follows the leaf package) |
| `packaging-docs` | pyproject, SKILL.md, strategy-generate SKILL.md (one bullet), READMEs, .gitignore, .env.example, CHANGELOG (one section) | local-only |
| `ci-env-gate` | alpha_routes (one line) | upstreamable: upstream's own gate e fails on it |
| `grounding-tex-dollar` | grounding/figures (one helper, one call site) | upstreamable: a closing `$$` after a number made it a dollar amount |
| `strategy-fidelity` | agent/context (one prompt bullet) | upstreamable: the agent ran a different strategy than the one asked for |
| `ir-validate-all` | none (leaf package and its tool only) | local-only (follows the leaf package) |
| `research-truth` | tools/__init__ (one session-injected class), grounding/ledger (one tool name), grounding/evidence (one branch, five amount fields), agent/context (one placeholder, one lookup), agent/loop (one helper, two call sites), test_context_attribution_layers (one placeholder) | local-only (follows the leaf package) |
| `upstream-test-fixes` | _market_hooks (two patterns), alpha_bench_tool (one `getattr`) | upstreamable: two upstream tests are red on upstream main without them |
| `research-integrity` | validation, backtest_tool, grounding ledger + policies, loop, context, 9 locale labels | partly upstreamable (Monte Carlo, cross-turn grounding, tool-name refs); the rest local-only |
| `ir-execution` | none (leaf package, its tool and tests only) | local-only (follows the leaf package) |
| `research-integrity-r2` | crypto + composite engine (one refusal each), read_file_tool (one namespace), engine card column, research contract (ledger, tool arg, card), agent/context (four bullets), two skill notes, factor-name and cited-retrieval gates (grounding ledger, policies, release), `zoo_factor` helper and engine-screen facts, round-trip count for item 1.2, multiple-testing lines on the engine card (`backtest/multiple_testing.py`) | spot-short refusal and the `trials/` namespace are upstreamable in principle; the ledger lineage, card column, research contract, factor-name gate, `zoo_factor` and the retrieval rule for cited figures and the multiple-testing card lines are local-only |
| `research-integrity-r2-guards` | agent/context (recall filter, wrapper note), grounding ledger + policies + release (verdict-word gate), ledger `session_has_passed_trial`, write_file / edit_file (order-code refusal) | recall scope, verdict-word gate and the order-code refusal are local-only (they follow the research ledger and the hand-off boundary) |

---

## `frontend-mobile` — mobile layout and i18n

**Purpose.** Phone polish on top of upstream's own compact sidebar (upstream e532650b ships a 12-wide icon rail, a feature
menu and a sessions disclosure, which replaced our former slide-in drawer in Layout.tsx and the `layout.close`/`layout.menu`
labels; both were dropped in the 2026-10-09 merge): the app shell uses `h-dvh` and the page body never scrolls, the chat list
uses `overscroll-contain`, and the assistant avatar is hidden on mobile (it indented assistant text by 44 px).

**Files.**
- `frontend/index.html`
- `frontend/src/components/chat/AgentAvatar.tsx`
- `frontend/src/pages/Agent.tsx`

**Commits.** `6335f98f` `42e3aec7` `220d3b9b` `6fbb6d15` `8a1c8de6` `58fcfcb9` `2d02e96c`, plus merge `98e8248e`
(the `id.json` labels exist only in that merge's resolution, because `id` arrived with 0.1.16).

**Upstreamable.** Yes: the `h-dvh` body, `overscroll-contain` and the AgentAvatar change. Not submitted.

**Anchors.**
- upstream `frontend/index.html`: `<div id="root"></div>`
- hook `frontend/index.html`: `<body class="h-dvh overflow-hidden`
- preimage `frontend/index.html`: `<body class="min-h-screen bg-background text-foreground antialiased">`
- upstream `frontend/src/components/chat/AgentAvatar.tsx`: `export function AgentAvatar() {`
- hook `frontend/src/components/chat/AgentAvatar.tsx`: `hidden md:block h-8 w-8`
- preimage `frontend/src/components/chat/AgentAvatar.tsx`: `<div className="h-8 w-8 shrink-0 mt-0.5" aria-hidden="true">`
- upstream `frontend/src/pages/Agent.tsx`: `ref={listRef}`
- hook `frontend/src/pages/Agent.tsx`: `chat-scroll-container flex-1 overflow-auto overscroll-contain`
- preimage `frontend/src/pages/Agent.tsx`: `className="chat-scroll-container flex-1 overflow-auto p-6 relative"`

## `local-deploy` — local deployment overlay

**Purpose.** The files the production Docker deployment on this host is built from: `Dockerfile.local` is the upstream
`Dockerfile` plus one block that installs the hash-pinned Anthropic/DeepSeek adapters from
`requirements-local-providers-lock.txt` (`--require-hashes --no-deps`, then `pip check`);
`docker-compose.override.yml` selects it, host networking, a 127.0.0.1:8899 listener and `init: true` (grandchildren
of an `alphakeel_research` child killed by process group on timeout are reaped by init); `DEPLOYMENT.local.md`
documents the build, backup and upgrade rules.

**Files.**
- `Dockerfile.local`
- `docker-compose.override.yml`
- `requirements-local-providers-lock.txt`
- `DEPLOYMENT.local.md`
- `tools/deploy-vt.sh`
- `.github/workflows/deploy-gate.yml`
- `agent/tests/test_deploy_vt.py`

**Commits.** `8cfc14f0` (Dockerfile.local, compose override, DEPLOYMENT.local.md), `e0245ca9` (AlphaKeel paragraph in
DEPLOYMENT.local.md), merge `98e8248e` (`requirements-local-providers-lock.txt` exists only in that merge), and
`7d8b0772` (T8, pointer to `patches/`), `2925c004` (`tools/deploy-vt.sh`, audit 2026-10-12 D6-1); `.github/workflows/deploy-gate.yml` (2026-10-13: the release
gate runs what production runs — Linux, Python 3.11, frontend — instead of upstream's whole "CI" matrix).

**Upstreamable.** No, local-only (host networking, local listener, extra providers).

**Anchors.** The `derived` anchor fails as soon as upstream's `Dockerfile` changes: refresh `Dockerfile.local` by
copying the new `Dockerfile` and re-inserting the overlay block after `RUN pip install --no-cache-dir --no-deps -e .`.
The `langchain-core==1.6.1` anchor fails when upstream moves that pin; then the overlay lock has to be regenerated.
- upstream `Dockerfile`: `COPY requirements-channels-lock.txt requirements-channels-lock.txt`
- upstream `Dockerfile`: `RUN pip install --no-cache-dir --no-deps -e .` — the provider overlay is inserted right after this line
- derived `Dockerfile.local` = `Dockerfile` + overlay block starting `# Deployment adapters, pinned separately from the upstream dependency locks.`
- hook `Dockerfile.local`: `pip install --no-cache-dir --require-hashes --no-deps -r requirements-local-providers-lock.txt`
- upstream `docker-compose.yml`: `  vibe-trading:`
- upstream `docker-compose.yml`: `  vibe-home:` — DEPLOYMENT.local.md relies on the vibe-home volume
- hook `docker-compose.override.yml`: `dockerfile: Dockerfile.local`
- hook `docker-compose.override.yml`: `ALPHAKEEL_HANDOFF_DIR: /home/vibe/handoff` — the hand-off bind mount and its two variables
- hook `docker-compose.override.yml`: `init: true` — grandchildren of a timed-out alphakeel_research child (killpg) are reaped by init
- upstream `requirements-lock.txt`: `langchain-core==1.6.1 \` — the overlay's langchain-core 1.6.3 pin replaces exactly this base pin; a change means regenerating the overlay
- hook `requirements-local-providers-lock.txt`: `langchain-anthropic==1.7.2`
- hook `tools/deploy-vt.sh`: `refuse pairing "contract/PIN differs from $AK_PIN"` — the release gate (audit 2026-10-12 D6-1): "Deploy gate" workflow success on the exact commit, commit = AlphaKeel `ci/vibe-trading.ref`, PIN = `ci/vibe-trading.pin`, backup, health + `contract_pin: matched`, rollback; log in `deploy-log.jsonl`
- hook `tools/deploy-vt.sh`: `--workflow "Deploy gate"` — the gate waits for `.github/workflows/deploy-gate.yml`, not upstream's "CI"
- hook `tools/deploy-vt.sh`: `docker builder prune -f --filter` — post-success prune of old `vibe-trading-rollback:*` tags (keep `VT_ROLLBACK_KEEP`=3 and the current one) and builder cache older than `VT_BUILDER_MAX_AGE`=72h; warn-only
- hook `DEPLOYMENT.local.md`: `tools/deploy-vt.sh   # pair = AlphaKeel origin/main:ci/vibe-trading.{ref,pin}`
- hook `tools/deploy-vt.sh`: `refuse checkout "the checkout has modified or untracked files` — wave 2 (R2-D-7): untracked files refuse; a refusal after the build restores the image tag; backup dir mode 700; tests agent/tests/test_deploy_vt.py
- hook `tools/deploy-vt.sh`: `docker tag "$OLD_IMAGE_ID" "$IMAGE"`
- hook `tools/deploy-vt.sh`: `git -C "$AK_REPO" fetch --quiet origin main` — wave 2 (R2-D-1): the pair is read from AlphaKeel origin/main, not its working tree
- hook `tools/deploy-vt.sh`: `--allow-pair-override) ALLOW_PAIR_OVERRIDE=1; shift ;;` — `--ak-ref`/`--ak-pin` are refused at pairing without `--allow-pair-override` (round-3 review P2)
- hook `tools/deploy-vt.sh`: `|| refuse backup "cannot write $BACKUP/image.json"` — every failure after the build goes through refuse(): log line + image tag restored (round-3 review P2)

## `leaf-package` — AlphaKeel research client

**Purpose.** Everything AlphaKeel-specific that lives in new files: the `alphakeel_research` package (contract pin and
vectors, research-service client and CLI, dataset packs and pack reader, simulator, policy sandbox host, hand-off
writer, Strategy IR mirror, the long-history IR backtest and its robustness battery), the `alphakeel:<spec>` alpha-bench universe
(`src/factors/alphakeel_universe.py`), the `alphakeel_pack` and `alphakeel_b2` loaders, the
AlphaKeel-convention statistics (`backtest/alphakeel_metrics.py`), the `alphakeel_research` agent tool, and their
tests and fixtures. It only *uses* upstream code through the anchors below; the edits to upstream files that make it
reachable are the next groups.

**Files.**
- `agent/alphakeel_research/`
- `agent/backtest/loaders/alphakeel_pack_loader.py`
- `agent/backtest/loaders/alphakeel_b2_loader.py`
- `agent/backtest/alphakeel_metrics.py`
- `agent/src/tools/alphakeel_research_tool.py`
- `agent/src/factors/alphakeel_universe.py`
- `agent/tests/fixtures/alphakeel/`
- `agent/tests/fixtures/alphakeel_server.py`
- `agent/tests/test_alphakeel_b2_loader.py`
- `agent/tests/test_alphakeel_contract.py`
- `agent/tests/test_alphakeel_differential.py`
- `agent/tests/test_alphakeel_e2e.py`
- `agent/tests/test_alphakeel_handoff.py`
- `agent/tests/test_alphakeel_ir.py`
- `agent/tests/test_alphakeel_ir_backtest.py`
- `agent/tests/test_alphakeel_metrics.py`
- `agent/tests/test_alphakeel_policy_host.py`
- `agent/tests/test_alphakeel_precheck.py`
- `agent/tests/test_alphakeel_validation_v2.py`
- `agent/tests/test_alphakeel_protocol.py`
- `agent/tests/test_alphakeel_registration.py`
- `agent/tests/test_alphakeel_robustness.py`
- `agent/tests/test_alphakeel_sim_funding_gaps.py`
- `agent/tests/test_alphakeel_universe.py`

**Commits.** `e0245ca9` `220330b0` `83ea7c2a` `5370ab12` `e0141135` `b703f994` `22e33c58` `83af5312` `41f8c94b`
(alphakeel_metrics) `9257a920` `a379d8d2` `a232931b` `1e17c469` `bc7d3464` `0d9afe7d` `dbcea0e6` (T9b contract re-export:
`strategy-ir/README.md` and its `PIN` line), `2c8d1143` (dataset-pack market objects read per venue *and* market,
`instrument_meta` point in time by snapshot day; pairs with AlphaKeel `fix/harden-pack`), and `b2c37e39` (scan-gap funding
fix: `sim.py` `funding_feed_plan` settles boundaries a scan gap skipped, ambiguous gaps are counted and strict-failed; same
rule as AlphaKeel's engine feed; no contract change).

**Upstreamable.** No, local-only (talks to a private service, mirrors a private contract).

**Anchors.**
- upstream `agent/backtest/loaders/registry.py`: `def register(cls: Type[Any]) -> Type[Any]:` — @register on both alphakeel loaders
- upstream `agent/backtest/loaders/base.py`: `class NoAvailableSourceError(Exception):`
- upstream `agent/backtest/loaders/base.py`: `def validate_date_range(start_date: str, end_date: str) -> None:`
- upstream `agent/backtest/loaders/base.py`: `class DataLoaderProtocol(Protocol):` — name / markets / requires_auth / is_available / fetch
- upstream `agent/backtest/loaders/base.py`: `regex (?m)^    name: str\n    markets: set\[str\]\n    requires_auth: bool$`
- upstream `agent/src/config/accessor.py`: `def get_env_value(name: str, default: str = "") -> str:`
- upstream `agent/src/agent/tools.py`: `class BaseTool(ABC):`
- upstream `agent/src/agent/tools.py`: `    parameters: Dict[str, Any] = {}`
- upstream `agent/src/agent/tools.py`: `    repeatable: bool = False`
- upstream `agent/src/agent/tools.py`: `    is_readonly: bool = True`
- upstream `agent/src/agent/tools.py`: `    def execute(self, **kwargs: Any) -> str:`
- upstream `agent/src/tools/__init__.py`: `pkgutil.iter_modules([pkg_dir])` — the tool is auto-discovered, no registration edit

## `loader-registry-hooks` — loader registration

**Purpose.** Makes `alphakeel_pack` and `alphakeel_b2` real sources: listed in `VALID_SOURCES` and `_loader_modules`,
never part of a network fallback (`_NO_NETWORK_FALLBACK_SOURCES`), excluded from the public health canary
(`EXCLUDED_PUBLIC_SOURCES`, which `coverage_errors()` otherwise reports as catalog drift), and annualised like the other
24/7 crypto sources in `_TRADING_DAYS` / `_BARS_PER_DAY`.

**Files.**
- `agent/backtest/loaders/registry.py`
- `agent/backtest/loader_health.py`
- `agent/backtest/metrics.py`
- `agent/tests/test_loader_contract.py`
- `agent/backtest/runner.py`

**Commits.** `e0245ca9` `1f83ef6f` `83ea7c2a`; `ce16793f` (audit 2026-10-12 D2-15a, runner guard).

`runner._get_loader` re-raises the registry's no-fallback error for those sources instead of handing back tushare as
an "ultimate fallback" (audit 2026-10-12 D2-15a: an unavailable `alphakeel_b2` was logged as "served by tushare").

**Upstreamable.** No; it follows the leaf package. The `_get_loader` guard is upstreamable on its own (upstream's
`local`, `fmp`, `nobitex`, ... have the same no-fallback promise). (Only `alphakeel_pack` needs the health exclusion: `alphakeel_b2`
declares `requires_auth = True`, and `coverage_errors()` only maps public sources; re-check if upstream changes that rule.)

**Anchors.**
- upstream `agent/backtest/loaders/registry.py`: `VALID_SOURCES: set[str] = {`
- upstream `agent/backtest/loaders/registry.py`: `_loader_modules = [`
- upstream `agent/backtest/loaders/registry.py`: `"backtest.loaders.local_loader",`
- upstream `agent/backtest/loaders/registry.py`: `_NO_NETWORK_FALLBACK_SOURCES: frozenset[str] = frozenset(`
- hook `agent/backtest/loaders/registry.py`: `regex "local",\n    "alphakeel_pack",\n    "alphakeel_b2",\n    "auto",`
- hook `agent/backtest/loaders/registry.py`: `"backtest.loaders.alphakeel_pack_loader",`
- hook `agent/backtest/loaders/registry.py`: `"backtest.loaders.alphakeel_b2_loader",`
- hook `agent/backtest/loaders/registry.py`: `"nobitex", "wallex", "alphakeel_pack", "alphakeel_b2"}`
- preimage `agent/backtest/loaders/registry.py`: `{"local", "qveris", "tickerall", "fmp", "nobitex", "wallex"}`
- upstream `agent/backtest/loader_health.py`: `"local": "operator files, not a public endpoint"`
- upstream `agent/backtest/loader_health.py`: `def coverage_errors() -> list[str]:` — catalog drift check: every source is canaried or excluded
- hook `agent/backtest/loader_health.py`: `"alphakeel_pack": "frozen AlphaKeel pack directory`
- preimage `agent/backtest/loader_health.py`: `EXCLUDED_PUBLIC_SOURCES = {"local": "operator files, not a public endpoint"}`
- upstream `agent/backtest/metrics.py`: `_TRADING_DAYS = {`
- upstream `agent/backtest/metrics.py`: `_BARS_PER_DAY = {`
- upstream `agent/backtest/metrics.py`: `"nobitex": 365, "wallex": 365,`
- hook `agent/backtest/metrics.py`: `"alphakeel_pack": 365, "alphakeel_b2": 365,`
- hook `agent/backtest/metrics.py`: `"nobitex": 1440, "wallex": 1440, "alphakeel_pack": 1440, "alphakeel_b2": 1440,`
- upstream `agent/backtest/runner.py`: `def _get_loader(source: str):`
- upstream `agent/backtest/runner.py`: `# Ultimate fallback for unknown sources\n        if "tushare" in LOADER_REGISTRY:` — the fallback the guard sits before
- hook `agent/backtest/runner.py`: `# A source that must never degrade (local, alphakeel_b2, ...) keeps its own error: no tushare "ultimate fallback"\n        if is_no_network_fallback_source(source):\n            raise`

## `alpha-bench-universe` — `alphakeel:<spec>` universe in the alpha bench

**Purpose.** Lets the alpha zoo run on a crypto perpetual cross-section (audit 2026-10-09, U1): a universe
`alphakeel:<universe.json | inline JSON>` whose panel `src/factors/alphakeel_universe.py` (leaf package) builds from a
frozen AlphaKeel dataset pack (`kline_1m` trade bars → complete UTC 1h/4h/1d OHLCV + quote amount + vwap per base,
official funding settlements by visibility time), with the bases filter of the Strategy IR (`universe.bases` /
`excluded_bases`, optionally read from the IR itself). Three small edits make it reachable: `_load_universe_panel`
dispatches `alphakeel:` before its universe check and its pickle cache (every bench path goes through it: the
`alpha_bench` tool, `vibe-trading alpha bench|compare`, the strict and compare runners, MCP); the CLI's
`_UNIVERSE_CHOICES` is a `UniverseChoices` list that also accepts `alphakeel:<spec>`; the tool's `universe` parameter
description names it so the agent can pick it. The REST routes (`src/api/alpha_routes.py`, `_BENCH_UNIVERSES`) are
deliberately unchanged: a file path in a web request is not offered.

**Files.**
- `agent/src/tools/alpha_bench_tool.py`
- `agent/src/factors/cli_handlers.py`

**Commits.** `445c7889` (module, hooks, tests), plus the README section that follows it.

**Upstreamable.** No; it follows the leaf package. (A generic "universe provider" hook in `_load_universe_panel` would be.)

**Anchors.** On upgrade re-read `_load_universe_panel` and `_compute_forward_returns`: the panel's point-in-time rule
(a row holds what is visible at its bar close; the forward return is the next close over this one) assumes both.
- upstream `agent/src/tools/alpha_bench_tool.py`: `def _load_universe_panel(\n    universe: str, period: str, *, use_cache: bool = True\n) -> dict[str, pd.DataFrame]:` — every bench path (tool, CLI, strict and compare runners, MCP) loads its panel here
- upstream `agent/src/tools/alpha_bench_tool.py`: `def _parse_period(period: str) -> tuple[str, str]:`
- upstream `agent/src/tools/alpha_bench_tool.py`: `    if universe not in _UNIVERSE_TAG:` — the alphakeel dispatch sits right before this check (and before the pickle cache)
- upstream `agent/src/tools/alpha_bench_tool.py`: `fwd = close.pct_change(fill_method=None).shift(-1)` — forward return = next bar's close over this close: the panel's point-in-time rule (row = values visible at the bar close) relies on it
- hook `agent/src/tools/alpha_bench_tool.py`: `if isinstance(universe, str) and universe.startswith("alphakeel:"):  # AlphaKeel patch layer`
- hook `agent/src/tools/alpha_bench_tool.py`: `"alphakeel:<universe.json or inline JSON> (crypto cross-section from an AlphaKeel dataset pack; "`
- preimage `agent/src/tools/alpha_bench_tool.py`: `"description": "csi300 | sp500 | btc-usdt (resolved via existing data tools).",`
- upstream `agent/src/factors/cli_handlers.py`: `        choices=_UNIVERSE_CHOICES,` — argparse checks `value in choices`; UniverseChoices.__contains__ also accepts alphakeel:<spec>
- hook `agent/src/factors/cli_handlers.py`: `_UNIVERSE_CHOICES = UniverseChoices(["csi300", "sp500", "btc-usdt"])`
- hook `agent/src/factors/cli_handlers.py`: `from src.factors.alphakeel_universe import UniverseChoices  # AlphaKeel patch layer`
- preimage `agent/src/factors/cli_handlers.py`: `_UNIVERSE_CHOICES = ["csi300", "sp500", "btc-usdt"]`

## `crypto-funding` — crypto engine funding, fee and valuation semantics

**Purpose.** Brings `CryptoEngine` (and the crypto leg of `CompositeEngine`) to AlphaKeel's accounting. Funding is
charged at the real settlement instants in `(previous bar open, this bar open]`, at the bar open before
that bar's fills (`before_rebalance_bar`), one settlement at a time in `Decimal` (`funding_events`): a position pays a
settlement it was held over (one opened at the instant is not charged, one closed at it is), valued at the settlement's
published mark (AlphaKeel F04; `alphakeel_b2` carries it) or else the most recent close, which the metrics flag as
`funding_estimate` (audit 2026-10-12 D2-11; the old hook summed the bar's rates at the next open and only checked the
last settlement). The cash rate column is `funding_rate` (ccxt) or `funding_cash_rate` (`alphakeel_b2`, which also
serves `funding_visible_rate` by `available_at` for signals, D2-10). A perpetual without settlement data (`funding_rate` + `funding_settlement_time` columns)
is refused instead of being charged an assumed 0.0001; spot-priced proxies keep the assumed rate and report it
(`funding_models`). Every fill pays taker (the old "closes are maker" assumption was optimistic), and equity is valued
close-now, net of the taker fee to exit. The ccxt loader requires every settlement on the 8h grid and attaches the
settlements through `funding_settlements.attach_settlements` (shared with the `alphakeel_b2` loader). The
`perpetual_strict` path is untouched.

**Files.**
- `agent/backtest/engines/_market_hooks.py`
- `agent/backtest/engines/crypto.py`
- `agent/backtest/engines/composite.py`
- `agent/backtest/loaders/ccxt_loader.py`
- `agent/backtest/funding_settlements.py`
- `agent/tests/test_crypto_engine.py`
- `agent/tests/test_composite_engine_fallback.py`
- `agent/backtest/loaders/base.py`
- `agent/backtest/perpetual_evidence.py`
- `agent/tests/test_composite_currency_guard.py`
- `agent/tests/test_local_source_routing.py`
- `agent/tests/test_yfinance_crypto_routing.py`
- `agent/tests/test_korea_equity_engine.py`
- `agent/src/shadow_account/backtester.py`
- `agent/src/tools/trade_journal_parsers.py`
- `agent/src/shadow_account/codegen.py`
- `agent/src/tools/shadow_account_tool.py`
- `agent/src/tools/autopilot_tool.py`
- `agent/tests/test_shadow_account.py`
- `agent/tests/test_autopilot_tool.py`
- `agent/tests/test_korea_equity_engine.py`

**Wave 2 (2026-10-13, R2-D-4 b / R2-D-6).** `CryptoEngine` has no default taker fee: `config.taker_rate` is required
(`taker_rate_of`; a crypto run without it is refused, the rate used is in the metrics), and the strict path's evidence
summary reads the same key; the upstream tests that built a crypto engine without it now set it (the KRX short-config
routing test too, `037d73d5`).
`resample_bars` keeps settlement-attributed funding whole on 1W/1M bars (events concatenated, cash rate and count
summed, last settlement time) instead of the last day's, and the composite reports the crypto leg's funding summary
(`funding_estimate`, `funding_priced_at_close`) like `CryptoEngine`. The strict perpetual path still reads `funding_rate`
per bar (ccxt only: `alphakeel_b2` frames carry no `mark_open`/`execution_open` and never enter it). The two production paths that write a crypto `config.json` take the fee from a real source: the shadow
account's crypto pool from `crypto_taker_rate` or the journal's own crypto fills (highest fee/notional, counting only
fills whose fee is charged in the pair's quote currency: `TradeRecord.fee_currency`, which the generic parser reads
from a `fee_currency`/`fee_asset`/`commission_asset` column or the fee cell; a BNB or unstated-currency fee is left
out, round-3 review P2), else it is refused before any pool runs (`resolve_crypto_taker_rate`); `generate_backtest_config` refuses a crypto universe without
`taker_rate`.

**Commits.** `622ea894` (engines, ccxt loader, funding_settlements, tests), `41f8c94b` (composite test expectation), `fdab5523`
(audit 2026-10-12 D2-10/D2-11: per-settlement Decimal funding at the published mark, `funding_estimate`).

**Upstreamable.** Yes, as a bug-fix PR: the daily-bar/intraday over- and under-charging of funding (the old hook
charged a 1H bar four times a day), the silent fixed rate for perpetuals and the missing-settlement check in
`ccxt_loader` are defects upstream too. Taker-on-every-fill and close-now valuation are modelling conventions and
would be a separate, optional PR (config switch). The PR would contain `CryptoFunding`/`funding_model_for`,
`funding_settlements.py`, the engine changes and `test_crypto_engine.py`.

**Anchors.** The engine hooks depend on the bar loop's order (`before_rebalance_bar` → open valuation → fills →
`after_rebalance_bar`/`on_bar`), which no anchor can prove: any upstream change to `engines/base.py`'s
`_execute_bars` means re-reading it even when every anchor holds.
- upstream `agent/backtest/engines/base.py`: `    def before_rebalance_bar(\n        self,\n        timestamp: pd.Timestamp,\n        data_map: Dict[str, pd.DataFrame],\n        codes: List[str],\n    ) -> bool:` — funding is charged here, pre-fill
- upstream `agent/backtest/engines/base.py`: `stop_run = self.before_rebalance_bar(ts, data_map, codes)` — called before the open valuation and the fills
- upstream `agent/backtest/engines/base.py`: `equity = self._calc_open_equity(data_map, close_df, ts)`
- upstream `agent/backtest/engines/base.py`: `self.on_bar(code, data_map[code].loc[timestamp], timestamp)` — on_bar runs post-fill (after_rebalance_bar); liquidation only
- upstream `agent/backtest/engines/base.py`: `    def _calc_equity(self, close_df: pd.DataFrame, ts: pd.Timestamp) -> float:`
- upstream `agent/backtest/engines/base.py`: `    def _calc_open_equity(\n        self,\n        data_map: Dict[str, pd.DataFrame],\n        close_df: pd.DataFrame,\n        ts: pd.Timestamp,\n    ) -> float:`
- upstream `agent/backtest/engines/base.py`: `        close_val_df: Optional[pd.DataFrame] = None,\n    ) -> None:` — _execute_bars(dates, data_map, close_df, target_pos, codes, close_val_df=None)
- upstream `agent/backtest/engines/base.py`: `    def calc_commission(self, size: float, price: float, direction: int, is_open: bool) -> float:`
- upstream `agent/backtest/engines/base.py`: `    def valuation_open(self, bar: pd.Series) -> float:`
- upstream `agent/backtest/engines/base.py`: `        _val_arr: "np.ndarray | None" = None,` — _safe_price keyword-only fast-path args used by the close-now valuation
- upstream `agent/backtest/engines/base.py`: `self._close_arr = _close_arr`
- upstream `agent/backtest/engines/base.py`: `self._code_to_col = _code_to_col`
- upstream `agent/backtest/engines/base.py`: `self._bar_idx = i`
- upstream `agent/backtest/engines/base.py`: `    def _write_artifacts(\n        self,\n        run_dir: Path,`
- upstream `agent/backtest/engines/crypto.py`: `self.perpetual_strict = bool(config.get("perpetual_strict", False))` — the strict perpetual path keeps upstream accounting; our hooks only run when not strict
- hook `agent/backtest/engines/crypto.py`: `self._funding = CryptoFunding(self.funding_rate)`
- hook `agent/backtest/engines/crypto.py`: `        return size * price * self.taker_rate`
- hook `agent/backtest/engines/crypto.py`: `    def _exit_fees(self, prices: dict[str, float]) -> float:`
- hook `agent/backtest/engines/crypto.py`: `"valuation": "close_now_net_of_exit_fee"`
- preimage `agent/backtest/engines/crypto.py`: `rate = self.taker_rate if self.perpetual_strict or is_open else self.maker_rate`
- preimage `agent/backtest/engines/crypto.py`: `self._funding_daily_done: set = set()`
- upstream `agent/backtest/engines/composite.py`: `self._rule_engines = _build_rule_engines(config, codes)`
- upstream `agent/backtest/engines/composite.py`: `market = self._symbol_market.get(symbol)`
- hook `agent/backtest/engines/composite.py`: `self._funding = CryptoFunding(crypto.funding_rate) if crypto is not None else None`
- hook `agent/backtest/engines/composite.py`: `self.capital -= self._funding.settle(code, frame.loc[timestamp], timestamp, self.positions)`
- preimage `agent/backtest/engines/composite.py`: `self._funding_daily_done: set = set()`
- upstream `agent/backtest/engines/_market_hooks.py`: `def check_crypto_liquidation(`
- upstream `agent/backtest/engines/_market_hooks.py`: `def _liquidation_mark(bar: pd.Series, pos: Position) -> float:`
- hook `agent/backtest/engines/_market_hooks.py`: `class CryptoFunding:`
- hook `agent/backtest/engines/_market_hooks.py`: `def funding_model_for(code: str, frame: pd.DataFrame) -> str:`
- preimage `agent/backtest/engines/_market_hooks.py`: `def calc_crypto_funding_fee(`
- preimage `agent/backtest/engines/_market_hooks.py`: `FUNDING_HOURS = {0, 8, 16}`
- hook `agent/backtest/engines/_market_hooks.py`: `FUNDING_CASH_RATE_COLUMNS = ("funding_cash_rate", "funding_rate")`
- hook `agent/backtest/engines/_market_hooks.py`: `payment += size * px * rate` — per-settlement Decimal booking at the published mark, else the most recent close
- preimage `agent/backtest/engines/_market_hooks.py`: `payment = pos.direction * pos.size * float(price) * total_rate`
- hook `agent/backtest/engines/crypto.py`: `"funding_estimate": f["funding_estimate"]`
- upstream `agent/backtest/loaders/ccxt_loader.py`: `funding = cls._fetch_funding_history(exchange, symbol, since_ms, end_ms)`
- upstream `agent/backtest/loaders/ccxt_loader.py`: `if bracket_artifact is not None:`
- hook `agent/backtest/loaders/ccxt_loader.py`: `result = attach_settlements(result, events)`
- preimage `agent/backtest/loaders/ccxt_loader.py`: `_FUNDING_HOURS = {0, 8, 16}`
- hook `agent/backtest/engines/crypto.py`: `self.taker_rate: float = taker_rate_of(config)` — no default taker fee: a crypto run without config.taker_rate is refused (R2-D-4 b)
- preimage `agent/backtest/engines/crypto.py`: `self.taker_rate: float = config.get("taker_rate", 0.0005)`
- hook `agent/backtest/engines/crypto.py`: `"taker_rate": self.taker_rate,`
- upstream `agent/backtest/perpetual_evidence.py`: `def build_perpetual_summary(`
- hook `agent/backtest/perpetual_evidence.py`: `"taker_rate": float(config["taker_rate"]),`
- preimage `agent/backtest/perpetual_evidence.py`: `"taker_rate": float(config.get("taker_rate", 0.0005)),`
- hook `agent/backtest/engines/composite.py`: `def _write_artifacts(self, run_dir, data_map, dates, equity_series, bench_equity, bench_ret, target_pos, metrics, codes) -> None:` — the crypto leg funding summary in the composite metrics (R2-D-6)
- upstream `agent/backtest/loaders/base.py`: `def resample_bars(frame: pd.DataFrame, interval: str) -> pd.DataFrame:`
- upstream `agent/backtest/loaders/base.py`: `        elif column == "funding_rate":\n            columns[column] = values.mean()` — kept for frames without funding_events
- hook `agent/backtest/loaders/base.py`: `settled = "funding_events" in frame.columns` — 1W/1M bars keep every settlement of the period (R2-D-6)
- upstream `agent/src/shadow_account/backtester.py`: `def run_shadow_backtest(`
- hook `agent/src/shadow_account/backtester.py`: `def resolve_crypto_taker_rate(explicit` — the crypto pool taker fee from the caller or the journal crypto fills, else refused (R2-D-4)
- hook `agent/src/shadow_account/backtester.py`: `if not quote or r.fee_currency != quote:` — a journal fee counts toward the crypto taker rate only when charged in the pair's quote currency (round-3 review P2)
- upstream `agent/src/tools/trade_journal_parsers.py`: `def parse_generic(df: pd.DataFrame) -> list[TradeRecord]:`
- hook `agent/src/tools/trade_journal_parsers.py`: `    fee_currency: str = ""\n` — TradeRecord.fee_currency, read by parse_generic from a fee-currency column or the fee cell (round-3 review P2)
- upstream `agent/src/shadow_account/codegen.py`: `def write_run_dir(`
- hook `agent/src/shadow_account/codegen.py`: `        extra=extra,\n    )`
- hook `agent/src/tools/shadow_account_tool.py`: `crypto_taker_rate=kwargs.get("crypto_taker_rate"),`
- upstream `agent/src/tools/autopilot_tool.py`: `class GenerateBacktestConfigTool(BaseTool):`
- hook `agent/src/tools/autopilot_tool.py`: `config["taker_rate"] = taker_rate_of({"taker_rate": kwargs["taker_rate"]})` — a crypto universe needs the caller taker_rate (R2-D-4)

## `metrics-conventions` — undefined ratios are `None`; Sortino definition

**Purpose.** Undefined Sharpe and Sortino (fewer than two returns, no variance or no downside, non-finite returns) are
`None` instead of `mean / (std + 1e-10)` (which turned a flat curve into an astronomically large ratio); Sortino uses the
downside deviation `sqrt(mean(min(r, 0)^2))` over all returns with target 0 (Sortino & Price, AlphaKeel's definition);
`validation.py`'s Monte Carlo, bootstrap and walk-forward use a ddof=1 `_sharpe` that returns `None` and refuse to
report a statistic built on undefined samples.

**Files.**
- `agent/backtest/metrics.py`
- `agent/backtest/validation.py`
- `agent/tests/test_metrics.py`
- `agent/tests/test_metrics_inf_zero_equity.py`
- `agent/src/shadow_account/reporter.py`
- `agent/tests/test_shadow_account.py`
- `agent/tests/test_metrics_sortino.py`
- `agent/tests/test_validation_initial_capital.py`

**Commits.** `41f8c94b`; shadow report N/A (audit 2026-10-09).

**Upstreamable.** Partly done upstream since BASE: `95389681` adopted the same full-sample downside deviation for
Sortino (but keeps a `1e-10` fallback when there is no downside) and `f110ad6c` made profit factor / P-L ratio `None`
when no trade lost. The remaining PR would be: `None` for undefined Sharpe/Sortino in `calc_metrics`/`_empty_metrics`,
the `validation.py` guards, and the tests.

**Anchors.**
- upstream `agent/backtest/metrics.py`: `def calc_metrics(`
- upstream `agent/backtest/metrics.py`: `vol = float(port_ret.std()) if len(port_ret) > 1 and returns_finite else 0.0`
- upstream `agent/backtest/metrics.py`: `def _empty_metrics(initial_cash: float) -> Dict[str, Any]:`
- hook `agent/backtest/metrics.py`: `sharpe: float | None = (`
- hook `agent/backtest/metrics.py`: `sortino: float | None = None`
- hook `agent/backtest/metrics.py`: `"sharpe": None, "calmar": 0, "sortino": None,`
- preimage `agent/backtest/metrics.py`: `float(port_ret.mean() / (vol + 1e-10) * np.sqrt(bpy))`
- preimage `agent/backtest/metrics.py`: `else 1e-10  # Preserve the existing no-downside fallback.`
- preimage `agent/backtest/metrics.py`: `"sharpe": 0, "calmar": 0, "sortino": 0,`
- upstream `agent/backtest/validation.py`: `def monte_carlo_test(`
- upstream `agent/backtest/validation.py`: `def bootstrap_sharpe_ci(`
- upstream `agent/backtest/validation.py`: `def walk_forward_analysis(`
- upstream `agent/backtest/validation.py`: `def _path_metrics(`
- hook `agent/backtest/validation.py`: `def _sharpe(returns: np.ndarray, bars_per_year: int = 252) -> float | None:`
- hook `agent/src/shadow_account/reporter.py`: `"N/A" if value is None else f"{value:.2f}"`
- hook `agent/backtest/validation.py`: `"sharpe_undefined_windows"`
- preimage `agent/backtest/validation.py`: `def _sharpe(returns: np.ndarray, bars_per_year: int = 252) -> float:`
- preimage `agent/backtest/validation.py`: `sharpe = float(returns.mean() / (std + 1e-10) * np.sqrt(bars_per_year))`

## `run-card-data-audit` — data audit in the run card

**Purpose.** The run card records a `data_audit` block: which sources are auditable (frozen, digest-verified AlphaKeel
packs, with dataset versions per read) and which are not (live ccxt/okx/binance pulls), adds a warning for
non-auditable data and renders a "Data Audit" section. Loaders record provenance in-process
(`data_audit.record_provenance`). Since AlphaKeel protocol 2.5 (02 WP5) every non-empty read through the shared
loader cache wrapper (`cached_loader_fetch`, used by the ccxt/okx/binance loaders) also records the end of the data it
returned (`data_audit.record_exposure`); the IR export writes the latest one as `provenance.data_exposure_end_ms`.
Every tool call that reads data runs in a process of its own (the backtest tool's engine child, the research tool's
CLI child), so the in-process record alone missed a direct exchange pull made earlier in the conversation (round 1
review, appendix C1). `record_exposure` therefore also appends to the conversation's exposure file
`<sessions dir>/<session_id>/data_exposure.jsonl` (flock + fsync, as the IR trial ledger) for the session the agent loop
binds (`src.providers.session_context`); the backtest tool folds its child's exposure into it after the child (the run
card's `data_exposure_end_ms`, and for a live source the config's end, also after a failure or timeout); `ir_export`
passes the latest end as `--data-exposure-end-ms`. The loop runs readonly tool threads in a copy of the caller's
context so the binding reaches them (as upstream already does for the compaction thread).

**Files.**
- `agent/backtest/run_card.py`
- `agent/backtest/data_audit.py`
- `agent/backtest/loaders/base.py`
- `agent/backtest/loaders/yfinance_loader.py`
- `agent/backtest/loaders/india_broker_loader.py`
- `agent/src/tools/backtest_tool.py`
- `agent/src/agent/loop.py`
- `agent/tests/test_session_data_exposure.py`

**Commits.** `83ea7c2a` `5370ab12`, plus the session-exposure commit on `fix/session-data-exposure`.

**Upstreamable.** Not as written (the auditable set is AlphaKeel's). A generic "source provenance / reproducibility
class" field in the run card would be upstreamable.

**Anchors.**
- upstream `agent/backtest/run_card.py`: `def write_run_card(`
- upstream `agent/backtest/run_card.py`: `    data_sources: Sequence[str] | None = None,`
- upstream `agent/backtest/run_card.py`: `"data_sources": list(data_sources or []),`
- upstream `agent/backtest/run_card.py`: `def _render_markdown(card: Mapping[str, Any]) -> str:`
- upstream `agent/backtest/run_card.py`: `lines.extend(["", "## Metrics"])`
- hook `agent/backtest/run_card.py`: `from backtest.data_audit import data_audit, non_auditable_warning`
- hook `agent/backtest/run_card.py`: `"data_audit": audit,`
- hook `agent/backtest/run_card.py`: `"## Data Audit"`
- preimage `agent/backtest/run_card.py`: `"warnings": [*(warnings or []), *provenance_warnings],`
- upstream `agent/backtest/loaders/base.py`: `def cached_loader_fetch(`
- hook `agent/backtest/loaders/base.py`: `def _note_exposure(source: str, end_date: str, frame: pd.DataFrame | None, symbol: str = "") -> None:`
- hook `agent/backtest/loaders/base.py`: `_note_exposure(source, end_date, cached, symbol)`
- hook `agent/backtest/loaders/base.py`: `_note_exposure(source, end_date, frame, symbol)`
- upstream `agent/backtest/loaders/yfinance_loader.py`: `results[original_code] = normalized.copy()`
- hook `agent/backtest/loaders/yfinance_loader.py`: `_note_exposure(self.name, end_date, cached, original_code)` (yfinance
  and the India broker loader bypass `cached_loader_fetch`, so they record their reads' exposure themselves)
- hook `agent/backtest/loaders/yfinance_loader.py`: `_note_exposure(self.name, end_date, normalized, original_code)`
- upstream `agent/backtest/loaders/india_broker_loader.py`: `result[code] = frame`
- hook `agent/backtest/loaders/india_broker_loader.py`: `_note_exposure(self.name, end_date, frame, code)`
- upstream `agent/src/tools/backtest_tool.py`: `runner = Runner(timeout=_backtest_timeout_seconds())`
- upstream `agent/src/tools/backtest_tool.py`: `state_store = RunStateStore()`
- hook `agent/src/tools/backtest_tool.py`: `def _note_child_exposure(run_path: Path, config: dict[str, Any]) -> None:`
- hook `agent/src/tools/backtest_tool.py`: `    _note_child_exposure(run_path, config)\n    state_store = RunStateStore()`
- upstream `agent/src/agent/loop.py`: `name=f"tool-{tool_name}",`
- hook `agent/src/agent/loop.py`: `args=(_worker,),`
- hook `agent/src/agent/loop.py`: `pool.submit(contextvars.copy_context().run, _run, item)`
- preimage `agent/src/agent/loop.py`: `futures = [pool.submit(_run, item) for item in runnable]`

## `alphakeel-handoff` — hand-off of an IR run to AlphaKeel

**Purpose.** The "Hand off to AlphaKeel" button on the run page (docs/45 section 7, plan WP2 2.7). An `ir backtest` run is a
first-class Vibe-Trading run (its directory under `RUNS_DIR` carries `ir.json`, `req.json`, `state.json`, ...); the run page
shows the robustness grade and the research-side G1 precheck (`alphakeel_research/precheck.py`, the same protocol and
vectors as AlphaKeel's verdict) and, on the user's click, `ir_backtest.export` writes the package into the shared
hand-off directory (`ALPHAKEEL_HANDOFF_DIR`, bind-mounted from the host, see `local-deploy`). The agent tool cannot
export there. New files carry everything; the upstream files only get hooks.

**Files.**
- `agent/src/api/alphakeel_handoff_routes.py`
- `agent/api_server.py`
- `agent/tests/test_alphakeel_handoff_routes.py`
- `frontend/src/components/run/AlphaKeelHandoff.tsx`
- `frontend/src/pages/RunDetail.tsx`
- `frontend/src/components/run/__tests__/AlphaKeelHandoff.test.tsx`
- `frontend/src/lib/api.ts`
- `frontend/src/i18n/locales/ar.json`
- `frontend/src/i18n/locales/de.json`
- `frontend/src/i18n/locales/en.json`
- `frontend/src/i18n/locales/es.json`
- `frontend/src/i18n/locales/id.json`
- `frontend/src/i18n/locales/ja.json`
- `frontend/src/i18n/locales/ko.json`
- `frontend/src/i18n/locales/pt-BR.json`
- `frontend/src/i18n/locales/zh-CN.json`

**Commits.** `e736638b` (backend, tests, patch records), `a0c12108` (button, `RunDetail.tsx` hooks, `api.ts` export, locales), merged in `c830d89f`.

**Upstreamable.** No, local-only (the hand-off target is AlphaKeel's).

**Anchors.** `api_server.py` keeps the registration right after `register_runs_routes(app)`; the routes resolve
`require_auth`, `_validate_path_param` and `RUNS_DIR` from the host `api_server` module at call time, like the runs routes,
so an upstream rename of those fails the route tests, not an import. `RunDetail.tsx` keeps the header action group the
button is mounted in.
- upstream `agent/api_server.py`: `register_runs_routes(app)` — the hand-off routes are registered right after the runs routes (they reuse its auth dependency, run-id validation and RUNS_DIR)
- hook `agent/api_server.py`: `register_alphakeel_handoff_routes(app)`
- upstream `frontend/src/pages/RunDetail.tsx`: `<div className="ml-auto flex flex-wrap gap-1">` — the header action group; the hand-off button is its first child
- hook `frontend/src/pages/RunDetail.tsx`: `import { AlphaKeelHandoff } from "@/components/run/AlphaKeelHandoff";`
- hook `frontend/src/lib/api.ts`: `export async function request<T>(` — the component reuses the authenticated request helper (one `export` keyword)
- preimage `frontend/src/lib/api.ts`: `\nasync function request<T>(`
- hook `frontend/src/pages/RunDetail.tsx`: `<AlphaKeelHandoff runId={runId} />`
- hook `frontend/src/i18n/locales/<9 locales>.json`: `"needRobustness":` — the `runDetail.alphakeel` block

## `packaging-docs` — packaging and documentation

**Purpose.** Ships the leaf package (`packages.find` include, contract package data), adds `hypothesis` to `dev` and an
`alphakeel-dev` extra for the cross-project CI, documents the two sources in `SKILL.md` and the seven READMEs, the
environment variables in `agent/.env.example`, ignores build artifacts; un-ignores `AGENTS.md` (upstream ignores it as a
generated mirror of CLAUDE.md; here it is the real, hand-written project rules file, new file, local-only); and this patch
layer itself (`patches/`).

**Files.**
- `pyproject.toml`
- `agent/SKILL.md`
- `.gitignore`
- `AGENTS.md`
- `agent/.env.example`
- `README.md`
- `README_ar.md`
- `README_es.md`
- `README_id.md`
- `README_ja.md`
- `README_ko.md`
- `README_zh.md`
- `patches/`
- `agent/tests/test_alphakeel_patch_layer.py`
- `CHANGELOG.md`
- `agent/src/skills/strategy-generate/SKILL.md`
- `agent/src/skills/strategy-generate/examples.md`
- `agent/src/skills/minute-analysis/SKILL.md`
- `agent/src/skills/pair-trading/SKILL.md`
- `agent/src/skills/cross-market-strategy/SKILL.md`
- `agent/src/skills/yfinance/SKILL.md`

**Commits.** `e0245ca9` `220330b0` `1f83ef6f` `83ea7c2a` `9257a920`, `7d8b0772` (T8, `patches/`), `456b918f`
(upgrade-rehearsal record), `ff1d0f04` (T9b: `alphakeel_b2` in every README, 30 sources; cherry-picked from the
rehearsal's `66327a89`), `2a92d711` (T9b verification record), the commit that adds these lists and the one that adds `2c8d1143` to them.

**Upstreamable.** No, local-only.

**Anchors.** The READMEs' counts ("28 → 30 market-data sources", "107 → 108 agent tools", the `loaders/` list) are
not anchored: upstream edits these lines constantly (41 commits to README.md since BASE). On upgrade take upstream's
text and re-derive the counts (upstream count + 2 sources, + 1 tool). Until `ff1d0f04` the text said 29 sources and only
README.md had the `alphakeel_b2` row; every README and SKILL.md now say 30 and list `alphakeel_b2` in the table and
the `loaders/` tree line.
- upstream `pyproject.toml`: `[tool.setuptools.packages.find]`
- upstream `pyproject.toml`: `"backtest" = ["*.py"]`
- upstream `pyproject.toml`: `dev = [`
- hook `pyproject.toml`: `"evals*", "alphakeel_research*"]`
- hook `pyproject.toml`: `"alphakeel_research" = [`
- hook `pyproject.toml`: `alphakeel-dev = [`
- hook `pyproject.toml`: `"hypothesis>=6.100,<7",`
- preimage `pyproject.toml`: `include = ["src*", "backtest*", "cli*", "evals*"]`
- upstream `agent/SKILL.md`: `- **China A-shares** via AKShare`
- upstream `agent/SKILL.md`: `` | `get_market_data` | Fetch OHLCV data ``
- hook `agent/SKILL.md`: `explicit-only alphakeel_pack and alphakeel_b2`
- hook `agent/SKILL.md`: `"source": "alphakeel_b2"`
- preimage `agent/SKILL.md`: `- **Cryptocurrency** via OKX or CCXT/100+ exchanges (free, no API key)`
- upstream `.gitignore`: `.idea/*`
- hook `.gitignore`: `agent/alphakeel_research/*.egg-info/`
- upstream `.gitignore`: `# Agent tool aliases (auto-generated mirror of CLAUDE.md)`
- hook `.gitignore`: `!AGENTS.md`
- hook `.gitignore`: `/deploy-log.jsonl` — written by `tools/deploy-vt.sh` (group `local-deploy`)
- hook `.gitignore`: `\n.schemathesis/\n` — the schemathesis run cache of the contract tests (a test artifact)
- upstream `CHANGELOG.md`: `## [Unreleased]`
- hook `CHANGELOG.md`: `### AlphaKeel patch layer — 2026-10-12 (end-to-end audit)` — the patch layer's own section under Unreleased; on an upstream upgrade keep it at the top of Unreleased (upstream edits this file constantly)
- hook `agent/SKILL.md`: `` **`CryptoEngine` has no default fee:** `` — wave 2: `taker_rate` is required (R2-D-4 b)
- hook `agent/SKILL.md`: `` a signal reads only `funding_visible_rate` `` — wave 2: the cash funding columns are accounting only (R2-D-5)
- upstream `agent/src/skills/strategy-generate/SKILL.md`: `` - `commission`: default 0.1% ``
- hook `agent/src/skills/strategy-generate/SKILL.md`: `` - `taker_rate`: **required for crypto** ``
- hook `agent/src/skills/strategy-generate/examples.md`: `"taker_rate": 0.0005` — crypto config example sets taker_rate (CryptoEngine has no default fee, R2-D-4)
- hook `agent/src/skills/minute-analysis/SKILL.md`: `"taker_rate": 0.0005` — crypto config example sets taker_rate (CryptoEngine has no default fee, R2-D-4)
- hook `agent/src/skills/pair-trading/SKILL.md`: `"taker_rate": 0.0005` — crypto config example sets taker_rate (CryptoEngine has no default fee, R2-D-4)
- hook `agent/src/skills/cross-market-strategy/SKILL.md`: `"taker_rate": 0.0005` — crypto config example sets taker_rate (CryptoEngine has no default fee, R2-D-4)
- hook `agent/src/skills/yfinance/SKILL.md`: `"taker_rate": 0.0005` — crypto config example sets taker_rate (CryptoEngine has no default fee, R2-D-4)
- upstream `agent/.env.example`: `# ETORO_USER_KEY=your_user_key`
- hook `agent/.env.example`: `# ALPHAKEEL_RESEARCH_URL=`
- upstream `README.md`: `` regex (?m)^\| `local` \| `` — source-table row the alphakeel rows follow
- hook `README.md`: `` | `alphakeel_pack` | ``
- upstream `README_ar.md`: `` regex (?m)^\| `local` \| `` — source-table row the alphakeel rows follow
- hook `README_ar.md`: `` | `alphakeel_pack` | ``
- upstream `README_es.md`: `` regex (?m)^\| `local` \| `` — source-table row the alphakeel rows follow
- hook `README_es.md`: `` | `alphakeel_pack` | ``
- upstream `README_id.md`: `` regex (?m)^\| `local` \| `` — source-table row the alphakeel rows follow
- hook `README_id.md`: `` | `alphakeel_pack` | ``
- upstream `README_ja.md`: `` regex (?m)^\| `local` \| `` — source-table row the alphakeel rows follow
- hook `README_ja.md`: `` | `alphakeel_pack` | ``
- upstream `README_ko.md`: `` regex (?m)^\| `local` \| `` — source-table row the alphakeel rows follow
- hook `README_ko.md`: `` | `alphakeel_pack` | ``
- upstream `README_zh.md`: `` regex (?m)^\| `local` \| `` — source-table row the alphakeel rows follow
- hook `README_zh.md`: `` | `alphakeel_pack` | ``

## `upstream-test-fixes` — two upstream tests that are red on upstream main

**Purpose.** `tools/deploy-vt.sh` refuses to rebuild the production container unless the "CI" workflow is green on the
exact commit, and two upstream tests fail on a pristine upstream checkout (`e532650b`, reproduced locally 2026-10-12):
`test_explicit_crypto_pairs_stay_crypto[ETH/USD]` (the `^[A-Z]{3}/[A-Z]{3}$` forex rule in `_MARKET_PATTERNS` claimed
`ETH/USD` before any crypto rule saw it) and `test_alpha_bench_does_not_follow_preexisting_report_symlink` (its registry
stub has no `.meta` while `_validate_bench_data_requirements` read `registry.get(aid).meta`). Fixes: two slash-form
crypto patterns ahead of the forex rule (stablecoin quote, or a major crypto base with a USD quote); `getattr(…, "meta",
None)`. Both are candidates for an upstream PR; drop the group when upstream carries them.

**Files.**
- `agent/backtest/engines/_market_hooks.py` (also in `crypto-funding`)
- `agent/src/tools/alpha_bench_tool.py` (also in `alpha-bench-universe`)

**Commits.** (this commit; audit 2026-10-12 round-2 D6-1 follow-up).

## `ci-env-gate` — repository safety gate e

**Purpose.** Upstream's CI step "Repository safety gates" (`tools/ci_grep_gates.sh`, gate e = `tools/ci_env_var_gate.py`)
rejects any raw `os.getenv`/`os.environ` read outside `agent/src/config/`; the job stops there and the test suite never
runs (audit 2026-10-12 D6-1: every production commit 10-05..10-12 was red at this step). Upstream's own
`/alpha/readiness` read `TUSHARE_TOKEN` raw; it now reads the typed field `get_env_config().data.tushare_token`
(the mechanism `src/preflight.py` uses). The patch layer's own reads (`alphakeel_handoff_routes.py`,
`alphakeel_research_tool.py`) go through `src.config.accessor.get_env_value`, like `alphakeel_universe.py`.

**Files.**
- `agent/src/api/alpha_routes.py`

**Commits.** `2925c004` (audit 2026-10-12 D6-1).

**Upstreamable.** Yes: the readiness line is a defect against upstream's own gate.

**Anchors.**
- upstream `tools/ci_env_var_gate.py`: `ALLOWED_PREFIX: str = "agent/src/config/"`
- upstream `tools/ci_grep_gates.sh`: `E_OUTPUT=$(python tools/ci_env_var_gate.py 2>&1)`
- upstream `agent/src/config/accessor.py`: `def get_env_config() -> EnvConfig:`
- upstream `agent/src/config/env_schema.py`: `tushare_token: str = Field(alias="TUSHARE_TOKEN", default="")`
- upstream `agent/src/api/alpha_routes.py`: `async def alpha_readiness():`
- hook `agent/src/api/alpha_routes.py`: `configured = bool(get_env_config().data.tushare_token)`
- preimage `agent/src/api/alpha_routes.py`: `configured = bool(os.getenv("TUSHARE_TOKEN"))`

## `grounding-tex-dollar` — TeX `$`/`$$` is not a dollar mark

**Purpose.** In `scan_figures` the closing `$$` of `$$ … \times 365$$` read as a currency mark on 365, so it was flagged
`figure_undeclared` and released as `(omitted※)`, breaking the formula (session 8125728230d5). `_blank_math_delimiters`
blanks the delimiter `$` of display math and pandoc-rule inline math (same length, offsets stay valid) in the text the
currency helpers see; `\$` stays a literal dollar, `$5` / `100$` in prose stay currency. Upstreamable as a bug fix.

**Files.**
- `agent/src/agent/grounding/figures.py`
- `agent/tests/test_grounding_figures_contract.py`

**Commits.** (this commit).

## `strategy-fidelity` — a blocked backtest is reported, never swapped for another strategy

**Purpose.** In session 8125728230d5 the user asked for a cross-venue funding carry backtest; AlphaKeel's service had no
published market dataset, the agent offered "use the built-in engine now", read the reply 「立即利用」 as consent and ran a
single-asset spot breakout on OKX instead. One bullet in the system prompt's general rules: report the exact blocker,
offer only fallbacks that keep the same strategy, run a different strategy only after the user confirms it by name, and
say in the first line when what ran differs from what was asked. It sits outside "Output Principles" because upstream's
tests pin that block at six principles. The tool half (an `alphakeel_research` error meaning "no market dataset" gains a
`next: BLOCKED …` instruction) lives in `leaf-package`.

**Files.**
- `agent/src/agent/context.py`
- `agent/tests/test_strategy_fidelity.py`

**Commits.** (this commit).

**Upstreamable.** Yes, the prompt bullet (without the AlphaKeel example). Not submitted.

**Anchors.**
- upstream `agent/src/agent/context.py`: `- Ask the user if critical info is missing (assets, dates, strategy type). Never guess.` — the rule is the bullet right after this one
- hook `agent/src/agent/context.py`: `- Backtest the strategy that was asked for, or say you did not.`

## `ir-validate-all` — ir_validate lists every Strategy IR problem at once

**Purpose.** In session b9aa13a496a4 the agent spent ~20 `ir_validate` calls guessing the Strategy IR shape: the call went
straight to AlphaKeel's Rust validator, which stops at the first serde error. `ir.problems()` runs the same checks as
`ir.validate()` but collects them: every missing and unknown field of every object, each section checked independently
(semantic checks of a section only when its shape passed, parameter paths and `strategy_id` only when the whole shape
passed). `validate()`/`load()` keep their first-error result (contract vectors, Rust parity); the shape and semantic rules
are shared per-section functions, not a second rule set. The tool's `ir_validate` returns the local problems with
`source: "local"` without calling AlphaKeel, and asks AlphaKeel (still authoritative) only when there are none.
`ir.unit_warnings()` flags an `exit.max_loss` / `exit.take_profit` below 0.1% of `sizing.notional_per_leg` (the agent wrote the fraction
`"0.01"`; the IR's `max_loss` is a USDT amount); `ir_validate` and `ir_backtest` envelopes carry it as `warnings`, never
blocking. The tool description no longer claims dataset packs carry bbo/depth20/instrument_meta for any window (they are
live captures of recent days) and says `ir_backtest` freezes its own pack. The code lives in `leaf-package` files; this
group holds the regression test. Same session: `ir export` rebuilt `provenance` from scratch and dropped
the input's `provenance.fixed_literals`, which AlphaKeel's G1 1.19 and `precheck.py` read from the exported strategy.json;
the export now carries it through unchanged.

**Files.**
- `agent/tests/test_ir_validate_all.py`

**Commits.** `d8d6e4e3` (local checker, unit warning, tool, description), `055a488a` (export keeps
`provenance.fixed_literals`), `ed0aaaec` (review round 1: `ir_backtest` needs `fees` (object or path), argparse
stderr in the tool error, `take_profit` unit warning, paths relative to the agent's run directory), (this commit)
(review round 2: `fees`/`instruments`/`filter` sent as a JSON string, `~` in paths, `fees` in the tool schema).

**Upstreamable.** No, local-only (follows the leaf package).

**Anchors.** None: every change is in `leaf-package` files (`ir.py`, `ir_backtest.py`, the tool), which carry no hook
anchors (same as `strategy-fidelity`'s tool half).

## `research-truth` — IR backtests report what the code measured

**Purpose.** In session b9aa13a496a4 and its replays 3d53fa72a8e9 / a061ea4c35f2 the agent looped on `ir_backtest`
arguments, under-counted its trials and presented losing variants as recommendations; its real figures were refused by
grounding as "no evidence". The model now writes only the IR and the explanation; code produces the rest, on upstream's
own mechanisms:
B1: the leaf writes `artifacts/metrics.csv` with upstream's column names (`final_value` … `trade_count`, plus
`net_usdt` `fees_usdt` `funding_usdt` `price_pnl_usdt` `max_drawdown_usdt` `days`); the tool copies `run_card.json` and
`metrics.csv` of every run to `<session run dir>/ir_runs/<run dir name>/` and returns `evidence_dir`; grounding records
that copy with upstream's unchanged `_record_backtest_metrics` (`alphakeel_research` joins `_ANALYSIS_TOOLS`; the five
`*_usdt` columns join `_AMOUNT_FIELDS`, because a currency-marked figure such as "-138.19 USDT" is answered only from
money fields).
B2: `src/tools/alphakeel_ir_request.py` (`IrBacktestArgs`, as upstream's `BacktestConfigSchema`): only `ir` is
required; the window defaults to the 180 days before the hold-out cutoff; instruments, step and the row-limit precheck
are derived from the IR and `check()`; fees default to each venue's public base-tier taker with the source label
(`--fees-source`; the leaf keeps "no default fee"); `instruments`/`step_minutes`/`trials`/`trials_evidence` from the
model are ignored and listed in `ignored_args`.
B3: `alphakeel_research/screen.py` labels every run card `insufficient` / `failed_screen` / `passed_screen` from G1 1.1,
1.2, 1.3, 1.16 (protocol thresholds, precheck comparisons); `src/tools/alphakeel_research_guard.py` adds a routing block
(as upstream's `strategy_discovery/guard.py`) when `alphakeel_research` is registered.
B4: scope is the conversation (`session_id`; every turn has a new run dir): `build_registry` injects
`default_session_id` into `AlphakeelResearchTool` (`src/tools/__init__.py`, as the goal tools).
`src/tools/alphakeel_research_ledger.py` keeps the tool's private ledger `<sessions dir>/<sid>/ir_ledger.jsonl` (outside
the write roots; one row per successful run: strategy_id, name, run_card_path, run_card_sha256, evidence_dir, run_dir,
ts); its distinct `strategy_id` are the run's `--trials`, listed in `<turn run dir>/ir_trials_evidence.jsonl`
(`--trials-evidence`). Every run is also linked to one upstream `HypothesisRegistry` hypothesis per conversation (id in
`<sessions dir>/<sid>/ir_hypothesis.json`), which is not a source of counts or of the card. flock on every
read-modify-write. No session id: nothing is registered, no `--trials`, no card (a warning says so).
§5: the loop appends a result card of the conversation after the (grounded) answer, at both answer sites; a failure
only logs a warning. The card reads only the private ledger and re-reads every run card, checking its sha256 (a changed
or missing one keeps a marked row, see round 2 below); one row per backtest, `name (strategy_id[:8])`; header "N versions / M
backtests" over every private ledger row (= `trials.count`); it never prints the hypothesis id. Review round 2: a row whose
run card changed or is missing keeps its line (ledger name, `—` figures, "run card modified or missing"), sorts last,
is always shown and adds ", modified t" to the header; `out` may not be a directory that already holds a run card; the
tool's import in `build_registry` is guarded (a failure logs a warning and skips the session injection).
Card rows (2026-10-17, research session 90b12bd25f7a, where a 288-trade failed run fell off a top-10-by-net card): rows sort
by screen class (passed, failed, insufficient), then net descending; every passed or failed row (1.1/1.2 held) is always
shown; a `价差盈亏 USDT` / `Price PnL USDT` column (`amounts.price_pnl_closed`) shows the gross the fees are weighed against.
Replay fixes (2026-10-14): the tool ran `cli.main` under `contextlib.redirect_stdout`, which is process-wide, so two
conversations at once read each other's output (replay 4cada46ec0b4: `{"output": ""}`, one run in both ledgers). Every
action now runs `python -m alphakeel_research` in a child process (as upstream's backtest tool / `src/core/runner.py`;
timeout `VIBE_TRADING_TOOL_TIMEOUT_SECONDS`; `cwd=None` and `python -P`, so a module the model wrote into its run dir is
never imported by the child, which holds the service credential); planning stays in-process. Tests drive the child
through `agent/tests/fixtures/alphakeel_tool_cli.py`. The routing block declares fee rates `count` and fee, funding
and PnL amounts `observed` single values; the card gains fee and funding columns. `scripts/alphakeel_replay_check.py` is the read-only acceptance check of a replayed session.
The upstream test `test_context_attribution_layers.py` formats `_SYSTEM_PROMPT` with every placeholder and gains the new one.
Review round 1 (2026-10-14): conversation scope and private ledger (above); `fees` must name every derived venue and the
default table is cut to them; evidence copies never overwrite (`-2`, `-3`…) and `out` may not point into `ir_runs/`;
the row estimate counts hourly settlements; `screen` is computed for the `ir backtest` run only (`_backtest`), not the
robustness sub-runs.

**Files.**
- `agent/src/agent/grounding/ledger.py`
- `agent/src/agent/grounding/evidence.py`
- `agent/src/agent/context.py`
- `agent/src/agent/loop.py`
- `agent/src/tools/__init__.py`
- `agent/src/tools/alphakeel_ir_request.py`
- `agent/src/tools/alphakeel_research_guard.py`
- `agent/src/tools/alphakeel_research_ledger.py`
- `agent/src/tools/alphakeel_research_tool.py`
- `agent/alphakeel_research/screen.py`
- `agent/alphakeel_research/ir_backtest.py`
- `agent/alphakeel_research/cli.py`
- `agent/tests/test_alphakeel_research_truth.py`
- `agent/tests/test_alphakeel_upstream_alignment.py`
- `agent/tests/fixtures/alphakeel_tool_cli.py`
- `agent/tests/test_ir_validate_all.py`
- `agent/tests/test_strategy_fidelity.py`
- `agent/tests/test_context_attribution_layers.py`
- `scripts/alphakeel_replay_check.py`

**Commits.** (this commit).

**Upstreamable.** No, local-only (follows the leaf package).

**Anchors.**
- upstream `agent/src/tools/__init__.py`: `session_injected_classes = goal_tool_classes | {` — tools that get default_session_id / event_callback
- hook `agent/src/tools/__init__.py`: `from src.tools.alphakeel_research_tool import AlphakeelResearchTool` — inside try/except: an import failure logs a warning and the tool is not session-injected
- hook `agent/src/tools/__init__.py`: `session_injected_classes = session_injected_classes | {AlphakeelResearchTool}`
- upstream `agent/src/agent/grounding/ledger.py`: `_ANALYSIS_TOOLS = frozenset(`
- hook `agent/src/agent/grounding/ledger.py`: `     "alphakeel_research"}`
- preimage `agent/src/agent/grounding/ledger.py`: `{"backtest", "factor_analysis", "run_shadow_backtest", "quantlib_call"}`
- upstream `agent/src/agent/grounding/evidence.py`: `elif tool_name == "quantlib_call":`
- upstream `agent/src/agent/grounding/evidence.py`: `def _record_backtest_metrics(` — called unchanged for the IR backtest's evidence copy
- hook `agent/src/agent/grounding/evidence.py`: `elif tool_name == "alphakeel_research":`
- hook `agent/src/agent/grounding/evidence.py`: `recorded = self._record_backtest_metrics({"run_dir": evidence_dir}, payload, call_id)`
- hook `agent/src/agent/grounding/evidence.py`: `# research-truth: IR backtest metrics.csv USDT amount columns` — net_usdt, fees_usdt, funding_usdt, price_pnl_usdt, max_drawdown_usdt join _AMOUNT_FIELDS, so a USDT-marked figure can be answered from them
- upstream `agent/src/agent/context.py`: `{strategy_discovery_routing}` — the IR routing placeholder is the next line
- hook `agent/src/agent/context.py`: `{alphakeel_ir_routing}`
- hook `agent/src/agent/context.py`: `from src.tools.alphakeel_research_guard import routing_block as alphakeel_ir_routing_block`
- hook `agent/src/agent/context.py`: `alphakeel_ir_routing=alphakeel_ir_routing,`
- upstream `agent/src/agent/loop.py`: `{"type": "answer", "iter": current_iter},` — the card is appended right before both answer records
- hook `agent/src/agent/loop.py`: `def _ir_result_card(session_id: str, user_message: str) -> str | None:`
- hook `agent/src/agent/loop.py`: `card = _ir_result_card(session_id, user_message)  # research-truth: card at the answer`
- hook `agent/src/agent/loop.py`: `card = _ir_result_card(session_id, user_message)  # research-truth: card at the no-progress answer`
- hook `agent/tests/test_context_attribution_layers.py`: `alphakeel_ir_routing="[test alphakeel routing]",`

## `research-integrity` — research results the code can vouch for

**Purpose.** Audit of in-app session f9941f6d027f (2026-10-16): ten generic `backtest` runs (nine in one run dir) were
invisible to the result card, which said "2 versions / 2 backtests"; the model called a 5-round-trip, Sharpe 0.10 pair
trade "validated" and a long-only trend run a funding carry; the Monte Carlo "permutation test" could not test a
Sharpe (order-invariant, p = 0.543); OKX data was read 13 days into AlphaKeel's hold-out; and real figures of an earlier
turn were redacted because grounding evidence was per turn and an IR run's metrics were recorded under `backtest`.
Fixes: (1) `backtest/engine_screen.py` screens every engine run on G1 1.1/1.2/1.3/1.6 (DSR with the session's trial
count)/1.8 plus code-measured facts (direction, time in market, exposure, data audit); the backtest tool records each
run in `<session>/engine_ledger.jsonl` (trial key = sha256 of the engine-written metrics.csv, so a parameter swept
outside code/config still counts) with a snapshot (`engine_runs/NNNN/`: code, config, metrics, screen, daily returns)
and returns
`research_screen` (with `provisional`); 1.16 comes from the run card or the run's `ohlcv_*.csv` coverage. The result card
gains an engine section (1.6 recomputed at render time with the current trial count and the larger of the stored and
current variance; a render never improves a stored verdict, only worsens it; IR trial counts include engine trials) and
a hold-out warning that makes no network call. (2) One classifier, `data_audit.is_crypto_code` (after
stripping `local:`, venue prefixes, `=X`, MT5/broker suffixes and a ccxt `:SETTLE`: ISO-4217 pairs are forex, not crypto;
then Yahoo CME crypto futures (`BTC=F`, `MBT=F`, `ETH=F`), runner `_detect_market`, perp/swap and dated-future names,
`*USDT`/`*USDC` and non-ISO `XXX+USD` CFD names, BTC/ETH fund tickers such as IBIT/GBTC/bare `BTC`, `BASE-QUOTE` with a
known crypto base or a non-ISO base priced in fiat/stablecoin, unseparated crypto-base pairs, `XBT*`; bare `ETH` is not
crypto), and `crypto_sources()` (static set plus crypto-only registered loaders). A backtest with a crypto
source or code, or a `local` source once the session has read crypto data past the cutoff, whose plain `YYYY-MM-DD`
`end_date` reaches the AlphaKeel hold-out (service `holdout_cutoff_ms`, cached 6 h per session; fallback today − 60 d)
is refused before the engine runs. After a run, any `ohlcv_*.csv` with weekend bars (≥ 20 %) that reaches the hold-out is
recorded as crypto exposure and warned on, whatever its name (a CSV laundered in another session). Exposure rows carry a `crypto` flag (forced True for crypto sources); the card counts
flagged rows, or unflagged legacy rows of crypto sources. Market-data reads are not blocked (quotes stay usable) but are
flagged on the card.
(3) `monte_carlo_test`: sign-flip test for the Sharpe, order permutation only for the drawdown, `(k+1)/(n+1)` p-values,
`method` and `sharpe_basis` fields. (4) `GroundingLedger(prior_ledgers=…, session_copy=…)` carries observed non-quote evidence of
earlier completed turns from their copies in `<session>/grounding/<start ms>_<run dir>.json` (outside the model's write roots; run-dir files are
never read back), without prices, bash rows or engine-run figures (only immutable `ir_runs/` copies); a tool-name ref also names records its calls made under another tool.
(5) Prompt: verdicts come from the card; factor ids only when a zoo tool returned them; Layer 4 wording.

**Files.**
- `agent/backtest/engine_screen.py`
- `agent/backtest/validation.py`
- `agent/src/tools/backtest_tool.py`
- `agent/src/tools/alphakeel_research_ledger.py`
- `agent/src/agent/grounding/ledger.py`
- `agent/src/agent/grounding/policies.py`
- `agent/src/agent/loop.py`
- `agent/src/agent/context.py`
- `frontend/src/i18n/locales/ar.json`
- `frontend/src/i18n/locales/de.json`
- `frontend/src/i18n/locales/en.json`
- `frontend/src/i18n/locales/es.json`
- `frontend/src/i18n/locales/id.json`
- `frontend/src/i18n/locales/ja.json`
- `frontend/src/i18n/locales/ko.json`
- `frontend/src/i18n/locales/pt-BR.json`
- `frontend/src/i18n/locales/zh-CN.json`
- `agent/tests/test_engine_screen.py`
- `agent/tests/test_grounding_prior_turns.py`
- `agent/tests/test_validation.py`
- `agent/tests/test_strategy_fidelity.py`
- `agent/tests/test_alphakeel_research_truth.py`
- `agent/tests/test_backtest_summary.py`

**Commits.** (this commit). `test_backtest_summary.py`: its fixture gets a pre-hold-out `end_date` and the golden
envelope pops `research_screen`.

**Upstreamable.** Monte Carlo, cross-turn grounding and the tool-name ref fix are upstream bugs (upstream main 596fcf3d
still has them); the engine screen, ledger, hold-out guard and card are local-only (AlphaKeel protocol).

**Anchors.**
- upstream `agent/backtest/validation.py`: `def monte_carlo_test(`
- hook `agent/backtest/validation.py`: `"method": {"sharpe": "sign_flip", "max_dd": "order_permutation"}`
- preimage `agent/backtest/validation.py`: `"p_value_sharpe": round(sharpe_count / n_simulations, 4),`
- upstream `agent/src/tools/backtest_tool.py`: `def run_backtest(run_dir: str) -> str:`
- hook `agent/src/tools/backtest_tool.py`: `refusal = _holdout_refusal(`
- hook `agent/src/tools/backtest_tool.py`: `envelope["research_screen"] = _research_screen(`
- upstream `agent/src/agent/grounding/ledger.py`: `GROUNDING_ARTIFACT = "grounding_evidence.json"`
- hook `agent/src/agent/grounding/ledger.py`: `prior_ledgers: Sequence[Path] = (),`
- upstream `agent/src/agent/grounding/policies.py`: `def _analysis_entries(self, symbol: str | None) -> list[dict[str, Any]]:`
- hook `agent/src/agent/grounding/policies.py`: `calls = self._calls_of_tool(key)`
- preimage `agent/src/agent/grounding/policies.py`: `if key in (record.call_id, record.tool)`
- upstream `agent/src/agent/loop.py`: `self._grounding = GroundingLedger(`
- hook `agent/src/agent/loop.py`: `prior_ledgers=_prior_ledgers(session_id, grounding_copy),`
- hook `agent/src/agent/context.py`: `- Research verdicts come from the system result card appended after your answer`
- preimage `agent/src/agent/context.py`: `**Layer 4 — Monte Carlo Permutation Test**`
- upstream `frontend/src/i18n/locales/en.json`: `"pValueSharpe"`
- hook `frontend/src/i18n/locales/en.json`: `Sign-flip p-value (Sharpe)`

## `ir-execution` — Strategy IR `execution` section (one maker entry leg)

**Purpose.** AlphaKeel added an optional top-level `execution` to Strategy IR v1 (README §1, §2, §3 rule 5b, §5b): one
post-only maker entry leg (`{"entry": {"short": {"maker": {"model": "M1", "timeout_s": 600}}}}`), taker exit. The leaf
package now parses and validates it (shape errors `ir.shape`, range errors `ir.invalid` with the vector paths),
writes the defaults into the definition when a maker leg exists and drops `execution` otherwise (a taker-only IR keeps
its `strategy_id`), and exposes `ir.maker_leg(doc)`. Fee tables: a maker IR's default table adds
`maker.perp` 0.0002 per venue, `scale_fees` scales the nested maker rates, and `view_of(card, ir_doc)` sends a venue's
`maker.perp` only when the IR has a maker leg and the fee row carries it (a taker-only IR never sends maker fees, even
when the request table has them). The local simulator does not model maker fills (taker fill at the touch, a conservative
cost bound): the run card's `approximations` gets a `maker_not_modelled` entry. The maker request checks are one function,
`ir.maker_request_problem(ir_doc, fees, step_minutes)`, used by `plan()` (ValueError) and by the CLI `ir backtest` entry
(`ir_backtest.backtest`, ApiError `request.invalid`): `step_minutes <= 2` (the CLI default is 2: pass
`--step-minutes >= 5`), a fee row without `maker.perp`, and a maker leg on a spot leg (the long leg of `spot_perp`; VT
sends no spot maker fees). Taker-only IRs, fee tables and run cards are byte-identical to before. `models.py` is
regenerated from the new `openapi.json` by `gen_models.sh` (two enum values, two description changes). Protocol 2.6: the
trial idempotency key (`trial_key_salt`) includes the protocol version, so every key changes with 2.6; this is intended,
and the AlphaKeel ledger dedupes by `request_sha`, so trial counts do not change. The contract files are AlphaKeel's
export of 2026-10-17 (re-exported with `ir.shape` error vectors). All code lives in
`leaf-package` and `research-truth` files, so the anchors only pin the new entry points.

**Files.**
- `agent/alphakeel_research/contract/PIN`
- `agent/alphakeel_research/contract/openapi.json`
- `agent/alphakeel_research/contract/strategy-ir/README.md`
- `agent/alphakeel_research/contract/strategy-ir/schema.json`
- `agent/alphakeel_research/contract/strategy-ir/vectors/canonical.json`
- `agent/alphakeel_research/contract/validation/protocol.json`
- `agent/alphakeel_research/ir.py`
- `agent/alphakeel_research/ir_backtest.py`
- `agent/alphakeel_research/ir_crosscheck.py`
- `agent/alphakeel_research/models.py`
- `agent/alphakeel_research/robustness.py`
- `agent/src/tools/alphakeel_ir_request.py`
- `agent/src/tools/alphakeel_research_ledger.py`
- `agent/src/tools/alphakeel_research_tool.py`
- `agent/tests/test_alphakeel_ir.py`
- `agent/tests/test_alphakeel_ir_backtest.py`
- `agent/tests/test_ir_validate_all.py`

**Commits.** (this commit)

**Upstreamable.** No, local-only (follows the leaf package).

**Anchors.**
- hook `agent/alphakeel_research/ir.py`: `def maker_leg(doc: Any)`
- hook `agent/alphakeel_research/ir_backtest.py`: `def approximations_of(`
- hook `agent/src/tools/alphakeel_ir_request.py`: `DEFAULT_PUBLIC_MAKER =`

## `research-integrity-r2` — a spot short is refused; an earlier backtest's code is readable; a strategy change is visible

**Purpose.** A replay showed two defects. (1) `CryptoEngine` let a strategy short a spot code (OKX `BTC-USDT`) and charged
an assumed fixed funding rate, so a pair or long/short result had a leg that cannot be traded as modelled. Now a negative
target weight on a code whose funding model is `assumed_fixed_rate` (no settlement data) is refused with
`spot_short_refusal(symbol)`: in `CryptoEngine.can_execute` and, because the rebalance planner logs and skips an order whose
`can_execute` raises, once before the first bar in `CryptoEngine._execute_bars` (after `_funding.prepare`). A short on a perp
code with settlement data (ccxt `BASE-USDT-PERP`, `alphakeel_b2` `venue:perp:SYMBOL`) runs as before; the composite engine's
composite engine refuses the same way: `CompositeEngine._execute_bars` runs the same loop over its crypto codes right after `_funding.prepare` (its funding models live on the composite; the crypto sub-engine is not changed). The two skills say spot codes are long-only and
the pair-trading crypto example uses `ccxt` perps. (2) In a later turn the agent could not read the previous turn's code
(`read_file` is scoped to the current run dir), re-implemented it, and the same parameters gave -3.80 % instead of -13.67 %.
`read_file` gains a `trials/NNNN/{code/signal_engine.py,config.json,artifacts/metrics.csv,screen.json}` namespace bound to
`engine_snapshot_dir(current session, NNNN)` (never falls through to run_dir; errors `no_session`, `not_allowed`,
`not_found` with the existing trial numbers); the engine ledger row gains `code_sha256` / `config_sha256`; `record_engine_run`
returns `trial_files`; the engine card gains a `code` column (`= #k` for the code of an earlier row, `新`/`new`, `—` for
legacy rows); one prompt bullet tells the agent to start from `trials/NNNN/` files or to say it re-implemented.
(3) In session f9941f6d027f the agent silently moved from an IR cross-venue funding carry to long-only spot trend to an
ETH/BTC pair and kept calling it one research; the rules against it existed only in the prompt. Now the code derives a
research contract from what each backtest measured (`agent/backtest/research_contract.py`, pure functions: engine,
direction, instrument class spot/perp/cash/mixed, base assets, bar interval) and keeps it per session in `research_contract.json`
(`alphakeel_research_ledger.update_contract`, atomic under the ledger's flock). A run that differs in engine, direction,
instrument class, assets (any difference of the two non-empty sets) or bar interval (`config.json` `interval`, default `1D`; `unknown` or a contract file from before the field is not a change) is never refused; it appends a history entry (declared when `config.json`
`supersedes_contract`, or `args.supersedes_contract` of `ir_backtest`, holds a reason of at least 10 characters), the engine
tool result carries `contract_drift` with a message, the ledger rows carry `contract_change`, and the result card gets a
"research contract changes" section plus a mark on the verdict of an undeclared row. `supersedes_contract` is read by the
ledger only and is never sent to the AlphaKeel CLI nor listed in `ignored_args`; the screen verdict logic, hold-out logic and
the IR evaluator are unchanged. One more prompt bullet tells the agent to say so and declare the change.

(4) In the same session the model attached factor-library names to hand-written rules ("GTJA-191 Alpha 010" computed as
downside/total volatility, not the zoo's `RANK(MAX(...))`; "Kakushadze Alpha 017/053" while `alpha_zoo(zoo="kakushadze101")`
returned 0 items) and declared a figure `cited` ("彭博社") although no retrieval tool had run. Now `backtest/zoo_signal.py`
`zoo_factor(alpha_id, data_map)` is the only way a signal engine uses a library alpha (it builds the panel with the runner's
`_build_price_panel` and calls `Registry.compute`; the runner's import check already allows it, a test pins that).
`grounding/factor_names.py` (pure) finds factor names in text (literal registry ids, "GTJA-191 Alpha 010", "Kakushadze Alpha
17", "Alpha#53") and the engine screen records `facts.zoo_factors_used` (string-literal first arguments of `zoo_factor(...)`)
and `facts.zoo_names_in_code_not_used` (names the code mentions, comments included, without such a call); the engine card says
which rows are hand-written. The grounding ledger tracks the library ids factor tools returned (or a backtest ran through
`zoo_factor`), the number of successful retrieval calls and the numeric literals of the retrieved texts (`load_skill` is no
retrieval; first 5000 numbers), persists them (`factor_ids`, `retrieval_calls`, `retrieved_numbers`) and carries them
across turns; the final answer gets `factor_unknown` (id not in the library) / `factor_unsourced` (id never returned) issues,
which are not redactable (a name cannot be cut like a figure; the run corrects or fails closed on `safe_fallback`), and a figure
declared `cited` with no retrieval this session gets `citation_not_retrieved`, and one whose value (as is, /100 or x100) is in no
retrieved text gets `citation_value_not_retrieved`. A bare number is a factor id only behind an id marker (`#`, `No.`, `第`,
`_`, `-`, "Alpha") and never before a count word (个, bps, regions ...); a prefix of word-named ids (`academic_`) is not open. One more prompt bullet states both rules.

(5) A session compared eleven near-identical versions and named "the best" with nothing testing whether it is distinguishable
from luck, and item 1.2 (minimum trades) counted closed legs, so a two-leg pair trade counted twice. Now (a) the engine
screen counts round trips for 1.2: `_round_trips` rebuilds each symbol's position lifecycle from `artifacts/fills.jsonl`
(first `open` to the fill that brings it flat, `close`; `increase` / `reduce` stay inside) and counts distinct
(open, close) timestamp pairs (legs opened and closed on the same bars are one bet, so 1.2 is never inflated), falling back to `metrics.csv` `trade_count` when the file is missing or unreadable; the facts
gain `closed_legs`, `round_trips`, `round_trips_source`, and the card column is "round trips"; no verdict rule changes except
this observed value. (b) `backtest/multiple_testing.py` (numpy only, seeded) holds the stationary bootstrap, Hansen (2005)
SPA against zero, CSCV PBO, a bootstrap Sharpe interval and the 75/25 split Sharpe. (c) The engine screen also returns
`daily_dates`; `record_engine_run` writes `engine_runs/NNNN/returns_dates.json` (`returns_dates_sha256` on the row,
`verified_engine_dates`); rows without dates are skipped and counted. (d) At render time `_multiple_testing_lines` takes the
latest row of each distinct trial key (at most the 20 most recent), aligns them on their common dates and prints the SPA
line (3 or more versions and 60 or more common days), the PBO line (8 or more), the latest row's split and interval line, and
three separate notes (legacy rows without dates; rows whose return snapshots do not match the record; versions under 60 days,
which are left out of the date intersection so one short run cannot hide the SPA and PBO lines); reference information only, seeds fixed so the card is stable. One more prompt bullet tells
the agent to quote the SPA line and to say "cannot be told apart from luck" when p is 0.05 or more.

(6) Review fixes. Annualisation uses `research_contract._is_crypto` (venue-prefixed codes are crypto, 365). The tool result
of `backtest` carries `contract_drift` and `trial_files` (the `_research_screen` whitelist dropped them). A free-text
`supersedes_contract` reason is stored as one line (whitespace collapsed, `| * # ` < >` replaced by their full-width forms,
200 characters at most) and re-sanitised on render, so it cannot add rows to the system-generated card. A run whose screen is
unavailable or has no codes does not touch the contract. `read_file trials/NNNN/...` requires the seq to have a row in the
session's engine ledger and compares `code/signal_engine.py` and `config.json` with the row's `code_sha256` / `config_sha256`
(`snapshot_mismatch` on a difference, `not_found` without a row). The funding model is not part of the contract and is not
measured.

**Files.**
- `agent/backtest/engines/crypto.py`
- `agent/backtest/engines/composite.py`
- `agent/backtest/research_contract.py`
- `agent/src/tools/read_file_tool.py`
- `agent/src/tools/alphakeel_research_ledger.py`
- `agent/src/tools/alphakeel_research_tool.py`
- `agent/src/tools/alphakeel_ir_request.py`
- `agent/src/agent/context.py`
- `agent/src/skills/strategy-generate/SKILL.md`
- `agent/src/skills/pair-trading/SKILL.md`
- `agent/tests/test_crypto_engine.py`
- `agent/tests/test_read_file_trials.py`
- `agent/tests/test_engine_screen.py`
- `agent/tests/test_research_contract.py`
- `agent/tests/test_composite_engine_fallback.py`
- `agent/tests/test_alphakeel_research_truth.py`
- `agent/backtest/zoo_signal.py`
- `agent/backtest/engine_screen.py`
- `agent/src/agent/grounding/factor_names.py`
- `agent/src/agent/grounding/ledger.py`
- `agent/src/agent/grounding/policies.py`
- `agent/src/agent/grounding/release.py`
- `agent/tests/test_factor_names.py`
- `agent/tests/test_grounding_factor_citation.py`
- `agent/tests/test_backtest_runner_security.py`
- `agent/tests/test_grounding_figures_contract.py`
- `agent/tests/test_grounding_language_parity.py`
- `agent/tests/test_grounding_percentage_points.py`
- `agent/tests/test_grounding_release.py`
- `agent/tests/test_grounding_role_hardening.py`
- `agent/tests/test_grounding_shape_hardening.py`
- `agent/backtest/multiple_testing.py`
- `agent/tests/test_multiple_testing.py`

**Commits.** (this commit)

**Upstreamable.** The spot-short refusals and the `trials/` namespace are generic; the ledger lineage fields, card column,
research contract, factor-name gate, `zoo_factor` helper and prompt bullets are local-only (follow the engine ledger).

**Anchors.**
- upstream `agent/src/tools/read_file_tool.py`: `if file_path.startswith("skills/"):`
- hook `agent/src/tools/read_file_tool.py`: `if file_path.startswith("trials/"):`
- upstream `agent/backtest/engines/crypto.py`: `def can_execute(self, symbol: str, direction: int, bar: pd.Series) -> bool:`
- hook `agent/backtest/engines/crypto.py`: `raise ValueError(spot_short_refusal(symbol))`
- hook `agent/src/agent/context.py`: `- To change an earlier generic backtest, start from its files`
- upstream `agent/backtest/engines/composite.py`: `self._funding.prepare(data_map, self._crypto_codes(codes))`
- hook `agent/backtest/engines/composite.py`: `raise ValueError(spot_short_refusal(symbol))`
- hook `agent/src/agent/context.py`: `- The session has a research contract measured from its first backtest`
- hook `agent/src/tools/alphakeel_research_tool.py`: `ledger.record(session_id, run_dir, result, rel, supersedes=supersedes)`
- hook `agent/src/skills/strategy-generate/SKILL.md`: `Crypto spot codes are long-only in backtests`
- hook `agent/src/skills/pair-trading/SKILL.md`: `"codes": ["BTC-USDT-PERP", "ETH-USDT-PERP"]`
- hook `agent/src/agent/context.py`: `- A factor-library alpha is used only through `from backtest.zoo_signal import zoo_factor``
- upstream `agent/src/agent/grounding/release.py`: `_REDACTABLE_CODES = frozenset(`
- hook `agent/src/agent/grounding/release.py`: `_FACTOR_NAME_CODES = frozenset(`
- hook `agent/src/agent/grounding/release.py`: `"citation_not_retrieved":`
- upstream `agent/src/agent/grounding/policies.py`: `def _check_cited(`
- hook `agent/src/agent/grounding/policies.py`: `"citation_not_retrieved",`
- hook `agent/src/agent/grounding/policies.py`: `def _validate_factor_names(self, content: str)`
- upstream `agent/src/agent/grounding/ledger.py`: `def persist(self) -> None:`
- hook `agent/src/agent/grounding/ledger.py`: `issues.extend(self._validate_factor_names(content))`
- hook `agent/src/agent/grounding/ledger.py`: `"retrieval_calls": self._retrieval_calls,`
- hook `agent/backtest/engine_screen.py`: `facts["zoo_factors_used"], facts["zoo_names_in_code_not_used"] = _zoo_names(run_dir)`
- hook `agent/src/tools/alphakeel_research_ledger.py`: `"zoo_unused":`
- upstream `agent/backtest/runner.py`: `def _build_price_panel(`
- upstream `agent/src/factors/registry.py`: `def get_default_registry() -> Registry:`
- hook `agent/src/agent/context.py`: `- When you compare versions or name the best one`
- hook `agent/src/tools/alphakeel_research_ledger.py`: `def _multiple_testing_lines(`

## `research-integrity-r2-guards` — recalled project memories are scoped by asset; verdict words need a passed screen; no order-placing code

**Purpose.** Three defects of session f9941f6d027f, each a new guard in code rather than a prompt line.
(1) Auto-recall injected an unrelated QQQ research memory into a crypto funding-arb conversation, and the model spoke of it as
something the user had done there. `src/memory/scope.py` (pure) reads the upper-case ticker-like tokens of a text
(`asset_tokens`; `BTC-USDT` gives `BTC-USDT` and `BTC`, a concatenated pair such as `BTCUSDT` gives itself and `BTC`, aliases
such as bitcoin / 比特币 give `BTC`, quote currencies, indicator names and report abbreviations are stop words); `ContextBuilder.build_messages` fetches 10 recalls, filters, and keeps the first 3 in scope: a recalled `project` memory is kept only when its own tokens (title, keywords,
first 2000 characters of the body) meet the message's, or it has none; other memory types are never filtered. The wrapper is
`<recalled-memories note="background from earlier sessions; not evidence for this session and not something the user did here">`.
(2) The model called a 5-round-trip, Sharpe 0.10 result "已验证" / "风险真正得到控制" / "稳健正收益" while no trial passed the screen.
`grounding/verdict_words.py` (pure) finds verdict phrases (zh list, en word-bounded) that are not negated (zh: 不/没/未/无法/尚未/
没有/并未/不能/不可 within 3 characters before, cut at a clause end; en: not/never/no/cannot/n't before it) and are
not inside the figures block. The system result card is appended after grounding, so a draft never contains it and no title
the model writes (not "**研究结果汇总**" either) skips anything. "无风险利率" / "risk-free rate" and 不错/不仅/不但/不少/不断 are not claims
or negations; en negation reaches back 60 characters across commas, cut at . ; : and newline. The grounding
ledger's `_validate_verdict_words` turns each one into a `verdict_unsupported` issue (reason `no_passed_trial`) unless
`alphakeel_research_ledger.session_has_passed_trial(current_llm_session_id())` (an IR row whose verified run card, or an engine
row whose current screen, is `passed_screen`). The issue is not redactable (`release._VERDICT_CODES`): the draft is corrected
without the verdict or the run fails closed on `safe_fallback` (zh/en text). The numeric-claim logic, identity checks, IR code
and engines are unchanged.
(3) The agent wrote exchange order code ("改 dry_run=False 即可实盘"), although this repository never places orders.
`tools/order_code_guard.py` holds the case-insensitive pattern list (`*create*order`, `order_market_*` / `order_limit_*`,
`*place_?order`, `*submit_?order`, `*private_?post*order`, `/api|fapi|dapi|sapi|papi/vN/**/order`, the OKX / Bybit order
endpoints, `trading_place_order`); `write_file` (content) and `edit_file` (the whole replaced file content, not only new_text)
refuse with `error_code: order_code_refused` before touching the file. Scope: best effort for the file-writing tools only;
the bash tool is NOT covered by this guard, and string concatenation such as `"/api/v3/" + "order"` is not detected.

**Files.**
- `agent/src/memory/scope.py`
- `agent/src/agent/context.py`
- `agent/src/agent/grounding/verdict_words.py`
- `agent/src/agent/grounding/ledger.py`
- `agent/src/agent/grounding/policies.py`
- `agent/src/agent/grounding/release.py`
- `agent/src/tools/alphakeel_research_ledger.py`
- `agent/src/tools/order_code_guard.py`
- `agent/src/tools/write_file_tool.py`
- `agent/src/tools/edit_file_tool.py`
- `agent/tests/test_memory_scope.py`
- `agent/tests/test_grounding_verdict_words.py`
- `agent/tests/test_order_code_guard.py`
- `agent/tests/test_system_prompt_cache_stability.py`
- `agent/tests/test_agent_grounding.py`
- `agent/tests/test_agent_output_discipline.py`

**Pre-existing tests changed (fix round A).** Fixtures that declare a `cited` figure now ingest a retrieval result containing the
cited number (a `web_search` result text), because a retrieval without the value no longer backs the figure and `load_skill` no
longer counts as a retrieval; no assertion was changed.

**Pre-existing test changed.** `test_system_prompt_cache_stability.py` (`test_state_envelope_sits_before_recalled_memories`): the
envelope opens `<recalled-memories note=...>` now, so it asserts the prefix `<recalled-memories`.

**Commits.** (this commit)

**Upstreamable.** The recall scope and the order-code refusal are generic in principle; the verdict-word gate follows the local
research ledger (local-only).

**Anchors.**
- upstream `agent/src/agent/context.py`: `# Auto-recall: inject relevant memories into user message`
- preimage `agent/src/agent/context.py`: `f"<recalled-memories>\n{recall_block}`
- hook `agent/src/agent/context.py`: `<recalled-memories note=`
- upstream `agent/src/agent/grounding/ledger.py`: `def persist(self) -> None:`
- hook `agent/src/agent/grounding/ledger.py`: `self._validate_verdict_words(content)`
- upstream `agent/src/agent/grounding/policies.py`: `def _check_cited(`
- hook `agent/src/agent/grounding/policies.py`: `def _validate_verdict_words(`
- upstream `agent/src/agent/grounding/release.py`: `_REDACTABLE_CODES = frozenset(`
- hook `agent/src/agent/grounding/release.py`: `"no_passed_trial":`
- upstream `agent/src/tools/write_file_tool.py`: `resolved.parent.mkdir(parents=True, exist_ok=True)`
- hook `agent/src/tools/write_file_tool.py`: `order_code_refusal(`
- upstream `agent/src/tools/edit_file_tool.py`: `new_content = content.replace(old_text, new_text, 1)`
- hook `agent/src/tools/edit_file_tool.py`: `order_code_refusal(`
- hook `agent/src/tools/alphakeel_research_ledger.py`: `def session_has_passed_trial(`
