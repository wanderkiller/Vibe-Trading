# Upgrading the patch series onto a new upstream release

The series is the set of commits `$(cat patches/BASE)..feat/alphakeel-ir`, grouped in [MANIFEST.md](MANIFEST.md).
An upgrade replays it onto a new upstream commit. **Old patches are never assumed valid on a new base**: a rebase
that applies cleanly only proves the text still fits. Every hook's anchors and the engine/metrics tests must pass on
the new base, and any upstream change to a file we hook (even one that merged without a conflict) is re-read before
the upgrade is accepted, because upstream can change the behaviour our hook relies on without touching our lines.

Interpreter used below: `PY=/home/ubuntu/vt-phase2/.venv/bin/python` (any Python with the project installed works).

## 1. Fetch the release and assess it before touching anything

```bash
git fetch origin --tags
NEW=v0.1.17                     # or a commit, e.g. 7f6908b7
BASE=$(cat patches/BASE)
$PY patches/check_patches.py --upstream "$NEW"
git log --oneline "$BASE..$NEW" -- agent/backtest/engines agent/backtest/metrics.py agent/backtest/validation.py
```

Read the checker's output by status:

| Status | Meaning | Action |
|---|---|---|
| `MISS` (upstream) | upstream removed/renamed code a patch relies on | re-derive the hook against the new code; update the anchor |
| `CHANGED` (preimage) | upstream rewrote the code a patch replaces | re-read upstream's version and decide what the patch should now be |
| `DRIFT` (derived) | upstream `Dockerfile` changed | refresh `Dockerfile.local` (step 7) |
| `COLLISION` | upstream added a path one of our new files uses | rename ours or adopt theirs |
| `REREAD` | upstream commits since BASE touch a file we modify | read those commits (list in the output) |
| `ABSENT` (hook) | our hook is not upstream | expected; the rebase re-applies it |
| `PRESENT` (hook) | upstream already has our change | drop that part of the patch |

Record the output; it goes into the rehearsal/upgrade note (see `REHEARSAL-2026-10-07.md` for the format).

## 2. Create the upgrade branch in its own worktree

```bash
git worktree add ../vt-upgrade-$NEW -b upgrade/$NEW feat/alphakeel-ir
cd ../vt-upgrade-$NEW
```

Never upgrade in the deployed checkout, and never on `feat/alphakeel-ir` itself until the result is accepted.

## 3. Rebase

```bash
git rebase --onto "$NEW" "$BASE"
```

While the series still contains the merges `d9aa82d1`/`98e8248e`/`cc65301e` (until the first upgrade), the rebase
linearises it and drops them; see step 5.

## 4. Resolve conflicts per group (MANIFEST.md)

General rule: **keep upstream's new behaviour and re-apply our hook minimally**; never resolve a hooked file by taking
our whole old version (that silently reverts upstream fixes) or upstream's whole version (that drops the hook — the
checker reports it as `MISS`/`REGRESSED`).

* `frontend-mobile`: re-apply the drawer onto upstream's `Layout.tsx`; add `layout.close`/`layout.menu` to every
  locale, including new ones. Run the frontend tests (`npm test` in `frontend/`) before deploying.
* `local-deploy`: no conflicts expected (new files); `Dockerfile.local` is handled in step 7.
* `leaf-package`: new files, no conflicts; breakage shows up as failing anchors (`register`, `BaseTool`, loader
  protocol, `get_env_value`) or failing `test_alphakeel_*`.
* `loader-registry-hooks`: insert our two sources into upstream's current `VALID_SOURCES`, `_loader_modules`,
  `_NO_NETWORK_FALLBACK_SOURCES`, `EXCLUDED_PUBLIC_SOURCES`, `_TRADING_DAYS` and `_BARS_PER_DAY`; if upstream added a
  new annualisation table, add our sources there too.
* `crypto-funding`: re-read `engines/base.py`'s bar loop (`_execute_bars`: `before_rebalance_bar` → open valuation →
  fills → `after_rebalance_bar`/`on_bar`) and `crypto.py`/`composite.py`/`_market_hooks.py` in full if upstream touched
  any of them; our funding must still be charged once per (symbol, bar) before the fills. Any new upstream caller of
  `calc_crypto_funding_fee`/`FUNDING_HOURS` must be ported to `CryptoFunding`.
* `metrics-conventions`: keep upstream's improvements (e.g. its Sortino change) and re-apply only the
  `None`-for-undefined rule; update upstream's new tests that assert the old fallbacks, saying why in the commit.
* `run-card-data-audit`: re-insert the `data_audit` block and warning next to upstream's current card fields.
* `packaging-docs`: take upstream's README/SKILL text, then re-add our rows and re-derive the counts
  (upstream sources + 2, tools + 1); add our `include`/package-data/extras entries to upstream's `pyproject.toml`.

Document every resolution (file, what upstream changed, what we kept) in the upgrade note.

## 5. Restore merge-only content (first upgrade only)

The plain rebase loses what exists only in merge `98e8248e`'s resolution:

```bash
git show feat/alphakeel-ir:requirements-local-providers-lock.txt > requirements-local-providers-lock.txt
# and re-add "close"/"menu" to the "layout" block of frontend/src/i18n/locales/id.json
git add -A && git commit -m "local: restore merge-only overlay content lost by linearising the series"
```

After this the series is linear and later upgrades skip this step.

## 6. Verify

```bash
$PY patches/check_patches.py --tests   # anchors on the working tree + AlphaKeel/crypto/metrics tests
$PY -m pytest agent/tests -q -x        # the full backend suite, once per upgrade
```

Both must pass. A failing upstream test in a hooked area is resolved deliberately (fix the hook, or change the test
with a written reason), never skipped. Check also that no new upstream code consumes `metrics["sharpe"]`/`["sortino"]`
as a number without handling `None`: `git diff "$BASE" "$NEW" -- agent | grep -n "sharpe\|sortino"`.

## 7. Dependencies and Dockerfile.local

* If `requirements-lock.txt` or `requirements-channels-lock.txt` changed, review them against
  `requirements-local-providers-lock.txt` as DEPLOYMENT.local.md describes (the overlay replaces the base
  `langchain-core` pin; if upstream moved that pin, regenerate the overlay with hashes and keep `pip check` green).
* Refresh `Dockerfile.local`: copy the new upstream `Dockerfile` and re-insert the provider-overlay block right after
  `RUN pip install --no-cache-dir --no-deps -e .`. The `derived` anchor must be `OK` afterwards.

## 8. Build and deploy

```bash
docker compose config --quiet
docker compose build vibe-trading
```

Then deploy per DEPLOYMENT.local.md (backup image, env files and volumes first; `up -d --no-build --no-deps`).

## 9. Record the new base

On the accepted upgrade branch, before it replaces `feat/alphakeel-ir`:

```bash
git rev-parse "$NEW" > patches/BASE
```

and update MANIFEST.md: the per-group commit lists (new hashes after the rebase: `git log --format='%h %s'
"$NEW"..HEAD -- <files>`), any anchor that had to change (also in `anchors.json`), the upstreamable notes (drop what
upstream adopted), and the history note once the merges are gone. `check_patches.py` (working tree) must pass,
including its coverage check, which compares against the new `patches/BASE`.
