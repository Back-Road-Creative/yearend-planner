"""The planner's patches to the installed policyengine-us build and run against
it. A dependency bump that moves a variable (a dated formula key, a formula that
replaces an ``adds`` list) must fail here, in one place, not as a KeyError or
TypeError deep inside the Windows zip proof's ``planner run``. The Schedule D
Tax Worksheet's 28% rate gain, unrecaptured section 1250 gain and line 47 limit
belong to the engine (capital_gains_tax, capital_gains_excluded_from_taxable_income);
the planner overrides none of them, which test_sdtw.py holds to the worksheet."""

from __future__ import annotations

import pytest

from planner.engine import tax
from planner.engine.household import Household

pytestmark = pytest.mark.engine

ENGINE_OWNED = (
    "capital_gains_tax",
    "capital_gains_excluded_from_taxable_income",
    "regular_tax_before_credits",
    "income_tax_main_rates",
)


def test_the_patched_system_builds_and_prices_a_return() -> None:
    system = tax._system()
    assert tax.ISO in system.variables
    hh = Household(age=40, filing_status="SINGLE", state="NC", wages=80000)
    v = tax.values(2025, hh, ("amt_income", "regular_tax_before_credits"))
    assert v["amt_income"] > 0 and v["regular_tax_before_credits"] > 0


def test_the_sdtw_variables_are_the_engines_own() -> None:
    system = tax._system()
    for name in ENGINE_OWNED:
        variable = system.variables[name]
        formulas = list(variable.formulas.values())
        # an engine formula (or an adds list), never a function defined here
        assert all("planner" not in f.__module__ for f in formulas), name


def test_the_iso_input_joins_amt_income_through_the_engines_formula() -> None:
    def amt(iso: float) -> float:
        hh = Household(
            age=40,
            filing_status="SINGLE",
            state="FL",
            wages=150000,
            tax_unit_inputs={tax.ISO: iso},
        )
        return tax.values(2025, hh, ("amt_income",))["amt_income"]

    assert amt(1000.0) == amt(0.0) + 1000.0
