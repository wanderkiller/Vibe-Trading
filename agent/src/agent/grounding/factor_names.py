"""Factor-library names in text: which ids a piece of prose or code claims to use.

Pure. A name counts as a factor reference when it is a literal registry id
(``gtja191_010``) or a paper-style spelling of one (``GTJA-191 Alpha 010``,
``Kakushadze Alpha 17``, ``Alpha#53``). The grounding gate compares the result
with what the session's factor tools returned; the engine screen compares it
with the ``zoo_factor`` calls in a signal engine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

#: A registry prefix shared by at least this many ids is a family name; shorter
#: ones (``fund_``) are ordinary words, so only their exact ids count.
_OPEN_PREFIX_MIN_IDS = 10
_MAX_GAP = 15


@dataclass(frozen=True)
class FactorRef:
    """One factor name found in text.

    Attributes:
        canonical: The registry id it names, lower case, e.g. ``gtja191_010``.
        raw: The text as written.
        span: ``(start, end)`` character offsets of ``raw``.
    """

    canonical: str
    raw: str
    span: tuple[int, int]


@lru_cache(maxsize=1)
def known_factor_ids() -> frozenset[str]:
    """Every alpha id of the bundled factor library."""
    from src.factors.registry import get_default_registry

    return frozenset(get_default_registry().list())


@lru_cache(maxsize=1)
def _literal_re() -> re.Pattern[str]:
    ids = known_factor_ids()
    counts: dict[str, int] = {}
    numbered: dict[str, int] = {}
    for alpha_id in ids:
        prefix, sep, _ = alpha_id.partition("_")
        if sep:
            counts[prefix + "_"] = counts.get(prefix + "_", 0) + 1
            if alpha_id[-1].isdigit():
                numbered[prefix + "_"] = numbered.get(prefix + "_", 0) + 1
    # A family is open (any suffix is a claimed id) only when its ids are numbered ("gtja191_010", "qlib158_beta10"); a prefix of
    # word-named ids ("academic_hml") is an ordinary word start ("academic_papers"), so only its exact ids count.
    open_prefixes = sorted(
        (p for p, n in counts.items() if n >= _OPEN_PREFIX_MIN_IDS and numbered.get(p, 0) * 2 > n),
        key=len,
        reverse=True,
    )
    exact = sorted((i for i in ids if not i.startswith(tuple(open_prefixes))), key=len, reverse=True)
    parts = [re.escape(p) + r"[a-z0-9_]+" for p in open_prefixes] + [re.escape(i) for i in exact]
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.IGNORECASE)


_GTJA_RE = re.compile(
    rf"(?:GTJA|国泰君安)[\s\-\u2010-\u2015_]?191(?!\d)(?P<gap>[^\d]{{0,{_MAX_GAP}}})(?P<num>\d{{1,3}})(?!\d)", re.IGNORECASE
)
_ALPHA101_RE = re.compile(
    rf"(?:Kakushadze|WorldQuant|Alpha[\s\-_]?101)(?P<gap>[^\d]{{0,{_MAX_GAP}}})"
    rf"(?:101(?!\d)[^\d]{{0,{_MAX_GAP}}})?\s*(?P<num>\d{{1,3}})(?!\d)",
    re.IGNORECASE,
)
_ALPHA_HASH_RE = re.compile(r"\bAlpha\s?[#\uff03]\s?(?P<num>\d{1,3})(?!\d)", re.IGNORECASE)
# A bare number is a factor id only when an id marker sits right before it ("#10", "No.10", "第10", "_10", "-10", "Alpha 10").
_ID_MARKER = re.compile(r"(?:[#\uff03_\-\u2013]|\bno\.|第|(?<![a-z])alpha)\s*$", re.IGNORECASE)
# ... and never when a counting or measuring word follows it ("40 个因子", "12 bps", "5 regions").
_COUNT_UNIT = re.compile(r"\s*(?:个|种|项|bps|bp|%|区域|regions|factors|alphas)", re.IGNORECASE)


def _is_marked_id(text: str, between: str, num_end: int) -> bool:
    """Whether the number ending at ``num_end``, preceded by ``between``, is written as a factor id."""
    return _ID_MARKER.search(between) is not None and _COUNT_UNIT.match(text, num_end) is None


def _overlaps(span: tuple[int, int], taken: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in taken)


def scan_factor_refs(text: str) -> list[FactorRef]:
    """Factor-library names written in ``text``, in document order.

    Args:
        text: Prose or source code (comments included).

    Returns:
        One :class:`FactorRef` per name; overlapping spellings count once. A
        number that is the size of a family ("GTJA-191 has 191 factors",
        "WorldQuant Alpha 101") is not an id unless marked as one.
    """
    refs: list[FactorRef] = []
    taken: list[tuple[int, int]] = []

    def add(canonical: str, match: re.Match[str]) -> None:
        span = (match.start(), match.end())
        if _overlaps(span, taken):
            return
        taken.append(span)
        refs.append(FactorRef(canonical=canonical, raw=match.group(0), span=span))

    for match in _literal_re().finditer(text):
        add(match.group(0).lower().rstrip("_"), match)
    for match in _GTJA_RE.finditer(text):
        num = int(match.group("num"))
        gap = match.group("gap")
        if not _is_marked_id(text, gap, match.end("num")):
            continue
        if num == 191 and "#" not in gap and "alpha" not in gap.lower():
            continue
        add(f"gtja191_{num:03d}", match)
    for match in _ALPHA101_RE.finditer(text):
        num = int(match.group("num"))
        between = text[match.start("gap"):match.start("num")]
        if not _is_marked_id(text, between, match.end("num")):
            continue
        if num == 101 and "#" not in between:
            continue
        add(f"alpha101_{num:03d}", match)
    for match in _ALPHA_HASH_RE.finditer(text):
        if _COUNT_UNIT.match(text, match.end("num")):
            continue
        add(f"alpha101_{int(match.group('num')):03d}", match)
    refs.sort(key=lambda ref: ref.span)
    return refs


def ids_in_text(text: str) -> set[str]:
    """Registry ids that appear in ``text`` as a whole token (letters, digits, underscore)."""
    return set(re.findall(r"[a-z0-9_]+", text.lower())) & known_factor_ids()
