"""Phase 4d: estimated tax — the safe harbor, four installments, payments from
bank rows and typed ones, and the next due amount."""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest import ingest
from planner.ingest.needs import enter
from planner.paths import Layout
from planner.plan import esttax, glidepath, magi
from tests.test_spending import lay  # noqa: F401

runner = CliRunner()
BANK2 = """\
Transaction ID,Date,Description,Amount,Account
T-9,04/10/2026,IRS USATAXPYMT,(1200.00),Checking
T-10,06/20/2026,NCDOR TAX PYMT,(300.00),Checking
"""


def test_safe_harbor_and_installments(lay: Layout) -> None:  # noqa: F811
    enter(lay, 2026, "se_income", "80,000")
    enter(lay, 2026, "prior_agi", "70,000")
    enter(lay, 2026, "prior_total_tax", "8,000")
    enter(lay, 2026, "prior_nc_tax", "2,000")
    (lay.data / "inbox" / "bank2.csv").write_text(BANK2, encoding="utf-8")
    ingest(lay)
    esttax.record(lay, 2026, "nc", "2026-04-12", 400.0)
    et = esttax.estimate(lay, 2026, date(2026, 7, 10))
    fed, nc = et.agencies
    assert fed.name == "fed" and nc.name == "nc"
    # prior-year tax is the lower leg for both (projected tax well above it)
    assert fed.required == 8_000.0 and "last year" in fed.basis
    assert nc.required == 2_000.0
    assert [p.amount for p in fed.payments] == [1_200.0]
    assert [(p.date, p.amount, p.origin) for p in nc.payments] == [
        ("2026-04-12", 400.0, "typed"),
        ("2026-06-20", 300.0, "bank bank2.csv"),
    ]
    assert [i.due for i in fed.installments] == [
        "2026-04-15",
        "2026-06-15",
        "2026-09-15",
        "2027-01-15",
    ]
    assert [i.required for i in fed.installments] == [
        2_000.0,
        4_000.0,
        6_000.0,
        8_000.0,
    ]
    assert [i.paid for i in fed.installments] == [1_200.0] * 4
    assert fed.installments[0].shortfall == 800.0
    # the June NC payment landed after June 15: it does not cure installment 2
    assert [i.paid for i in nc.installments] == [400.0, 400.0, 700.0, 700.0]
    assert (
        nc.installments[1].shortfall == 600.0 and nc.installments[2].shortfall == 800.0
    )
    assert (fed.next_due, fed.next_amount) == ("2026-09-15", 4_800.0)
    assert (nc.next_due, nc.next_amount) == ("2026-09-15", 800.0)
    assert fed.penalty is not None and fed.penalty > 0
    # D-422 interest on the April and June shortfalls the plan cures
    assert nc.penalty is not None and nc.penalty > 0
    assert any("Form D-422 interest" in n for n in nc.notes)
    assert any("were short" in n for n in fed.notes)
    # 110% leg over 150k prior AGI; de minimis under 1,000
    assert esttax.safe_harbor("fed", 100_000.0, 20_000.0, 160_000.0) == (
        22_000.0,
        "110% of last year's tax",
    )
    assert esttax.safe_harbor("nc", 100_000.0, 20_000.0, 160_000.0)[0] == 20_000.0
    assert esttax.safe_harbor("fed", 5_000.0, None, None) == (
        4_500.0,
        "90% of this year's projected tax (prior year unknown)",
    )
    enter(lay, 2026, "se_income", "6,000")
    enter(lay, 2026, "fed_withheld", "500")
    small = esttax.estimate(lay, 2026, date(2026, 7, 10)).agencies[0]
    assert small.de_minimis and small.installments[-1].required == 0.0


def test_current_year_tax_is_net_of_refundable_credits(lay: Layout) -> None:  # noqa: F811
    """Form 2210 Part I and the 1040-ES worksheet take the EITC and other
    refundable credits off line 24 before the 90% test; compute's fed_total_tax is
    the gross line 24."""
    enter(lay, 2026, "se_income", "12,345")
    res = magi.project(lay, 2026).result
    assert res.refundable_credits > 0
    fed = esttax.estimate(lay, 2026, date(2026, 7, 10)).agencies[0]
    assert fed.name == "fed"
    assert fed.current_tax == round(res.fed_total_tax - res.refundable_credits, 2)
    assert fed.current_tax < res.fed_total_tax


def test_lumpy_income_flags_the_annualized_method(lay: Layout) -> None:  # noqa: F811
    enter(lay, 2026, "se_income", "30,000")
    et = esttax.estimate(lay, 2026, date(2026, 7, 10))
    assert et.annualized  # the fixture's deposits all land in Q1
    assert any("annualized" in n for n in et.notes)


def test_cli_esttax_and_paid(lay: Layout) -> None:  # noqa: F811
    enter(lay, 2026, "se_income", "80,000")
    r = runner.invoke(
        app,
        [
            "paid",
            "--year",
            "2026",
            "--agency",
            "fed",
            "--on",
            "2026-04-10",
            "--amount",
            "1200",
        ],
    )
    assert r.exit_code == 0 and "installment 1" in r.output, r.output
    r = runner.invoke(app, ["esttax", "--year", "2026", "--as-of", "2026-07-10"])
    assert r.exit_code == 0, r.output
    assert "fed:" in r.output and "nc:" in r.output and "next:" in r.output
    assert "due 2026-09-15" in r.output and "1,200.00" in r.output


def test_cash_line_matches_esttax_installments(lay: Layout) -> None:  # noqa: F811
    enter(lay, 2026, "prior_agi", "70,000")
    enter(lay, 2026, "prior_total_tax", "8,000")
    enter(lay, 2026, "prior_nc_tax", "2,000")
    esttax.record(lay, 2026, "fed", "2026-04-10", 1_200.0)
    asof = date(2026, 7, 10)
    et = esttax.estimate(lay, 2026, asof)
    fed, nc = et.agencies
    g = glidepath.glide(lay, 2026, asof)
    row = {(m.year, m.month): m for m in g.months}
    # a past month shows what was paid, not a quarter of the year's tax
    assert row[(2026, 4)].est_tax == 1_200.0
    assert row[(2026, 6)].est_tax == 0.0
    # the next due date takes esttax's own next amount, both agencies
    assert fed.next_due == "2026-09-15" and nc.next_due == "2026-09-15"
    assert row[(2026, 9)].est_tax == round(fed.next_amount + nc.next_amount, 2)
    assert row[(2026, 9)].est_tax == 6_300.0
    # January's installment is the rest of the safe harbor, not another quarter
    assert row[(2027, 1)].est_tax == 2_500.0
    paid = sum(m.est_tax for m in g.months if (m.year, m.month) <= (2027, 1))
    assert paid == fed.required + nc.required == 10_000.0
    # months with no installment carry none
    assert [m.est_tax for m in g.months if m.year == 2026 and m.month in (1, 2, 3)] == [
        0.0
    ] * 3
    assert row[(2026, 8)].est_tax == 0.0 and row[(2026, 12)].est_tax == 0.0
    # next year: 90% of the repeated year less withholding, over the shifted dates
    due = esttax.due_dates(2027)
    nxt = [row[(d.year, d.month)].est_tax for d in due[:3]]
    want = round(0.9 * (fed.current_tax + nc.current_tax) / 4, 2)
    assert nxt == pytest.approx([want] * 3, abs=0.02)
    # what the installments leave unpaid falls due with the return
    # (and each agency's penalty or interest on the April and June shortfalls)
    owed = sum(ag.current_tax - ag.withheld - ag.required for ag in (fed, nc))
    owed += (fed.penalty or 0.0) + (nc.penalty or 0.0)
    assert row[(2027, 4)].balance_due == pytest.approx(owed, abs=0.02)
    assert sum(m.balance_due for m in g.months) == row[(2027, 4)].balance_due
    # the net of each month counts the payment and the balance due
    assert row[(2027, 4)].net == round(
        row[(2027, 4)].se
        + row[(2027, 4)].dividends
        - row[(2027, 4)].living
        - row[(2027, 4)].mortgage
        - row[(2027, 4)].premiums
        - row[(2027, 4)].est_tax
        - row[(2027, 4)].balance_due
        - row[(2027, 4)].irregular,
        2,
    )


def _agency(name: str, current: float, withheld: float) -> esttax.Agency:
    return esttax.Agency(
        name, current, None, None, 0.0, "test", withheld, current - withheld < 1_000.0
    )


def test_next_year_required_is_the_one_figure_behind_cash_and_calendar() -> None:
    et = esttax.EstTax(2026, "2026-07-10", 80_000.0, False)
    # tax 10,000 with 9,000 withheld: 1,000 left is not under the de minimis, but
    # withholding already meets next year's 90% safe harbor (IRC 6654(d)(1)(B))
    covered = _agency("fed", 10_000.0, 9_000.0)
    assert not covered.de_minimis
    assert esttax.next_year_required(covered, et.agi) == 0.0
    # 2,000 withheld leaves 90% of 10,000 less 2,000 to pay in installments
    short = _agency("nc", 10_000.0, 2_000.0)
    assert esttax.next_year_required(short, et.agi) == 7_000.0
    # under the de minimis nothing is required whatever the harbor says
    tiny = _agency("fed", 1_500.0, 600.0)
    assert tiny.de_minimis and esttax.next_year_required(tiny, et.agi) == 0.0
    et.agencies = [covered, short]
    flows, _ = esttax.cash_flows(et)
    nxt = {
        (f.agency, f.kind): f.amount for f in flows if f.kind.startswith("next year")
    }
    assert [nxt[("fed", f"next year installment {n}")] for n in (1, 2, 3)] == [0.0] * 3
    assert [nxt[("nc", f"next year installment {n}")] for n in (1, 2, 3)] == [
        1_750.0,
        1_750.0,
        1_750.0,
    ]
