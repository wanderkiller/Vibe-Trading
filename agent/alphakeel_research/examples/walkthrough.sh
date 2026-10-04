#!/usr/bin/env bash
# Runs every documented command against AlphaKeel's offline fixture server (a separate process, formal HTTP interface only).
#   ALPHAKEEL_FIXTURE_SERVER=/path/to/research_fixture_server  (cargo build --example research_fixture_server in alphakeel)
# Prints each command, then its JSON result. Exit status is non-zero if any command fails or any expected result is missing.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AGENT="$(cd "$HERE/../.." && pwd)"
BIN="${ALPHAKEEL_FIXTURE_SERVER:-/home/ubuntu/alphakeel/target/debug/examples/research_fixture_server}"
PY="${PYTHON:-python3}"
W="$(mktemp -d)"; trap 'kill "$SRV" 2>/dev/null || true; exec 3>&- 2>/dev/null || true; rm -rf "$W"' EXIT
mkfifo "$W/stdin"; "$BIN" --dir "$W/data" --frames 61 <"$W/stdin" >"$W/info.json" 2>/dev/null & SRV=$!
exec 3>"$W/stdin"
for _ in $(seq 100); do [ -s "$W/info.json" ] && break; sleep 0.1; done
export ALPHAKEEL_RESEARCH_URL="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["url"])' "$W/info.json")"
export ALPHAKEEL_RESEARCH_TOKEN="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["token"])' "$W/info.json")"
START="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["start_ms"])' "$W/info.json")"
END="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["end_ms"])' "$W/info.json")"
export PYTHONPATH="$AGENT"
ak() { echo >&2; echo "\$ alphakeel-research $*" >&2; "$PY" -m alphakeel_research --cache "$W/cache" --work "$W/work" "$@"; }
ak check
echo "== 1. freeze and read data"
ak freeze --start-ms "$START" --end-ms "$END" --out "$W/pack.json" | tee "$W/freeze.json"
PACK="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["pack_id"])' "$W/freeze.json")"
ak read --pack "$PACK" --venue binance --symbol BTCUSDT --start-ms "$START" --end-ms "$((START+120001))" --as-of-ms "$((START+120000))"
echo "-- the point-in-time reader refuses a window that ends after as_of (a future read):" >&2
if ak read --pack "$PACK" --venue binance --symbol BTCUSDT --start-ms "$START" --end-ms "$((START+180000))" --as-of-ms "$((START+120000))" >"$W/future.json"; then echo "future read was NOT refused" >&2; exit 1; fi
cat "$W/future.json"; grep -q data.future_read "$W/future.json"
echo "== 2. independent Python backtest, reviewed by the engine"
ak review --pack "$PACK" --intents "$HERE/intents.json" | tee "$W/review.json"
"$PY" -c 'import json,sys;d=json.load(open(sys.argv[1]));assert d["passed"] is True,d' "$W/review.json"
echo "== 3. the same policy locally and under AlphaKeel state feedback, with comparison"
ak policy --pack "$PACK" --strategy "$HERE/carry_policy" --instruments "$HERE/instruments.json" --parameters "$HERE/parameters.json" --seed 7 | tee "$W/policy.json"
"$PY" -c 'import json,sys;d=json.load(open(sys.argv[1]));assert d["passed"] is True,d' "$W/policy.json"
echo "== 4. AlphaKeel native strategy"
ak native --pack "$PACK" --params "$HERE/params.json" | tee "$W/native.json"
echo "== fault injection: a wrong fee, then a flipped funding sign"
ak inject --pack "$PACK" --intents "$HERE/intents.json" --fault fee | tee "$W/inject-fee.json"
"$PY" -c 'import json,sys;d=json.load(open(sys.argv[1]));assert d["passed"] is False and d["first_difference"]["class"]=="fee",d' "$W/inject-fee.json"
ak inject --pack "$PACK" --intents "$HERE/intents.json" --fault funding-sign | tee "$W/inject-funding.json"
"$PY" -c 'import json,sys;d=json.load(open(sys.argv[1]));assert d["passed"] is False and d["first_difference"]["class"]=="funding",d' "$W/inject-funding.json"
echo; echo "walkthrough: all documented commands ran and every expectation held"
