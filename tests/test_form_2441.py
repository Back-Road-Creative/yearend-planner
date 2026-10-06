"""Phase 10, unit 3c-1: Form 2441, the credit for child and dependent care
expenses and the dependent care benefits an employer paid (2025 Form 2441 and
its instructions).

Oracles: the instructions' own examples. Line 16: $2,000 of benefits in W-2
box 10 and $900 of care incurred, so line 12 is 2,000, line 16 900 and the
$1,100 left over is taxable (line 26, Form 1040 line 1e). Line 14: $5,000 set
aside, $4,950 of care incurred and reimbursed, $50 forfeited, so nothing is
taxable. The rest is the form's arithmetic worked by hand: line 30 is the
care paid less the benefits excluded (line 28), which the engine alone does
not take (it caps care at the $3,000 limit less the exclusion instead, so
$2,500 of care with $2,000 excluded is a $1,000 base there, $500 on the form).
Synthetic households only."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.engine import tax
from planner.engine.household import Dependent, Household, Person
from planner.ingest.needs import NEEDS, enter, need_for, needed
from planner.paths import Layout
from planner.taxprep import draft

YEAR = 2025


def _hoh(**kw: object) -> Household:
    base: dict[str, object] = {
        "age": 40,
        "filing_status": "HEAD_OF_HOUSEHOLD",
        "state": "TX",
        "wages": 40_000,
        "dependents": (Dependent(age=5),),
    }
    return Household(**(base | kw))  # type: ignore[arg-type]


def test_benefits_above_the_care_incurred_are_taxable() -> None:
    """i2441 Line 16 example: line 12 2,000, line 16 900, line 26 1,100."""
    hh, care = tax.dependent_care(
        YEAR, _hoh(care_expenses=900, dependent_care_benefits=2_000)
    )
    assert care is not None
    assert (care.line12, care.line15, care.line16, care.line17) == (
        2_000,
        2_000,
        900,
        900,
    )
    assert (care.line25, care.line26) == (900, 1_100)
    assert hh.wages == 41_100  # line 26 goes on 1040 line 1e
    assert hh.tax_unit_inputs["dependent_care_assistance_exclusion"] == 900
    assert hh.care_expenses == 0  # line 30: none of the care is left to claim
    assert tax.compute(YEAR, _hoh(care_expenses=900, dependent_care_benefits=2_000))


def test_a_forfeited_amount_is_not_taxable() -> None:
    """i2441 Line 14 example: $5,000 in box 10, $4,950 incurred, $50 forfeited."""
    _, care = tax.dependent_care(
        YEAR,
        _hoh(
            care_expenses=4_950,
            dependent_care_benefits=5_000,
            dependent_care_forfeited=50,
        ),
    )
    assert care is not None
    assert (care.line14, care.line15, care.line25, care.line26) == (50, 4_950, 4_950, 0)


def test_the_credit_base_is_the_care_less_the_benefits_excluded() -> None:
    """Lines 27-31: 3,000 less 2,000 excluded is 1,000 (line 29); the care
    not paid with benefits is 500 (line 30); the credit is 500 x .22 (AGI
    40,000, line 8) = 110. Without line 30 the engine would give 220."""
    hh = _hoh(care_expenses=2_500, dependent_care_benefits=2_000)
    _, care = tax.dependent_care(YEAR, hh)
    assert care is not None
    assert (care.line29, care.line30, care.line31) == (1_000, 500, 500)
    v = tax.values(YEAR, hh, ("cdcc", "cdcc_rate", "employment_income"))
    assert v["cdcc_rate"] == pytest.approx(0.22)
    assert v["cdcc"] == pytest.approx(110)
    assert v["employment_income"] == 40_000  # nothing taxable on line 26


def test_two_qualifying_persons_raise_the_limit_to_6000() -> None:
    hh = _hoh(
        wages=60_000,
        care_expenses=7_000,
        dependents=(Dependent(age=3), Dependent(age=8)),
    )
    v = tax.values(YEAR, hh, ("cdcc", "cdcc_relevant_expenses"))
    assert v["cdcc_relevant_expenses"] == 6_000
    assert v["cdcc"] == pytest.approx(1_200)  # AGI over 43,000: .20


@pytest.mark.parametrize(
    ("agi", "rate"),
    [(15_000, 0.35), (17_000, 0.34), (17_001, 0.33), (43_000, 0.21), (43_001, 0.20)],
)
def test_line_8_follows_the_form_table(agi: int, rate: float) -> None:
    v = tax.values(YEAR, _hoh(wages=agi, care_expenses=1_000), ("cdcc_rate",))
    assert v["cdcc_rate"] == pytest.approx(rate)


def test_a_spouse_with_no_earned_income_makes_the_benefits_taxable() -> None:
    """Lines 18-20: the exclusion is no more than the lower earner's income."""
    hh = Household(
        age=40,
        filing_status="JOINT",
        state="TX",
        wages=50_000,
        spouse=Person(age=38),
        dependents=(Dependent(age=4),),
        care_expenses=4_000,
        dependent_care_benefits=2_000,
    )
    out, care = tax.dependent_care(YEAR, hh)
    assert care is not None
    assert (care.line18, care.line19, care.line20, care.line26) == (
        50_000,
        0,
        0,
        2_000,
    )
    assert out.wages == 52_000
    assert tax.values(YEAR, hh, ("cdcc",))["cdcc"] == 0  # line 5 is 0


def test_the_spouse_benefits_are_taxed_on_the_spouse() -> None:
    hh = Household(
        age=40,
        filing_status="JOINT",
        state="TX",
        wages=50_000,
        spouse=Person(age=38, wages=30_000, dependent_care_benefits=2_000),
        dependents=(Dependent(age=4),),
        care_expenses=900,
    )
    out, care = tax.dependent_care(YEAR, hh)
    assert care is not None and (care.line18, care.line19) == (50_000, 30_000)
    assert care.line26 == 1_100
    assert out.spouse is not None and out.spouse.wages == 31_100
    assert out.wages == 50_000


def test_dependent_care_is_idempotent_and_skips_a_household_without_it() -> None:
    hh = _hoh(care_expenses=900, dependent_care_benefits=2_000)
    once, _ = tax.dependent_care(YEAR, hh)
    twice, care = tax.dependent_care(YEAR, once)
    assert twice == once and care is None
    plain = _hoh(care_expenses=900)
    assert tax.dependent_care(YEAR, plain) == (plain, None)


def _home(home: Path, care: str, benefits: str) -> Layout:
    lay = Layout(home)
    lay.ensure()
    for key, text in (
        ("birth_date", "1985-04-01"),
        ("filing_status", "head_of_household"),
        ("state", "NC"),
        ("wages", "40,000"),
        ("se_income", "0"),
        ("ordinary_dividends", "0"),
        ("qualified_dividends", "0"),
        ("dependents", "2020-05-05"),
        ("care_expenses", care),
        ("dependent_care_benefits", benefits),
    ):
        enter(lay, YEAR, key, text)
    return lay


def test_care_items_are_asked_only_with_dependents(planner_home: Path) -> None:
    asked = need_for("care_expenses").asked
    assert asked is not None
    assert asked({"dependents": [{"birth_date": "2020-05-05"}]})
    assert not asked({"dependents": []})
    forfeited = need_for("dependent_care_forfeited").asked
    assert forfeited is not None
    assert forfeited({"dependent_care_benefits": 2_000})
    assert not forfeited({"dependent_care_benefits": 0})
    assert forfeited({"spouse_dependent_care_benefits": 500})
    box = need_for("dependent_care_benefits")
    assert box.boxes == (("W-2", "10"),)
    assert "spouse_dependent_care_benefits" in {n.key for n in NEEDS}
    lay = _home(planner_home, "900", "2,000")
    states = {s.need.key: s.state for s in needed(lay, YEAR).items}
    assert states["dependent_care_forfeited"] == "missing"


def test_the_draft_carries_form_2441(planner_home: Path) -> None:
    lay = _home(planner_home, "2,500", "2,000")
    for key in ("dependent_care_grace", "dependent_care_forfeited"):
        enter(lay, YEAR, key, "0")
    d = draft.build(lay, YEAR)
    assert d.get("2441", "12") == 2_000
    assert d.get("2441", "26") == 0
    assert d.get("2441", "30") == 500
    assert d.get("2441", "3") == 500
    assert d.get("2441", "8") == pytest.approx(0.22)
    assert d.get("2441", "11") == pytest.approx(110)
    assert d.get("Sch 3", "2") == pytest.approx(110)
    assert d.get("1040", "1e") is None
    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes
    assert "Form 2441" in draft.render(d)


def test_the_draft_puts_taxable_benefits_on_line_1e(planner_home: Path) -> None:
    lay = _home(planner_home, "900", "2,000")
    for key in ("dependent_care_grace", "dependent_care_forfeited"):
        enter(lay, YEAR, key, "0")
    d = draft.build(lay, YEAR)
    assert d.get("2441", "26") == 1_100
    assert d.get("1040", "1e") == 1_100
    assert d.get("1040", "1z") == 41_100
    assert d.get("2441", "11") == 0  # line 31: no care left after the benefits
    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes
