"""Errors of the AlphaKeel research client. The codes are the service's stable error codes (error-codes.json)."""

from __future__ import annotations


class ApiError(Exception):
    """A structured error returned by the service (or raised locally with the same vocabulary)."""

    def __init__(self, code: str, message: str, *, field: str | None = None, event: str | None = None,
                 retryable: bool = False, request_id: str | None = None, job_id: str | None = None,
                 status: int | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.field = field
        self.event = event
        self.retryable = retryable
        self.request_id = request_id
        self.job_id = job_id
        self.status = status

    @classmethod
    def from_body(cls, body: dict, status: int | None) -> "ApiError":
        e = body.get("error") if isinstance(body, dict) else None
        if not isinstance(e, dict):
            return cls("engine.failure", f"unexpected response (HTTP {status})", status=status)
        return cls(str(e.get("code", "engine.failure")), str(e.get("message", "")), field=e.get("field"),
                   event=e.get("event"), retryable=bool(e.get("retryable")), request_id=e.get("request_id"),
                   job_id=e.get("job_id"), status=status)


class EvidenceError(ApiError):
    """Downloaded or local evidence fails a size, schema, hash or reference check."""


class FutureRead(ApiError):
    """A strategy asked the point-in-time reader for data that was not visible yet."""

    def __init__(self, message: str):
        super().__init__("data.future_read", message)


class Unsupported(ApiError):
    """A scenario outside what the execution profile models. Nothing is guessed: the run stops with this error."""

    def __init__(self, message: str, code: str = "capability.unsupported"):
        super().__init__(code, message)
