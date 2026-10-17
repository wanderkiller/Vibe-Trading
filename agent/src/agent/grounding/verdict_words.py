"""Research verdict words in prose: "已验证", "可上实盘", "risk-free", "validated" ...

Pure, and a heuristic: the verdict of a backtest is carried by the system result card (screen status from the trial ledger).
When no backtest of the session passed the screen, the card states that the verdict words the answer used are not a system
verdict; nothing is blocked, so a phrasing this list misses, or a sentence it misreads, only changes that card line. A negated phrase ("未验证", "not validated") is not a claim. The figures block is not read. The
system result card is appended AFTER grounding, so the draft never contains it and nothing in a draft is skipped on the
strength of a title the model wrote itself.
"""

from __future__ import annotations

import re

from src.agent.grounding.figures import parse_figures_block

_ZH_PHRASES = (
    "已验证", "验证通过", "通过验证", "经过验证的策略", "经过验证[，,]\\s*可", "可以上实盘", "可上实盘", "可以上线", "可上线",
    "风险真正得到控制", "风险得到有效控制", "风险已得到控制", "无风险", "稳健正收益", "稳健的正收益", "已被证实",
    "已经过验证", "已经验证", "已获验证",
)
# "无风险利率" names a finance quantity, not a claim that the strategy has no risk; only an immediately following
# 利率 / 收益率 / 资产 / 利息 excludes it ("无风险 收益稳定" is still a claim).
_ZH_RE = re.compile(
    "|".join(
        (p + r"(?!利率|收益率|资产|利息)" if p == "无风险" else p if "[" in p or "\\" in p else re.escape(p))
        for p in sorted(_ZH_PHRASES, key=len, reverse=True)
    )
)
# "verified" is a claim only as a passive state ("is verified"); "I verified the data" is an action. "risk-free rate",
# "risk-free asset" ... name a finance quantity.
_EN_RE = re.compile(
    r"\b(?:validated|(?:(?:is|are|was|were|been|be)\s+|['\u2019]s\s+)(?:(?:fully|now)\s+)?(?P<v>verified)|proven\s+edge|ready\s+for\s+live"
    r"|production[- ]ready|risk[- ]free(?!\s*(?:rate|asset|interest|bond|treasury)))\b",
    re.IGNORECASE,
)
# U+2010..U+2015 and U+2212 to "-", one character each, so offsets stay valid.
_HYPHENS = {c: "-" for c in [*range(0x2010, 0x2016), 0x2212]}
# "不错 / 不仅 / 不但 / 不少 / 不断" are not negations; neither is the 未 of 未来. 并非 / 绝非 are.
_ZH_NEG_RE = re.compile(r"不(?![错仅但少断])|没|未(?!来)|无法|尚未|并未|并非|绝非")
_EN_NEG = re.compile(r"\b(?:not|never|no|cannot)\b|n't", re.IGNORECASE)
# Phrases that contain a negation word and do not negate ("not only robust", "no doubt", "no leakage was found", "found no").
_EN_NOT_NEG = re.compile(
    r"\bnot\s+(?:only|just)\b|\bno\s+(?:doubt|question)\b|\bno\s+\w+\s+(?:was|were|is)\s+found\b|\bfound\s+no\b",
    re.IGNORECASE,
)
# A conjunction after the negation starts a new statement ("not overfit, so validated"); "yet" directly after "not" is part
# of the negation ("not yet validated"). "and" starts one only before a new subject ("not bad and the model is
# validated"); "not tested and validated" negates both.
_EN_CONJ = re.compile(
    r"\b(?:so|but|therefore|thus)\b|(?<!not\s)(?<!n't\s)\byet\b"
    r"|\band\s+(?:the|it|its|it's|this|that|these|those|we|our|they|their|i|he|she)\b",
    re.IGNORECASE,
)
_ZH_WINDOW = 3
_EN_WINDOW = 40
# A bracket, a slash or a spaced / doubled dash also ends the clause a negation reaches into ("not overfit - validated").
_ZH_END = ".!?;,:。！？；，：\n()（）/／"
_EN_END = ".!?;,:。！？；，：\n()（）/／"
_DASH_BREAK = re.compile(r"\s-+\s|-{2,}")


def _before(text: str, start: int, width: int, ends: str) -> str:
    """Up to ``width`` characters before ``start``, cut at the last sentence (or clause) end in ``ends``."""
    begin = max(0, start - width)
    floor = max(0, begin - 12)
    # Widen to the whole word the window starts in ("cannot", not "annot").
    while begin > floor and not text[begin - 1].isspace() and text[begin - 1] not in ends and text[begin].isascii():
        begin -= 1
    window = text[begin:start]
    cut = max((window.rfind(c) for c in ends), default=-1)
    window = window[cut + 1:]
    dashes = list(_DASH_BREAK.finditer(window))
    return window[dashes[-1].end():] if dashes else window


def _negated(window: str, zh: bool) -> bool:
    """Whether the clause fragment ``window`` before a phrase negates it."""
    if zh:
        return _ZH_NEG_RE.search(window) is not None
    for skip in _EN_NOT_NEG.finditer(window):
        window = window[:skip.start()] + " " * (skip.end() - skip.start()) + window[skip.end():]
    negations = list(_EN_NEG.finditer(window))
    return bool(negations) and _EN_CONJ.search(window, negations[-1].start()) is None


def scan_verdict_claims(text: str) -> list[tuple[str, tuple[int, int]]]:
    """Verdict phrases ``text`` states (not negated), as ``(phrase as written, (start, end))``."""
    if not text:
        return []
    text = text.translate(_HYPHENS)
    block = parse_figures_block(text)
    skipped = list(block.spans or ((block.span,) if block.span else ()))
    found: list[tuple[str, tuple[int, int]]] = []
    for regex, width, ends in ((_ZH_RE, _ZH_WINDOW, _ZH_END), (_EN_RE, _EN_WINDOW, _EN_END)):
        for match in regex.finditer(text):
            # "is verified" is reported (and its negation read) at "verified".
            start, end = match.span("v") if regex is _EN_RE and match.group("v") else match.span()
            if any(s <= start and end <= e for s, e in skipped):
                continue
            window = _before(text, start, width, ends)
            negated = _negated(window, regex is _ZH_RE)
            if not negated:
                found.append((text[start:end], (start, end)))
    return sorted(found, key=lambda item: item[1])
