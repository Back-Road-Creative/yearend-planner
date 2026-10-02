"""The Phase 0 proof: one real federal calculation through policyengine-us.

Not a tax function the planner uses. It exists so a clean Windows machine can
show the engine actually computes, not merely imports.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SelfCheck:
    engine_version: str
    year: int
    wages: int
    income_tax: float


def run(year: int = 2026, wages: int = 50_000) -> SelfCheck:
    from importlib.metadata import version

    from policyengine_us import Simulation

    situation = {
        "people": {"p": {"age": {year: 40}, "employment_income": {year: wages}}},
        "tax_units": {"tu": {"members": ["p"]}},
        "households": {"hh": {"members": ["p"], "state_name": {year: "NC"}}},
    }
    sim = Simulation(situation=situation)
    tax = float(sim.calculate("income_tax", year)[0])
    return SelfCheck(version("policyengine-us"), year, wages, tax)
