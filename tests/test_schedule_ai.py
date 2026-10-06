"""Unit 3g-2: Form 2210 Schedule AI, the annualized income installment method
(2025 Form 2210 page 3 and its instructions), against synthetic households."""

from __future__ import annotations

from datetime import date

import pytest

from planner.engine.household import Household
from planner.ingest.needs import enter
from planner.paths import Layout
from planner.plan import esttax
from planner.plan.inputs import Overrides
from planner.taxprep import schedule_ai
from tests.test_spending import lay  # noqa: F401


def test_parse_each_line_through_march_may_and_august() -> None:
    got = schedule_ai.parse(
        "income_by_period", "se_income 0 0 20,000; long_term_gains -500 0 $1,000"
    )
    assert got == {
        "se_income": [0.0, 0.0, 20_000.0],
        "long_term_gains": [-500.0, 0.0, 1_000.0],
    }
    assert schedule_ai.parse("income_by_period", "none") == {}
    for bad in (
        "rent 1 2 3",  # not an income line
        "wages 1 2",  # three periods each
        "wages -1 2 3",  # wages are not negative
        "wages 1 2 3; wages 4 5 6",  # twice
        "wages 1 2 x",
    ):
        with pytest.raises(ValueError, match="income_by_period"):
            schedule_ai.parse("income_by_period", bad)


def test_lines_20_to_27_carry_the_unused_regular_installment() -> None:
    # line 19 by column, and Form 2210 line 9 of 16,000 (line 24: 4,000 each)
    got = schedule_ai.installments([4_000.0, 6_000.0, 10_000.0, 20_000.0], 16_000.0)
    # (a) 22.5% of 4,000 = 900; (b) 45% of 6,000 less 900 = 1,800;
    # (c) 67.5% of 10,000 less 2,700 = 4,050; (d) 90% of 20,000 less 6,750 =
    # 11,250, capped at line 26: 4,000 + (9,300 - 4,050) = 9,250
    assert got == [900.0, 1_800.0, 4_050.0, 9_250.0]
    # the annualized installment never tops the regular one plus what is unused
    assert schedule_ai.installments([40_000.0] * 4, 16_000.0) == [4_000.0] * 4


def test_period_households_annualize_the_typed_lines_only() -> None:
    hh = Household(
        age=50, filing_status="SINGLE", state="NC", wages=60_000, long_term_gains=4_000
    )
    got = schedule_ai.households(hh, {"long_term_gains": [0.0, 400.0, 1_000.0]})
    assert [h.long_term_gains for h in got] == [0, 960, 1_500]
    # a line not typed is taken as received evenly: annualized, the year's figure
    assert [h.wages for h in got] == [60_000] * 3


def test_penalty_on_the_annualized_installments() -> None:
    req = [900.0, 1_800.0, 4_050.0, 9_250.0]
    # June 15, 2025 is a Sunday: June 16 is on time
    paid = [
        (date(2025, 4, 15), 900.0),
        (date(2025, 6, 16), 1_800.0),
        (date(2025, 9, 15), 4_050.0),
        (date(2026, 1, 15), 9_250.0),
    ]
    pen = esttax.penalty(2025, "fed", 16_000.0, 0.0, paid, required=req)
    assert pen.amount == 0.0 and pen.underpaid == [0.0] * 4
    assert esttax.penalty(2025, "fed", 16_000.0, 0.0, paid).amount > 0


def _fixture(lay: Layout, se: str) -> None:  # noqa: F811
    enter(lay, 2026, "se_income", se)
    enter(lay, 2026, "prior_agi", "70,000")
    enter(lay, 2026, "prior_total_tax", "8,000")
    enter(lay, 2026, "prior_nc_tax", "2,000")


# nothing paid by July 10: the plan pays installments 3 and 4 on their dates
CATCH_UP = [(date(2026, 9, 15), 6_000.0), (date(2027, 1, 15), 2_000.0)]


def test_typed_late_income_takes_the_annualized_method(lay: Layout) -> None:  # noqa: F811
    _fixture(lay, "80,000")
    enter(lay, 2026, "income_by_period", "se_income 0 0 30,000")
    et = esttax.estimate(lay, 2026, date(2026, 7, 10))
    fed = et.agencies[0]
    assert fed.required == 8_000.0 and fed.schedule_ai is not None
    # nothing earned by May: no tax owed for periods (a) and (b)
    assert fed.schedule_ai[:2] == [0.0, 0.0]
    regular = esttax.penalty(2026, "fed", 8_000.0, 0.0, CATCH_UP).amount
    assert (
        fed.penalty
        == esttax.penalty(
            2026, "fed", 8_000.0, 0.0, CATCH_UP, required=fed.schedule_ai
        ).amount
    )
    assert fed.penalty is not None and fed.penalty < regular
    assert any(
        "annualized method (Schedule AI)" in n and f"{regular:,.2f}" in n
        for n in fed.notes
    )
    assert not any("not computed" in n for n in et.notes)


def test_a_planned_year_end_conversion_counts_in_the_last_period(
    lay: Layout,  # noqa: F811
) -> None:
    _fixture(lay, "20,000")
    ov = Overrides(planned_conversion=100_000.0)
    et = esttax.estimate(lay, 2026, date(2026, 7, 10), ov)
    fed = et.agencies[0]
    assert fed.required == 8_000.0 and fed.schedule_ai is not None
    # periods (a)-(c) price the year without the conversion: well under the
    # regular 2,000 a quarter
    assert 0 < fed.schedule_ai[0] < 2_000.0
    assert (
        fed.penalty
        == esttax.penalty(
            2026, "fed", 8_000.0, 0.0, CATCH_UP, required=fed.schedule_ai
        ).amount
    )
    assert any("planned" in n and "September-December" in n for n in fed.notes)


def test_even_income_skips_the_annualized_method_and_says_how(
    lay: Layout,  # noqa: F811
) -> None:
    _fixture(lay, "30,000")
    et = esttax.estimate(lay, 2026, date(2026, 7, 10))
    fed = et.agencies[0]
    assert fed.schedule_ai is None
    # the fixture's deposits all land in Q1: lumpy, so the note names the input
    assert et.annualized
    assert any("income_by_period" in n for n in et.notes)
    with pytest.raises(ValueError, match="income_by_period"):
        enter(lay, 2026, "income_by_period", "se_income 1 2")
