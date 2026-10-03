"""``compute(year, household)``: every tax figure the planners use, from the engine.

No tax math is written here. The only arithmetic is subtraction of a threshold
from the engine's taxable income to report headroom.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from planner.engine.household import Household

ROUND = 2


@dataclass(frozen=True)
class TaxResult:
    year: int
    engine_version: str
    fed_income_tax_after_credits: float  # Form 1040 line 22
    se_tax: float  # Schedule 2 line 4
    fed_total_tax: float  # Form 1040 line 24
    ltcg_tax: float  # capital-gains portion of income tax
    state_tax: float
    agi: float
    aca_magi: float
    taxable_income: float
    qualified_div_and_ltcg_in_taxable: float
    qbi_deduction: float
    aca_ptc: float
    aca_fpl_pct: float
    medicaid_fpl_pct: float
    medicaid_magi_monthly: float
    room_to_0pct_ltcg: float
    room_to_12pct_top: float


@lru_cache(maxsize=1)
def _system() -> Any:
    from policyengine_us import CountryTaxBenefitSystem

    return CountryTaxBenefitSystem()


def engine_version() -> str:
    from importlib.metadata import version

    return version("policyengine-us")


def _param(path: str, year: int) -> float:
    node = _system().parameters
    for part in path.split("."):
        node = getattr(node, part)
    return float(node(f"{year}-01-01"))


def thresholds(year: int, filing_status: str) -> dict[str, float]:
    """Bracket edges from the engine's own parameters, for the headroom fields."""
    fs = filing_status
    return {
        "ltcg_0pct_top": _param(f"gov.irs.capital_gains.thresholds.1.{fs}", year),
        "bracket_12pct_top": _param(f"gov.irs.income.bracket.thresholds.2.{fs}", year),
    }


PREFERENTIAL = ("qualified_dividend_income", "long_term_capital_gains")


def _sim(
    year: int,
    household: Household,
    axes: list[dict[str, Any]] | None = None,
    omit: tuple[str, ...] = (),
) -> Any:
    from policyengine_us import Simulation

    situation = household.situation(year, omit=omit)
    if axes:
        situation["axes"] = [axes]
    return Simulation(tax_benefit_system=_system(), situation=situation)


def _calc(sim: Any, var: str, year: int) -> Any:
    return sim.calculate(var, year)


def r(x: float) -> float:
    return round(x, ROUND)


def compute(year: int, household: Household) -> TaxResult:
    sim = _sim(year, household)
    v = {
        name: float(_calc(sim, name, year)[0])
        for name in (
            "income_tax",
            "self_employment_tax",
            "income_tax_before_credits",
            "state_income_tax",
            "adjusted_gross_income",
            "aca_magi",
            "taxable_income",
            "qualified_business_income_deduction",
            "aca_ptc",
            "tax_unit_medicaid_income_level",
            "medicaid_magi",
            "tax_unit_fpg",
        )
    }
    th = thresholds(year, household.filing_status)
    pref = min(
        float(_calc(sim, "qualified_dividend_income", year)[0])
        + max(float(_calc(sim, "long_term_capital_gains", year)[0]), 0.0),
        v["taxable_income"],
    )
    # Tax attributable to preferential income: the same household with qualified
    # dividends and long-term gains removed, run again. Zero inside the 0% band.
    ltcg_tax = 0.0
    if pref > 0:
        without = _sim(year, household, omit=PREFERENTIAL)
        ltcg_tax = v["income_tax_before_credits"] - float(
            _calc(without, "income_tax_before_credits", year)[0]
        )
    fpg = v["tax_unit_fpg"]
    return TaxResult(
        year=year,
        engine_version=engine_version(),
        fed_income_tax_after_credits=r(v["income_tax"]),
        se_tax=r(v["self_employment_tax"]),
        fed_total_tax=r(v["income_tax"] + v["self_employment_tax"]),
        ltcg_tax=r(max(ltcg_tax, 0.0)),
        state_tax=r(v["state_income_tax"]),
        agi=r(v["adjusted_gross_income"]),
        aca_magi=r(v["aca_magi"]),
        taxable_income=r(v["taxable_income"]),
        qualified_div_and_ltcg_in_taxable=r(max(pref, 0.0)),
        qbi_deduction=r(v["qualified_business_income_deduction"]),
        aca_ptc=r(v["aca_ptc"]),
        aca_fpl_pct=r(100 * v["aca_magi"] / fpg) if fpg else 0.0,
        medicaid_fpl_pct=r(100 * v["tax_unit_medicaid_income_level"]),
        medicaid_magi_monthly=r(v["medicaid_magi"] / 12),
        room_to_0pct_ltcg=r(th["ltcg_0pct_top"] - v["taxable_income"]),
        room_to_12pct_top=r(th["bracket_12pct_top"] - v["taxable_income"]),
    )


def compute_sweep(
    year: int, household: Household, variable: str, lo: int, hi: int, step: int
) -> list[dict[str, float]]:
    """Sweep one person-level input in a single engine run (policyengine axes)."""
    if step <= 0 or hi < lo:
        raise ValueError("sweep needs step > 0 and hi >= lo")
    count = (hi - lo) // step + 1
    axis = {
        "name": variable,
        "min": lo,
        "max": lo + step * (count - 1),
        "count": count,
        "period": year,
    }
    sim = _sim(year, household, axes=[axis], omit=(variable,))
    cols = (
        "income_tax",
        "self_employment_tax",
        "state_income_tax",
        "adjusted_gross_income",
        "taxable_income",
        "aca_magi",
        "aca_ptc",
    )
    series = {c: _calc(sim, c, year) for c in cols}
    xs = _calc(sim, variable, year)
    return [
        {variable: float(xs[i]), **{c: round(float(series[c][i]), ROUND) for c in cols}}
        for i in range(count)
    ]
