"""Phase 4f: the year-end levers, what-if and threshold drift, on the Phase 4c
household (test_withdraw's lots fixture: 80,000 SE income, over the ACA cliff,
a VTSAX loss lot bought back inside the wash window)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest.needs import enter
from planner.paths import Layout
from planner.plan import levers
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
    assert not by["defer_sales"].available
    _, found, _ = levers.catalog(lots, 2026, AS_OF, Overrides(planned_lt_sales=5_000.0))
    defer = next(lv for lv in found if lv.key == "defer_sales")
    assert defer.delta == (("long_term_gains", -5_000),)


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
    assert by["traditional_ira"] is not None
    assert by["traditional_ira"].amount == 8_600.0  # 7,500 + 50+ 1,100
    assert m.together is not None and m.together.net > 0
    assert f"{CLIFF} (now under)" in m.together.crosses
    assert m.together.result.aca_ptc > 0 and m.base.aca_ptc == 0
    text = levers.render_menu(m)
    assert f"get under: {CLIFF}, over by" in text
    assert f"{'carryforward':20} not available: needs prior_capital_loss" in text
    assert "note: specific-lot selection" in text


@pytest.mark.engine
def test_cli_levers_thresholds(lots: Layout) -> None:  # noqa: F811
    answered(lots)
    r = runner.invoke(app, ["levers", "--year", "2026", "--as-of", "2026-07-10"])
    assert r.exit_code == 0, r.output
    assert f"get under: {CLIFF}" in r.output and "together: net +" in r.output
    r = runner.invoke(app, ["thresholds", "--year", "2026"])
    assert r.exit_code == 0, r.output
    assert "std_deduction_single" in r.output and "engine agrees" in r.output
    # the engine's IRA limit lags Notice 2025-67; the config row wins for sizing
    assert "ENGINE HAS 7,000" in r.output and "row(s) differ" in r.output
    assert runner.invoke(app, ["thresholds", "--year", "1990"]).exit_code == 1
