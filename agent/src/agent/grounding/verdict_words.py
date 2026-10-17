"""Research verdict words in prose: "已验证", "可上实盘", "risk-free", "validated" ...

Pure. The verdict of a backtest is carried by the system result card (screen status from the trial ledger); a model sentence
that states one on its own is only allowed when a backtest of the session passed the screen. This module finds the claims;
the grounding gate decides. A negated phrase ("未验证", "not validated") is not a claim. The figures block is not read. The
system result card is appended AFTER grounding, so the draft never contains it and nothing in a draft is skipped on the
strength of a title the model wrote itself.
"""

from __future__ import annotations

import re

from src.agent.grounding.figures import parse_figures_block

_ZH_PHRASES = (
    "已验证", "验证通过", "通过验证", "经过验证", "可以上实盘", "可上实盘", "可以上线", "可上线",
    "风险真正得到控制", "风险得到有效控制", "风险已得到控制", "无风险", "稳健正收益", "稳健的正收益", "已被证实",
)
# "无风险利率" / "risk-free rate" name a finance quantity, not a claim that the strategy has no risk.
_ZH_RE = re.compile(
    "|".join(
        re.escape(p) + (r"(?!\s*(?:利率|收益|资产))" if p == "无风险" else "")
        for p in sorted(_ZH_PHRASES, key=len, reverse=True)
    )
)
_EN_RE = re.compile(
    r"\b(?:validated|verified|proven\s+edge|ready\s+for\s+live|production-ready"
    r"|risk-free(?!\s+(?:rate|return|asset|interest)))\b",
    re.IGNORECASE,
)
# U+2010..U+2015 and U+2212 to "-", one character each, so offsets stay valid.
_HYPHENS = {c: "-" for c in [*range(0x2010, 0x2016), 0x2212]}
# "不错 / 不仅 / 不但 / 不少 / 不断" are not negations.
_ZH_NEG_RE = re.compile(r"不(?![错仅但少断])|没|未|无法|尚未|并未")
_EN_NEG = re.compile(r"\b(?:not|never|no|cannot)\b|n't", re.IGNORECASE)
_ZH_WINDOW = 3
_EN_WINDOW = 60
_ZH_END = ".!?;,:。！？；，：\n"
# An English negation reaches across commas ("not yet fully, completely and thoroughly validated").
_EN_END = ".!?;:。！？；\n"


def _before(text: str, start: int, width: int, ends: str) -> str:
    """Up to ``width`` characters before ``start``, cut at the last sentence (or clause) end in ``ends``."""
    window = text[max(0, start - width):start]
    cut = max((window.rfind(c) for c in ends), default=-1)
    return window[cut + 1:]


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
            start, end = match.span()
            if any(s <= start and end <= e for s, e in skipped):
                continue
            window = _before(text, start, width, ends)
            negated = (_ZH_NEG_RE if regex is _ZH_RE else _EN_NEG).search(window) is not None
            if not negated:
                found.append((match.group(0), (start, end)))
    return sorted(found, key=lambda item: item[1])
