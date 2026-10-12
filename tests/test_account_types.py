"""SIMPLE IRA and governmental 457(b) accounts (synthetic): the SIMPLE IRA's
2-year window (IRC 72(t)(6), 408(d)(3)(G)) gates conversion; a 457(b) is open
on leaving the employer, except money rolled in from another plan or an IRA
before 59 1/2 (Pub. 575, IRC 457(d)(1)(A))."""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest import ingest
from planner.ingest.needs import enter, needed
from planner.ledger import portfolio
from planner.paths import Layout
from planner.taxprep import capgains, expected
from tests.test_csv import FUND, TXN_HEADER, drop
from tests.test_portfolio import lay, loaded  # noqa: F401

runner = CliRunner()


def _missing(lay: Layout) -> set[str]:  # noqa: F811
    return {s.need.key for s in needed(lay, 2026).items if s.state == "missing"}


def _status(lay: Layout, day: str) -> portfolio.Status:  # noqa: F811
    d = date.fromisoformat(day)
    return portfolio.status(lay, d.year, d)


def test_simple_ira_converts_only_after_its_two_years(lay: Layout) -> None:  # noqa: F811
    loaded(lay)
    env = {"PLANNER_HOME": str(lay.root)}
    enter(lay, 2026, "account:33333333", "simple_ira")
    assert "account:33333333:simple" in _missing(lay)
    st = _status(lay, "2026-10-02")
    assert st.convertible == 0
    assert any("33333333" in n and "first contribution" in n for n in st.notes)
    r = runner.invoke(
        app, ["convert", "2026-11-02", "1000", "--from", "33333333"], env=env
    )
    assert r.exit_code == 2 and "no first contribution date" in r.output
    enter(lay, 2026, "account:33333333:simple", "2025-03-01")
    assert "account:33333333:simple" not in _missing(lay)
    st = _status(lay, "2026-10-02")
    assert st.simple_free == {"33333333": "2027-03-01"}
    assert st.convertible == 0 and st.locked == pytest.approx(60250)
    assert any("2-year window until 2027-03-01" in n and "25%" in n for n in st.notes)
    r = runner.invoke(
        app, ["convert", "2026-11-02", "1000", "--from", "33333333"], env=env
    )
    assert r.exit_code == 2 and "inside its 2-year window until 2027-03-01" in r.output
    st = _status(lay, "2027-03-01")
    assert st.convertible == pytest.approx(60250)
    assert not any("2-year window" in n for n in st.notes)
    r = runner.invoke(
        app, ["convert", "2027-03-02", "1000", "--from", "33333333"], env=env
    )
    assert r.exit_code == 0 and "recorded conversion" in r.output


def test_traditional_ira_is_convertible_and_457b_is_not(lay: Layout) -> None:  # noqa: F811
    loaded(lay)
    env = {"PLANNER_HOME": str(lay.root)}
    portfolio.save_account(lay, "33333333", type="trad_ira")
    portfolio.save_account(lay, "44444444", type="gov_457b")
    assert _status(lay, "2026-10-02").convertible == pytest.approx(60250)
    r = runner.invoke(
        app, ["convert", "2026-11-02", "1000", "--from", "44444444"], env=env
    )
    assert r.exit_code == 2 and "only a traditional or SIMPLE IRA" in r.output


def test_457b_opens_on_leaving_the_employer_less_rolled_in_money(lay: Layout) -> None:  # noqa: F811
    loaded(lay)
    for number, kind in (("12345678", "taxable"), ("22222222", "roth")):
        portfolio.save_account(lay, number, type=kind)
    portfolio.save_account(lay, "Checking", type="cash", balance=10000.0)
    base = 125514.82 + 10000
    enter(lay, 2026, "account:44444444", "gov_457b")
    asked = {k for k in _missing(lay) if k.startswith("account:44444444")}
    assert asked == {"account:44444444:separated"}  # rollovers matter only once out
    st = _status(lay, "2026-10-02")
    assert st.accessible == pytest.approx(base)
    assert any(
        "44444444" in n and "left that employer not entered" in n for n in st.notes
    )
    enter(lay, 2026, "account:44444444:separated", "no")
    st = _status(lay, "2026-10-02")
    assert st.accessible == pytest.approx(base)
    assert any("still with that employer" in n for n in st.notes)
    with pytest.raises(ValueError):
        enter(lay, 2026, "account:44444444:separated", "maybe")
    enter(lay, 2026, "account:44444444:separated", "yes")
    assert "account:44444444:rolled" in _missing(lay)
    st = _status(lay, "2026-10-02")
    assert st.accessible == pytest.approx(base)
    assert any("rolled in" in n and "not entered" in n for n in st.notes)
    enter(lay, 2026, "account:44444444:rolled", "4,100")
    st = _status(lay, "2026-10-02")
    assert st.accessible == pytest.approx(base + 20000)
    assert any("4,100.00 rolled in" in n and "10%" in n for n in st.notes)
    # 59 1/2 before the year: the rolled-in money is open too
    enter(lay, 2026, "birth_date", "1960-01-01")
    st = _status(lay, "2026-10-02")
    assert st.accessible == pytest.approx(base + 24100)
    assert not any("44444444" in n for n in st.notes)
    statuses = {s.need.key: s for s in needed(lay, 2026).items}
    assert statuses["account:44444444:separated"].value == "yes"
    assert statuses["account:44444444:rolled"].value == 4100


def test_cli_account_takes_the_new_fields(lay: Layout) -> None:  # noqa: F811
    env = {"PLANNER_HOME": str(lay.root)}
    r = runner.invoke(
        app,
        ["account", "7001", "--type", "gov_457b", "--separated", "--rolled-in", "250"],
        env=env,
    )
    assert r.exit_code == 0, r.output
    assert "separated=True" in r.output and "rolled_in=250.0" in r.output
    r = runner.invoke(
        app,
        [
            "account",
            "7002",
            "--type",
            "simple_ira",
            "--first-contribution",
            "2024-07-15",
        ],
        env=env,
    )
    assert r.exit_code == 0 and "first_contribution=2024-07-15" in r.output
    assert portfolio.simple_free(portfolio.load_accounts(lay)["7002"]) == date(
        2026, 7, 15
    )
    r = runner.invoke(app, ["account", "7002", "--first-contribution", "July"], env=env)
    assert r.exit_code == 2 and "refused" in r.output
    with pytest.raises(ValueError):
        portfolio.save_account(lay, "7001", rolled_in=-5)


def test_plan_accounts_skip_schedule_d_and_expect_a_1099r(lay: Layout) -> None:  # noqa: F811
    assert {"simple_ira", "gov_457b"} <= set(capgains.NOT_REPORTED)
    drop(
        lay,
        "plans.csv",
        "\n".join(
            [
                "Account Number,Investment Name,Symbol,Shares,Share Price,Total Value,",
                f"7101,{FUND},VTSAX,10.0,120.50,1205.00,",
                f"7102,{FUND},VTSAX,10.0,120.50,1205.00,",
                "",
                TXN_HEADER,
                f"7101,02/03/2026,02/03/2026,Contribution,Employer contribution,{FUND},"
                "VTSAX,1.0,120.50,120.50,0.0,120.50,0.0,RETIREMENT,",
                f"7102,02/03/2026,02/03/2026,Contribution,Payroll deferral,{FUND},"
                "VTSAX,1.0,120.50,120.50,0.0,120.50,0.0,RETIREMENT,",
                f"7102,03/03/2026,03/03/2026,Withdrawal,Distribution,{FUND},"
                "VTSAX,-1.0,120.50,-120.50,0.0,-120.50,0.0,RETIREMENT,",
                "",
            ]
        ),
    )
    ingest(lay)
    portfolio.save_account(lay, "7101", type="simple_ira")
    portfolio.save_account(lay, "7102", type="gov_457b")
    inv = expected.inventory(lay, 2026, date(2026, 12, 1))
    why = {(e.form, e.reason) for e in inv.items}
    assert ("5498", "contribution to 7101") in why
    assert ("1099-R", "money out of 7102") in why
    # 457(b) deferrals show on the W-2 (box 12 code G), not a 5498
    assert ("5498", "contribution to 7102") not in why
