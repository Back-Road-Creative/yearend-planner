from __future__ import annotations

from pathlib import Path

import pytest

from planner.engine.verify import verify_return

FIX = Path(__file__).parent / "fixtures"


@pytest.mark.engine
def test_synthetic_2025_return_verifies_line_by_line() -> None:
    report = verify_return(FIX / "2025_return.yaml", tolerance=1.0)
    bad = [line for line in report.lines if not line.ok]
    assert report.passed, bad
    assert report.year == 2025
    assert {line.name for line in report.lines} >= {"agi", "state_tax", "fed_total_tax"}


def test_unknown_filed_line_is_an_error(tmp_path: Path) -> None:
    p = tmp_path / "r.yaml"
    p.write_text(
        "year: 2025\nhousehold: {age: 50, filing_status: SINGLE, state: NC}\n"
        "filed: {line_99: 1}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown filed lines"):
        verify_return(p)
