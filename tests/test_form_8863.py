"""Phase 10, unit 3c-2: Form 8863, the American opportunity and lifetime
learning credits (2025 Form 8863 and its instructions), from the typed
students and their 1098-T.

Oracles: the instructions' own examples. Adjusted Qualified Education
Expenses, Example 2: $5,000 of expenses with $1,000 of a Pell grant applied
to them leaves $4,000, so the refundable credit is $1,000 and the
nonrefundable credit up to $1,500 (Example 1: the whole $5,000 grant applied
leaves $0 and no credit). Recapture, Example: $8,000 of tuition is a $1,600
lifetime learning credit, refigured on $6,600 as $1,320. The rest is the
form's arithmetic: modified AGI $85,000 single is halfway through the
$80,000-$90,000 phase-out (line 6 0.5), and a filer the line 7 conditions
cover gets no refundable part. Synthetic households only."""

# ruff: noqa: E501  (fixture lines mirror the printed forms verbatim)

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from planner.engine import tax
from planner.engine.household import Dependent, Household, Student
from planner.ingest.needs import enter, need_for, needed, parse_value
from planner.ingest.pdf import load_templates, parse_texts
from planner.paths import Layout
from planner.plan import inputs
from planner.taxprep import draft, expected

YEAR = 2025
EDU = (
    "american_opportunity_credit",
    "refundable_american_opportunity_credit",
    "non_refundable_american_opportunity_credit",
    "lifetime_learning_credit",
    "education_credit_phase_out",
)
TEMPLATES = load_templates(
    Path(__file__).resolve().parent.parent / "templates" / "forms"
)


def _values(**kw: object) -> dict[str, float]:
    base: dict[str, object] = {
        "age": 40,
        "filing_status": "SINGLE",
        "state": "TX",
        "wages": 60_000,
    }
    return tax.values(YEAR, Household(**(base | kw)), EDU)  # type: ignore[arg-type]


def test_a_pell_grant_applied_leaves_4000_and_a_1000_refund() -> None:
    """i8863 Adjusted Qualified Education Expenses, Example 2."""
    v = _values(
        filing_status="HEAD_OF_HOUSEHOLD",
        dependents=(Dependent(age=18, full_time_student=True),),
        students=(Student("d1", 5_000 - 1_000, "aotc"),),
    )
    assert v["refundable_american_opportunity_credit"] == pytest.approx(1_000)
    assert v["non_refundable_american_opportunity_credit"] == pytest.approx(1_500)


def test_the_lifetime_learning_credit_is_a_fifth_of_tuition() -> None:
    """i8863 Recapture example: $8,000 is $1,600; refigured on $6,600, $1,320."""
    for paid, credit in ((8_000, 1_600), (6_600, 1_320)):
        v = _values(students=(Student("p", paid, "llc"),))
        assert v["lifetime_learning_credit"] == pytest.approx(credit)
        assert v["american_opportunity_credit"] == 0


def test_the_phase_out_and_the_line_7_bar() -> None:
    v = _values(wages=85_000, students=(Student("p", 4_000, "aotc"),))
    assert v["education_credit_phase_out"] == pytest.approx(0.5)
    assert v["american_opportunity_credit"] == pytest.approx(1_250)
    v = _values(
        age=20, students=(Student("p", 4_000, "aotc"),), aotc_refundable_barred=True
    )
    assert v["refundable_american_opportunity_credit"] == 0
    assert v["non_refundable_american_opportunity_credit"] == pytest.approx(2_500)


@pytest.mark.parametrize(
    "students",
    [
        (Student("s", 1_000, "llc"),),  # no spouse on a single return
        (Student("d1", 1_000, "llc"),),  # no dependents
        (Student("p", 1_000, "llc"), Student("p", 1_000, "aotc")),
        (Student("p", 1_000, "hope"),),
        (Student("p", -1, "llc"),),
    ],
)
def test_a_student_the_return_lacks_is_refused(students: tuple[Student, ...]) -> None:
    with pytest.raises(ValueError, match="student"):
        Household(age=40, filing_status="SINGLE", state="TX", students=students)


def test_the_education_answer() -> None:
    need = need_for("education")
    assert need.asked is None and need.doc == "f1098t"
    assert parse_value(need, "dependent 1 6,500 aid $1,500 AOTC; you 3000 llc") == [
        {"student": "dependent 1", "paid": 6_500, "aid": 1_500, "credit": "aotc"},
        {"student": "you", "paid": 3_000, "aid": 0, "credit": "llc"},
    ]
    assert parse_value(need, "none") == []
    for bad, why in (
        ("you 3000", "not who, paid"),
        ("you 1 llc; you 2 aotc", "named twice"),
        ("dependent 0 5 llc", "count from 1"),
        ("spouse 500 aid llc", "not who, paid"),
    ):
        with pytest.raises(ValueError, match=why):
            parse_value(need, bad)
    bar = need_for("aotc_refundable_barred").asked
    assert bar is not None
    aotc = [{"student": "you", "paid": 1, "aid": 0, "credit": "aotc"}]
    assert bar({"filing_status": "single", "education": aotc})
    assert not bar({"filing_status": "married_joint", "education": aotc})
    assert not bar(
        {"filing_status": "single", "education": [aotc[0] | {"credit": "llc"}]}
    )
    assert not bar({"filing_status": "single", "education": []})


def _home(home: Path, education: str, **extra: str) -> Layout:
    lay = Layout(home)
    lay.ensure()
    for key, text in {
        "birth_date": "1980-04-01",
        "filing_status": "head_of_household",
        "state": "NC",
        "wages": "60,000",
        "se_income": "0",
        "ordinary_dividends": "0",
        "qualified_dividends": "0",
        "dependents": "2007-03-01 student",
        "care_expenses": "0",
        "dependent_care_benefits": "0",
        "education": education,
        **extra,
    }.items():
        enter(lay, YEAR, key, text)
    return lay


def test_the_draft_carries_form_8863(planner_home: Path) -> None:
    """Example 2 on a drafted return: Part III, Part I, the Credit Limit
    Worksheet, Schedule 3 line 3 and 1040 line 29."""
    lay = _home(planner_home, "dependent 1 5000 aid 1000 aotc")
    states = {s.need.key: s.state for s in needed(lay, YEAR).items}
    assert states["aotc_refundable_barred"] == "missing"
    enter(lay, YEAR, "aotc_refundable_barred", "no")
    d = draft.build(lay, YEAR)
    assert [d.get("8863", f"S1-{n}") for n in (27, 28, 29, 30)] == [
        4_000,
        2_000,
        500,
        2_500,
    ]
    assert d.get("8863", "2") == 90_000 and d.get("8863", "3") == 60_000
    assert d.get("8863", "6") == 1 and d.get("8863", "7") == 2_500
    assert d.get("8863", "8") == pytest.approx(1_000)
    assert d.get("8863", "9") == pytest.approx(1_500)
    assert d.get("8863", "19") == pytest.approx(1_500)
    assert d.get("Sch 3", "3") == pytest.approx(1_500)
    assert d.get("1040", "29") == pytest.approx(1_000)
    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes
    assert "Form 8863 (education credits)" in draft.render(d)


def test_the_draft_lifetime_learning_and_a_spent_grant(planner_home: Path) -> None:
    lay = _home(
        planner_home,
        "you 8000 llc; dependent 1 5000 aid 5000 aotc",  # Example 1: no credit
        aotc_refundable_barred="no",
    )
    d = draft.build(lay, YEAR)
    assert d.get("8863", "S1-31") == 8_000
    assert d.get("8863", "S2-30") == 0 and d.get("8863", "8") == 0
    assert d.get("8863", "12") == pytest.approx(1_600)
    assert d.get("8863", "18") == pytest.approx(1_600)
    assert d.get("8863", "19") == pytest.approx(1_600)
    assert d.get("1040", "29") == 0
    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes


def test_the_education_answer_is_a_tax_item(planner_home: Path) -> None:
    lay = Layout(planner_home)
    lay.ensure()
    for key, text in (
        ("birth_date", "1980-04-01"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("wages", "60,000"),
    ):
        enter(lay, YEAR, key, text)
    assert "education" in inputs.build(lay, YEAR).tax_unknown
    enter(lay, YEAR, "education", "none")
    assert "education" not in inputs.build(lay, YEAR).tax_unknown
    assert not [ln for ln in draft.build(lay, YEAR).lines if ln.form == "8863"]


def test_a_student_not_on_the_return_is_a_note(planner_home: Path) -> None:
    lay = _home(planner_home, "spouse 4000 aotc; dependent 2 4000 llc")
    enter(lay, YEAR, "aotc_refundable_barred", "no")
    got = inputs.build(lay, YEAR)
    assert got.household.students == ()
    assert sum("is not on this return" in n for n in got.notes) == 2


def test_separate_filers_take_no_education_credit(planner_home: Path) -> None:
    lay = _home(planner_home, "you 4000 aotc", filing_status="married_separate")
    enter(lay, YEAR, "aotc_refundable_barred", "no")
    d = draft.build(lay, YEAR)
    assert d.get("Sch 3", "3") == 0 and d.get("1040", "29") == 0
    assert any("filing separately cannot take" in n for n in d.notes)


@pytest.mark.parametrize(
    ("answer", "due"), [("dependent 1 5000 aid 1000 aotc", True), ("none", False)]
)
def test_a_student_expects_a_1098_t(planner_home: Path, answer: str, due: bool) -> None:
    lay = _home(planner_home, answer)
    by = {
        (e.form, e.issuer)
        for e in expected.inventory(lay, YEAR, date(2026, 1, 5)).items
    }
    assert (("1098-T", "each school") in by) is due


def test_the_1098_t_and_w2_box_10_templates() -> None:
    page = "\n".join(
        [
            "Form 1098-T Tuition Statement 2025",
            "FILER'S name, street address, city or town: Example State University (synthetic)",
            "1 Payments received for qualified tuition and related expenses $ 6,500.00",
            "5 Scholarships or grants $ 1,500.00",
        ]
    )
    (f,) = parse_texts([page], TEMPLATES)
    assert (f.form, f.tax_year) == ("1098-T", 2025)
    assert f.issuer == "Example State University (synthetic)"
    assert f.boxes["1"][1] == 6_500.0 and f.boxes["5"][1] == 1_500.0
    w2 = "\n".join(
        [
            "Form W-2 Wage and Tax Statement 2025",
            "c Employer's name, address, and ZIP code: Example Employer Inc (synthetic)",
            "1 Wages, tips, other compensation $ 30,000.00",
            "2 Federal income tax withheld $ 2,400.00",
            "10 Dependent care benefits $ 2,000.00",
        ]
    )
    (w,) = parse_texts([w2], TEMPLATES)
    assert w.boxes["10"][1] == 2_000.0
