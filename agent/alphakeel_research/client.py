"""HTTP client for the AlphaKeel research service (stdlib + httpx, both already locked in Vibe-Trading).

Rules the client keeps (the plan's service requirements):
- the credential is read by the parent process only and never logged or put in artifacts;
- only safe reads and same-key submissions are retried, with bounded backoff; a new key is never generated to
  re-run a job whose outcome is unknown (query by key instead);
- every write carries an idempotency key; ``key_for(kind, payload)`` derives one from the request content so a retry
  of the same request reuses it;
- errors are the service's structured errors (``ApiError``), retryability comes from the service.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any, Callable

import httpx

from . import contract
from .errors import ApiError

DEFAULT_URL = "http://127.0.0.1:8731"
_RETRY_STATUS = {502, 503, 504}


def key_for(kind: str, payload: Any) -> str:
    """Deterministic idempotency key (40 hex chars) from the request content."""
    return hashlib.sha256((kind + "|" + contract.canonical_bytes(payload).decode("utf-8")).encode("utf-8")).hexdigest()[:40]


class Client:
    def __init__(self, base_url: str | None = None, token: str | None = None, *, timeout: float = 60.0,
                 retries: int = 3, backoff: float = 0.4, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.base_url = (base_url or os.environ.get("ALPHAKEEL_RESEARCH_URL") or DEFAULT_URL).rstrip("/")
        tok = token or os.environ.get("ALPHAKEEL_RESEARCH_TOKEN")
        tok_file = os.environ.get("ALPHAKEEL_RESEARCH_TOKEN_FILE")
        if not tok and tok_file:
            with open(tok_file, encoding="utf-8") as f:
                tok = f.read().strip()
        if not tok:
            raise ApiError("auth.missing", "no service credential: set ALPHAKEEL_RESEARCH_TOKEN or ALPHAKEEL_RESEARCH_TOKEN_FILE")
        self._token = tok
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep
        self._http = httpx.Client(base_url=self.base_url + "/api/v1/research", timeout=timeout, transport=transport,
                                  headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})

    def __repr__(self) -> str:  # never show the credential
        return f"Client({self.base_url!r})"

    def close(self) -> None:
        self._http.close()

    # -- transport ---------------------------------------------------------------------------------------------

    def _request(self, method: str, path: str, *, safe: bool, **kw: Any) -> httpx.Response:
        """Send; retry only when it is safe (reads, or writes that carry an idempotency key)."""
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self._http.request(method, path, **kw)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = e
                if not safe or attempt == self.retries:
                    raise ApiError("service.unavailable", f"transport error: {e.__class__.__name__}", retryable=True) from e
                self._sleep(self.backoff * (2 ** attempt))
                continue
            if r.status_code in _RETRY_STATUS and safe and attempt < self.retries:
                self._sleep(self.backoff * (2 ** attempt))
                continue
            return r
        raise ApiError("service.unavailable", f"gave up after {self.retries} retries: {last}", retryable=True)

    def _json(self, r: httpx.Response) -> Any:
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code >= 400:
            raise ApiError.from_body(body if isinstance(body, dict) else {}, r.status_code)
        return body

    def get(self, path: str, **params: Any) -> Any:
        return self._json(self._request("GET", path, safe=True, params={k: v for k, v in params.items() if v is not None}))

    def post(self, path: str, body: Any, *, key: str | None = None) -> Any:
        headers = {"Idempotency-Key": key} if key else {}
        return self._json(self._request("POST", path, safe=key is not None, json=body, headers=headers))

    def put(self, path: str, body: Any) -> Any:
        # step submissions are idempotent by (session, step, content): the same bytes may be re-sent safely
        return self._json(self._request("PUT", path, safe=True, json=body))

    # -- connection --------------------------------------------------------------------------------------------

    def whoami(self) -> dict:
        return self.get("/whoami")

    def capabilities(self) -> dict:
        return self.get("/capabilities")

    def check(self) -> dict:
        """Connection check with version negotiation: refuses a service speaking another major contract."""
        who = self.whoami()
        cap = self.capabilities()
        major = cap.get("schemas", {}).get("supported_major")
        if major != contract.MAJOR:
            raise ApiError("contract.unknown_major", f"the service speaks contract major {major}; this client speaks {contract.MAJOR}")
        return {"credential": who["credential"], "scopes": who["scopes"], "build": who.get("build"), "contract": cap["contract"],
                "modes": cap["modes"], "limits": cap["limits"]}

    # -- data packs --------------------------------------------------------------------------------------------

    def coverage(self, start_ms: int, end_ms: int) -> dict:
        return self.get("/coverage", start_ms=start_ms, end_ms=end_ms)

    def create_pack(self, request: dict, *, key: str | None = None) -> dict:
        key = key or key_for("pack", request)
        return self.post("/packs", {"key": key, "request": request}, key=key)

    def pack(self, job_or_pack_id: str) -> dict:
        return self.get(f"/packs/{job_or_pack_id}")

    def pack_lock(self, pack_id: str) -> dict:
        return self.get(f"/packs/{pack_id}/lock")

    def list_packs(self, key: str | None = None) -> dict:
        return self.get("/packs", key=key)

    def wait_pack(self, job_id: str, *, timeout: float = 900.0, poll: float = 0.5) -> dict:
        return self._wait(lambda: self.pack(job_id), ("ready", "failed", "cancelled", "interrupted", "expired"), timeout, poll)

    def slice(self, pack_id: str, **q: Any) -> dict:
        return self.get(f"/packs/{pack_id}/slice", **q)

    def object_range(self, pack_id: str, name: str, start: int, end: int, *, identity: bool = False) -> httpx.Response:
        r = self._request("GET", f"/packs/{pack_id}/object", safe=True, params={"name": name, **({"encoding": "identity"} if identity else {})},
                          headers={"Range": f"bytes={start}-{end}"})
        if r.status_code >= 400:
            raise ApiError.from_body(r.json() if r.headers.get("content-type", "").startswith("application/json") else {}, r.status_code)
        return r

    def object_head(self, pack_id: str, name: str, *, identity: bool = False) -> httpx.Response:
        return self.object_range(pack_id, name, 0, 0, identity=identity)

    # -- strategies, uploads, runs -----------------------------------------------------------------------------

    def upload(self, kind: str, data: bytes) -> dict:
        sha = hashlib.sha256(data).hexdigest()
        r = self._request("POST", "/uploads", safe=True, params={"kind": kind}, content=data,
                          headers={"X-Content-SHA256": sha, "Content-Type": "application/octet-stream"})
        return self._json(r)

    def register_strategy(self, manifest: dict, bundle_sha256: str | None = None) -> dict:
        # idempotent by strategy_id + content: safe to retry
        return self._json(self._request("POST", "/strategies", safe=True, json={"manifest": manifest, "bundle_sha256": bundle_sha256}))

    def render_profile(self, pack_id: str, mode: str, profile: dict | None = None) -> dict:
        """The execution profile document exactly as the service renders it (both sides use this one document)."""
        return self.post("/profiles/render", {"pack_id": pack_id, "mode": mode, "profile": profile})

    def validate_params(self, params: dict, pack_id: str | None = None) -> dict:
        return self.post("/validate/params", {"params": params, "pack_id": pack_id})

    def create_run(self, body: dict, *, key: str | None = None) -> dict:
        body = dict(body)
        key = key or key_for("run", body)
        body["key"] = key
        return self.post("/runs", body, key=key)

    def register_external(self, body: dict, *, key: str | None = None) -> dict:
        body = dict(body)
        key = key or key_for("external", body)
        body["key"] = key
        return self.post("/runs/external", body, key=key)

    def run(self, run_id: str) -> dict:
        return self.get(f"/runs/{run_id}")

    def runs(self, key: str | None = None) -> dict:
        return self.get("/runs", key=key)

    def cancel_run(self, run_id: str) -> dict:
        return self.post(f"/runs/{run_id}/cancel", {})

    def lineage(self, run_id: str) -> dict:
        return self.get(f"/runs/{run_id}/lineage")

    def wait_run(self, run_id: str, *, timeout: float = 900.0, poll: float = 0.5) -> dict:
        return self._wait(lambda: self.run(run_id), ("complete", "failed", "cancelled", "interrupted"), timeout, poll)

    def artifact(self, run_id: str, name: str, *, stored: bool = False) -> bytes:
        r = self._request("GET", f"/runs/{run_id}/artifacts/{name}", safe=True, params={"encoding": "stored"} if stored else None)
        if r.status_code >= 400:
            raise ApiError.from_body(r.json(), r.status_code)
        want = r.headers.get("x-content-sha256")
        if want and not stored and hashlib.sha256(r.content).hexdigest() != want:
            raise ApiError("evidence.hash_mismatch", f"artifact {name} does not match its digest header")
        return r.content

    # -- policy sessions ---------------------------------------------------------------------------------------

    def next_step(self, run_id: str, wait_ms: int = 20_000) -> dict:
        return self.get(f"/runs/{run_id}/step", wait_ms=wait_ms)

    def get_step(self, run_id: str, seq: int) -> dict:
        return self.get(f"/runs/{run_id}/steps/{seq}")

    def submit_step(self, run_id: str, seq: int, response: dict) -> dict:
        return self.put(f"/runs/{run_id}/steps/{seq}", response)

    # -- comparison --------------------------------------------------------------------------------------------

    def compare(self, a_run: str, b_run: str, mode: str, *, key: str | None = None) -> dict:
        body = {"mode": mode, "a_run": a_run, "b_run": b_run}
        key = key or key_for("comparison", body)
        return self.post("/comparisons", {**body, "key": key}, key=key)

    def comparison(self, cid: str) -> dict:
        return self.get(f"/comparisons/{cid}")

    def wait_comparison(self, cid: str, *, timeout: float = 300.0, poll: float = 0.5) -> dict:
        return self._wait(lambda: self.comparison(cid), ("complete", "failed", "interrupted"), timeout, poll)

    # -- helpers -----------------------------------------------------------------------------------------------

    def _wait(self, fetch: Callable[[], dict], done: tuple[str, ...], timeout: float, poll: float) -> dict:
        t0 = time.monotonic()
        while True:
            v = fetch()
            if v.get("status") in done:
                return v
            if time.monotonic() - t0 > timeout:
                raise ApiError("service.unavailable", f"timed out waiting (status {v.get('status')}); the job continues on the server", retryable=True)
            self._sleep(poll)
