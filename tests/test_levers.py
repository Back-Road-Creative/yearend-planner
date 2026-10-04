"""Phase 4f: the year-end levers, what-if and threshold drift, on the Phase 4c
household (test_withdraw's lots fixture: 80,000 SE income, over the ACA cliff,
a VTSAX loss lot bought back inside the wash window)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest.needs import enter
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
        "harvest_losses",
        "defer_sales",
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
