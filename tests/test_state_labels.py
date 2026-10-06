"""Output lines name the household's state tax by its code, not NC (unit
3d-3). Synthetic households only."""

from __future__ import annotations

import pytest

from planner import states
from planner.ingest.needs import enter
from planner.paths import Layout
from planner.plan import levers, year
from tests.test_spending import AS_OF, lay  # noqa: F401
from tests.test_withdraw import lots  # noqa: F401


def test_label_is_the_code_of_a_taxing_state() -> None:
    assert states.label("ca") == "CA"
    assert states.label("NC") == "NC"
    assert states.label("TX") == "state"  # no income tax
    assert states.label(None) == "state"
    assert states.label("ZZ") == "state"


def test_what_if_figures_name_no_state() -> None:
    assert ("state tax", "state_tax") in levers.FIGURES
    assert not any("NC" in label for label, _ in levers.FIGURES)


@pytest.mark.engine
@pytest.mark.parametrize("code", ["CA", "NC"])
def test_the_year_plan_names_the_households_state(
    lots: Layout,  # noqa: F811
    code: str,
) -> None:
    enter(lots, 2026, "state", code)
    yp = year.assemble(lots, 2026, AS_OF)
    other = {"CA": "NC", "NC": "CA"}[code]
    for name in ("magi", "conversion"):
        text = "\n".join(yp.section(name).lines)
        assert f"  {code} " in text, name
        assert f" {other} " not in text, name
