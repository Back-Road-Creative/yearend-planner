"""Phase 10, unit 2d: one feasibility check for every move and every set of
moves (master plan rule 5, finding F05). Cash above the reserve, shares that
exist once the other moves have used theirs, and the IRA limit traditional and
Roth contributions share; sizing, the menu and whatif all ask it."""

from __future__ import annotations

import pytest

from planner.ingest import ingest
from planner.ingest.needs import enter
from planner.ledger import portfolio
from planner.paths import Layout
from planner.plan import conversion, feasible, levers
from tests.test_levers import _objective, answered
from tests.test_spending import AS_OF, FUND, lay  # noqa: F401
from tests.test_withdraw import lots  # noqa: F401

CASH_ACCOUNT = "22222222"  # test_spending's cash account; cash_target is 20,000


def _cash(home: Layout, balance: float) -> Layout:
    portfolio.save_account(home, CASH_ACCOUNT, type="cash", balance=balance)
    return home


def test_no_reasons_when_every_need_fits() -> None:
    f = feasible.Funds(cash=50_000.0, reserve=20_000.0, lots=((10_000.0, 4_000.0),))
    needs = {"hsa": feasible.Need(cash=5_000.0), "give": feasible.Need(shares=6_000)}
    v = feasible.check(f, needs, tax=2_000.0)
    assert v.ok and v.hard == [] and v.cash is None


def test_cash_counts_deposits_and_the_tax_against_the_reserve() -> None:
    f = feasible.Funds(cash=25_000.0, reserve=20_000.0)
    v = feasible.check(f, {"hsa": feasible.Need(cash=4_000.0)}, tax=1_500.0)
    assert not v.ok and v.hard == []
    assert v.cash == (
        "takes 5,500 of cash by the deadline (4,000 paid in, 1,500 of tax); 5,000 "
        "is on hand above the 20,000 reserve"
    )
    # a set that lowers the tax still needs its deposits: the refund comes later
    assert feasible.check(f, {"hsa": feasible.Need(cash=4_000.0)}, tax=-900.0).ok
    unset = feasible.Funds(cash=3_000.0, reserve=0.0, reserve_set=False)
    text = feasible.check(unset, {"hsa": feasible.Need(cash=4_000.0)}).cash
    assert text is not None and text.endswith("(cash_target not set: no reserve)")


def test_no_cash_account_is_not_checked_and_says_so() -> None:
    f = feasible.Funds(cash=None, reserve=20_000.0)
    v = feasible.check(f, {"hsa": feasible.Need(cash=99_000.0)}, tax=5_000.0)
    assert v.ok
    assert feasible.unchecked(f) == (
        "no account is typed cash: moves are not checked against cash on hand "
        "(planner account <number> --type cash --balance <amount>)"
    )


def test_shares_given_twice_and_gain_already_given_away_are_refused() -> None:
    # most gain per dollar first: the 2019-style lot, then the newer one
    f = feasible.Funds(
        cash=None, reserve=0.0, lots=((10_000.0, 8_000.0), (5_000.0, 500.0))
    )
    two = {
        "donate_shares": feasible.Need(shares=12_000),
        "daf_bunch": feasible.Need(shares=12_000),
    }
    v = feasible.check(f, two)
    assert v.hard == [
        "donate_shares and daf_bunch give 24,000 of shares held over a year; the "
        "taxable account holds 15,000"
    ]
    # 10,000 given takes the 8,000 of gain in the first lot; 500 is left to harvest
    v = feasible.check(
        f,
        {
            "donate_shares": feasible.Need(shares=10_000),
            "gain_harvest": feasible.Need(gain=2_000),
        },
    )
    assert v.hard == [
        "gain_harvest realizes 2,000 of long-term gain; 500 is left once "
        "donate_shares gives its shares"
    ]


def test_contributions_past_the_shared_ira_limit_are_refused() -> None:
    f = feasible.Funds(cash=None, reserve=0.0, ira_left=8_600.0)
    v = feasible.check(
        f,
        {
            "traditional_ira": feasible.Need(ira=8_600),
            "roth_contribution": feasible.Need(ira=8_600),
        },
    )
    assert v.hard == [
        "traditional_ira and roth_contribution share one IRA limit: 17,200 is more "
        "than the 8,600 left this year"
    ]


@pytest.mark.engine
def test_conversion_sizing_keeps_the_tax_inside_the_cash_above_the_reserve(
    lots: Layout,  # noqa: F811
) -> None:
    """F05: sizing capped the conversion by the IRA balance and conversion_cap
    only; the tax on it came out of the cash reserve."""
    _objective(lots)
    enter(lots, 2026, "se_income", "30,000")  # room under the 12% top to size
    full = conversion.size(lots, 2026).recommendation
    assert full is not None and full.cash_needed > 1_000
    sz = conversion.size(_cash(lots, 21_000.0), 2026)
    rec = sz.recommendation
    assert rec is not None and rec.amount < full.amount
    assert 0 < rec.cash_needed <= 1_000
    cut = next(n for n in sz.notes if n.startswith("bracket_12: cut to"))
    assert "1,000 is on hand above the 20,000 reserve" in cut
    none = conversion.size(_cash(lots, 15_000.0), 2026)
    assert none.recommendation is None
    assert any(
        n.startswith("bracket_12:") and "0 is on hand above the 20,000 reserve" in n
        for n in none.notes
    )


@pytest.mark.engine
def test_whatif_refuses_contributions_past_the_shared_ira_limit(
    lots: Layout,  # noqa: F811
) -> None:
    lay_ = answered(lots)
    with pytest.raises(ValueError, match="share one IRA limit: 17,200 is more"):
        levers.whatif(lay_, 2026, ["traditional_ira", "roth_contribution"], as_of=AS_OF)


@pytest.mark.engine
def test_whatif_and_menu_name_a_set_that_takes_the_reserve(
    lots: Layout,  # noqa: F811
) -> None:
    """Short of cash is shown, not refused: money can be moved in first."""
    lay_ = _cash(answered(lots), 25_000.0)
    w = levers.whatif(lay_, 2026, ["traditional_ira", "hsa"], as_of=AS_OF)
    assert w.short is not None
    assert w.short.startswith("takes 14,000 of cash by the deadline (14,000 paid in")
    assert "5,000 is on hand above the 20,000 reserve" in w.short
    assert f"cash: {w.short}" in levers.render_whatif(w)
    m = levers.menu(lay_, 2026, AS_OF)
    assert any(
        n.startswith("together the moves take more cash than is spare: takes")
        for n in m.notes
    )
    assert any(n.startswith("hsa: takes 5,400 of cash") for n in m.notes)
    roomy = levers.whatif(
        _cash(lay_, 200_000.0), 2026, ["traditional_ira", "hsa"], as_of=AS_OF
    )
    assert roomy.short is None


SMALL_LOT = "\n".join(
    [
        "Account Number,Investment Name,Symbol,Date Acquired,Shares,Total Cost,"
        "Market Value,Term",
        f'11111111,{FUND},VTSAX,01/15/2019,100.000,"5,000.00","15,000.00",Long-term',
        "",
    ]
)


@pytest.mark.engine
def test_menu_prices_a_second_gift_of_the_same_shares_alone(
    lay: Layout,  # noqa: F811
) -> None:
    """donate_shares and daf_bunch each picked the same 15,000 lot for a
    12,000 gift, and the combined set stacked both."""
    (lay.data / "inbox" / "basis.csv").write_text(SMALL_LOT, encoding="utf-8")
    ingest(lay)
    enter(lay, 2026, "planned_giving", "12,000")
    enter(lay, 2026, "real_estate_taxes", "9,000")
    m = levers.menu(lay, 2026, AS_OF)
    assert {"donate_shares", "daf_bunch"} <= {rw.name for rw in m.lower[1:]}
    assert any(
        n.startswith(
            "daf_bunch is priced alone, not stacked: donate_shares and "
            "daf_bunch give 24,000 of shares held over a year"
        )
        for n in m.notes
    )
    with pytest.raises(ValueError, match="taxable account holds 15,000"):
        levers.whatif(lay, 2026, ["donate_shares", "daf_bunch"], as_of=AS_OF)
