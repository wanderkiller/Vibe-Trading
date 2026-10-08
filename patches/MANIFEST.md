# Local patch layer over upstream HKUDS/Vibe-Trading

This fork (`wanderkiller/Vibe-Trading`, branch `feat/alphakeel-ir`) is upstream
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
| `frontend-mobile` | Layout, AgentAvatar, Agent page, index.html, 9 locales | upstreamable (mobile drawer PR), not submitted |
| `local-deploy` | none (new files only; derived from `Dockerfile`) | local-only |
| `leaf-package` | none (new files only) | local-only |
| `loader-registry-hooks` | registry, loader_health, metrics tables | local-only (follows the leaf package) |
| `crypto-funding` | engines/_market_hooks, crypto, composite; ccxt_loader | upstreamable as a bug-fix PR (see below) |
| `metrics-conventions` | metrics, validation | partly upstreamed already; rest upstreamable |
| `run-card-data-audit` | run_card | local-only as written; a generic provenance hook is upstreamable |
| `packaging-docs` | pyproject, SKILL.md, READMEs, .gitignore, .env.example | local-only |

---

## `frontend-mobile` — mobile layout and i18n

**Purpose.** Makes the web UI usable on a phone: below the `md` breakpoint the sidebar becomes a slide-in drawer (its
own copy of brand, navigation, sessions and footer, opened from a hamburger in a mobile-only header, closed by Escape,
backdrop, navigation or resizing to desktop), the app shell uses `h-dvh` and the page body never scrolls, the chat list
uses `overscroll-contain`, and the assistant avatar is hidden on mobile (it indented assistant text by 44 px). Every
locale gets `layout.close` and `layout.menu`.

**Files.**
- `frontend/index.html`
- `frontend/src/components/chat/AgentAvatar.tsx`
- `frontend/src/components/layout/Layout.tsx`
- `frontend/src/components/layout/__tests__/Layout.test.tsx`
- `frontend/src/pages/Agent.tsx`
- `frontend/src/i18n/locales/ar.json`
- `frontend/src/i18n/locales/de.json`
- `frontend/src/i18n/locales/en.json`
- `frontend/src/i18n/locales/es.json`
- `frontend/src/i18n/locales/id.json`
- `frontend/src/i18n/locales/ja.json`
- `frontend/src/i18n/locales/ko.json`
- `frontend/src/i18n/locales/pt-BR.json`
- `frontend/src/i18n/locales/zh-CN.json`

**Commits.** `6335f98f` `42e3aec7` `220d3b9b` `6fbb6d15` `8a1c8de6` `58fcfcb9` `2d02e96c`, plus merge `98e8248e`
(the `id.json` labels exist only in that merge's resolution, because `id` arrived with 0.1.16).

**Upstreamable.** Yes. A PR would contain the Layout drawer (with the shared `renderSessionsList`), the `h-dvh`
body, `overscroll-contain`, the AgentAvatar change, the two labels in every locale and the Layout tests. Not submitted.

**Anchors.**
- upstream `frontend/index.html`: `<div id="root"></div>`
- hook `frontend/index.html`: `<body class="h-dvh overflow-hidden`
- preimage `frontend/index.html`: `<body class="min-h-screen bg-background text-foreground antialiased">`
- upstream `frontend/src/components/chat/AgentAvatar.tsx`: `export function AgentAvatar() {`
- hook `frontend/src/components/chat/AgentAvatar.tsx`: `hidden md:block h-8 w-8`
- preimage `frontend/src/components/chat/AgentAvatar.tsx`: `<div className="h-8 w-8 shrink-0 mt-0.5" aria-hidden="true">`
- upstream `frontend/src/components/layout/Layout.tsx`: `export function Layout() {`
- upstream `frontend/src/components/layout/Layout.tsx`: `const [collapsed, setCollapsed] = useState(() => safeGet("qa-sidebar") === "collapsed");`
- upstream `frontend/src/components/layout/Layout.tsx`: `const loadSessions = () => {`
- hook `frontend/src/components/layout/Layout.tsx`: `const MOBILE_SIDEBAR_TRANSITION_MS = 200;`
- hook `frontend/src/components/layout/Layout.tsx`: `const renderSessionsList = (onNavigate?: () => void) => (`
- upstream `frontend/src/pages/Agent.tsx`: `ref={listRef}`
- hook `frontend/src/pages/Agent.tsx`: `chat-scroll-container flex-1 overflow-auto overscroll-contain`
- preimage `frontend/src/pages/Agent.tsx`: `className="chat-scroll-container flex-1 overflow-auto p-6 relative"`
- upstream `frontend/src/i18n/locales/ar.json`: `"skipToMain":` — layout.* key block the drawer labels sit in
- hook `frontend/src/i18n/locales/ar.json`: `"menu":` — layout.menu / layout.close labels for the drawer; add them to every new locale
- upstream `frontend/src/i18n/locales/de.json`: `"skipToMain":` — layout.* key block the drawer labels sit in
- hook `frontend/src/i18n/locales/de.json`: `"menu":` — layout.menu / layout.close labels for the drawer; add them to every new locale
- upstream `frontend/src/i18n/locales/en.json`: `"skipToMain":` — layout.* key block the drawer labels sit in
- hook `frontend/src/i18n/locales/en.json`: `"menu":` — layout.menu / layout.close labels for the drawer; add them to every new locale
- upstream `frontend/src/i18n/locales/es.json`: `"skipToMain":` — layout.* key block the drawer labels sit in
- hook `frontend/src/i18n/locales/es.json`: `"menu":` — layout.menu / layout.close labels for the drawer; add them to every new locale
- upstream `frontend/src/i18n/locales/id.json`: `"skipToMain":` — layout.* key block the drawer labels sit in
- hook `frontend/src/i18n/locales/id.json`: `"menu":` — layout.menu / layout.close labels for the drawer; add them to every new locale
- upstream `frontend/src/i18n/locales/ja.json`: `"skipToMain":` — layout.* key block the drawer labels sit in
- hook `frontend/src/i18n/locales/ja.json`: `"menu":` — layout.menu / layout.close labels for the drawer; add them to every new locale
- upstream `frontend/src/i18n/locales/ko.json`: `"skipToMain":` — layout.* key block the drawer labels sit in
- hook `frontend/src/i18n/locales/ko.json`: `"menu":` — layout.menu / layout.close labels for the drawer; add them to every new locale
- upstream `frontend/src/i18n/locales/pt-BR.json`: `"skipToMain":` — layout.* key block the drawer labels sit in
- hook `frontend/src/i18n/locales/pt-BR.json`: `"menu":` — layout.menu / layout.close labels for the drawer; add them to every new locale
- upstream `frontend/src/i18n/locales/zh-CN.json`: `"skipToMain":` — layout.* key block the drawer labels sit in
- hook `frontend/src/i18n/locales/zh-CN.json`: `"menu":` — layout.menu / layout.close labels for the drawer; add them to every new locale

## `local-deploy` — local deployment overlay

**Purpose.** The files the production Docker deployment on this host is built from: `Dockerfile.local` is the upstream
`Dockerfile` plus one block that installs the hash-pinned Anthropic/DeepSeek adapters from
`requirements-local-providers-lock.txt` (`--require-hashes --no-deps`, then `pip check`);
`docker-compose.override.yml` selects it, host networking and a 127.0.0.1:8899 listener; `DEPLOYMENT.local.md`
documents the build, backup and upgrade rules.

**Files.**
- `Dockerfile.local`
- `docker-compose.override.yml`
- `requirements-local-providers-lock.txt`
- `DEPLOYMENT.local.md`

**Commits.** `8cfc14f0` (Dockerfile.local, compose override, DEPLOYMENT.local.md), `e0245ca9` (AlphaKeel paragraph in
DEPLOYMENT.local.md), merge `98e8248e` (`requirements-local-providers-lock.txt` exists only in that merge), and
`7d8b0772` (T8, pointer to `patches/`).

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
- upstream `requirements-lock.txt`: `langchain-core==1.6.1 \` — the overlay's langchain-core 1.6.3 pin replaces exactly this base pin; a change means regenerating the overlay
- hook `requirements-local-providers-lock.txt`: `langchain-anthropic==1.7.2`

## `leaf-package` — AlphaKeel research client

**Purpose.** Everything AlphaKeel-specific that lives in new files: the `alphakeel_research` package (contract pin and
vectors, research-service client and CLI, dataset packs and pack reader, simulator, policy sandbox host, hand-off
writer, Strategy IR mirror and the long-history IR backtest), the `alphakeel_pack` and `alphakeel_b2` loaders, the
AlphaKeel-convention statistics (`backtest/alphakeel_metrics.py`), the `alphakeel_research` agent tool, and their
tests and fixtures. It only *uses* upstream code through the anchors below; the edits to upstream files that make it
reachable are the next groups.

**Files.**
- `agent/alphakeel_research/`
- `agent/backtest/loaders/alphakeel_pack_loader.py`
- `agent/backtest/loaders/alphakeel_b2_loader.py`
- `agent/backtest/alphakeel_metrics.py`
- `agent/src/tools/alphakeel_research_tool.py`
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
- `agent/tests/test_alphakeel_protocol.py`
- `agent/tests/test_alphakeel_registration.py`

**Commits.** `e0245ca9` `220330b0` `83ea7c2a` `5370ab12` `e0141135` `b703f994` `22e33c58` `83af5312` `41f8c94b`
(alphakeel_metrics) `9257a920` `a379d8d2` `a232931b` `1e17c469` `bc7d3464` `0d9afe7d` `dbcea0e6` (T9b contract re-export:
`strategy-ir/README.md` and its `PIN` line), `2c8d1143` (dataset-pack market objects read per venue *and* market,
`instrument_meta` point in time by snapshot day; pairs with AlphaKeel `fix/harden-pack`).

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

**Commits.** `e0245ca9` `1f83ef6f` `83ea7c2a`.

**Upstreamable.** No; it follows the leaf package. (Only `alphakeel_pack` needs the health exclusion: `alphakeel_b2`
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

## `crypto-funding` — crypto engine funding, fee and valuation semantics

**Purpose.** Brings `CryptoEngine` (and the crypto leg of `CompositeEngine`) to AlphaKeel's accounting. Funding is
charged at the real 00/08/16 UTC settlement instants in `(previous bar open, this bar open]`, at the bar open before
that bar's fills (`before_rebalance_bar`), valued at the open/mark-open price; a position opened at the instant is not
charged, one closed at it is. A perpetual without settlement data (`funding_rate` + `funding_settlement_time` columns)
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

**Commits.** `622ea894` (engines, ccxt loader, funding_settlements, tests), `41f8c94b` (composite test expectation).

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
- upstream `agent/backtest/loaders/ccxt_loader.py`: `funding = cls._fetch_funding_history(exchange, symbol, since_ms, end_ms)`
- upstream `agent/backtest/loaders/ccxt_loader.py`: `if bracket_artifact is not None:`
- hook `agent/backtest/loaders/ccxt_loader.py`: `result = attach_settlements(result, events)`
- preimage `agent/backtest/loaders/ccxt_loader.py`: `_FUNDING_HOURS = {0, 8, 16}`

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

**Commits.** `41f8c94b`.

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
- preimage `agent/backtest/metrics.py`: `downside_std = float(downside.std()) if len(downside) > 1 else 1e-10`
- preimage `agent/backtest/metrics.py`: `"sharpe": 0, "calmar": 0, "sortino": 0,`
- upstream `agent/backtest/validation.py`: `def monte_carlo_test(`
- upstream `agent/backtest/validation.py`: `def bootstrap_sharpe_ci(`
- upstream `agent/backtest/validation.py`: `def walk_forward_analysis(`
- upstream `agent/backtest/validation.py`: `def _path_metrics(`
- hook `agent/backtest/validation.py`: `def _sharpe(returns: np.ndarray, bars_per_year: int = 252) -> float | None:`
- hook `agent/backtest/validation.py`: `"sharpe_undefined_windows"`
- preimage `agent/backtest/validation.py`: `def _sharpe(returns: np.ndarray, bars_per_year: int = 252) -> float:`
- preimage `agent/backtest/validation.py`: `sharpe = float(returns.mean() / (std + 1e-10) * np.sqrt(bars_per_year))`

## `run-card-data-audit` — data audit in the run card

**Purpose.** The run card records a `data_audit` block: which sources are auditable (frozen, digest-verified AlphaKeel
packs, with dataset versions per read) and which are not (live ccxt/okx/binance pulls), adds a warning for
non-auditable data and renders a "Data Audit" section. Loaders record provenance in-process
(`data_audit.record_provenance`).

**Files.**
- `agent/backtest/run_card.py`
- `agent/backtest/data_audit.py`

**Commits.** `83ea7c2a` `5370ab12`.

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
- preimage `agent/backtest/run_card.py`: `"warnings": list(warnings or []),`

## `packaging-docs` — packaging and documentation

**Purpose.** Ships the leaf package (`packages.find` include, contract package data), adds `hypothesis` to `dev` and an
`alphakeel-dev` extra for the cross-project CI, documents the two sources in `SKILL.md` and the seven READMEs, the
environment variables in `agent/.env.example`, ignores build artifacts; and this patch layer itself (`patches/`).

**Files.**
- `pyproject.toml`
- `agent/SKILL.md`
- `.gitignore`
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
