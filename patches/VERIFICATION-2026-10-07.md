# Verification of `feat/alphakeel-ir` (T9b, 2026-10-07)

Final Vibe-Trading-side check of the Strategy IR loop on `feat/alphakeel-ir` (BASE `e1dbea8a`, upstream 0.1.16),
interpreter `/home/ubuntu/vt-phase2/.venv/bin/python` (Python 3.12).

## Inputs

* **Contract.** Re-exported with AlphaKeel `tools/export-research-contract.sh` from the `ak-ir` checkout at `2695c2a`
  (commit `dbcea0e6`). Only `contract/strategy-ir/README.md` and its `contract/PIN` line changed; `openapi.json`,
  `error-codes.json` and all vectors are byte-identical, so `models.py` was not regenerated. `anchors.json` pins no
  contract hashes.
* **Fixture server.** `/home/ubuntu/ak-shared-target/ir/debug/examples/research_fixture_server`, built by T9a from
  AlphaKeel `3b41dca` (reports build `unknown+78122988ad7d`). `Client.check()` against it: `contract_pin: matched`,
  modes `native_strategy, ir_strategy, fixed_intent_replay, python_policy`.

## README source counts (`ff1d0f04`)

Cherry-pick of the rehearsal's `66327a89` (applied cleanly): every `README*.md` lists `alphakeel_b2` in the source
table and the `loaders/` tree line and says 30 sources; `agent/SKILL.md` says 30. `test_readme_counts.py` cannot pass
in this venv (the registry loses `yfinance`, the tool registry loses `web_search` without `ddgs`, the badge counters
import `openpyxl`). With empty stub `yfinance` and `ddgs` packages on `PYTHONPATH` (scratch only) every source-count,
loader-tree and agent-tool-count test passes (93 passed); the 15 that remain need a real `openpyxl`
(`test_feature_badges_state_the_real_counts`, `test_quantlib_badge_states_both_the_function_and_module_counts`,
`test_all_readmes_agree_with_each_other`).

## Full suite

`ALPHAKEEL_FIXTURE_SERVER=<above> ALPHAKEEL_REQUIRE_FIXTURE=1 pytest tests -q -p no:cacheprovider
--continue-on-collection-errors` from `agent/` (pytest-timeout is not installed; without
`--continue-on-collection-errors` collection aborts on the 15 errors below):

**15713 passed, 158 failed, 268 skipped, 15 collection errors, 800 s.**

None of the failures is caused by this branch. Outside the hooked files listed in `anchors.json` (all covered by
`check_patches.py --tests`, green), the branch's only change under `agent/src` is the new file
`src/tools/alphakeel_research_tool.py`; every failure below is a missing or mismatched package in this venv, the same
class T8 recorded on pristine upstream (REHEARSAL-2026-10-07.md §4: sklearn, yfinance, README counts, warm-up window).

| Cause | Tests |
|---|---|
| `yfinance` not installed | 13 collection errors (`test_yfinance_*` ×7, `test_weekly_monthly_bars`, `test_vietnam_equity`, `test_local_source_routing`, `test_loader_volume_units`, `test_benchmark_excess_return_consistency`, `test_argentina_market_routing`); `test_warmup_window` ×2, `test_engine_robustness` ×2 (via `backtest.benchmark`), `test_loader_retry_helpers` ×1, `test_loader_health::test_catalog_covers_every_public_network_loader` |
| `yfinance`/`ddgs`/`openpyxl` (registry and badge counts) | `test_readme_counts` ×36 (see above) |
| `sklearn` not installed | `test_shadow_account` ×34, `test_ml_strategy_skill` ×2, `test_social_media_intelligence_skill` ×1 |
| `langchain-openai` not installed | `test_llm` ×14, `test_llm_reasoning_effort` ×16, `test_provider_diagnostics` ×4, `test_provider_header_isolation` ×2, `test_chat_llm_lifecycle` ×1 |
| `oauth-cli-kit` not installed | `test_openai_codex` ×7 |
| `openpyxl` / `docx` / `pptx` / `PIL` not installed | `test_quantlib_tool` ×4, `test_doc_reader` ×3, `test_ths_/futu_/eastmoney_*excel_serial` ×3, `test_ocr_engine` ×4; collection error `quantlib/valuation/test_artifact.py` |
| `openai` not installed | collection error `test_swarm_error_surfacing.py` |
| `ddgs` not installed (`web_search` unavailable) | `test_mcp_regression` ×1, `test_preset_honesty` ×1, `test_swarm_presets_packaging` ×1 |
| `mcp`/`fastmcp` version differs from upstream's lock | `test_mcp_client_adapter` ×2, `test_oauth_token_cache` ×1 (`MCPError.__init__` signature), `test_mcp_oauth_schema` ×3 (timeout type), `test_mcp_goal_session_isolation` ×1 |
| `akshare` version (`symbol_market_map` lacks the pairs) | `test_akshare_loader` ×6 (forex detection) |
| `bottleneck` not installed | `test_factor_operators::test_bottleneck_available` |
| package not installed editable in this venv | `test_governance::test_collect_key_package_versions_reflects_real_environment` |
| host sets the sandbox credentials the test expects absent | `test_runner_env::test_sandbox_credentials_absent_in_this_environment` |
| `ALPHAKEEL_REQUIRE_FIXTURE=1` turns missing dev tools into failures | `test_alphakeel_contract::test_models_are_the_current_generation_of_the_pinned_openapi` (`datamodel-code-generator`), `test_alphakeel_protocol::test_schemathesis_fuzzes_…` (`schemathesis`); both skip without the flag and `openapi.json` is unchanged |

## Patch layer

`check_patches.py --tests` (run with the venv interpreter and the fixture server; the system `python3` has no
pytest): working tree `OK=228`, all anchors hold; tests **371 passed, 3 skipped, 0 failed**.

## Not covered

The suite under upstream's complete lock (Docker image from `Dockerfile.local`), the frontend tests, and the two
cross-project tests that need `datamodel-code-generator` and `schemathesis` (install `.[dev,alphakeel-dev]`).
