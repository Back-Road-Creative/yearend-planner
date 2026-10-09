"""Phase 10, unit 3c-4: the earned income credit, 1040 line 27a, by the EIC
worksheet (A, or B with self-employment) and the EIC Table, and Schedule EIC
for each qualifying child (2025 Form 1040 instructions, line 27a; 2025
Schedule EIC).

Oracles: rows copied from the printed 2025 EIC Table (each $50 row, the
single/head of household/qualifying surviving spouse columns then the joint
columns, for 0, 1, 2 and 3 or more children; a starred cell is the table's
footnote), the footnotes' last rows, and the worksheets' own arithmetic.
Synthetic households only."""

# ruff: noqa: E501  (fixture lines mirror the printed table verbatim)

from __future__ import annotations

from pathlib import Path

import pytest

from planner.engine import tax
from planner.ingest.needs import enter
from planner.paths import Layout
from planner.taxprep import draft

YEAR = 2025
# Printed rows: (at least, [single 0-3 children, joint 0-3]); None is a star.
ROWS = [
    (1, [2, 9, 10, 11, 2, 9, 10, 11]),
    (2450, [189, 842, 990, 1114, 189, 842, 990, 1114]),
    (8000, [614, 2729, 3210, 3611, 614, 2729, 3210, 3611]),
    (8450, [649, 2882, 3390, 3814, 649, 2882, 3390, 3814]),
    (10600, [649, 3613, 4250, 4781, 649, 3613, 4250, 4781]),
    (11150, [607, 3800, 4470, 5029, 649, 3800, 4470, 5029]),
    (12700, [488, 4328, 5090, 5726, 649, 4328, 5090, 5726]),
    (15000, [312, 4328, 6010, 6761, 649, 4328, 6010, 6761]),
    (17700, [105, 4328, 7090, 7976, 649, 4328, 7090, 7976]),
    (17850, [94, 4328, 7152, 8046, 638, 4328, 7152, 8046]),
    (20000, [0, 4328, 7152, 8046, 473, 4328, 7152, 8046]),
    (22000, [0, 4328, 7152, 8046, 320, 4328, 7152, 8046]),
    (23350, [0, 4324, 7147, 8041, 217, 4328, 7152, 8046]),
    (25000, [0, 4060, 6799, 7693, 91, 4328, 7152, 8046]),
    (26200, [0, 3869, 6547, 7441, None, 4328, 7152, 8046]),
    (30450, [0, 3189, 5651, 6545, 0, 4328, 7152, 8046]),
    (40000, [0, 1663, 3640, 4534, 0, 2801, 5140, 6034]),
    (50400, [0, None, 1450, 2344, 0, 1139, 2949, 3843]),
    (57300, [0, 0, None, 891, 0, 37, 1496, 2390]),
    (61500, [0, 0, 0, 6, 0, 0, 612, 1506]),
    (68600, [0, 0, 0, 0, 0, 0, 0, 11]),
]
# The footnotes' last rows: (from, below, children, joint, credit), and the
# amount where each column's credit ends.
STARS = [
    (19_100, 19_104, 0, False, 0),
    (26_200, 26_214, 0, True, 1),
    (50_400, 50_434, 1, False, 3),
    (57_300, 57_310, 2, False, 1),
    (57_550, 57_554, 1, True, 0),
    (61_550, 61_555, 3, False, 1),
    (64_400, 64_430, 2, True, 3),
    (68_650, 68_675, 3, True, 3),
]


@pytest.mark.parametrize(("low", "cells"), ROWS)
def test_the_eic_table_is_the_printed_one(low: int, cells: list[int | None]) -> None:
    high = 50 if low == 1 else low + 50
    for i, want in enumerate(cells):
        if want is None:
            continue
        for amount in (low, high - 1, low + 0.5):
            assert tax.eic_table(YEAR, i % 4, i >= 4, amount) == want, (low, i, amount)


@pytest.mark.parametrize(("low", "end", "kids", "joint", "credit"), STARS)
def test_the_eic_table_footnotes(
    low: int, end: int, kids: int, joint: bool, credit: int
) -> None:
    assert tax.eic_table(YEAR, kids, joint, low) == credit
    assert tax.eic_table(YEAR, kids, joint, end - 1) == credit
    assert tax.eic_table(YEAR, kids, joint, end) == 0
    assert tax.eic_table(YEAR, 5, joint, 100) == tax.eic_table(YEAR, 3, joint, 100)
    assert tax.eic_table(YEAR, kids, joint, 0.5) == 0


def test_the_printed_limits() -> None:
    p = tax.eic_parameters(YEAR)
    assert (p["investment_income"], p["min_age"], p["max_age"]) == (11_950, 25, 64)
    starts = [
        tax.eic_phaseout_start(YEAR, k, j) for j in (False, True) for k in (0, 1, 3)
    ]
    assert starts == [10_620, 23_350, 23_350, 17_730, 30_470, 30_470]


def _home(home: Path, **extra: str) -> Layout:
    lay = Layout(home)
    lay.ensure()
    for key, text in {
        "birth_date": "1990-04-01",
        "filing_status": "head_of_household",
        "state": "NC",
        "wages": "20,000",
        "dependents": "2020-05-05, 2015-03-01",
        **extra,
    }.items():
        enter(lay, YEAR, key, text)
    return lay


def _label(d: draft.Draft, form: str, line: str) -> str:
    return next(ln.label for ln in d.lines if (ln.form, ln.line) == (form, line))


def _checks(d: draft.Draft) -> list[str]:
    return [n for n in d.notes if n.startswith("CHECK")]


def test_worksheet_a_with_two_children(planner_home: Path) -> None:
    d = draft.build(_home(planner_home), YEAR)
    assert d.get("EIC", "A1") == 20_000 and d.get("EIC", "A3") == 20_000
    assert d.get("EIC", "A2") == 7_152 and d.get("EIC", "A6") == 7_152
    assert d.get("EIC", "A5") is None
    assert d.get("1040", "27a") == 7_152
    assert d.get("Sch EIC", "3-1") == 2020 and d.get("Sch EIC", "3-2") == 2015
    assert d.get("Sch EIC", "4a-1") is None
    assert not _checks(d), d.notes
    assert "Schedule EIC (qualifying child information)" in draft.render(d)


def test_agi_past_the_phaseout_start_is_looked_up(planner_home: Path) -> None:
    """$22,000 of wages and $3,000 of interest: line 2 is $4,328, line 5 on the
    $25,000 AGI $4,060, the smaller."""
    lay = _home(planner_home, wages="22,000", interest="3,000", dependents="2020-05-05")
    d = draft.build(lay, YEAR)
    assert d.get("EIC", "A2") == 4_328 and d.get("EIC", "A3") == 25_000
    assert d.get("EIC", "A5") == 4_060 and d.get("EIC", "A6") == 4_060
    assert d.get("1040", "27a") == 4_060
    assert not _checks(d), d.notes


def test_worksheet_b_for_the_self_employed(planner_home: Path) -> None:
    """No child, age 35, $12,000 of Schedule C profit: Schedule SE line 13 is
    $847.77, so earned income and AGI are $11,152.23, the $11,150 row's $607."""
    lay = _home(
        planner_home,
        filing_status="single",
        wages="0",
        se_income="12,000",
        dependents="none",
    )
    d = draft.build(lay, YEAR)
    assert d.get("EIC", "B1c") == 12_000 and d.get("EIC", "B1d") == pytest.approx(
        847.77
    )
    assert d.get("EIC", "B4b") == pytest.approx(11_152.23)
    assert d.get("EIC", "B7") == 607 and d.get("EIC", "B11") == 607
    assert d.get("EIC", "B10") is None
    assert d.get("1040", "27a") == 607
    assert not _checks(d), d.notes


def test_investment_income_over_the_limit(planner_home: Path) -> None:
    lay = _home(planner_home, wages="15,000", interest="12,000")
    d = draft.build(lay, YEAR)
    assert d.get("1040", "27a") == 0
    assert any("over $11,950 (Step 2)" in n for n in d.notes), d.notes
    assert not _checks(d), d.notes


def test_no_child_needs_25_to_64(planner_home: Path) -> None:
    """Step 4: born before January 2, 2001 (25 on the day before the birthday)."""
    young = _home(
        planner_home,
        filing_status="single",
        wages="8,000",
        dependents="none",
        birth_date="2001-01-02",
    )
    d = draft.build(young, YEAR)
    assert d.get("1040", "27a") == 0 and not _checks(d), d.notes
    d = draft.build(
        _home(
            planner_home,
            birth_date="2001-01-01",
            filing_status="single",
            wages="8,000",
            dependents="none",
        ),
        YEAR,
    )
    assert d.get("EIC", "A2") == 614 and d.get("1040", "27a") == 614
    assert not _checks(d), d.notes


def test_students_the_disabled_and_a_joint_return(planner_home: Path) -> None:
    """A 22-year-old student and a disabled 30-year-old qualify; a 19-year-old
    who is not a student does not: two children, the joint $40,000 row."""
    lay = _home(
        planner_home,
        filing_status="married_joint",
        birth_date="1980-02-02",
        wages="40,000",
        spouse_birth_date="1981-03-03",
        spouse_wages="0",
        dependents="2003-06-01 student, 2006-01-05, 1995-07-07 disabled",
    )
    d = draft.build(lay, YEAR)
    assert d.get("EIC", "A2") == 5_140 and d.get("1040", "27a") == 5_140
    assert d.get("Sch EIC", "3-1") == 2003 and d.get("Sch EIC", "3-2") == 1995
    assert "Yes" in _label(d, "Sch EIC", "4a-1")
    assert "No" in _label(d, "Sch EIC", "4a-2")
    assert "Yes" in _label(d, "Sch EIC", "4b-2")
    assert not _checks(d), d.notes
