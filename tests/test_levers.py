"""Phase 4f: the year-end levers, what-if and threshold drift, on the Phase 4c
household (test_withdraw's lots fixture: 80,000 SE income, over the ACA cliff,
a VTSAX loss lot bought back inside the wash window)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.engine.tax import TaxResult, compute
from planner.ingest.needs import enter
from planner.ledger import portfolio
from planner.paths import Layout
from planner.plan import conversion, levers, year
from planner.plan.inputs import Overrides
from tests.test_spending import AS_OF, lay  # noqa: F401
from tests.test_withdraw import lots  # noqa: F401

runner = CliRunner()
CLIFF = "ACA 400% FPL cliff"


def answered(home: Layout, **extra: str) -> Layout:
    for key, text in {"hsa_coverage": "self", "workplace_plan": "no", **extra}.items():
        enter(home, 2026, key, text)
    return home


@pytest.mark.engine
def test_catalog_sizes_from_the_ledger_and_names_what_it_needs(
    lots: Layout,  # noqa: F811
) -> None:
    _, found, _ = levers.catalog(lots, 2026, AS_OF)
    by = {lv.key: lv for lv in found}
    assert [lv.key for lv in found] == [
        "pair_losses",
        "harvest_losses",
        "spend_basis",
        "defer_sales",
        "donate_shares",
        "daf_bunch",
        "hsa",
        "se_health",
        "traditional_ira",
        "carryforward",
        "conversion",
        "gain_harvest",
        "inherited_ira",
        "roth_contribution",
    ]
    # unanswered: never guessed, the Needed panel question is named
    assert not by["hsa"].available and by["hsa"].why.startswith("needs hsa_coverage")
    assert not by["traditional_ira"].available
    assert by["traditional_ira"].why.startswith("needs workplace_plan")
    assert by["carryforward"].why.startswith("needs prior_capital_loss")
    harvest = by["harvest_losses"]
    assert (harvest.amount, harvest.friction) == (10_000.0, levers.WASH)
    assert harvest.delta == (("short_term_gains", -10_000),)
    assert "bought 2026-06-20: selling before 2026-07-21 washes it" in harvest.why
    assert by["se_health"].delta == (("se_health_premiums", 3_600),)
    assert not by["defer_sales"].available and not by["inherited_ira"].available
    _, found, _ = levers.catalog(lots, 2026, AS_OF, Overrides(planned_lt_sales=5_000.0))
    defer = next(lv for lv in found if lv.key == "defer_sales")
    assert defer.delta == (("long_term_gains", -5_000),)


@pytest.mark.engine
def test_se_health_lever_counts_the_advance_credit_in_the_premiums(
    lots: Layout,  # noqa: F811
) -> None:
    """premium_monthly is what you pay after the advance credit; the household
    carries the premiums before it, so the lever adds the advance back."""
    enter(lots, 2026, "aptc", "1,200")
    _, found, _ = levers.catalog(lots, 2026, AS_OF)
    lever = next(lv for lv in found if lv.key == "se_health")
    assert lever.delta == (("se_health_premiums", 4_800),)  # 300 x 12 + 1,200
    assert "1,200 advance credit" in lever.why and "Pub. 974" in lever.side_effects


@pytest.mark.engine
def test_menu_ranks_the_moves_and_gets_under_the_cliff(lots: Layout) -> None:  # noqa: F811
    m = levers.menu(answered(lots), 2026, AS_OF)
    assert m.target is not None and m.target.name == CLIFF
    names = [rw.name for rw in m.lower]
    assert names[0] == levers.DO_NOTHING
    assert set(names[1:]) == {"traditional_ira", "hsa", "se_health", "harvest_losses"}
    assert [rw.net for rw in m.lower[1:]] == sorted(
        (rw.net for rw in m.lower[1:]), reverse=True
    )
    by = {rw.name: rw.lever for rw in m.lower[1:]}
    assert by["hsa"] is not None and by["hsa"].amount == 5_400.0  # 4,400 + 55+ 1,000
    payroll = levers.menu(
        answered(lots, hsa_employer_contributions="1,500"), 2026, AS_OF
    )
    hsa_lever = {rw.name: rw.lever for rw in payroll.lower[1:]}["hsa"]
    assert hsa_lever is not None and hsa_lever.amount == 3_900.0  # less W-2 code W
    assert "1,500 through payroll" in hsa_lever.why
    assert by["traditional_ira"] is not None
    assert by["traditional_ira"].amount == 8_600.0  # 7,500 + 50+ 1,100
    assert m.together is not None and m.together.net > 0
    assert f"{CLIFF} (now under)" in m.together.crosses
    assert m.together.result.aca_ptc > 0 and m.base.aca_ptc == 0
    # the room levers share one room and are ranked by cost per dollar
    rooms = [rw for rw in m.rooms[1:] if rw.rate is not None]
    assert m.room is not None and all(rw.lever.amount <= m.room for rw in rooms)  # type: ignore[union-attr]
    assert [rw.rate for rw in rooms] == sorted(rw.rate for rw in rooms)  # type: ignore[type-var]
    roth = next(rw for rw in m.rooms if rw.name == "roth_contribution")
    assert roth.net == 0 and roth.rate is None
    text = levers.render_menu(m)
    assert f"get under: {CLIFF}, over by" in text
    assert f"{'carryforward':20} not available: needs prior_capital_loss" in text
    assert "note: specific-lot selection" in text


@pytest.mark.engine
def test_whatif_recomputes_the_year(lots: Layout) -> None:  # noqa: F811
    lay_ = answered(lots)
    w = levers.whatif(lay_, 2026, ["traditional_ira", "hsa"], {"hsa": 1_000.0}, AS_OF)
    assert [lv.amount for lv in w.applied] == [8_600.0, 1_000.0]
    assert w.after.agi == pytest.approx(w.before.agi - 9_600, abs=0.01)
    assert w.net == pytest.approx(levers.cost(w.before) - levers.cost(w.after))
    assert "hsa 1,000" in levers.render_whatif(w)
    with pytest.raises(ValueError, match="unknown lever nope"):
        levers.whatif(lay_, 2026, ["nope"], as_of=AS_OF)
    with pytest.raises(ValueError, match="carryforward is not available: needs"):
        levers.whatif(lay_, 2026, ["carryforward"], as_of=AS_OF)
    with pytest.raises(ValueError, match="--set hsa: add it to --apply"):
        levers.whatif(lay_, 2026, ["se_health"], {"hsa": 500.0}, AS_OF)


@pytest.mark.engine
def test_whatif_refuses_what_menu_would_never_stack(lots: Layout) -> None:  # noqa: F811
    """Overlapping levers, a key twice, a size at or under zero or past what the
    lever can move are refused, not priced as a saving that cannot happen."""
    lay_ = answered(lots)
    ov = Overrides(planned_lt_sales=8_000.0)
    with pytest.raises(ValueError, match="spend_basis and defer_sales move the same"):
        levers.whatif(lay_, 2026, ["defer_sales", "spend_basis"], as_of=AS_OF, ov=ov)
    with pytest.raises(ValueError, match="hsa is listed twice"):
        levers.whatif(lay_, 2026, ["hsa", "hsa"], as_of=AS_OF)
    for bad in (0.0, -500.0, float("nan")):
        with pytest.raises(ValueError, match="--set hsa=.*more than 0"):
            levers.whatif(lay_, 2026, ["hsa"], {"hsa": bad}, AS_OF)
    with pytest.raises(
        ValueError, match=r"--set defer_sales=9,000 is more than .*8,000"
    ):
        levers.whatif(lay_, 2026, ["defer_sales"], {"defer_sales": 9_000.0}, AS_OF, ov)
    # a smaller size is still allowed
    w = levers.whatif(lay_, 2026, ["defer_sales"], {"defer_sales": 2_000.0}, AS_OF, ov)
    assert [lv.amount for lv in w.applied] == [2_000.0]


@pytest.mark.engine
def test_room_when_income_is_low(lots: Layout) -> None:  # noqa: F811
    m = levers.menu(answered(lots, se_income="30,000"), 2026, AS_OF)
    # Medicaid tests monthly income when you apply: never the year-end target
    assert m.target is None
    assert m.room_line is not None and m.room_line.name == "ACA CSR 250% FPL"
    priced = [rw for rw in m.rooms if rw.rate is not None]
    assert {rw.name for rw in priced} == {"conversion", "gain_harvest"}
    assert all(rw.lever.amount == m.room for rw in priced)  # type: ignore[union-attr]
    # NC taxes the gain, the conversion is ordinary income: the gain is cheaper
    assert priced[0].name == "gain_harvest"
    assert any(n.startswith("together the moves overshoot") for n in m.notes)
    assert any(n.startswith("a move crosses the Medicaid line") for n in m.notes)


@pytest.mark.engine
def test_cli_levers_whatif_thresholds(lots: Layout) -> None:  # noqa: F811
    answered(lots)
    r = runner.invoke(app, ["levers", "--year", "2026", "--as-of", "2026-07-10"])
    assert r.exit_code == 0, r.output
    assert f"get under: {CLIFF}" in r.output and "together: net +" in r.output
    args = ["whatif", "--year", "2026", "--as-of", "2026-07-10"]
    r = runner.invoke(app, [*args, "--apply", "se_health", "--set", "se_health=1,200"])
    assert r.exit_code == 0, r.output
    assert "What if 2026: se_health 1,200" in r.output and "net (saved +)" in r.output
    r = runner.invoke(app, [*args, "--apply", "nope"])
    assert r.exit_code == 2 and "unknown lever nope" in r.output
    r = runner.invoke(app, ["thresholds", "--year", "2026"])
    assert r.exit_code == 0, r.output
    assert "std_deduction_single" in r.output and "engine agrees" in r.output
    # the engine's IRA limit lags Notice 2025-67; the config row wins for sizing
    assert "ENGINE HAS 7,000" in r.output and "row(s) differ" in r.output
    assert runner.invoke(app, ["thresholds", "--year", "1990"]).exit_code == 1


def _objective(home: Layout) -> None:
    for key, text in (
        ("conversion_objective", "bracket_12"),
        ("conversion_margin", "1,000"),
        ("conversion_cap", "150,000"),
    ):
        enter(home, 2026, key, text)


def _conversion_lever(home: Layout, ov: Overrides) -> levers.Lever:
    _, found, _ = levers.catalog(home, 2026, AS_OF, ov)
    return next(lv for lv in found if lv.key == "conversion")


@pytest.mark.engine
def test_adopted_conversion_is_not_proposed_again(lots: Layout) -> None:  # noqa: F811
    _objective(lots)
    manual = _conversion_lever(lots, Overrides())
    assert manual.available and manual.amount > 0
    # auto adopts the recommendation into the year; the lever sees it as done
    ov, sz = conversion.resolve(lots, 2026, Overrides(conversion_target="auto"))
    assert sz is not None and ov.planned_conversion > 0
    auto = _conversion_lever(lots, ov)
    assert not auto.available and auto.amount == 0.0
    assert auto.why.endswith("already met")
    page = year.assemble(lots, 2026, AS_OF, Overrides(conversion_target="auto"))
    assert not any(
        ln.startswith("Roth conversion") and "already met" not in ln
        for ln in page.section("levers").lines
    )


@pytest.mark.engine
def test_total_income_changes_the_conversion_lever_sizing(lots: Layout) -> None:  # noqa: F811
    _objective(lots)
    ledger = _conversion_lever(lots, Overrides())
    typed = _conversion_lever(lots, Overrides(total_income=120_000.0))
    # the typed 120,000 leaves no room for the objective; the ledger wages do
    assert ledger.available and ledger.amount > 0
    assert not typed.available and typed.amount == 0.0


def test_conversion_names_the_nc_rate_step(lay: Layout) -> None:  # noqa: F811
    """NC's 3.99% is scheduled to drop to 3.49% in 2027: the conversion lever
    says what waiting would save on the NC side."""
    _, found, _ = levers.catalog(lay, 2026, AS_OF)
    conv = next(lv for lv in found if lv.key == "conversion")
    assert conv.available, conv.why
    saved = round(conv.amount * (0.0399 - 0.0349))
    assert (
        f"NC tax on it would be {saved:,} lower in 2027 at 3.49% (scheduled): "
        "a conversion that can wait saves that"
    ) in conv.side_effects


@pytest.mark.engine
def test_pair_losses_nets_gain_to_zero(lots: Layout) -> None:  # noqa: F811
    """The loss sold is sized to the year's realized gain, not every loser."""
    ov = Overrides(planned_lt_sales=8_000.0)
    ctx, found, _ = levers.catalog(lots, 2026, AS_OF, ov)
    gain = ctx.hh.short_term_gains + ctx.hh.long_term_gains
    assert 0 < gain < 10_000
    by = {lv.key: lv for lv in found}
    pair, harvest = by["pair_losses"], by["harvest_losses"]
    assert pair.amount == gain and pair.friction == levers.WASH
    paired = pair.apply(ctx.hh)
    assert paired.short_term_gains + paired.long_term_gains == 0
    assert f"nets the year's realized gain of {gain:,.0f} to 0" in pair.why
    assert harvest.amount == 10_000 - gain  # the rest of the loss, past the gains
    m = levers.menu(answered(lots), 2026, AS_OF, ov)
    rows = {rw.name: rw for rw in m.lower[1:]}
    assert {"pair_losses", "harvest_losses", "spend_basis"} <= set(rows)
    # inside the set the other losses already cover the gains; alone it saves
    assert rows["pair_losses"].net >= 0
    alone = levers.whatif(lots, 2026, ["pair_losses"], as_of=AS_OF, ov=ov)
    assert alone.net > 0 and alone.after.agi == alone.before.agi - gain
    # no realized gain: nothing to pair, the whole loss is the harvest
    _, found, _ = levers.catalog(lots, 2026, AS_OF)
    by = {lv.key: lv for lv in found}
    assert not by["pair_losses"].available
    assert by["pair_losses"].why.startswith("no realized gain")
    assert by["harvest_losses"].amount == 10_000.0


@pytest.mark.engine
def test_spend_basis_zero_magi(lots: Layout) -> None:  # noqa: F811
    """Cash and Roth basis fund spending with no MAGI: the planned gain is
    never realized. Deferring the same gain is an alternative, not stacked."""
    ov = Overrides(planned_lt_sales=8_000.0)
    _, found, _ = levers.catalog(lots, 2026, AS_OF, ov)
    spend = next(lv for lv in found if lv.key == "spend_basis")
    assert (spend.amount, spend.delta) == (8_000.0, (("long_term_gains", -8_000),))
    assert "200,000 of cash and 0 of Roth basis" in spend.why
    m = levers.menu(answered(lots), 2026, AS_OF, ov)
    rows = {rw.name: rw for rw in m.lower[1:]}
    # deferring is priced alone; spending basis alone saves the same
    alone = levers.whatif(answered(lots), 2026, ["spend_basis"], as_of=AS_OF, ov=ov)
    assert rows["defer_sales"].net > 0 and alone.net == rows["defer_sales"].net
    assert any(
        n.startswith("defer_sales and spend_basis move the same") for n in m.notes
    )
    ctx, found, _ = levers.catalog(answered(lots), 2026, AS_OF, ov)
    stacked = [
        lv
        for lv in found
        if lv.mode == levers.LOWER and lv.available and lv.key != "defer_sales"
    ]
    assert m.together is not None
    together = compute(2026, levers._apply(ctx.hh, stacked))
    assert m.together.result.aca_magi == together.aca_magi
    _, found, _ = levers.catalog(lots, 2026, AS_OF)
    idle = next(lv for lv in found if lv.key == "spend_basis")
    assert not idle.available and idle.why.startswith("no planned sale")
    assert "200,000 of cash" in idle.why and "spend with no MAGI" in idle.why


@pytest.mark.engine
def test_spend_basis_avoids_only_the_gain_in_the_proceeds_replaced(
    lots: Layout,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2,000 of cash against a planned 20,000 long-term gain drawn from the 2019
    lot (260,000 of gain in 560,000 of value) avoids 2,000 x 260/560 of gain."""
    monkeypatch.setattr(levers, "zero_magi_funds", lambda st: (2_000.0, 0.0))
    ov = Overrides(planned_lt_sales=20_000.0)
    _, found, _ = levers.catalog(lots, 2026, AS_OF, ov)
    spend = next(lv for lv in found if lv.key == "spend_basis")
    avoided = round(2_000 * 260 / 560, 2)
    assert spend.amount == avoided
    assert spend.delta == (("long_term_gains", -round(avoided)),)
    assert "2,000 of sale proceeds" in spend.why


@pytest.mark.engine
def test_income_targeting_sets_medicaid_beside_the_400_cliff(lots: Layout) -> None:  # noqa: F811
    """Medicaid is never the optimizer's pick: the Medicaid line and the
    just-under-400% option are priced side by side for the owner to choose."""
    m = levers.menu(answered(lots, se_income="30,000"), 2026, AS_OF)
    assert m.target is None
    names = [t.name for t in m.targeting]
    assert names[-1] == "aca_400" and names[0] in ("medicaid_under", "medicaid_over")
    for t in m.targeting:
        assert t.fed_delta >= 0 and 0 <= t.medicaid_months <= 12
    cliff = m.targeting[-1]
    assert cliff.aca_magi < m.base.aca_fpg * 4 and cliff.medicaid_months == 0
    text = "\n".join(levers.summary(m))
    assert "income targeting (your choice; never picked for you):" in text
    assert "Medicaid 0 of 12 months" in text
    assert "aca_400" not in {lv.key for lv in m.levers}


@pytest.mark.engine
def test_donate_appreciated_priced_when_itemizing(lots: Layout) -> None:  # noqa: F811
    """Shares instead of cash: the same deduction, the gain never taxed. A
    donor-advised fund bunches next year's gift into this one, worth what the
    engine says only once itemizing beats the standard deduction."""
    _, found, _ = levers.catalog(lots, 2026, AS_OF)
    by = {lv.key: lv for lv in found}
    assert by["donate_shares"].why.startswith("needs planned_giving")
    assert by["daf_bunch"].why.startswith("needs planned_giving")
    answered(lots, planned_giving="12,000", real_estate_taxes="9,000")
    ctx, found, _ = levers.catalog(lots, 2026, AS_OF)
    by = {lv.key: lv for lv in found}
    give, daf = by["donate_shares"], by["daf_bunch"]
    assert ctx.base.itemizes and ctx.base.itemized_deductions > 21_000
    assert give.amount == 12_000.0
    assert give.delta == (("charitable_cash", -12_000), ("charitable_shares", 12_000))
    # the 2019 lot first (most gain per dollar): 12,000 x 260/560
    assert "5,571 of gain is never taxed" in give.why
    assert (daf.amount, daf.friction) == (12_000.0, levers.IRREVERSIBLE)
    assert daf.delta == (("charitable_shares", 12_000),)
    assert "itemized" in daf.why
    alone = levers.whatif(lots, 2026, ["daf_bunch"], as_of=AS_OF)
    assert alone.net > 0
    m = levers.menu(lots, 2026, AS_OF)
    assert {"donate_shares", "daf_bunch"} <= {rw.name for rw in m.lower[1:]}
    assert not any("donating appreciated shares" in n for n in m.notes)
    # a small gift and nothing else to itemize: the standard deduction wins
    enter(lots, 2026, "planned_giving", "500")
    enter(lots, 2026, "real_estate_taxes", "0")
    _, found, _ = levers.catalog(lots, 2026, AS_OF)
    daf = next(lv for lv in found if lv.key == "daf_bunch")
    assert "the standard deduction is larger" in daf.why


def test_swap_flags_a_high_yield_fund_when_the_gain_is_about_zero() -> None:
    bond = "VANGUARD TOTAL BOND MARKET INDEX ADMIRAL"
    reit = "VANGUARD REAL ESTATE INDEX ADMIRAL"
    stock = "VANGUARD TOTAL STOCK MARKET INDEX ADMIRAL"
    st = portfolio.Status(
        "2026-07-10",
        2026,
        positions=[
            portfolio.Position("9", "Brokerage", "taxable", 0.0, "", "typed"),
            portfolio.Position("8", "IRA", "trad_ira", 0.0, "", "typed"),
        ],
        lots=[
            portfolio.Lot(
                "9", "VBTLX", "2026-01-05", "short", 4_000, 40_100, 40_000, bond
            ),
            portfolio.Lot("9", "VGSLX", "2020-02-03", "long", 100, 5_000, 9_000, reit),
            portfolio.Lot("9", "VTSAX", "2021-03-01", "long", 100, 9_000, 9_050, stock),
            portfolio.Lot("8", "VBTLX", "2020-01-02", "long", 100, 1_000, 1_000, bond),
        ],
    )
    found, notes = levers.swaps(st)
    assert [(s.account, s.symbol, s.gain) for s in found] == [("9", "VBTLX", -100.0)]
    assert notes == [
        f"VGSLX ({reit}) in 9: selling realizes 4,000 of gain; hold it, or move "
        "new money into a broad index fund instead"
    ]
    text = "\n".join(levers.swap_lines(found))
    assert "swap VBTLX" in text and "next year's MAGI" in text


@pytest.mark.engine
def test_lots_carry_the_fund_name(lots: Layout) -> None:  # noqa: F811
    st = portfolio.status(lots, 2026, AS_OF)
    assert {lot.name for lot in st.lots} == {
        "VANGUARD TOTAL STOCK MARKET INDEX ADMIRAL"
    }
    m = levers.menu(answered(lots), 2026, AS_OF)
    assert m.swaps == [] and not any("tax-efficient funds" in n for n in m.notes)


@pytest.mark.engine
def test_cost_counts_the_credit_allowed_once_when_the_advance_is_above_it() -> None:
    """Form 8962: an advance above the credit allowed is repaid on line 29, which
    line 24 holds (2026: no repayment cap); only a credit above the advance is
    paid out (line 26, Schedule 3 line 9). So the household's cost is line 24
    less the refundable credits and that net credit, plus the state tax: a
    raise that shrinks the credit allowed costs each credit dollar once, not
    twice. Synthetic single filer."""
    from dataclasses import replace

    from planner.engine.household import Household

    base = Household(
        age=45,
        filing_status="SINGLE",
        state="NC",
        wages=40_000,
        aptc=9_000,
        slcsp_monthly=600,
    )
    a, b = compute(2026, base), compute(2026, replace(base, wages=42_000))
    assert a.aptc_repayment > 0 and b.aptc_repayment > 0  # both repay an excess
    assert b.aca_ptc < a.aca_ptc  # the raise shrinks the credit allowed

    def owed(res: TaxResult) -> float:
        return res.fed_total_tax + res.state_tax - res.refundable_credits - res.net_ptc

    assert levers.cost(b) - levers.cost(a) == pytest.approx(owed(b) - owed(a), abs=0.02)
