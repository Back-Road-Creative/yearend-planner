"""New York City and Yonkers resident income tax is priced for a full-year
resident of New York who answers local_income_tax "nyc" or "yonkers", and added
to NY's tax in the estimated-tax figures (the 300 test, the installments, the
safe harbor, the underpayment penalty). Any other local tax stays a named gap.
Synthetic figures only."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from planner import coverage, states
from planner.engine import tax
from planner.engine.household import Household
from planner.ingest.needs import enter, need_for, offered_choices, parse_value
from planner.paths import Layout
from planner.plan import esttax
from tests.test_coverage import _home
from tests.test_spending import lay  # noqa: F401

YEAR = 2026
# Yonkers resident surcharge: 16.75% of New York State tax (policyengine-us
# tests/policy/baseline/gov/local/ny/yonkers/tax/income/ny_yonkers_income_tax.yaml)
YONKERS_RATE = 0.1675


def _hh(**kw: object) -> Household:
    base: dict[str, object] = {
        "age": 45,
        "filing_status": "SINGLE",
        "state": "NY",
        "wages": 90_000,
    }
    return Household(**(base | kw))  # type: ignore[arg-type]


def test_nyc_and_yonkers_are_choices_offered_only_in_new_york() -> None:
    local = need_for("local_income_tax")
    assert local.choices == ("no", "yes", "nyc", "yonkers")
    assert offered_choices(local, {"state": "NY"}) == ("no", "yes", "nyc", "yonkers")
    for code in ("NC", "NJ", "TX", None):
        assert offered_choices(local, {"state": code}) == ("no", "yes")
    assert parse_value(local, "NYC") == "nyc"
    assert parse_value(local, "Yonkers") == "yonkers"
    assert "nyc" in local.source and "yonkers" in local.source


@pytest.mark.parametrize("answer", ["nyc", "yonkers"])
def test_a_full_year_new_yorker_in_the_city_has_no_local_gap(
    planner_home: Path, answer: str
) -> None:
    home = _home(planner_home, state="NY")
    assert (
        coverage.gate(home, "NY", "SINGLE", residency="full_year", local=answer) == []
    )
    assert coverage.priced_local("NY", "full_year", answer) == answer


@pytest.mark.parametrize("answer", ["nyc", "yonkers"])
def test_the_city_answers_are_still_gaps_outside_new_york_or_part_year(
    planner_home: Path, answer: str
) -> None:
    home = _home(planner_home, state="NJ")
    (gap,) = coverage.gate(home, "NJ", "SINGLE", residency="full_year", local=answer)
    assert "city, county or school district" in gap.reason
    assert coverage.priced_local("NJ", "full_year", answer) is None
    ny = _home(planner_home, state="NY")
    reasons = [
        g.reason
        for g in coverage.gate(ny, "NY", "SINGLE", residency="moved", local=answer)
    ]
    assert any("part-year" in r for r in reasons)
    assert any("city, county or school district" in r for r in reasons)


def test_another_local_tax_in_new_york_is_still_a_gap(planner_home: Path) -> None:
    home = _home(planner_home, state="NY")
    (gap,) = coverage.gate(home, "NY", "SINGLE", residency="full_year", local="yes")
    assert "city, county or school district" in gap.reason
    assert coverage.gate(home, "NY", "SINGLE", residency="full_year", local="no") == []


def test_the_new_york_notes_no_longer_say_the_local_tax_is_left_out() -> None:
    rule, _ = states.rules("NY")
    assert not any("does not price" in n for n in rule.notes)
    pen = states.rules("NY")[0].penalty
    assert pen is not None
    assert not any("not in the figure" in n for n in pen.notes)


@pytest.mark.engine
def test_the_nyc_resident_tax_is_on_top_of_the_state_tax() -> None:
    plain = tax.compute(YEAR, _hh())
    nyc = tax.compute(YEAR, _hh(local_resident="nyc"))
    assert plain.local_tax == 0.0
    assert nyc.local_tax > 0
    # the engine's state tax does not already carry the city tax
    assert nyc.state_tax == plain.state_tax
    assert nyc.state_and_local_tax == pytest.approx(
        plain.state_tax + nyc.local_tax, abs=0.01
    )
    assert nyc.state_and_local_tax > plain.state_and_local_tax


@pytest.mark.engine
def test_the_nyc_figures_pinned_to_the_engine() -> None:
    # 16,000 of wages, single (synthetic): measured with policyengine-us 2.23.4
    hh = _hh(wages=16_000)
    assert tax.compute(YEAR, hh).state_tax == pytest.approx(230.76, abs=0.01)
    nyc = tax.compute(YEAR, replace(hh, local_resident="nyc"))
    assert nyc.local_tax == pytest.approx(107.28, abs=0.01)
    assert nyc.state_and_local_tax == pytest.approx(338.04, abs=0.01)


@pytest.mark.engine
def test_the_yonkers_resident_tax_is_the_surcharge_on_the_state_tax() -> None:
    plain = tax.compute(YEAR, _hh())
    yon = tax.compute(YEAR, _hh(local_resident="yonkers"))
    assert yon.local_tax == pytest.approx(YONKERS_RATE * plain.state_tax, abs=0.02)
    assert yon.state_tax == plain.state_tax
    assert yon.state_and_local_tax > plain.state_and_local_tax


@pytest.mark.engine
def test_no_local_answer_prices_no_local_tax() -> None:
    assert tax.compute(YEAR, _hh(local_resident=None)).local_tax == 0.0


def _answers(lay: Layout, wages: str, local: str) -> None:  # noqa: F811
    enter(lay, YEAR, "state", "NY")
    enter(lay, YEAR, "state_residency", "full_year")
    enter(lay, YEAR, "local_income_tax", local)
    enter(lay, YEAR, "wages", wages)
    for key in ("se_income", "interest", "ordinary_dividends", "qualified_dividends"):
        enter(lay, YEAR, key, "0")  # the fixture's other income
    enter(lay, YEAR, "state_withheld", "0")
    enter(lay, YEAR, "prior_agi", "70,000")
    enter(lay, YEAR, "prior_total_tax", "8,000")
    enter(lay, YEAR, "prior_state_tax", "2,000")


def _ny(lay: Layout) -> esttax.Agency:  # noqa: F811
    return esttax.estimate(lay, YEAR, date(2026, 3, 1)).agencies[1]


@pytest.mark.engine
def test_the_300_test_counts_the_city_tax(lay: Layout) -> None:  # noqa: F811
    # a wage where New York State tax alone is under 300 and State plus the
    # city's is over it (the planner's own engine runs pick the figures)
    wages = "18,000"  # AGI 16,000 after the fixture's IRA deduction
    _answers(lay, wages, "no")
    state_only = _ny(lay)
    assert state_only.current_tax < 300 and state_only.de_minimis
    _answers(lay, wages, "nyc")
    both = _ny(lay)
    assert both.current_tax > 300 and not both.de_minimis
    assert state_only.current_tax == pytest.approx(267.0, abs=0.01)  # engine figure
    assert both.current_tax > state_only.current_tax
    assert both.required > 0 and any(i.required for i in both.installments)
