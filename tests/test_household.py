from __future__ import annotations

from pathlib import Path

import pytest

from planner.engine.household import Household, MissingInputError, load_household

FIX = Path(__file__).parent / "fixtures"


def test_situation_has_one_person_one_unit_one_household() -> None:
    h = Household(
        age=55, filing_status="SINGLE", state="NC", county="WAKE_COUNTY_NC", wages=1
    )
    s = h.situation(2026)
    assert s["people"]["p"]["employment_income"] == {2026: 1}
    assert s["tax_units"]["tu"]["filing_status"] == {2026: "SINGLE"}
    assert s["households"]["hh"]["state_name"] == {2026: "NC"}
    assert s["households"]["hh"]["county"] == {2026: "WAKE_COUNTY_NC"}
    assert "slcsp" not in s["tax_units"]["tu"]


def test_slcsp_is_given_per_month_and_omit_drops_an_input() -> None:
    h = Household(
        age=55, filing_status="SINGLE", state="NC", slcsp_monthly=600, wages=5
    )
    s = h.situation(2026, omit=("employment_income",))
    assert len(s["tax_units"]["tu"]["slcsp"]) == 12
    assert s["tax_units"]["tu"]["slcsp"]["2026-07"] == 600
    assert "employment_income" not in s["people"]["p"]


def test_unknown_is_refused_not_zeroed() -> None:
    with pytest.raises(MissingInputError):
        Household(age=None, filing_status="SINGLE", state="NC")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="filing_status"):
        Household(age=50, filing_status="MARRIED", state="NC")
    with pytest.raises(ValueError, match="unknown household fields"):
        Household.from_mapping(
            {"age": 50, "filing_status": "SINGLE", "state": "NC", "wage": 1}
        )


def test_load_household_yaml() -> None:
    h = load_household(FIX / "household_2026.yaml")
    assert h.roth_conversion == 40000
    assert h.qualified_dividends == 3000
