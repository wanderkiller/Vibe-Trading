# Upgrade rehearsal onto upstream `7f6908b7` (2026-10-07)

Rehearsal of [UPGRADE.md](UPGRADE.md) for the series on `feat/alphakeel-ir` (`7d8b0772`, base `e1dbea8a` = 0.1.16)
onto upstream `main` HEAD `7f6908b7` (168 upstream commits ahead of the base). Nothing on `feat/alphakeel-ir` or `main`
was changed; the result is the branch `rehearsal/upstream-7f6908b7` in the worktree `/home/ubuntu/vt-rehearsal`, left in
place for review. Not done: frontend build/tests (`npm`), `docker compose build`, deploy.

## 1. Anchor check before rebasing (`check_patches.py --upstream 7f6908b7`)

164 anchors: `OK=105, ABSENT=56` (our hooks, expected), and 3 needing attention:

| Status | Group | File | Finding |
|---|---|---|---|
| `DRIFT` | local-deploy | `Dockerfile.local` | upstream `Dockerfile` changed (adds `fonts-noto-core`, `fonts-noto-cjk` for PDF reports) |
| `CHANGED` | metrics-conventions | `agent/backtest/metrics.py` | upstream rewrote the Sortino block we replace (`95389681`) |
| `CHANGED` | run-card-data-audit | `agent/backtest/run_card.py` | upstream rewrote the `"warnings"` line we replace (`c28209f4`) |

No upstream anchor was `MISS`ing: every upstream signature, call site and literal our hooks rely on (engine bar loop,
`BaseTool`, loader protocol, `register`, registry tables, `write_run_card`) is unchanged. No `COLLISION`.
27 `REREAD` rows: upstream commits since the base touch `registry.py` (2), `loader_health.py` (3), `metrics.py` (2),
`validation.py` (1), `run_card.py` (1), `pyproject.toml` (5), `agent/SKILL.md` (5), `.gitignore` (1),
`agent/.env.example` (1), `Agent.tsx` (1), the nine locales (7 each) and the READMEs (17–41).
`engines/{base,crypto,composite,_market_hooks}.py` and `ccxt_loader.py`: 0 upstream commits.

## 2. Rebase (`git rebase --onto 7f6908b7 e1dbea8a`)

26 commits replayed (the three merges are dropped by linearisation). The frontend (7 commits), the deployment overlay
and 23 of the 26 commits applied without conflicts. Three stops:

| Commit | File | Conflict | Resolution |
|---|---|---|---|
| `1f83ef6f` register alphakeel_pack in docs | `README.md`, `README_{ar,es,id,ja,ko,zh}.md` | tool count in the repo tree: upstream 109, ours 108 (= old 107 + 1) | upstream's line with the count re-derived: **110** (109 + our tool) |
| `83ea7c2a` alphakeel_b2 loader, data audit | `agent/backtest/run_card.py` | upstream added model provenance (`_model_provenance`, `"model_provenance"`, provenance warnings) on the lines where we add `data_audit` | kept both: warnings = caller's + provenance + our non-auditable warning; card has `"data_audit"` and `"model_provenance"`; upstream's "Model provenance" and our "Data Audit" markdown sections both render |
| `41f8c94b` undefined Sharpe/Sortino are None | `agent/backtest/metrics.py` | upstream changed Sortino to the full-sample downside deviation (`sqrt(sum(min(r,0)^2)/N)`), which is the same formula as ours, but keeps a `1e-10` fallback without downside and `0.0` for non-finite returns | upstream's expression kept; only our rule re-applied: `None` when there is no downside or returns are non-finite |

Auto-merged without conflict but checked by hand: `validation.py` (upstream `227b94be` puts the initial capital at the
start of the Monte Carlo path; our `_sharpe` call sits after it — both kept), `metrics.py` profit factor (`f110ad6c`,
`None` when no trade lost — consistent with our convention), `loader_health.py` (evidence collector; our exclusion entry
intact), `registry.py` (`frame_caliber`; `price_caliber` classifies both alphakeel sources as `na`, like okx/ccxt),
`pyproject.toml`, `agent/SKILL.md`, `Agent.tsx` (upstream changed the prompt builder; our `overscroll-contain` class
is at a different line), the nine locales.

Follow-up commits on the rehearsal branch (after the replayed series):

1. `bf277ecc` restores what only merge `98e8248e` contained: `requirements-local-providers-lock.txt` and the
   `layout.close`/`layout.menu` labels in `id.json` (UPGRADE.md step 5).
2. `5d42d745` `patches/BASE` = `7f6908b7`; `Dockerfile.local` refreshed from the new upstream `Dockerfile` plus the
   unchanged provider-overlay block (now also installs the Noto fonts); the two `CHANGED` preimage anchors re-derived
   (`else 1e-10  # Preserve the existing no-downside fallback.`, `"warnings": [*(warnings or []), *provenance_warnings],`).
3. `4abbe376` upstream's new `test_metrics_sortino.py`: its four definition tests pass unchanged (same formula); its two
   convention tests asserted the fallbacks our patch replaces (`mean / 2e-10` without downside, `0.0` on degenerate
   paths) and now assert `None`. File added to the `metrics-conventions` group.
4. `66327a89` README/SKILL source counts: upstream's `test_readme_counts.py` checks every README against the registry and
   showed a **pre-existing defect of our docs patch** (also on `feat/alphakeel-ir`): `alphakeel_b2` was only in
   README.md's table, missing from every tree line, and the count said 29 for two added sources. Now every README has
   the `alphakeel_b2` row and tree entry and says 30 (upstream 28 + 2); SKILL.md says 30.
5. `2c4a9721` upstream's new `test_validation_initial_capital.py` expects Sharpe −3.3878 from the population std; our
   `validation._sharpe` uses the sample std (ddof=1, like `calc_metrics`), giving −2.7661 for the same path. Expected
   value changed with the reason in the test; upstream's behaviour under test (first trade counted from starting cash)
   is kept and still asserted. File added to the `metrics-conventions` group.

## 3. Anchor check after the rebase (working tree of the rehearsal branch)

`check_patches.py`: `OK=230` (all anchors, all listed files, coverage of every file changed since the new BASE),
exit 0. `check_patches.py --upstream 7f6908b7`: `OK=108, ABSENT=56`, exit 0.

## 4. Tests on the rehearsal branch

* `check_patches.py --tests` (`test_alphakeel_*`, `test_crypto_engine.py`, `test_metrics*.py`):
  **361 passed, 31 skipped, 0 failed** (the 31 skips need the AlphaKeel fixture server/repo, as on `feat/alphakeel-ir`).
* Wider run: the 117 test files that import a hooked module (run card, validation, loader health, metrics, engines,
  registry, ccxt loader): **2597 passed, 80 failed, 5 collection errors**. The same files on pristine upstream
  `7f6908b7` in the same interpreter give **the identical failure set** (80 failed, 5 errors): all environmental —
  `sklearn` and `yfinance` are not installed in `/home/ubuntu/vt-phase2/.venv` (38 shadow-account tests, 5 collection
  errors, loader-health catalog, README counts that count the registry), two timing tests (55 ms vs a 50 ms budget)
  and two warm-up-window benchmark tests. The venv also lacks upstream's new dependencies
  (`mistune`, `nh3`, `reportlab`, `arabic-reshaper`, `python-bidi`); the Docker build installs them from the new lock.
* Not run: the full backend suite in an environment with upstream's complete lock, the frontend tests.

## 5. Upstream changes in hooked files that need human re-reading

* `registry.py` (`85118116`, `91eeab4c`): new `frame_caliber()` — a loader that converts its adjustment basis reports
  it in `frame.attrs["adjustment"]`, and callers prefer that over the static `price_caliber` table. Our loaders set no
  `adjustment` attr and are `na` caliber; nothing to change, but `alphakeel_b2` frames now pass through this path.
* `metrics.py` (`95389681`, `f110ad6c`): Sortino now uses the same downside deviation as AlphaKeel (our patch shrinks to
  the `None` rule); profit factor and P/L ratio are `None` when no trade lost. Upstream is moving toward our convention;
  the remaining difference is `None` vs `1e-10`/`0.0` fallbacks for Sharpe and Sortino.
* `run_card.py` (`c28209f4`): model-training-exposure provenance (`strategy_provenance.json`, `model_training_cutoff`)
  with warnings and a markdown section. Merged next to our data audit; the warning order is caller, provenance, audit.
* `validation.py` (`227b94be`): the Monte Carlo path starts at the initial capital, so a single trade now yields one
  return — with our `_sharpe` (needs two returns) a one-trade path reports "Sharpe undefined" instead of a number.
  Intended under our convention, but worth knowing.
* `loader_health.py` (`712f8616`, `115cf06c`, `d81b0ad9`): rows carry sanitised loader warnings as `evidence`; credential
  redaction. No effect on our exclusion; `alphakeel_b2` (auth-required) stays outside the public canary.
* `pyproject.toml` (5 commits): new runtime deps `mistune`, `nh3`, `reportlab`, `arabic-reshaper`, `python-bidi`
  (report rendering) and shadow-account font package data. Our `include`, package-data and extras merged unchanged.
* Also: `loaders/base.py` loader cache version 7 → 8 (ccxt cache entries are rebuilt once); `Dockerfile` installs Noto
  fonts; `requirements-lock.txt` grew (+177 lines) but keeps `langchain-core==1.6.1`, so the provider overlay is still
  valid (anchor holds); `pip check` in the Docker build is the final word.
* Latent issue, not introduced by the upgrade: shadow-account reports format `metrics.get("sharpe", 0.0)`; with our
  `None` convention an undefined Sharpe is dropped by `_coerce_numeric` and rendered as `0.00`
  (`reporter.py`, and upstream's new `pdf_fallback.py`). Cosmetic, but it shows "0.00" for "undefined".

## 6. Recommendation

The rehearsal branch is a **usable upgrade candidate** for review, not yet for deployment. Every anchor holds on the new
base, the patch-layer tests pass, and the wider backend run fails exactly where pristine upstream fails in this
environment. Before it could replace `feat/alphakeel-ir` and be deployed:

1. a human reads the resolutions in §2 and the test-expectation changes (`4abbe376`, `2c4a9721`) — they encode our
   metric conventions over upstream's;
2. run the frontend tests (`npm test`; Layout drawer, Agent page, locales) and the full backend suite in the Docker
   image built from `Dockerfile.local` (complete lock; sklearn/yfinance present);
3. `docker compose build` (checks `pip check` with the new lock plus the overlay);
4. refresh the per-group commit lists in MANIFEST.md to the rebased hashes (the rehearsal only updated BASE, anchors and
   file lists), then deploy per DEPLOYMENT.local.md.

The README/SKILL source-count fix (`66327a89`) is independent of the upgrade and can also be applied to
`feat/alphakeel-ir` now.
