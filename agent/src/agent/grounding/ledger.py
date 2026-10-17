"""The :class:`GroundingLedger` facade the agent loop drives."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote, quote_plus, unquote_plus, urlsplit

from src.agent.resolution_context import ResolutionContext

from src.agent.grounding.identity import (
    IdentityRecord,
    _IdentityMixin,
    _RESOLVER_TOOL,
    _scan_symbols,
    _utc_now,
)
from src.agent.grounding.evidence import (
    _PRICE_FIELDS,
    EvidenceRecord,
    _EvidenceMixin,
    _json_object,
    _price_field_for_path,
)
from src.agent.grounding.factor_names import ids_in_text, known_factor_ids
from src.agent.grounding.figures import Figure, parse_figures_block, scan_figures, strip_figures_block
from src.agent.grounding.policies import ValidationResult, _PolicyMixin
from src.agent.grounding.registry import GROUNDING_CHECKS
from src.agent.grounding.release import (
    MAX_GROUNDING_RECOVERY_ROUNDS,
    MAX_PRICE_EVIDENCE_ATTEMPTS,
    MAX_SYMBOL_RESOLUTION_ATTEMPTS,
    _ReleaseMixin,
)

GROUNDING_ARTIFACT = "grounding_evidence.json"


# Tools whose successful completion can ground backtest/analysis metric claims
# (#1336). Success alone is not authority: only results that actually carried
# metric output (parsed from the result or its run-dir artifacts) raise
# ``analysis_claim_unavailable``. A deduplicated ("skipped") call is never a
# completion.
_ANALYSIS_TOOLS = frozenset(
    {"backtest", "factor_analysis", "run_shadow_backtest", "quantlib_call",
     "alphakeel_research"}
)

# Tools whose result names the factor-library alphas the session really handled
# (``backtest`` is read through its screen's ``zoo_factors_used``).
_FACTOR_TOOLS = frozenset({"alpha_zoo", "alpha_bench", "alpha_compare", "factor_analysis"})
# Tools whose success means a document, page, news item or paper was retrieved:
# the only sources a figure declared ``cited`` can stand on. ``sentiment`` scores a text the caller passed in
# (its ``sentiment_score`` mode echoes it) and retrieves nothing.
_RETRIEVAL_TOOLS = frozenset(
    {"web_search", "read_url", "read_document", "get_stock_news", "research_papers",
     "get_research_reports", "get_sec_filings"}
)
# ``load_skill`` is deliberately absent: every backtest session loads a skill first, and a skill text is no source.
# A thousands comma only joins groups of three digits ("1,234,567"); "5,10,15" is three numbers. A trailing % marks a percent.
_NUMBER_LITERAL_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(\s?[%\uff05])?")
_MAX_NUMBERS_PER_CALL = 2000
_MAX_SOURCES_PER_CALL = 40
_MAX_SOURCE_LEN = 80
_MAX_RETRIEVAL_RECORDS = 60
# Keys of a retrieval result that echo the call or describe the envelope, not a document body.
_ECHO_KEYS = frozenset(
    {"query", "q", "mode", "text", "input", "prompt", "backends", "backend", "length", "status", "error", "arguments",
     "params", "args", "limit", "page", "symbol", "ticker", "codes", "lang", "search_query", "phrase_query_returned_nothing"}
)
_SOURCE_KEYS = frozenset({"title", "source", "publisher", "site"})
_HOST_KEYS = frozenset({"url", "link", "domain"})
_BARE_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


# A calendar date or clock time is no figure: its parts ("2024-12-05", "12月5日", "10:30") would back "12%" or "5".
_DATE_RE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}[-/年]\d{1,2}(?:[-/月]\d{1,2}[日号]?)?|\d{1,2}月\d{1,2}[日号]|\d{1,2}:\d{2}(?::\d{2})?"
)


def _numbers_in(text: str) -> list[tuple[float, bool]]:
    """Numeric literals of ``text`` as ``(absolute value, is_pct)`` in order (thousands commas stripped; dates and clock
    times skipped)."""
    out: list[tuple[float, bool]] = []
    text = _DATE_RE.sub(" ", text)
    for match in _NUMBER_LITERAL_RE.finditer(text):
        try:
            out.append((float(match.group(1).replace(",", "")), match.group(2) is not None))
        except ValueError:
            continue
    return out


def _argument_strings(arguments: Any, out: list[str]) -> None:
    """Every string and numeric argument value of a call, rendered as the result would echo it."""
    if isinstance(arguments, Mapping):
        for value in arguments.values():
            _argument_strings(value, out)
    elif isinstance(arguments, (list, tuple, set, frozenset)):
        for value in arguments:
            _argument_strings(value, out)
    elif isinstance(arguments, str):
        if arguments.strip():
            out.append(arguments)
    elif isinstance(arguments, (int, float)) and not isinstance(arguments, bool):
        out.append(str(arguments))


def _strip_arguments(text: str, arguments: Mapping[str, Any]) -> str:
    """``text`` without any occurrence of the call's own argument values (a result echoes them).

    A value that is a bare number is removed only as a whole numeric token, so ``limit=5`` does not cut "12.5".
    """
    values: list[str] = []
    _argument_strings(arguments, values)
    # A result also echoes a value trimmed or URL-encoded ("a b 12.5 " as "a+b+12.5").
    for value in list(values):
        values.extend((value.strip(), quote_plus(value.strip()), quote(value.strip()), unquote_plus(value.strip())))
    values = [value for value in values if value.strip()]
    for value in sorted(set(values), key=len, reverse=True):
        if _BARE_NUMBER_RE.fullmatch(value.strip()):
            text = re.sub(r"(?<![\d.,])" + re.escape(value.strip()) + r"(?!\d|[.,]\d)", " ", text)
        else:
            text = text.replace(value, " ")
    return text


def _host(value: str) -> str:
    """Host of a url, link or domain string, casefolded."""
    value = value.strip()
    host = urlsplit(value if "//" in value else "//" + value).netloc or value
    return host.rsplit("@", 1)[-1].split(":", 1)[0].casefold()[:_MAX_SOURCE_LEN]


def _walk_body(
    node: Any,
    key: str,
    arguments: Mapping[str, Any],
    numbers: dict[tuple[float, bool], None],
    sources: dict[str, None],
    skip: frozenset[str],
) -> None:
    """Collect numbers and source names from the body of a retrieval result, skipping echoed keys."""
    if key.casefold() in skip:
        return
    if isinstance(node, Mapping):
        for k, v in node.items():
            _walk_body(v, str(k), arguments, numbers, sources, skip)
    elif isinstance(node, list):
        for v in node:
            _walk_body(v, key, arguments, numbers, sources, skip)
    elif isinstance(node, str):
        lowered = key.casefold()
        if lowered in _SOURCE_KEYS and node.strip():
            sources.setdefault(node.strip().casefold()[:_MAX_SOURCE_LEN], None)
        elif lowered in _HOST_KEYS and node.strip():
            # A url is an address, not a body: its numbers back nothing (it may be the call's own url, rewritten).
            sources.setdefault(_host(node), None)
            return
        for number in _numbers_in(_strip_arguments(node, arguments)):
            numbers.setdefault(number, None)
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        if str(node) not in {v.strip() for v in _flat_arguments(arguments)}:
            numbers.setdefault((abs(float(node)), False), None)


def _flat_arguments(arguments: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    _argument_strings(arguments, values)
    return values


def retrieval_record(
    call_id: str, tool: str, arguments: Mapping[str, Any], result: str
) -> dict[str, Any]:
    """The per-call record of a retrieval: its distinct body numbers and the sources it names."""
    numbers: dict[tuple[float, bool], None] = {}
    sources: dict[str, None] = {}
    try:
        body = json.loads(result)
    except ValueError:
        body = None
    if isinstance(body, (Mapping, list)):
        # ``read_document`` returns the document itself under "text"; for every other tool "text" is an echoed input.
        skip = _ECHO_KEYS - {"text"} if tool == "read_document" else _ECHO_KEYS
        _walk_body(body, "", arguments, numbers, sources, skip)
    else:
        for number in _numbers_in(_strip_arguments(result, arguments)):
            numbers.setdefault(number, None)
    return {
        "call_id": call_id,
        "tool": tool,
        "sources": list(sources)[:_MAX_SOURCES_PER_CALL],
        "numbers": [[value, pct] for value, pct in list(numbers)[:_MAX_NUMBERS_PER_CALL]],
    }


def _clean_record(raw: Any, call_id: str) -> dict[str, Any] | None:
    """A persisted retrieval record, validated, under ``call_id``; None when malformed."""
    if not isinstance(raw, dict):
        return None
    sources = [str(x) for x in raw.get("sources") or [] if isinstance(x, str)][:_MAX_SOURCES_PER_CALL]
    numbers: list[list[Any]] = []
    for pair in raw.get("numbers") or []:
        if (
            isinstance(pair, (list, tuple)) and len(pair) == 2
            and isinstance(pair[0], (int, float)) and not isinstance(pair[0], bool)
        ):
            numbers.append([float(pair[0]), bool(pair[1])])
        if len(numbers) >= _MAX_NUMBERS_PER_CALL:
            break
    return {"call_id": call_id, "tool": str(raw.get("tool") or ""), "sources": sources, "numbers": numbers}


logger = logging.getLogger(__name__)

_PRIOR_PREFIX = "prior:"
# Quotes are per turn (freshness): never carried from an earlier turn. ``bash``
# output is arbitrary model-run text, never durable evidence.
_PER_TURN_TOOLS = frozenset({"get_market_data", "search_symbol", "bash"})
# IR evidence copies are immutable per run; an engine run dir can be re-run and
# overwritten, so its figures belong to the turn that read them.
_IMMUTABLE_BACKTEST_SCOPE = "ir_runs/"
# Analysis-entry keys that would hold a path relative to another turn's run dir.
_RUN_DIR_RELATIVE_KEYS = ("artifact", "scope", "path", "run_dir", "file")
_MAX_CARRIED_RECORDS = 5000


def _passed_figures(
    figures: Sequence[Figure], issues: Sequence[Mapping[str, Any]]
) -> tuple[str, ...]:
    """The measured figures no issue flags, as written, in document order.

    The correction prompt needs them for its keep list. A spelling that fails
    anywhere in the draft is endorsed nowhere, so the list only carries values
    the gate can vouch for outright. Bare integers stay off: only the
    price-posing ones are checked, so a plain "bare" pass proves nothing.
    """
    flagged_spans = {
        (int(span[0]), int(span[1]))
        for issue in issues
        if isinstance((span := issue.get("span")), (list, tuple)) and len(span) == 2
    }
    flagged_values = {
        str(issue.get("value")) for issue in issues if issue.get("value") is not None
    }
    passed: list[str] = []
    seen: set[str] = set()
    for figure in figures:
        if figure.shape != "measured":
            continue
        if (figure.start, figure.end) in flagged_spans:
            continue
        if figure.text in flagged_values or figure.text in seen:
            continue
        seen.add(figure.text)
        passed.append(figure.text)
    return tuple(passed)


_ACTIONABLE_MARKET_RE = re.compile(
    r"(?:\bbuy\b|\bsell\b|\bentry\b|\btarget price\b|\bcurrent price\b|"
    r"\blatest price\b|\bprice of\b|\btrade\b|"
    r"\bvaluation of\b|\bwhat (?:is|are) .{1,80} worth\b|"
    r"\bis .{1,80} (?:listed|publicly traded)\b|"
    r"买入|卖出|入场|目标价|现价|最新价|股价|交易价格|估值|值多少钱|"
    r".{1,40}(?:是否|有没有|已经|已)(?:在.{0,20})?上市)",
    re.IGNORECASE,
)


class GroundingLedger(
    _IdentityMixin,
    _EvidenceMixin,
    _PolicyMixin,
    _ReleaseMixin,
):
    """Run-scoped identity state machine and evidence ledger."""

    def __init__(
        self,
        *,
        run_dir: Path,
        user_message: str,
        history: Sequence[Mapping[str, Any]] | None = None,
        contextual_identity_constraints: bool = True,
        prior_ledgers: Sequence[Path] = (),
        session_copy: Path | None = None,
    ) -> None:
        """Create a ledger and seed only authoritative prior identities.

        Args:
            run_dir: Active run directory.
            user_message: Current user request.
            history: Optional prior message history. It remains available to
                the model. Only explicit constraints whose clause names the
                current resolver subject may carry forward; stale global
                instructions cannot authorize a new subject.
            contextual_identity_constraints: Whether explicit market words in
                the original conversation may narrow resolver candidates.
            prior_ledgers: Ledger JSON files of earlier completed turns of the
                same conversation, oldest first, read from the session store
                (never from a run directory, which the model can write to).
                Their observed tool evidence (never quotes, bash output or
                engine backtest figures, which are per turn) and analysis
                metrics are carried under ``prior:<file stem>:<call_id>``.
            session_copy: Where ``persist`` also writes the ledger, outside the
                model's write roots, so a later turn can carry it.
        """
        self.run_dir = Path(run_dir)
        self.session_copy = Path(session_copy) if session_copy is not None else None
        self.user_message = user_message
        self.resolution_context = ResolutionContext.from_messages(
            user_message,
            history,
            enabled=contextual_identity_constraints,
        )
        self._identities: dict[str, IdentityRecord] = {}
        self._evidence: list[EvidenceRecord] = []
        self._tool_failures: list[dict[str, Any]] = []
        self._analysis_completed: list[dict[str, Any]] = []
        self._analysis_metrics: list[dict[str, Any]] = []
        self._validations: list[dict[str, Any]] = []
        # The one document that actually shipped through the release path, if
        # any. Not a draft, so it stays out of ``validation_count``.
        self._released: dict[str, Any] | None = None
        self._recovery_rounds = 0
        self._symbol_resolution_attempts = 0
        self._price_evidence_attempts = 0
        self._ingested_csvs: set[str] = set()
        # Per-bar tables completed backtests wrote: resolved path -> (sha256,
        # backtest scope), so a later read of one can be recognised as engine
        # output. Files the model wrote itself never qualify.
        self._engine_tables: dict[str, tuple[str, str]] = {}
        self._model_written: set[str] = set()
        # Card notes of the validation running now (see ``ValidationResult.card_notes``), and those of the text
        # ``redacted_release`` released.
        self._card_notes: list[tuple[str, str]] = []
        self.release_card_notes: tuple[tuple[str, str], ...] = ()
        # Backtest call -> its run directory, and each directory's latest call.
        self._backtest_scopes: dict[str, str] = {}
        self._scope_latest: dict[str, str] = {}
        self._identity_required = bool(_ACTIONABLE_MARKET_RE.search(user_message))
        self._buffer_output = self._identity_required
        # Every instrument this run is entitled to write about: the ones the
        # user named, plus the ones a succeeding tool call passed in or returned.
        self._session_symbols: set[str] = _scan_symbols(user_message)
        # Bare tickers a succeeding call passed in, e.g. "AAPL" for the nine
        # tools whose contract is a bare US ticker. "AAPL.US" in the answer then
        # names an instrument the run really handled.
        self._session_symbol_roots: set[str] = set()
        # Factor-library ids a factor tool returned (or a backtest ran through
        # ``zoo_factor``) and the count of successful retrieval calls; both carry
        # over from earlier turns of the conversation.
        self._factor_ids: set[str] = set()
        # One record per successful retrieval call (newest last, at most ``_MAX_RETRIEVAL_RECORDS``): the distinct numbers
        # of its body and the sources it names, which is what a cited figure can stand on.
        self._retrieval_records: list[dict[str, Any]] = []

        # call id -> tool of the analysis calls carried from earlier turns (``_calls_of_tool``).
        self._prior_call_tools: dict[str, str] = {}
        self._seed_symbols(user_message, source="user_message")
        self._carry_prior_evidence(prior_ledgers)
        self.persist()

    def _carry_prior_evidence(self, prior_ledgers: Sequence[Path]) -> None:
        """Carry observed evidence persisted by earlier turns of the conversation.

        Args:
            prior_ledgers: Earlier turns' ledger files, oldest first. Unreadable
                or foreign-schema ledgers are skipped.
        """
        carried_evidence: list[EvidenceRecord] = []
        carried_metrics: list[dict[str, Any]] = []
        known_factors = known_factor_ids()
        for path in prior_ledgers:
            path = Path(path)
            prefix = f"{_PRIOR_PREFIX}{path.stem}:"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.debug("prior grounding ledger %s not carried: %s", path, exc)
                continue
            if not isinstance(data, dict) or data.get("schema_version") != 1:
                logger.debug("prior grounding ledger %s has an unsupported schema", path)
                continue
            carried_calls: set[str] = set()
            self._factor_ids.update(
                str(x) for x in data.get("factor_ids") or [] if isinstance(x, str) and x in known_factors
            )
            # An older ledger's flat ``retrieved_numbers`` list is ignored: its numbers carried no call or source.
            for raw in data.get("retrieval_records") or []:
                if not isinstance(raw, dict) or not raw.get("call_id"):
                    continue
                call_id = str(raw["call_id"])
                record = _clean_record(raw, call_id if call_id.startswith(_PRIOR_PREFIX) else f"{prefix}{call_id}")
                if record is not None:
                    self._upsert_retrieval_record(record)
            for raw in data.get("evidence") or []:
                if not isinstance(raw, dict) or raw.get("status") != "observed":
                    continue
                tool = raw.get("tool")
                if tool in _PER_TURN_TOOLS:
                    continue
                field = str(raw.get("field") or "")
                if field in _PRICE_FIELDS or _price_field_for_path(field) is not None:
                    continue
                if tool == "backtest" and not str(raw.get("scope") or "").startswith(_IMMUTABLE_BACKTEST_SCOPE):
                    continue
                call_id = str(raw.get("call_id") or "")
                if call_id.startswith(_PRIOR_PREFIX):
                    continue
                try:
                    record = EvidenceRecord(**{**raw, "call_id": f"{prefix}{call_id}",
                                               "artifact": None, "scope": None})
                except TypeError:
                    continue
                carried_evidence.append(record)
                carried_calls.add(call_id)
            for raw in data.get("analysis_evidence") or []:
                if not isinstance(raw, dict):
                    continue
                call_id = str(raw.get("call_id") or "")
                if call_id.startswith(_PRIOR_PREFIX):
                    continue
                # Backtest-artifact metrics follow the immutability rule of their call.
                if raw.get("tool") == "backtest" and call_id not in carried_calls:
                    continue
                entry = {**raw, "call_id": f"{prefix}{call_id}"}
                for key in _RUN_DIR_RELATIVE_KEYS:
                    if key in entry:
                        entry[key] = None
                carried_metrics.append(entry)
            for raw in data.get("analysis_completed") or []:
                if isinstance(raw, dict) and raw.get("call_id") and raw.get("tool"):
                    call_id = str(raw["call_id"])
                    if not call_id.startswith(_PRIOR_PREFIX):
                        self._prior_call_tools[f"{prefix}{call_id}"] = str(raw["tool"])
        # Most recent ledgers win the cap.
        self._evidence.extend(carried_evidence[-_MAX_CARRIED_RECORDS:])
        self._analysis_metrics.extend(carried_metrics[-_MAX_CARRIED_RECORDS:])

    @property
    def authorized_symbols(self) -> set[str]:
        """Return exact symbols locked before the next tool batch."""
        return {
            record.symbol
            for record in self._identities.values()
            if record.status == "locked" and record.symbol
        }

    @property
    def identity_status(self) -> str:
        """Return the aggregate first-class identity state.

        ``conflicting`` is the only state that outranks a successful lock: two
        sources contradicting each other about one query is a fact about the
        data, not a gap in it. Every other blocking state means "not known
        yet", and a side query that failed, went unanswered, or returned a
        shortlist must not retract an identity the run did lock — one flaky
        resolver call otherwise poisons every remaining answer in the session,
        with no path back. Per-symbol safety does not depend on this aggregate:
        a consumer still has to match a locked symbol in
        :meth:`_match_authorized_symbol` before it may run.
        """
        records = list(self._identities.values())
        if not records:
            return "unresolved" if self._identity_required else "not_required"
        statuses = {record.status for record in records}
        if "conflicting" in statuses:
            return "conflicting"
        if "locked" in statuses:
            return "locked"
        for blocking in ("ambiguous", "invalidated", "unresolved"):
            if blocking in statuses:
                return blocking
        if "not_found" in statuses:
            return "not_found"
        return "unresolved"

    @property
    def should_buffer_output(self) -> bool:
        """Return whether unverified model prose must be hidden from live sinks."""
        return self._buffer_output or bool(self._evidence)

    @property
    def validation_count(self) -> int:
        """Return the number of final drafts checked so far."""
        return len(self._validations)

    @property
    def figures_removed(self) -> int:
        """Return how many figures the discounted release cut, or 0."""
        return int((self._released or {}).get("figures_removed", 0))

    @staticmethod
    def streamable_length(text: str) -> int:
        """Return how much of a streaming answer may be shown live.

        Held back: everything from the figures block's fence on (the model's
        declaration to the gate), a last line that could still become that fence,
        everything from the first measurement-shaped number (the gate has not
        checked it, and a rejected draft must not have shown it), and a number
        still being written ("0." before "666").

        Args:
            text: The answer streamed so far.

        Returns:
            The length of the prefix that is safe to emit.
        """
        span = parse_figures_block(text).span
        limit = len(text)
        if span is not None:
            limit = span[0]
        else:
            line_start = text.rfind("\n") + 1
            if text[line_start:].lstrip()[:1] in ("`", "~"):
                limit = line_start
        prefix = text[:limit]
        measured = GroundingLedger.measurement_start(prefix)
        if measured is not None:
            return measured
        trimmed = prefix.rstrip(" \t")
        cursor = len(trimmed)
        while cursor and (trimmed[cursor - 1].isdigit() or trimmed[cursor - 1] in ".,"):
            cursor -= 1
        if any(char.isdigit() for char in trimmed[cursor:]):
            return cursor
        return limit

    @staticmethod
    def measurement_start(text: str) -> int | None:
        """Where the first measurement-shaped number in ``text`` starts, or None."""
        for figure in scan_figures(text, parse_figures_block(text)):
            if figure.shape == "measured":
                return figure.start
        return None

    def identity_summary(self) -> dict[str, Any]:
        """Return compact identity state for traces and tool errors."""
        return {
            "status": self.identity_status,
            "authorized_symbols": sorted(self.authorized_symbols),
            "records": [asdict(record) for record in self._identities.values()],
            "recovery": self.recovery_summary(),
        }

    def recovery_summary(self) -> dict[str, Any]:
        """Return bounded-recovery budget state for traces and the artifact."""
        return {
            "rounds": self._recovery_rounds,
            "max_rounds": MAX_GROUNDING_RECOVERY_ROUNDS,
            "symbol_resolution_attempts": self._symbol_resolution_attempts,
            "max_symbol_resolution_attempts": MAX_SYMBOL_RESOLUTION_ATTEMPTS,
            "price_evidence_attempts": self._price_evidence_attempts,
            "max_price_evidence_attempts": MAX_PRICE_EVIDENCE_ATTEMPTS,
        }

    def ingest_tool_result(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: str,
        call_id: str,
        success: bool,
    ) -> None:
        """Consume the full untruncated tool result and persist its evidence.

        Args:
            tool_name: Executed tool name.
            arguments: Exact normalized tool arguments.
            result: Full raw result, before model-context truncation.
            call_id: Provider tool-call identity.
            success: Result-envelope success classification.
        """
        payload = _json_object(result)
        if not success:
            self._record_tool_failure(tool_name, call_id, result)
            if tool_name == _RESOLVER_TOOL:
                self._finish_failed_resolution(arguments, call_id)
            elif tool_name == "get_market_data" and payload is not None and payload.get("_unresolved"):
                # A fetch where no symbol returned data is a failed call, but
                # each symbol is still recorded as unavailable, as it was
                # when this envelope counted as a success.
                self._ingest_market_data(arguments, payload, call_id)
            self.persist()
            return

        self._track_session_symbols(arguments, result)
        self._track_factor_and_retrieval(tool_name, payload, result, arguments, call_id)
        self._note_model_write(tool_name, arguments, payload)
        if tool_name in _ANALYSIS_TOOLS:
            self._ingest_analysis_result(tool_name, arguments, payload, call_id)
        if tool_name == _RESOLVER_TOOL:
            self._ingest_resolution(arguments, payload, call_id)
        elif tool_name == "get_market_data":
            self._ingest_market_data(arguments, payload, call_id)
        elif tool_name == "read_file" and payload is not None:
            self._ingest_engine_table(payload, call_id)
        elif tool_name == "read_run_artifact" and payload is not None:
            self._ingest_engine_table(payload, call_id, tool_name=tool_name)
        elif payload is not None:
            self._ingest_generic_numeric(tool_name, arguments, payload, call_id)
        self.persist()

    @property
    def _retrieval_calls(self) -> int:
        """Number of distinct successful retrieval calls (by call id) still on record."""
        return len({record["call_id"] for record in self._retrieval_records})

    def _track_factor_and_retrieval(
        self,
        tool_name: str,
        payload: Mapping[str, Any] | None,
        result: str,
        arguments: Mapping[str, Any],
        call_id: str,
    ) -> None:
        """Note which factor-library ids a succeeding call returned and count retrievals."""
        if tool_name in _FACTOR_TOOLS:
            self._factor_ids.update(ids_in_text(result))
        elif tool_name == "backtest" and payload is not None:
            screen = payload.get("research_screen")
            facts = screen.get("facts") if isinstance(screen, Mapping) else None
            used = facts.get("zoo_factors_used") if isinstance(facts, Mapping) else None
            if isinstance(used, list):
                self._factor_ids.update(str(x) for x in used if isinstance(x, str))
        # A document the model wrote itself is no source.
        if tool_name == "read_document" and self._is_model_written(Path(str(arguments.get("file_path") or ""))):
            return
        if tool_name in _RETRIEVAL_TOOLS:
            self._upsert_retrieval_record(retrieval_record(call_id, tool_name, arguments, result))

    def _upsert_retrieval_record(self, record: dict[str, Any]) -> None:
        """Add ``record`` as the newest, merging into an earlier record of the same call id; keep the newest 60."""
        for index, old in enumerate(self._retrieval_records):
            if old["call_id"] == record["call_id"]:
                del self._retrieval_records[index]
                merged = {(float(v), bool(p)): None for v, p in [*old["numbers"], *record["numbers"]]}
                record = {
                    **record,
                    "sources": list(dict.fromkeys([*old["sources"], *record["sources"]]))[:_MAX_SOURCES_PER_CALL],
                    "numbers": [[v, p] for v, p in list(merged)[:_MAX_NUMBERS_PER_CALL]],
                }
                break
        self._retrieval_records.append(record)
        del self._retrieval_records[:-_MAX_RETRIEVAL_RECORDS]

    def validate_final_answer(self, content: str) -> ValidationResult:
        """Validate identity assertions and numeric price claims.

        Args:
            content: Candidate assistant answer.

        Returns:
            A deterministic validation result. A record containing the answer
            hash, structured issues, and the names of the declared checks that
            fired is appended to the artifact.
        """
        return self._validate(content, record=True)

    def revalidate(self, content: str) -> ValidationResult:
        """Validate text WITHOUT counting it as a rejected draft.

        For the recheck that follows a deterministic repair or redaction. No
        model round produced that text, so recording it would count a
        rejected draft nobody wrote and add an artifact attempt no draft
        stands behind.

        Args:
            content: The repaired or redacted answer.

        Returns:
            A deterministic validation result.
        """
        return self._validate(content, record=False)

    def _validate(self, content: str, *, record: bool) -> ValidationResult:
        """Run the gate, optionally without recording the attempt.

        ``validation_count`` is the run's checked-DRAFT count: the degraded-run
        reason prints it and the ``grounding_status`` round follows it (the
        revision cap itself is counted in ``loop.py``). The release path's own
        rechecks are not drafts, so they pass ``record=False``.

        Args:
            content: Candidate assistant answer.
            record: Whether to append the attempt to the persisted ledger.

        Returns:
            A deterministic validation result.
        """
        self._ingest_run_dir_ohlc_csvs()
        block = parse_figures_block(content)
        figures = scan_figures(content, block)
        issues: list[dict[str, Any]] = []
        # Free-text heuristics (cited figures, factor names, verdict words) add card notes, never issues.
        self._card_notes = []
        issues.extend(self._validate_identity(content))
        issues.extend(self._validate_unsourced_symbols(content, figures, block))
        issues.extend(self._validate_figures(content, block, figures))
        self._card_notes.extend(self._validate_factor_names(content))
        self._card_notes.extend(self._validate_verdict_words(content))
        self._card_notes.extend(self._validate_card_imitation(content))
        issues = self._dedupe_issues(issues)
        result = ValidationResult(
            valid=not issues,
            issues=issues,
            released_text=strip_figures_block(content, block),
            passed_figures=_passed_figures(figures, issues),
            card_notes=tuple(dict.fromkeys(self._card_notes)),
        )
        if not record:
            return result
        self._validations.append(
            {
                "attempt": len(self._validations) + 1,
                "checked_at": _utc_now(),
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "valid": result.valid,
                "issues": issues,
                "card_notes": [list(note) for note in result.card_notes],
                # Names let a run card say which declared checks fired without
                # re-running the gate; inline rules carry codes only until migrated.
                "fired_checks": GROUNDING_CHECKS.names_for_codes(
                    {str(issue.get("code") or "") for issue in issues}
                ),
                "figures_block": block.raw,
            }
        )
        self.persist()
        return result

    def persist(self) -> None:
        """Atomically persist the current structured ledger (and its session copy)."""
        payload = {
            "schema_version": 1,
            "updated_at": _utc_now(),
            "identity": self.identity_summary(),
            "session_symbols": sorted(self._session_symbols),
            "session_symbol_roots": sorted(self._session_symbol_roots),
            "factor_ids": sorted(self._factor_ids),
            "retrieval_calls": self._retrieval_calls,
            "retrieval_records": list(self._retrieval_records),
            "evidence": [asdict(record) for record in self._evidence],
            "tool_failures": list(self._tool_failures),
            "analysis_completed": list(self._analysis_completed),
            "analysis_evidence": list(self._analysis_metrics),
            "validations": list(self._validations),
            "released": dict(self._released) if self._released else None,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        # Grounding decisions remain in memory; a read-only/broken artifact
        # directory must not crash the agent's error path.
        targets = [self.run_dir / "artifacts" / GROUNDING_ARTIFACT]
        if self.session_copy is not None:
            targets.append(self.session_copy)
        for path in targets:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._write_atomic(path, text)
            except OSError:
                continue

    @staticmethod
    def _write_atomic(path: Path, text: str) -> None:
        """Write ``text`` to ``path`` through a temp file and an atomic replace."""
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)
