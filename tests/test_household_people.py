"""A household of more than one person (unit 3a-1): a spouse with income of
their own and dependents reach the engine as people of the tax unit, and every
figure the planner reads is the unit's, never the first person's alone.

SYNTHETIC households, hand-worked from Rev. Proc. 2025-32 (2026): joint standard
deduction 32,200 (sec. 4.15); joint rate table (sec. 4.01, Table 1) 10% to
24,800, then 2,480 plus 12% of the excess to 100,800; child tax credit 2,200 a
child, 1,700 of it refundable (sec. 4.05); the 500 credit for other dependents
is IRC 24(h)(4), not indexed. Under 100,000 of taxable income the return takes
the Tax Table row, priced at its midpoint (Form 1040 instructions, line 16). NC
2026: 3.99% (G.S. 105-153.7) after the joint standard deduction 25,500
(G.S. 105-153.5(a)(1))."""

from __future__ import annotations

from typing import Any

import pytest

from planner.engine.household import Dependent, Household, Person
from planner.engine.tax import compute, compute_sweep, values

pytestmark = pytest.mark.engine
D = 1.0
JOINT: dict[str, Any] = {"age": 45, "filing_status": "JOINT", "state": "NC"}
SPOUSE = Person(age=43, wages=40_000)


def test_a_spouses_wages_are_on_the_joint_return() -> None:
    # AGI 60,000 + 40,000 = 100,000; taxable 100,000 - 32,200 = 67,800 -> Tax
    # Table row 67,800-67,850 at 67,825: 2,480 + 12% x 43,025 = 7,643.
    # NC: 3.99% x (100,000 - 25,500) = 2,972.55.
    r = compute(2026, Household(**JOINT, wages=60_000, spouse=SPOUSE))
    assert r.agi == pytest.approx(100_000, abs=D)
    assert r.taxable_income == pytest.approx(67_800, abs=D)
    assert r.fed_income_tax_after_credits == pytest.approx(7_643, abs=D)
    assert r.state_tax == pytest.approx(2_972.55, abs=D)


def test_two_children_take_the_child_tax_credit() -> None:
    # 2 x 2,200 = 4,400 against 7,643 (AGI far under the 400,000 phase-out).
    kids = (Dependent(age=8), Dependent(age=12))
    r = compute(2026, Household(**JOINT, wages=60_000, spouse=SPOUSE, dependents=kids))
    assert r.fed_income_tax_after_credits == pytest.approx(7_643 - 4_400, abs=D)


def test_a_student_over_sixteen_takes_the_credit_for_other_dependents() -> None:
    # A 20-year-old full-time student is a qualifying child but not one under
    # 17 (IRC 24(c)(1)): the 500 credit for other dependents, not 2,200.
    kid = (Dependent(age=20, full_time_student=True),)
    r = compute(2026, Household(**JOINT, wages=60_000, spouse=SPOUSE, dependents=kid))
    assert r.fed_income_tax_after_credits == pytest.approx(7_643 - 500, abs=D)


def test_a_spouses_self_employment_tax_is_the_households() -> None:
    # 20,000 x 92.35% x 15.3% = 2,825.91 on the spouse's Schedule SE.
    spouse = Person(age=43, se_income=20_000)
    r = compute(2026, Household(**JOINT, wages=60_000, spouse=spouse))
    assert r.se_tax == pytest.approx(2_825.91, abs=D)
    assert r.fed_total_tax - r.fed_income_tax_after_credits == pytest.approx(
        2_825.91, abs=D
    )
    v = values(
        2026, Household(**JOINT, wages=60_000, spouse=spouse), ("self_employment_tax",)
    )
    assert v["self_employment_tax"] == pytest.approx(2_825.91, abs=D)


def test_a_sweep_moves_the_first_person_only() -> None:
    # The swept figure is the first person's; the spouse's own conversion stays
    # put, and each row prices the same as a point computation.
    spouse = Person(age=43, wages=40_000, roth_conversion=10_000)
    h = Household(**JOINT, wages=60_000, spouse=spouse)
    rows = compute_sweep(2026, h, "taxable_roth_conversions", 0, 20_000, 20_000)
    assert [row["taxable_roth_conversions"] for row in rows] == [0, 20_000]
    for row, conv in zip(rows, (0, 20_000), strict=True):
        point = compute(
            2026, Household(**JOINT, wages=60_000, spouse=spouse, roth_conversion=conv)
        )
        assert row["adjusted_gross_income"] == pytest.approx(point.agi, abs=D)
        assert row["state_income_tax"] == pytest.approx(point.state_tax, abs=D)


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"filing_status": "SINGLE", "spouse": SPOUSE}, "a spouse files JOINT"),
        ({"filing_status": "SEPARATE", "spouse": SPOUSE}, "a spouse files JOINT"),
    ],
)
def test_only_a_joint_return_carries_a_spouse(kw: dict[str, Any], why: str) -> None:
    """A separate return's spouse is not in its tax unit (that comparison is
    unit 3b). A joint or head-of-household profile with nobody named still
    prices, as one person, and the coverage gate tags it (unit 0b)."""
    with pytest.raises(ValueError, match=why):
        Household(**{**JOINT, **kw})


def test_a_household_file_names_its_people() -> None:
    h = Household.from_mapping(
        {
            **JOINT,
            "spouse": {"age": 43, "wages": 40_000},
            "dependents": [{"age": 8}, {"age": 20, "full_time_student": True}],
        }
    )
    assert h.spouse == SPOUSE
    assert h.dependents == (Dependent(age=8), Dependent(age=20, full_time_student=True))
    with pytest.raises(ValueError, match="unknown spouse fields: \\['salary'\\]"):
        Household.from_mapping({**JOINT, "spouse": {"age": 43, "salary": 1}})
    with pytest.raises(ValueError, match="unknown dependent fields: \\['name'\\]"):
        Household.from_mapping(
            {**JOINT, "spouse": {"age": 43}, "dependents": [{"age": 8, "name": "x"}]}
        )
