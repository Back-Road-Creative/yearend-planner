"""Phase 10 3g-1: the Form 2210 regular-method penalty (2025 Form 2210 Part III
and its instructions' penalty worksheet): a quarter of the required annual
payment due on each date, withholding counted a quarter on each date, later
payments applied to the earliest shortfall first, and each shortfall charged
the IRC 6621 underpayment rate of each calendar quarter it stays unpaid."""

from __future__ import annotations

from datetime import date

from typer.testing import CliRunner

from planner.cli import app
from planner.ingest.needs import enter
from planner.paths import Layout
from planner.plan import esttax
from tests.test_spending import lay  # noqa: F401

runner = CliRunner()


def owed(amount: float, *spans: tuple[int, float]) -> float:
    """``amount`` at each (days, rate) span of a 365-day year."""
    return sum(amount * rate * days / 365 for days, rate in spans)


def test_instructions_example_3_underpayments_and_days() -> None:
    """Example 3: 4,000 due each date; 2,000 paid 04/30, 3,000 06/15, 4,000
    09/15 and 4,000 01/15. Line 1a: 4,000 then 3,000 in each later column; the
    April shortfall is paid 15 and 61 days late."""
    paid = [
        (date(2025, 4, 30), 2_000.0),
        (date(2025, 6, 15), 3_000.0),
        (date(2025, 9, 15), 4_000.0),
        (date(2026, 1, 15), 4_000.0),
    ]
    pen = esttax.penalty(2025, "fed", 16_000.0, 0.0, paid)
    assert pen.underpaid == [4_000.0, 3_000.0, 3_000.0, 3_000.0]
    # 2025 is 7% throughout; 2026 Q1 7%, Q2 (from April 1) 6%
    want = (
        owed(2_000, (15, 0.07))
        + owed(2_000, (61, 0.07))
        + owed(3_000, (92, 0.07))
        + owed(3_000, (122, 0.07))
        + owed(3_000, (75, 0.07), (15, 0.06))
    )
    assert pen.amount == round(want, 2)
    assert pen.notes == []


def test_table_2_days_for_a_shortfall_never_paid() -> None:
    """Table 2: an April shortfall left unpaid runs 76 + 92 + 92 + 105 days to
    04/15/26; one from January 15 runs 90."""
    pen = esttax.penalty(2025, "fed", 4_000.0, 0.0, [])
    assert pen.underpaid == [1_000.0] * 4
    want = (
        owed(1_000, (76, 0.07), (92, 0.07), (92, 0.07), (90, 0.07), (15, 0.06))
        + owed(1_000, (15, 0.07), (92, 0.07), (92, 0.07), (90, 0.07), (15, 0.06))
        + owed(1_000, (15, 0.07), (92, 0.07), (90, 0.07), (15, 0.06))
        + owed(1_000, (75, 0.07), (15, 0.06))
    )
    assert pen.amount == round(want, 2)


def test_withholding_is_a_quarter_on_each_date_and_overpayment_carries() -> None:
    # 4,000 withheld covers 1,000 of each 3,000 installment; a 6,000 April
    # payment overpays the first and carries into the next two
    pen = esttax.penalty(2025, "fed", 12_000.0, 4_000.0, [(date(2025, 4, 1), 6_000.0)])
    assert pen.underpaid == [0.0, 0.0, 0.0, 2_000.0]
    assert pen.amount == round(owed(2_000, (75, 0.07), (15, 0.06)), 2)
    # withholding alone meeting the required payment: no penalty
    none = esttax.penalty(2025, "fed", 12_000.0, 12_000.0, [])
    assert none.amount == 0.0 and none.underpaid == [0.0] * 4


def test_weekend_due_date_payment_is_on_time() -> None:
    # 2028-01-15 is a Saturday and Monday the 17th is Martin Luther King Jr.
    # Day: a Tuesday 01/18 payment for tax year 2027 counts as made on time
    pen = esttax.penalty(
        2027,
        "fed",
        4_000.0,
        0.0,
        [
            (date(2027, 4, 15), 1_000.0),
            (date(2027, 6, 15), 1_000.0),
            (date(2027, 9, 15), 1_000.0),
            (date(2028, 1, 18), 1_000.0),
        ],
    )
    assert pen.underpaid == [0.0] * 4 and pen.amount == 0.0


def test_unpublished_quarters_take_the_last_rate_and_say_so() -> None:
    pen = esttax.penalty(2026, "fed", 4_000.0, 0.0, [])
    assert pen.amount > 0
    assert any("not published" in n and "2027" in n for n in pen.notes)


def test_leap_year_days_over_366() -> None:
    # tax year 2027 runs to 04/15/28; 2028 days are over a 366-day year (the
    # unpublished quarters at the last published 7%). Each date's 750 of
    # withholding first pays off the earlier shortfall (Section A lines 14-17),
    # so line 17 grows by 250 a column
    pen = esttax.penalty(2027, "fed", 4_000.0, 3_000.0, [])
    assert pen.underpaid == [250.0, 500.0, 750.0, 1_000.0]
    want = 0.07 * (
        250 * 61 / 365
        + 500 * 92 / 365
        + 750 * ((15 + 92) / 365 + 15 / 366)
        + 1_000 * (76 + 15) / 366
    )
    assert pen.amount == round(want, 2)


def test_estimate_charges_the_federal_penalty_and_the_states_own(
    lay: Layout,  # noqa: F811
) -> None:
    enter(lay, 2026, "se_income", "80,000")
    enter(lay, 2026, "prior_agi", "70,000")
    enter(lay, 2026, "prior_total_tax", "8,000")
    enter(lay, 2026, "prior_nc_tax", "2,000")
    esttax.record(lay, 2026, "fed", "2026-04-10", 1_200.0)
    et = esttax.estimate(lay, 2026, date(2026, 7, 10))
    fed, nc = et.agencies
    # 800 short from 04/15 and 2,000 from 06/15, both cured by the plan's
    # September catch-up payment (Q2 2026 6%, Q3 7%)
    want = owed(800, (76, 0.06), (77, 0.07)) + owed(2_000, (15, 0.06), (77, 0.07))
    assert fed.penalty == round(want, 2)
    assert any("Form 2210 penalty" in n and "regular method" in n for n in fed.notes)
    assert not any("unavailable" in n for n in fed.notes)
    # North Carolina's D-422 interest on its own shortfalls, not unavailable
    assert nc.penalty is not None and nc.penalty > 0
    assert not any("not computed" in n for n in nc.notes)
    r = runner.invoke(app, ["esttax", "--year", "2026", "--as-of", "2026-07-10"])
    assert r.exit_code == 0, r.output
    assert f"Form 2210 penalty {fed.penalty:,.2f}" in r.output
    assert f"Form D-422 interest {nc.penalty:,.2f}" in r.output
    # nothing short, nothing charged
    enter(lay, 2026, "fed_withheld", "8,000")
    clean = esttax.estimate(lay, 2026, date(2026, 7, 10)).agencies[0]
    assert clean.penalty == 0.0
    assert any("no Form 2210 penalty" in n for n in clean.notes)
