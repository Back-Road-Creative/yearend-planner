"""``compute(year, household)``: every tax figure the planners use, from the engine.

No tax math is written here. The only arithmetic is subtraction of a threshold
from the engine's taxable income to report headroom.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
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
    fpg: float  # the federal poverty guideline for this household size and state


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


# config/thresholds.yaml rows the engine also carries, checked against it.
CONFIG_PARAMS = {
    "std_deduction_single": "gov.irs.deductions.standard.amount.SINGLE",
    "ltcg_0pct_top_single": "gov.irs.capital_gains.thresholds.1.SINGLE",
    "bracket_12pct_top_single": "gov.irs.income.bracket.thresholds.2.SINGLE",
    "ira_contribution_limit": "gov.irs.gross_income.retirement_contributions.limit.ira",
    "ira_catchup_50plus": (
        "gov.irs.gross_income.retirement_contributions.catch_up.limit.ira"
    ),
    "nc_income_tax_rate": "gov.states.nc.tax.income.rate",
    "nc_std_deduction_single": (
        "gov.states.nc.tax.income.deductions.standard.amount.SINGLE"
    ),
}


def _config_node(name: str, system: Any = None) -> Any:
    node = (system or _system()).parameters
    for part in CONFIG_PARAMS[name].split("."):
        node = getattr(node, part)
    return node


# Inflation-indexed figures the IRS announces every year; a year counts as
# published when the engine's own file lists all of them for it.
YEAR_ANCHORS = (
    "std_deduction_single",
    "ltcg_0pct_top_single",
    "bracket_12pct_top_single",
)


def published_years(system: Any = None) -> tuple[int, ...]:
    """The tax years whose IRS figures the engine's parameter files list
    (same test as :func:`engine_value`: literal numbers, not index
    projections). A year the engine only projects is absent."""
    sets = [
        {
            int(v.instant_str[:4])
            for v in _config_node(name, system).values_list
            if isinstance(v.value, int)
        }
        for name in YEAR_ANCHORS
    ]
    return tuple(sorted(set.intersection(*sets)))


def engine_value(name: str, year: int) -> tuple[float, bool]:
    """(value, published) for a CONFIG_PARAMS row. An inflation-indexed
    parameter's value counts as published only when the engine's own file
    lists that year; for a later year the engine projects it by its uprating
    index, and those projected values come back as computed floats, never the
    file's literal numbers. Unindexed parameters (a statutory rate) are law
    until changed."""
    node = _config_node(name)
    value = float(node(f"{year}-01-01"))
    if not (node.metadata or {}).get("uprating"):
        return value, True
    at = [v for v in node.values_list if v.instant_str.startswith(f"{year}-")]
    return value, any(isinstance(v.value, int) for v in at)


def drift(year: int, rows: dict[str, Any]) -> list[tuple[str, float, float]]:
    """(name, config value, engine value) for each config row the engine has."""
    return [
        (name, float(rows[name]["value"]), _param(path, year))
        for name, path in CONFIG_PARAMS.items()
        if name in rows
    ]


def thresholds(year: int, filing_status: str) -> dict[str, float]:
    """Bracket edges from the engine's own parameters, for the headroom fields."""
    fs = filing_status
    return {
        "ltcg_0pct_top": _param(f"gov.irs.capital_gains.thresholds.1.{fs}", year),
        "bracket_12pct_top": _param(f"gov.irs.income.bracket.thresholds.2.{fs}", year),
        "std_deduction": _param(f"gov.irs.deductions.standard.amount.{fs}", year),
        "niit_threshold": _param(
            f"gov.irs.investment.net_investment_income_tax.threshold.{fs}", year
        ),
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


# One engine run per distinct household: the planners on the plan page price the
# same base household many times. The engine is deterministic and TaxResult is
# frozen, so a repeat is returned from here.
_MEMO: dict[tuple[int, str], TaxResult] = {}
MEMO_SIZE = 256


def compute(year: int, household: Household) -> TaxResult:
    key = (year, repr(household))
    hit = _MEMO.get(key)
    if hit is None:
        if len(_MEMO) >= MEMO_SIZE:
            _MEMO.clear()
        hit = _MEMO[key] = _compute(year, household)
    return hit


def _compute(year: int, household: Household) -> TaxResult:
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
        fpg=r(fpg),
    )


def values(
    year: int,
    household: Household,
    names: Iterable[str],
    prior: Iterable[str] = (),
) -> dict[str, float]:
    """Named engine variables for one household-year, in one run (the draft
    return reads its lines from these). Unrounded; one person, so entry 0.
    ``prior`` names are read for the year before, keyed ``<name>@prior``
    (Form 8962 uses the prior year's poverty line)."""
    sim = _sim(year, household)
    out = {name: float(_calc(sim, name, year)[0]) for name in names}
    for name in prior:
        out[f"{name}@prior"] = float(_calc(sim, name, year - 1)[0])
    return out


def engine_slcsp(year: int, household: Household) -> float:
    """The engine's benchmark (second-lowest-cost silver) premium for the
    household's county, in dollars for January of ``year`` (monthly; 0 for a
    household with no income, which the engine does not price). Ignores
    ``household.slcsp_monthly``: this is the engine's own figure, the one
    ``tests/test_tax.py`` checks against the CMS published county table."""
    sim = _sim(year, replace(household, slcsp_monthly=None))
    return float(sim.calculate("slcsp", f"{year}-01")[0])


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
