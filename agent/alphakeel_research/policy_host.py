"""Entry point of the strategy sandbox process: loads a policy script and serves ``initialize`` / ``on_step`` over JSON lines.

Protocol (one JSON object per line on stdin, one reply per line on the protocol channel):
  {"op":"init","parameters":{...}}            -> {"ok":true,"state_sha256":"..."}
  {"op":"step","context":{...}}               -> {"ok":true,"intents":[...],"state_sha256":"..."}
  {"op":"close"}                              -> exits
The policy script defines  initialize(parameters) -> state  and  on_step(context, state) -> {"intents": [...], "state": ...}.
State and intents must be canonical JSON (integers, strings, booleans, null, lists, objects; no floats): the host
refuses anything else, so state is never a pickle and always has a digest. ``context.pit`` is a point-in-time reader on the
exported pack directory (``ALPHAKEEL_PACK_DIR``), when one is provided. Anything the script prints goes to stderr; the
protocol channel is a duplicated stdout descriptor the script cannot reach by accident.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

from . import canon


class Context(dict):
    """The step context (a plain JSON document) plus the point-in-time reader."""

    pit = None
    pack = None


def _load(path: str):
    spec = importlib.util.spec_from_file_location("alphakeel_policy", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
    spec.loader.exec_module(mod)
    for fn in ("initialize", "on_step"):
        if not callable(getattr(mod, fn, None)):
            raise RuntimeError(f"the policy script must define {fn}()")
    return mod


def main() -> int:
    proto = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    # Descriptor 1 itself now points at stderr, so os.write(1, ...), child processes and native libraries that write
    # "stdout" land in stderr too; only the duplicated descriptor above carries protocol frames.
    sys.stdout.flush()
    os.dup2(2, 1)
    sys.stdout = sys.stderr  # print() from the script must not corrupt the protocol channel
    script = sys.argv[1]
    mod = _load(script)
    pack = None
    pack_dir = os.environ.get("ALPHAKEEL_PACK_DIR")
    if pack_dir:
        from .packfile import Pack

        pack = Pack.from_directory(pack_dir)
    state = None
    inited = False

    def reply(obj: dict) -> None:
        proto.write(json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n")
        proto.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            op = msg.get("op")
            if op == "close":
                return 0
            if op == "init":
                state = mod.initialize(json.loads(json.dumps(msg["parameters"])))
                inited = True
                reply({"ok": True, "state_sha256": canon.canonical_sha256(state)})
            elif op == "step":
                if not inited:
                    raise RuntimeError("initialize() has not run")
                ctx = Context(msg["context"])
                if pack is not None:
                    from .packfile import Pit

                    # Only the point-in-time view: the unfiltered pack is never handed to the strategy (it held every
                    # future row next to ctx.pit, so "no look-ahead" was a convention, not a rule).
                    ctx.pit = Pit(pack, int(msg["context"]["time_ms"]))
                out = mod.on_step(ctx, json.loads(json.dumps(state)))
                if not isinstance(out, dict) or set(out) != {"intents", "state"}:
                    raise RuntimeError("on_step must return exactly {'intents': [...], 'state': ...}")
                if not isinstance(out["intents"], list):
                    raise RuntimeError("intents must be a list (an empty list is an explicit 'no orders')")
                digest = canon.canonical_sha256(out["state"])  # refuses floats / non-JSON state
                canon.canonical_bytes(out["intents"])
                state = out["state"]
                reply({"ok": True, "intents": out["intents"], "state_sha256": digest})
            else:
                raise RuntimeError(f"unknown op {op!r}")
        except Exception as e:  # noqa: BLE001 - reported to the parent, which stops the run
            reply({"ok": False, "error": f"{e.__class__.__name__}: {e}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
