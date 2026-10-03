"""The Phase 0 proof: one real federal calculation through policyengine-us.

Not a tax function the planner uses. It exists so a clean Windows machine can
show the engine actually computes, not merely imports. It also reports the
tax years the engine publishes parameters for; ``planner update`` reads that
line from a candidate release to say which years the new engine covers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

YEARS_LABEL = "tax years published: "


@dataclass(frozen=True)
class SelfCheck:
    engine_version: str
    year: int
    wages: int
    income_tax: float
    years: tuple[int, ...] = ()


def format_years(years: Iterable[int]) -> str:
    """``(2015, 2018, 2019, 2020, 2026)`` -> ``2015, 2018-2020, 2026``."""
    runs: list[list[int]] = []
    for y in sorted(set(years)):
        if runs and y == runs[-1][1] + 1:
            runs[-1][1] = y
        else:
            runs.append([y, y])
    if not runs:
        return "none"
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def parse_years(text: str) -> tuple[int, ...]:
    """The inverse of :func:`format_years`; ``none`` and blank give ()."""
    out: set[int] = set()
    for lo, hi in re.findall(r"(\d{4})(?:\s*-\s*(\d{4}))?", text):
        out.update(range(int(lo), int(hi or lo) + 1))
    return tuple(sorted(out))


def years_in(output: str) -> tuple[int, ...]:
    """The years a selfcheck run printed; () for a release that prints none."""
    m = re.search(rf"^{re.escape(YEARS_LABEL)}(.*)$", output, re.MULTILINE)
    return parse_years(m.group(1)) if m else ()


def run(year: int = 2026, wages: int = 50_000) -> SelfCheck:
    from importlib.metadata import version

    from policyengine_us import CountryTaxBenefitSystem, Simulation

    from planner.engine.tax import published_years

    system = CountryTaxBenefitSystem()
    situation = {
        "people": {"p": {"age": {year: 40}, "employment_income": {year: wages}}},
        "tax_units": {"tu": {"members": ["p"]}},
        "households": {"hh": {"members": ["p"], "state_name": {year: "NC"}}},
    }
    sim = Simulation(tax_benefit_system=system, situation=situation)
    tax = float(sim.calculate("income_tax", year)[0])
    return SelfCheck(
        version("policyengine-us"), year, wages, tax, published_years(system)
    )
