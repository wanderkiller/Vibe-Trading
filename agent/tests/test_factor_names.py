"""Factor-library names in text: what ``scan_factor_refs`` recognises and what it leaves alone."""

from __future__ import annotations

import pytest

from src.agent.grounding.factor_names import known_factor_ids, scan_factor_refs

pytestmark = pytest.mark.unit


def _canon(text: str) -> list[str]:
    return [ref.canonical for ref in scan_factor_refs(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("GTJA-191 Alpha 010", ["gtja191_010"]),
        ("gtja191_002", ["gtja191_002"]),
        ("GTJA191 alpha 7", ["gtja191_007"]),
        ("Kakushadze Alpha 017", ["alpha101_017"]),
        ("Alpha#53", ["alpha101_053"]),
        ("WorldQuant Alpha 101 #17", ["alpha101_017"]),
        ("alpha101_033 and ACADEMIC_HML", ["alpha101_033", "academic_hml"]),
        ("a plain sentence about momentum, 0.5 Sharpe and a 191-day window", []),
        ("the GTJA-191 library has 191 factors; WorldQuant Alpha 101 is a paper", []),
        ("fund_flow is a variable name", []),
        ("WorldQuant 的 101 个因子中有 20 个用到成交量", []),
        ("GTJA191 里有 40 个因子依赖 vwap", []),
        ("WorldQuant BRAIN 有 5 个区域", []),
        ("WorldQuant reported 12 bps", []),
        ("WorldQuant has 5 regions and 20 factors", []),
        ("academic_papers and academic_notes", []),
        ("Alpha ＃53", ["alpha101_053"]),
        ("GTJA191 #10", ["gtja191_010"]),
        ("GTJA\u2013191 #10", ["gtja191_010"]),
        ("国泰君安191 第10号", ["gtja191_010"]),
        ("GTJA191 No.10", ["gtja191_010"]),
        ("Kakushadze no. 17", ["alpha101_017"]),
        ("qlib158_beta10 and qlib158_made_up", ["qlib158_beta10", "qlib158_made_up"]),
    ],
)
def test_scan_factor_refs(text: str, expected: list[str]) -> None:
    assert _canon(text) == expected


def test_a_ref_carries_its_raw_text_and_span() -> None:
    text = "see GTJA-191 Alpha 010 here"
    (ref,) = scan_factor_refs(text)
    assert ref.raw == "GTJA-191 Alpha 010" and text[ref.span[0] : ref.span[1]] == ref.raw


def test_known_factor_ids_come_from_the_registry() -> None:
    ids = known_factor_ids()
    assert {"gtja191_010", "alpha101_017", "academic_hml"} <= ids
    assert "alpha101_999" not in ids
