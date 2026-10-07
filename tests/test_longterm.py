"""Phase 10, unit 4e: the long-term path per account, with tax, RMDs and debt,
on hand-built accounts and on the synthetic household of test_spending (single,
born 1971: a taxable account, a cash account and a traditional IRA)."""

from __future__ import annotations

import math
from datetime import date

import pytest
from typer.testing import CliRunner

from planner import goals
from planner.cli import app
from planner.ledger import portfolio
from planner.paths import Layout
from planner.plan import longterm, spending, year
from planner.plan.longterm import Account, Household, Rules
from tests.test_spending import AS_OF, lay  # noqa: F401

runner = CliRunner()
INF = math.inf
# a toy schedule, so every figure below is worked by hand
TOY = Rules(
    2026,
    "SINGLE",
    15_000.0,
    2_000.0,
    ((10_000.0, 0.10), (INF, 0.20)),
    ((50_000.0, 0.0), (INF, 0.15)),
    (25_000.0, 34_000.0),
    0.04,
)


def _sp(floor: float, ceiling: float) -> spending.Spending:
    return spending.Spending(
        2026, "2026-07-10", 0.0, 0.0, "2026-01-01", 0.0, False, 0.035, floor, ceiling,
        floor,
    )  # fmt: skip


def test_rmd_age_and_the_uniform_table() -> None:
    # IRC 401(a)(9)(C)(v); Pub. 590-B (2025) Appendix B, Table III
    assert [longterm.rmd_age(date(y, 6, 1)) for y in (1950, 1951, 1959, 1960)] == [
        72,
        73,
        73,
        75,
    ]
    assert longterm.divisor(73) == 26.5 and longterm.divisor(75) == 24.6
    assert longterm.divisor(95) == 8.9 and longterm.divisor(130) == 2.0


def test_federal_tax_stacks_gains_and_taxes_social_security() -> None:
    assert TOY.federal(30_000, 0, 0, 1.0, 0) == (2_000.0, 30_000.0)
    # 15,000 ordinary taxable, gains 15,000 to 55,000: 35,000 at 0%, 5,000 at 15%
    assert TOY.federal(30_000, 40_000, 0, 1.0, 0)[0] == pytest.approx(2_750.0)
    assert TOY.federal(30_000, 0, 0, 1.0, 1)[0] == pytest.approx(1_600.0)
    # provisional 30,000: half the excess over 25,000 is taxable
    assert TOY.federal(20_000, 0, 20_000, 1.0, 0)[1] == pytest.approx(22_500.0)
    # prices doubled: the unindexed thresholds are 12,500 and 17,000 today
    assert TOY.federal(20_000, 0, 20_000, 2.0, 0)[1] == pytest.approx(33_300.0)
    assert TOY.both(30_000, 0, 0, 1.0, 0) == pytest.approx((2_000.0, 1_200.0))


def test_debt_payments_by_year_and_payoff() -> None:
    debts = [
        goals.Debt("car", 12_000.0, 0.0, 1_000.0),
        goals.Debt("card", 10_000.0, 24.0, 100.0),
    ]
    pays, lines = longterm.debt_schedule(debts, date(2026, 7, 10), 3, 0.0)
    assert pays == [6_600.0, 7_200.0, 1_200.0]
    assert lines[0] == "debt car: paid off 2027-06 at 1,000.00 a month"
    assert lines[1].startswith("debt card: not paid off on this path")


def test_rmd_beyond_the_need_is_taxed_and_reinvested() -> None:
    hh = Household(2046, date(1971, 6, 15), None, 59.5, {}, 0.0)
    acct = Account("33333333", "deferred", "self", 246_000.0)
    rows = longterm.run([acct], hh, _sp(5_000, 5_000), TOY, [0.0, 0.0], 0, 0, 0, 76)
    assert [rw.age for rw in rows] == [75, 76]
    assert rows[0].rmd == 10_000.0 and rows[0].fed == 0.0  # under 17,000 deducted
    assert rows[0].state == 400.0 and rows[0].short == 0.0
    assert rows[1].balances["taxable"] == 4_600.0  # 10,000 - 5,000 - 400
    assert rows[1].rmd == round(236_000 / 23.7, 2)
    assert acct.balance == 246_000.0  # the caller's accounts are not spent


def test_before_the_access_age_only_open_money_is_drawn() -> None:
    hh = Household(2026, date(1971, 6, 15), None, 59.5, {}, 10_000.0)
    accts = [
        Account("1", "cash", "self", 30_000.0),
        Account("2", "deferred", "self", 500_000.0),
        Account("3", "roth", "self", 100_000.0),
    ]
    rows = longterm.run(accts, hh, _sp(50_000, 50_000), TOY, [0.0], 20_000, 0, 0, 55)
    # cash above the reserve, the Roth basis, then the reserve: 40,000 of 50,000
    assert rows[0].drawn == 40_000.0 and rows[0].short == 10_000.0
    assert rows[0].fed == 0.0 and longterm.runs_out(rows) == 55


def test_taxable_draws_owe_gains_tax_on_the_gain_share() -> None:
    hh = Household(2026, date(1971, 6, 15), None, 59.5, {}, 0.0)
    accts = [Account("1", "taxable", "self", 200_000.0, basis=50_000.0)]
    rows = longterm.run(accts, hh, _sp(100_000, 100_000), TOY, [0.0], 0, 0, 0, 55)
    # 3/4 of a dollar drawn is gain. Drawn D = 100,000 + fed + state, with
    # fed 15% of (0.75 D - 15,000 - 50,000) and state 4% of 0.75 D:
    # D = 90,250 / 0.8575
    assert rows[0].drawn == 105_247.81
    assert (rows[0].fed, rows[0].state) == (2_090.38, 3_157.43)


def test_owner_is_typed_by_command(lay: Layout) -> None:  # noqa: F811
    res = runner.invoke(app, ["account", "33333333", "--owner", "spouse"])
    assert res.exit_code == 0, res.output
    assert portfolio.load_accounts(lay)["33333333"]["owner"] == "spouse"
    res = runner.invoke(app, ["account", "33333333", "--owner", "child"])
    assert res.exit_code == 2 and "owner must be self or spouse" in res.output


@pytest.mark.engine
def test_the_glide_section_carries_the_path(lay: Layout) -> None:  # noqa: F811
    goals.save_debt(lay, "car", balance=12_000, rate=0, payment=1_000)
    section = year.assemble(lay, 2026, AS_OF).section("glide")
    assert section is not None and section.ok
    text = "\n".join(section.lines)
    assert "RMDs (you): from age 75 in 2046" in text
    assert "debt car: paid off 2027-06" in text
    assert "case 30% drop in year one" in text and "case floor returns" in text
    assert "no success percentage" in text
    tbl = section.tables[0]
    assert tbl.headers[:4] == ["year", "age", "cash", "taxable"]
    assert len(tbl.rows) == 41 and tbl.rows[-1][:2] == ["2066", "95"]
    rmd = tbl.headers.index("RMD")
    assert all(float(rw[rmd].replace(",", "")) == 0 for rw in tbl.rows[:20])
    assert float(tbl.rows[20][rmd].replace(",", "")) > 0  # 2046, age 75
    assert any("state tax at this year's share of AGI" in n for n in section.notes)
