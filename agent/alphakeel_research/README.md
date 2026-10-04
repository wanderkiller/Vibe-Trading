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
| Python backtest + intent review | `review --pack ID --intents intents.json` |
| policy under both state sources | `policy --pack ID --strategy DIR --instruments instruments.json --parameters params.json --seed N` |
| AlphaKeel native strategy | `native --pack ID --params handoff-params.json` |
| compare two runs | `compare --a RUN --b RUN --mode fixed_intent_replay\|python_policy\|native_strategy` |
| fault-injection demo | `inject --pack ID --intents intents.json --fault fee\|funding-sign` |

`examples/walkthrough.sh` runs all of them against AlphaKeel's offline fixture server (a separate process). A pass means
the engine execution and accounting of the submitted intents agree; it does not certify strategy logic. The policy sandbox
is process isolation with a scrubbed environment and resource limits, not a security boundary: do not run untrusted code.

Inside Vibe-Trading the same operations are available as the `alphakeel_research` agent tool; the `alphakeel_pack` data
source only marks pack-backed data and refuses OHLCV requests (there is no fallback to another source).

Contract: `contract/` is a pinned copy of AlphaKeel's OpenAPI, error codes and shared test vectors (`PIN` holds their
hashes); `models.py` is generated from it (`gen_models.sh`), never edited by hand.
