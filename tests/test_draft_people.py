"""Phase 10 unit 3a-3: the draft lays out the spouse and the dependents.
Synthetic 2025 households only: Schedule 1-A line 36b takes a spouse 65 or
over; Schedule 8812 counts the children under 17 and the other dependents
(2025 Schedule 8812 lines 4-12 and 16a-17: $2,200 a child, $500 another
dependent, cut $50 a $1,000 over $200,000 or $400,000 joint, $1,700 a child
refundable)."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.ingest.needs import enter
from planner.paths import Layout
from planner.taxprep import draft

YEAR = 2025


def _lay(home: Path, **typed: str) -> Layout:
    lay = Layout(home)
    lay.ensure()
    base = {
        "state": "NC",
        "se_income": "0",
        "ordinary_dividends": "0",
        "qualified_dividends": "0",
        "dependents": "none",
    }
    for key, text in {**base, **typed}.items():
        enter(lay, YEAR, key, text)
    return lay


def _checks(d: draft.Draft) -> list[str]:
    return [n for n in d.notes if n.startswith("CHECK")]


def _need(d: draft.Draft, form: str, line: str) -> float:
    value = d.get(form, line)
    assert value is not None, (form, line)
    return value


def _joint_seniors(home: Path, wages: str, spouse: str = "1958-04-01") -> Layout:
    return _lay(
        home,
        birth_date="1955-03-01",
        filing_status="married_joint",
        spouse_birth_date=spouse,
        wages=wages,
    )


def test_line_36b_takes_the_spouse_65_or_over(planner_home: Path) -> None:
    d = draft.build(_joint_seniors(planner_home, "100,000"), YEAR)
    assert _checks(d) == [], d.notes
    assert _need(d, "Sch 1-A", "35") == 6_000.0
    assert _need(d, "Sch 1-A", "36a") == 6_000.0
    assert _need(d, "Sch 1-A", "36b") == 6_000.0
    assert _need(d, "Sch 1-A", "37") == 12_000.0
    assert not any("36b" in n and "not drafted" in n for n in d.notes)


def test_each_spouse_amount_is_cut_on_its_own(planner_home: Path) -> None:
    # 2025 Schedule 1-A: 6% of the MAGI over $150,000 comes off each $6,000
    d = draft.build(_joint_seniors(planner_home, "200,000"), YEAR)
    assert _checks(d) == [], d.notes
    assert _need(d, "Sch 1-A", "34") == 3_000.0
    assert _need(d, "Sch 1-A", "36a") == _need(d, "Sch 1-A", "36b") == 3_000.0
    assert _need(d, "Sch 1-A", "37") == 6_000.0


def test_a_spouse_alone_65_drafts_line_36b(planner_home: Path) -> None:
    lay = _lay(
        planner_home,
        birth_date="1970-03-01",
        filing_status="married_joint",
        spouse_birth_date="1958-04-01",
        wages="100,000",
    )
    d = draft.build(lay, YEAR)
    assert _checks(d) == [], d.notes
    assert _need(d, "Sch 1-A", "36a") == 0.0
    assert _need(d, "Sch 1-A", "36b") == 6_000.0
    assert _need(d, "Sch 1-A", "37") == 6_000.0


def test_no_spouse_birth_date_says_line_36b_waits_on_it(planner_home: Path) -> None:
    lay = _lay(
        planner_home,
        birth_date="1955-03-01",
        filing_status="married_joint",
        wages="100,000",
    )
    d = draft.build(lay, YEAR)
    assert d.get("Sch 1-A", "36b") is None
    assert any("36b" in n and "spouse_birth_date" in n for n in d.notes), d.notes


def test_schedule_8812_counts_children_and_other_dependents(
    planner_home: Path,
) -> None:
    lay = _lay(
        planner_home,
        birth_date="1980-03-01",
        filing_status="married_joint",
        spouse_birth_date="1982-04-01",
        dependents="2015-05-01; 2006-03-01 student",
        wages="120,000",
    )
    d = draft.build(lay, YEAR)
    assert _checks(d) == [], d.notes
    assert _need(d, "Sch 8812", "4") == 1.0
    assert _need(d, "Sch 8812", "5") == 2_200.0
    assert _need(d, "Sch 8812", "6") == 1.0
    assert _need(d, "Sch 8812", "7") == 500.0
    assert _need(d, "Sch 8812", "8") == 2_700.0
    assert _need(d, "Sch 8812", "9") == 400_000.0
    assert d.get("Sch 8812", "10") is None
    assert _need(d, "Sch 8812", "12") == 2_700.0
    assert _need(d, "Sch 8812", "14") == 2_700.0 == _need(d, "1040", "19")
    dependents = next(n for n in d.notes if n.startswith("1040 dependents"))
    assert "age 10" in dependents and "age 19" in dependents
    assert "credit for other dependents" in dependents


def test_schedule_8812_phase_out_rounds_up_to_whole_thousands(
    planner_home: Path,
) -> None:
    lay = _lay(
        planner_home,
        birth_date="1980-03-01",
        filing_status="head_of_household",
        dependents="2015-05-01",
        wages="229,500",
    )
    d = draft.build(lay, YEAR)
    assert _checks(d) == [], d.notes
    assert _need(d, "Sch 8812", "9") == 200_000.0
    assert _need(d, "Sch 8812", "10") == 30_000.0  # 29,500 rounded up
    assert _need(d, "Sch 8812", "11") == 1_500.0
    assert _need(d, "Sch 8812", "12") == 700.0


def test_schedule_8812_refundable_part(planner_home: Path) -> None:
    lay = _lay(
        planner_home,
        birth_date="1990-03-01",
        filing_status="head_of_household",
        dependents="2015-05-01, 2018-06-01",
        wages="20,000",
    )
    d = draft.build(lay, YEAR)
    assert _checks(d) == [], d.notes
    assert _need(d, "Sch 8812", "12") == 4_400.0
    assert _need(d, "Sch 8812", "14") == 0.0
    assert _need(d, "Sch 8812", "16a") == 4_400.0
    assert _need(d, "Sch 8812", "16b") == 3_400.0
    assert _need(d, "Sch 8812", "17") == 3_400.0
    # earned income 20,000 less 2,500, at 15%: under line 17, so it is the credit
    assert _need(d, "Sch 8812", "27") == pytest.approx(2_625.0)
    assert _need(d, "1040", "28") == pytest.approx(2_625.0)


def test_no_dependents_no_schedule_8812(planner_home: Path) -> None:
    d = draft.build(_joint_seniors(planner_home, "100,000"), YEAR)
    assert not [ln for ln in d.lines if ln.form == "Sch 8812"]
