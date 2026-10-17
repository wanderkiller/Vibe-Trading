"""Research verdict words in prose: "已验证", "可上实盘", "risk-free", "validated" ...

Pure. The verdict of a backtest is carried by the system result card (screen status from the trial ledger); a model sentence
that states one on its own is only allowed when a backtest of the session passed the screen. This module finds the claims;
the grounding gate decides. A negated phrase ("未验证", "not validated") is not a claim. The figures block and the system
result card (appended by the system, not written by the model) are not read.
"""

from __future__ import annotations

import re

from src.agent.grounding.figures import parse_figures_block

_ZH_PHRASES = (
    "已验证", "验证通过", "通过验证", "可以上实盘", "可上实盘", "可以上线", "可上线",
    "风险真正得到控制", "风险得到有效控制", "风险已得到控制", "无风险", "稳健正收益", "稳健的正收益", "已被证实",
)
_ZH_RE = re.compile("|".join(re.escape(p) for p in sorted(_ZH_PHRASES, key=len, reverse=True)))
_EN_RE = re.compile(
    r"\b(?:validated|proven\s+edge|ready\s+for\s+live|production[- ]ready|risk[- ]free)\b", re.IGNORECASE
)
_ZH_NEG = ("不", "没", "未", "无法", "尚未", "没有", "并未", "不能", "不可")
_EN_NEG = re.compile(r"\b(?:not|never|no|cannot)\b|n't", re.IGNORECASE)
_ZH_WINDOW = 3
_EN_WINDOW = 12
_SENTENCE_END = ".!?;,:。！？；，：\n"
# Line starts of the system result card (``alphakeel_research_ledger._TEXT`` / ``_ENGINE_TEXT`` titles).
_CARD_TITLES = ("**研究结果", "**通用引擎回测", "**Research result", "**Generic engine backtests")


def _before(text: str, start: int, width: int) -> str:
    """Up to ``width`` characters before ``start``, cut at the last sentence or clause end."""
    window = text[max(0, start - width):start]
    cut = max((window.rfind(c) for c in _SENTENCE_END), default=-1)
    return window[cut + 1:]


def _card_start(text: str) -> int | None:
    """Offset where the system result card begins: its ``---`` separator line, else its title line."""
    offset = 0
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.lstrip().startswith(_CARD_TITLES):
            if index > 0 and lines[index - 1].strip() == "---":
                return offset - len(lines[index - 1])
            return offset
        offset += len(line)
    return None


def scan_verdict_claims(text: str) -> list[tuple[str, tuple[int, int]]]:
    """Verdict phrases ``text`` states (not negated), as ``(phrase as written, (start, end))``."""
    if not text:
        return []
    block = parse_figures_block(text)
    skipped = list(block.spans or ((block.span,) if block.span else ()))
    card = _card_start(text)
    if card is not None:
        skipped.append((card, len(text)))
    found: list[tuple[str, tuple[int, int]]] = []
    for regex, width in ((_ZH_RE, _ZH_WINDOW), (_EN_RE, _EN_WINDOW)):
        for match in regex.finditer(text):
            start, end = match.span()
            if any(s <= start and end <= e for s, e in skipped):
                continue
            window = _before(text, start, width)
            negated = (
                any(n in window for n in _ZH_NEG) if regex is _ZH_RE else _EN_NEG.search(window) is not None
            )
            if not negated:
                found.append((match.group(0), (start, end)))
    return sorted(found, key=lambda item: item[1])
