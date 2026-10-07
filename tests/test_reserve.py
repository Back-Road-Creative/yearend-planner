"""Phase 10, unit 4b: the reserve rule. A typed cash_target wins; without one
the reserve is months of essential spending, the deductibles a claim takes
first, the next estimated payment each agency still lacks, and the goals dated
inside 12 months. A part not entered is named, never counted as 0. The
feasibility check (2d), the cash panel and the monthly line all hold it back."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
import yaml

from planner import goals
from planner.ingest.needs import enter, profile_path
from planner.paths import Layout
from planner.plan import esttax, feasible, glidepath, levers, reserve, withdraw, year
from planner.plan.esttax import Agency, EstTax
from planner.plan.inputs import Overrides
from tests.test_spending import AS_OF, lay  # noqa: F401

TODAY = date(2026, 7, 10)


def _profile(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "cash_target": None,
        "reserve_months": None,
        "reserve_deductibles": None,
        "spending_floor": None,
    }
    base.update(kw)
    return base


def _agency(name: str, due: str | None, amount: float) -> Agency:
    return Agency(
        name, 0.0, None, None, 0.0, "", 0.0, False, next_due=due, next_amount=amount
    )


def _et(*ags: Agency) -> EstTax:
    return EstTax(2026, TODAY.isoformat(), 0.0, False, list(ags))


GOALS = goals.Goals(
    goals=[
        goals.Goal("roof", 1, 8_000.0, "2026-12-01"),
        goals.Goal("car", 2, 30_000.0, "2027-09-01"),  # past 12 months
        goals.Goal("trip", 3, None, "2026-10-01"),  # no amount
    ]
)


def test_the_rule_adds_its_four_parts() -> None:
    rs = reserve.compute(
        _profile(
            reserve_months=6, reserve_deductibles=3_000.0, spending_floor=36_000.0
        ),
        GOALS,
        TODAY,
        _et(_agency("FED", "2026-09-15", 1_200.0), _agency("NC", "2026-09-15", 300.0)),
    )
    assert rs.basis == "computed"
    assert rs.amount == 18_000 + 3_000 + 1_500 + 8_000
    assert [p.name for p in rs.parts] == ["spending", "deductibles", "tax", "goals"]
    assert [p.amount for p in rs.parts] == [18_000, 3_000, 1_500, 8_000]
    text = "\n".join(rs.lines())
    assert "reserve 30,500.00: the rule" in text
    assert "6 months of the 36,000 spending floor" in text
    assert "FED 1,200 due 2026-09-15" in text and "NC 300 due 2026-09-15" in text
    assert "roof 8,000 by 2026-12-01" in text and "car" not in text
    assert any("trip" in n and "not counted as 0" in n for n in rs.notes)


def test_a_typed_cash_target_wins_and_the_rule_is_shown_beside_it() -> None:
    rs = reserve.compute(
        _profile(cash_target=15_000.0, reserve_months=6, spending_floor=36_000.0),
        None,
        TODAY,
        None,
    )
    assert rs.basis == "typed" and rs.amount == 15_000
    assert (
        rs.lines()[0] == "reserve 15,000.00: cash_target typed (it wins over the rule)"
    )


def test_a_part_not_entered_is_named_and_the_rest_is_a_floor() -> None:
    rs = reserve.compute(
        _profile(reserve_months=6, reserve_deductibles=2_000.0),  # no floor
        goals.Goals(),
        TODAY,
        None,
        why_no_tax="the projection needs se_income",
    )
    assert rs.basis == "computed" and rs.amount == 2_000
    spending = rs.parts[0]
    assert spending.amount is None and "spending_floor" in spending.detail
    tax = rs.parts[2]
    assert tax.amount is None and "the projection needs se_income" in tax.detail
    assert any(
        n.startswith("the reserve is at least 2,000.00: spending, tax not entered")
        for n in rs.notes
    )


def test_a_paid_installment_and_no_goals_count_zero() -> None:
    rs = reserve.compute(
        _profile(reserve_months=0, reserve_deductibles=0.0, spending_floor=36_000.0),
        goals.Goals(),
        TODAY,
        _et(_agency("FED", None, 0.0)),
    )
    assert rs.basis == "computed" and rs.amount == 0
    assert all(p.amount == 0 for p in rs.parts)
    assert not rs.notes


def test_nothing_entered_is_no_reserve_and_the_check_says_so() -> None:
    rs = reserve.compute(_profile(), goals.Goals(), TODAY, None, why_no_tax="x")
    assert rs.basis == "none" and rs.amount == 0
    assert rs.lines()[0].startswith("no reserve: cash_target is not typed")
    f = feasible.Funds(cash=3_000.0, reserve=rs.amount, reserve_set=rs.basis != "none")
    text = feasible.cash_reason(f, 5_000.0)
    assert text is not None and text.endswith(
        "(no reserve: cash_target not typed and the reserve rule has nothing entered)"
    )


def test_funds_hold_back_the_reserve_not_the_profile_line() -> None:
    rs = reserve.compute(
        _profile(reserve_months=1, spending_floor=24_000.0), goals.Goals(), TODAY, None
    )

    class _St:
        positions = [
            type("P", (), {"type": "cash", "value": 10_000.0, "account": "1"})()
        ]
        lots: list[Any] = []

    f = feasible.funds(_St(), rs)  # type: ignore[arg-type]
    assert f.reserve == 2_000 and f.reserve_set and f.spare == 8_000


@pytest.mark.parametrize("bad", ["-1", "abc"])
def test_reserve_months_refuses_a_bad_count(
    lay: Layout,  # noqa: F811
    bad: str,
) -> None:
    with pytest.raises(ValueError):
        enter(lay, 2026, "reserve_months", bad)


def _ruled(home: Layout) -> Layout:
    prof = yaml.safe_load(profile_path(home).read_text(encoding="utf-8"))
    prof["cash_target"] = None
    profile_path(home).write_text(yaml.safe_dump(prof), encoding="utf-8")
    enter(home, 2026, "reserve_months", "6")
    enter(home, 2026, "reserve_deductibles", "3,000")
    goals.save_goal(home, "roof", amount="8,000", date="2026-12-01")
    return home


@pytest.mark.engine
def test_the_rule_reaches_the_cash_panel_the_line_and_the_checks(
    lay: Layout,  # noqa: F811
) -> None:
    home = _ruled(lay)
    et = esttax.estimate(home, 2026, AS_OF)
    tax = sum(a.next_amount for a in et.agencies)
    rs = reserve.load(home, 2026, AS_OF)
    assert rs.basis == "computed"
    assert rs.amount == pytest.approx(25_000 + 3_000 + 8_000 + tax, abs=0.01)
    assert withdraw.pick(home, 2026, as_of=AS_OF).target == pytest.approx(rs.amount)
    g = glidepath.glide(home, 2026, AS_OF)
    assert g.target == pytest.approx(rs.amount)
    ctx, _ = levers._context(home, 2026, AS_OF, Overrides())
    assert ctx.funds().reserve == pytest.approx(rs.amount)
    cash = year.assemble(home, 2026, AS_OF).section("cash")
    assert cash is not None
    assert any(
        ln.startswith(f"reserve {rs.amount:,.2f}: the rule") for ln in cash.lines
    )
    assert any("6 months of the 50,000 spending floor" in ln for ln in cash.lines)
