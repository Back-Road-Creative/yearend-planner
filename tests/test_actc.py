"""Phase 10, unit 3c-5: Schedule 8812 lines 18a-27, the additional child tax
credit (2025 Schedule 8812 and its instructions: the Earned Income Chart, the
Earned Income Worksheet, Part II-B for three or more children). Oracles are
the form's own arithmetic on synthetic households: 15% of earned income over
$2,500 (line 20); with three or more children, W-2 boxes 4 and 6 plus
Schedule 1 line 15, less 1040 line 27a (lines 21-25); line 27 the smaller of
line 17 and line 20 or 26."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.engine import tax
from planner.ingest.needs import enter
from planner.ingest.pdf import load_templates, parse_texts
from planner.ledger import db
from planner.paths import Layout
from planner.taxprep import draft

YEAR = 2025
TEMPLATES = load_templates(
    Path(__file__).resolve().parent.parent / "templates" / "forms"
)
THREE = "2015-03-01, 2018-06-01, 2020-05-05"


def _home(home: Path, **extra: str) -> Layout:
    lay = Layout(home)
    lay.ensure()
    for key, text in {
        "birth_date": "1990-04-01",
        "filing_status": "head_of_household",
        "state": "NC",
        "wages": "10,000",
        "dependents": "2015-03-01, 2020-05-05",
        **extra,
    }.items():
        enter(lay, YEAR, key, text)
    return lay


def _checks(d: draft.Draft) -> list[str]:
    return [n for n in d.notes if n.startswith("CHECK")]


def _source(d: draft.Draft, form: str, line: str) -> str:
    return next(ln.source for ln in d.lines if (ln.form, ln.line) == (form, line))


def test_the_printed_phase_in() -> None:
    assert tax.actc_phase_in(YEAR) == (2_500.0, 0.15, 3)


def test_part_ii_a_on_wages(planner_home: Path) -> None:
    """Two children, $10,000 of wages, no tax: line 17 is $3,400 (2 x $1,700),
    line 20 15% of $7,500, so line 27 is $1,125."""
    d = draft.build(_home(planner_home), YEAR)
    assert d.get("Sch 8812", "17") == 3_400
    assert d.get("Sch 8812", "18a") == 10_000
    assert "EIC Worksheet A" in _source(d, "Sch 8812", "18a")
    assert d.get("Sch 8812", "19") == 7_500
    assert d.get("Sch 8812", "20") == pytest.approx(1_125)
    assert d.get("Sch 8812", "21") is None
    assert d.get("Sch 8812", "27") == pytest.approx(1_125)
    assert d.get("1040", "28") == pytest.approx(1_125)
    assert not _checks(d), d.notes


def test_earned_income_at_the_threshold_refunds_nothing(planner_home: Path) -> None:
    d = draft.build(_home(planner_home, wages="2,000", dependents="2020-05-05"), YEAR)
    assert d.get("Sch 8812", "17") == 1_700
    assert d.get("Sch 8812", "19") is None and d.get("Sch 8812", "20") == 0
    assert d.get("Sch 8812", "27") == 0 and d.get("1040", "28") == 0
    assert not _checks(d), d.notes


def test_part_ii_b_without_the_eic(planner_home: Path) -> None:
    """Three children, $4,000 of wages and $13,000 of interest: no EIC (over
    $11,950 of investment income), so line 18a is the Earned Income Worksheet's
    $4,000, line 20 $225, and Part II-B's payroll tax ($4,000 x 7.65% = $306)
    is the larger: line 27 is $306."""
    lay = _home(planner_home, wages="4,000", interest="13,000", dependents=THREE)
    d = draft.build(lay, YEAR)
    assert d.get("1040", "27a") == 0
    assert d.get("Sch 8812", "17") == 5_100
    assert d.get("Sch 8812", "18a") == 4_000
    assert "Earned Income Worksheet" in _source(d, "Sch 8812", "18a")
    assert d.get("Sch 8812", "20") == pytest.approx(225)
    assert d.get("Sch 8812", "21") == pytest.approx(306)
    assert "W-2 boxes 4 and 6 not on file" in _source(d, "Sch 8812", "21")
    assert d.get("Sch 8812", "22") == 0 and d.get("Sch 8812", "24") == 0
    assert d.get("Sch 8812", "25") == pytest.approx(306)
    assert d.get("Sch 8812", "26") == pytest.approx(306)
    assert d.get("Sch 8812", "27") == pytest.approx(306)
    assert d.get("1040", "28") == pytest.approx(306)
    assert not _checks(d), d.notes


def test_part_ii_b_reads_w2_boxes_4_and_6(planner_home: Path) -> None:
    lay = _home(planner_home, wages="4,000", interest="13,000", dependents=THREE)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-w2-payroll",
        file_name="w2.pdf",
        kind="pdf",
        pages=1,
        batch="b1",
        facts=[
            db.Fact("W-2", YEAR, "Example Employer (synthetic)", box, box, value, 1)
            for box, value in (
                ("1", 4_000.0),
                ("3", 4_000.0),
                ("4", 248.0),
                ("6", 58.0),
            )
        ],
    )
    conn.close()
    d = draft.build(lay, YEAR)
    assert d.get("Sch 8812", "21") == 306
    assert "W-2 box 4" in _source(d, "Sch 8812", "21")
    assert d.get("Sch 8812", "27") == 306
    assert not _checks(d), d.notes


def test_part_ii_b_with_self_employment(planner_home: Path) -> None:
    """Three children, $4,000 of Schedule C profit and $13,000 of interest:
    Schedule SE line 4a is $3,694.00, line 12 $565.19 ($458.06 + $107.13) and
    line 13 $282.60, so line 18a is $3,717.40, line 20 $182.61, and line 22's
    $282.60 is the larger."""
    lay = _home(
        planner_home, wages="0", se_income="4,000", interest="13,000", dependents=THREE
    )
    d = draft.build(lay, YEAR)
    assert d.get("Sch 8812", "18a") == pytest.approx(3_717.40, abs=0.005)
    assert d.get("Sch 8812", "20") == pytest.approx(182.61, abs=0.005)
    assert d.get("Sch 8812", "21") == 0
    assert d.get("Sch 8812", "22") == pytest.approx(282.60, abs=0.005)
    assert d.get("Sch 8812", "27") == pytest.approx(282.60, abs=0.005)
    assert not _checks(d), d.notes


def test_line_20_over_line_17_skips_part_ii_b(planner_home: Path) -> None:
    lay = _home(planner_home, wages="50,000", dependents=THREE)
    d = draft.build(lay, YEAR)
    l17 = d.get("Sch 8812", "17")
    assert l17 is not None and 0 < l17 < 5_100
    assert d.get("Sch 8812", "20") == pytest.approx(7_125)
    assert d.get("Sch 8812", "21") is None
    assert d.get("Sch 8812", "27") == l17
    assert not _checks(d), d.notes


def test_the_w2_payroll_boxes() -> None:
    w2 = "\n".join(
        [
            "Form W-2 Wage and Tax Statement 2025",
            "c Employer's name, address, and ZIP code: Example Inc (synthetic)",
            "1 Wages, tips, other compensation $ 30,000.00",
            "2 Federal income tax withheld $ 2,400.00",
            "4 Social security tax withheld $ 1,860.00",
            "6 Medicare tax withheld $ 435.00",
        ]
    )
    (w,) = parse_texts([w2], TEMPLATES)
    assert w.boxes["4"][1] == 1_860.0
    assert w.boxes["6"][1] == 435.0
