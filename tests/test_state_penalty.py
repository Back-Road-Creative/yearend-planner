"""Phase 10 3g-3: each state's own underpayment penalty or interest, from its
2025 form, pinned to the figures the form prints: OH IT/SD 2210 line 15's
multipliers, NJ-2210's, MI-2210's and Form 760C's daily factors, REV-1630's
and D-422's day counts; a taxing state without its own rules on the federal
Form 2210 method, marked Estimated. All amounts synthetic."""

from __future__ import annotations

from datetime import date

import pytest

from planner import states
from planner.plan import esttax


def days(amount: float, factor: float, *spans: int) -> float:
    return sum(amount * factor * d for d in spans)


def test_ohio_and_new_jersey_print_their_column_multipliers() -> None:
    # IT/SD 2210 line 15: rate x days to the next column date / 365.25 (8% for
    # 2025, 7% for 2026); NJ-2210: months x rate / 12 (10.75%, then 10.00%)
    assert esttax.span_factors(2025, "oh") == [0.013580, 0.019932, 0.026311, 0.017248]
    assert esttax.span_factors(2025, "nj") == [0.018, 0.027, 0.036, 0.025]


def test_column_method_charges_each_cumulative_shortfall() -> None:
    # nothing paid: 1,000 short at April, 2,000 at June, 3,000 at September,
    # 4,000 at January, each for its column's span
    oh = esttax.penalty(2025, "oh", 4_000.0, 0.0, [])
    assert oh.underpaid == [1_000.0, 2_000.0, 3_000.0, 4_000.0]
    assert oh.amount == round(
        1_000 * 0.013580 + 2_000 * 0.019932 + 3_000 * 0.026311 + 4_000 * 0.017248, 2
    )
    # a June 1 payment of 2,000 counts at the June column, not before
    nj = esttax.penalty(2025, "nj", 4_000.0, 0.0, [(date(2025, 6, 1), 2_000.0)])
    assert nj.underpaid == [1_000.0, 0.0, 1_000.0, 2_000.0]
    assert nj.amount == round(1_000 * 0.018 + 1_000 * 0.036 + 2_000 * 0.025, 2)


def test_daily_interest_states_count_the_forms_days() -> None:
    # 1,000 short on each date, never paid before the return
    va = esttax.penalty(2025, "va", 4_000.0, 0.0, [])
    assert va.amount == round(days(1_000, 0.00025, 365, 320, 228, 106), 2)
    nc = esttax.penalty(2025, "nc", 4_000.0, 0.0, [])
    assert nc.amount == round(days(1_000, 0.07 / 365, 365, 304, 212, 90), 2)
    ga = esttax.penalty(2025, "ga", 4_000.0, 0.0, [])
    assert ga.amount == round(days(1_000, 0.09 / 365, 365, 304, 212, 90), 2)
    ny = esttax.penalty(2025, "ny", 4_000.0, 0.0, [])
    assert ny.amount == round(days(1_000, 0.095 / 365, 365, 304, 212, 90), 2)
    # REV-1630 lines 14a-14c: 260, 198 and 107 days to December 31, then 105
    # (90 for January), at the printed 0.000192
    pa = esttax.penalty(2025, "pa", 4_000.0, 0.0, [])
    assert pa.amount == round(days(1_000, 0.000192, 365, 303, 212, 90), 2)
    for pen in (va, nc, ga, ny, pa):
        assert pen.notes == []


def test_california_splits_its_two_rate_periods() -> None:
    # 30/40/0/30 of 4,000; 8% to June 30, 2025, then 7%
    ca = esttax.penalty(2025, "ca", 4_000.0, 0.0, [])
    assert ca.underpaid == [1_200.0, 1_600.0, 1_200.0]
    want = (
        1_200 * (0.08 * 76 + 0.07 * 289) / 365
        + 1_600 * (0.08 * 15 + 0.07 * 289) / 365
        + 1_200 * 0.07 * 90 / 365
    )
    assert ca.amount == round(want, 2)


def test_pennsylvania_never_cures_an_earlier_shortfall() -> None:
    # 2,000 on May 1 counts in the June column; the April shortfall runs on,
    # the June overpayment carries to September
    paid = [(date(2025, 5, 1), 2_000.0)]
    pa = esttax.penalty(2025, "pa", 4_000.0, 0.0, paid)
    assert pa.underpaid == [1_000.0, 0.0, 0.0, 1_000.0]
    assert pa.amount == round(days(1_000, 0.000192, 365, 90), 2)
    # the same payment cures North Carolina's April shortfall on May 1 and
    # covers June; September and January stay short
    nc = esttax.penalty(2025, "nc", 4_000.0, 0.0, paid)
    assert nc.amount == round(days(1_000, 0.07 / 365, 16, 212, 90), 2)


def test_michigan_interest_and_its_ten_or_twenty_five_percent_penalty() -> None:
    a, b, c = 0.0002595, 0.0002373, 0.0002324
    interest = (
        1_000 * (a * 76 + b * 184 + c * 105)
        + 1_000 * (a * 14 + b * 184 + c * 105)
        + 1_000 * (b * 107 + c * 105)
        + 1_000 * c * 90
    )
    # nothing paid in any period: 25% of each period's 1,000
    mi = esttax.penalty(2025, "mi", 4_000.0, 0.0, [])
    assert mi.amount == round(interest + 0.25 * 4_000, 2)
    # 500 paid on each date: 10% of each period's 500
    paid = [(d, 500.0) for d in esttax.due_dates(2025, "mi")]
    half = esttax.penalty(2025, "mi", 4_000.0, 0.0, paid)
    assert half.amount == round(interest / 2 + 0.10 * 2_000, 2)


def test_illinois_two_or_ten_percent_by_days_late() -> None:
    # April's 1,000 paid 16 days late: 2%; the rest never paid: 10%
    il = esttax.penalty(2025, "il", 4_000.0, 0.0, [(date(2025, 5, 1), 1_000.0)])
    assert il.amount == round(0.02 * 1_000 + 0.10 * 3_000, 2)
    # 30 days late is still 2%; 31 days is 10%
    edge = esttax.penalty(2025, "il", 4_000.0, 0.0, [(date(2025, 5, 15), 1_000.0)])
    assert edge.amount == round(0.02 * 1_000 + 0.10 * 3_000, 2)
    late = esttax.penalty(2025, "il", 4_000.0, 0.0, [(date(2025, 5, 16), 1_000.0)])
    assert late.amount == round(0.10 * 4_000, 2)


def test_unpublished_dates_take_the_last_published_rate_and_say_so() -> None:
    nc = esttax.penalty(2026, "nc", 4_000.0, 0.0, [])
    assert nc.amount == round(days(1_000, 0.07 / 365, 365, 304, 212, 90), 2)
    assert nc.notes and "2026-06-30" in nc.notes[0] and "7%" in nc.notes[0]


def test_a_state_without_its_own_rules_takes_the_federal_method() -> None:
    other = next(
        st.agency for st in states.STATES.values() if st.income_tax and st.est is None
    )
    assert esttax.penalty(2025, other, 4_000.0, 0.0, []) == esttax.penalty(
        2025, "fed", 4_000.0, 0.0, []
    )
    assert esttax.penalty_label(other) == "Form 2210 penalty (federal method)"
    assert esttax.penalty_label("fed") == "Form 2210 penalty"
    assert esttax.penalty_label("nc") == "Form D-422 interest"


@pytest.mark.parametrize(
    "code", ["NC", "CA", "NY", "PA", "IL", "OH", "GA", "MI", "NJ", "VA"]
)
def test_every_state_with_own_rules_has_its_penalty(code: str) -> None:
    rule = states.STATES[code].est
    assert rule is not None and rule.penalty is not None
    assert rule.penalty.form and rule.penalty.source
