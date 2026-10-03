"""``compute(year, household)``: every tax figure the planners use, from the engine.

The engine prices the year. The arithmetic written here is subtraction of a
threshold from the engine's taxable income to report headroom, and the one
rule the engine does not carry: the self-employed health insurance deduction
and the premium tax credit settle together (IRS Pub. 974, Rev. Proc. 2014-41).
The engine deducts ``min(profit, premiums)`` and prices the credit from the
income that deduction leaves; the credit then reduces the premiums that
qualify, which moves income and the credit again. ``settle_se_health`` runs
that loop (Pub. 974's Iterative Calculation Method) by feeding the engine the
settled deduction as its premium input, so every figure read back (tax, MAGI,
credit, the draft's Schedule 1 line 17) is consistent with it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

import numpy as np

from planner.engine.household import Household

ROUND = 2


@dataclass(frozen=True)
class TaxResult:
    year: int
    engine_version: str
    fed_income_tax_after_credits: float  # Form 1040 line 22 (before excess APTC, 8962)
    se_tax: float  # Schedule 2 line 4
    fed_total_tax: float  # Form 1040 line 24
    refundable_credits: float  # Form 1040 lines 27-31 (paid out, not a cut in line 24)
    ltcg_tax: float  # capital-gains portion of income tax
    state_tax: float
    agi: float
    aca_magi: float
    taxable_income: float
    qualified_div_and_ltcg_in_taxable: float
    qbi_deduction: float
    aca_ptc: float  # Form 8962 line 24: the credit allowed (at most the premiums paid)
    aca_fpl_pct: float
    medicaid_fpl_pct: float
    medicaid_eligible: float  # 1.0 when the engine finds the person Medicaid-eligible
    medicaid_magi_monthly: float
    room_to_0pct_ltcg: float
    room_to_12pct_top: float
    fpg: float  # the federal poverty guideline for this household size and state
    se_health_deduction: float  # Schedule 1 line 17, after the Pub. 974 settlement
    se_health_rounds: int  # engine runs the settlement took (0: nothing to settle)
    se_health_converged: bool  # False: no fixed point (the 400% cliff); two-pass answer
    excess_aptc: float  # Form 8962 line 27: advance credit above the credit allowed
    aptc_repayment: float  # Form 8962 line 29: the excess after the line 28 limit
    net_ptc: float  # Form 8962 line 26: credit allowed above the advance (Sch 3 line 9)
    # The guideline of the year BEFORE the tax year: the one the premium tax credit
    # and cost-sharing tests use (Form 8962 line 4). Medicaid uses ``fpg``.
    aca_fpg: float


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
PREMIUMS = "self_employed_health_insurance_premiums"


def _sim(
    year: int,
    household: Household,
    axes: list[dict[str, Any]] | None = None,
    omit: tuple[str, ...] = (),
    premiums: Any = None,
) -> Any:
    """The engine simulation. ``premiums`` (one figure per axis point) replaces
    the household's self-employed health premiums: the deduction settlement
    feeds the engine the settled deduction here, never the premiums paid."""
    from policyengine_us import Simulation

    dropped = (*omit, PREMIUMS) if premiums is not None else omit
    situation = household.situation(year, omit=dropped)
    if axes:
        situation["axes"] = [axes]
    sim = Simulation(tax_benefit_system=_system(), situation=situation)
    if premiums is not None:
        sim.set_input(PREMIUMS, year, np.asarray(premiums, dtype=float))
    return sim


# Form 8962 line 28: the cap on repaying excess advance credit, by household
# income as a percent of the poverty line (under 200, 300, 400; none at 400 and
# over). Rev. Proc. 2024-35 for 2025; P.L. 119-21 sec. 71305 removes the cap
# from 2026. A year missing here repays in full (the worse case), and says so.
REPAY_CAP = {2025: {"SINGLE": (375.0, 975.0, 1625.0), "other": (750.0, 1950.0, 3250.0)}}
UNCAPPED_FROM = 2026


def repayment_cap(year: int, filing_status: str, pct: float) -> float | None:
    """Form 8962 line 28; None means no cap (repay the whole excess)."""
    if year >= UNCAPPED_FROM or pct >= 400:
        return None
    caps = REPAY_CAP.get(year)
    if caps is None:
        return None
    band = caps["SINGLE" if filing_status == "SINGLE" else "other"]
    return band[0] if pct < 200 else band[1] if pct < 300 else band[2]


def poverty_percent(fraction: float) -> int:
    """Form 8962 line 5: household income over the poverty line, whole percent,
    rounded down. The engine's float32 fraction (3.5599999) is cleaned first so
    356.0 is not cut to 355."""
    return math.floor(round(100 * fraction, 3))


# Pub. 974 (Iterative Calculation Method, step 6): stop when neither the
# deduction nor the credit moved by a dollar. The loop is a contraction: a round
# shrinks the error by the credit's slope in MAGI, the applicable percentage
# plus MAGI times its rise per dollar in the phase-in bands (2026, 250-300% of
# the poverty line: 0.0844 + 2.5 x 0.0304 = 0.16, Rev. Proc. 2025-25 table;
# about 0.10 in a flat band), so it needs about six rounds. Two things stop it
# short: the 400% cliff (no fixed point) and, where the applicable percentage
# rises with income, the whole-percent step of Form 8962 line 5 (the engine
# floors the fraction the same way), which moves the credit by about $13-15 at
# each percent and can leave two nearly consistent points. Both are cut off here.
CONVERGED_WITHIN = 1.0
MAX_ROUNDS = 25


@dataclass
class _Settled:
    sim: Any  # the engine simulation at the settled deduction
    deduction: Any  # per axis point
    ptc: Any  # Form 8962 line 24 per axis point; None when nothing was settled
    rounds: int
    converged: Any  # per axis point


def _settle(
    year: int,
    household: Household,
    axes: list[dict[str, Any]] | None = None,
    omit: tuple[str, ...] = (),
) -> _Settled:
    """Settle the self-employed health insurance deduction with the premium tax
    credit (IRS Pub. 974, Worksheets W and X, Iterative Calculation Method).

    The deduction is the premiums less the credit allowed, but no more than the
    profit less half the SE tax and any SEP/SIMPLE contribution (Worksheet W).
    The credit depends on household income, which the deduction lowers. Start
    from the deduction with no credit, price the credit on the income that
    leaves, take the credit off the premiums, and repeat until neither moves
    by $1. The engine does the pricing; each round is one simulation, with the
    whole axis (a sweep) carried as one array.

    A household with no fixed point at the 400% cliff (from 2026: the credit
    drops to zero as the deduction shrinks, which sends the deduction back up;
    the credit is zero at one end of the cycle) gets the two-pass answer: the
    deduction from the credit at the full deduction, with the credit then priced
    on that. ``converged`` is False for it. A cycle with a credit at both ends is
    the whole-percent step of Form 8962 line 5, not the cliff: the lower
    deduction (the conservative one) is the answer, the deduction plus the credit
    is within the step (about $15) of the premiums, and ``converged`` is True.

    Assumes premiums for a full year of one plan, all of them specified premiums
    (Pub. 974 Worksheet W; no nonspecified premiums), and one business."""
    n = axes[0]["count"] if axes else 1
    paid = float(household.se_health_premiums)
    if paid <= 0 or PREMIUMS in omit:
        sim = _sim(year, household, axes, omit)
        return _Settled(sim, np.zeros(n), None, 0, np.ones(n, dtype=bool))
    first = _sim(year, household, axes, omit, premiums=np.zeros(n))
    limit = np.maximum(
        _f(first, "self_employment_income", year)
        - _f(first, "self_employment_tax_ald", year)
        - _f(first, "self_employed_pension_contribution_ald", year),
        0.0,
    )
    if not limit.any():  # no profit to deduct against
        return _Settled(first, np.zeros(n), None, 0, np.ones(n, dtype=bool))
    deduction = np.minimum(paid, limit)
    last_ptc = np.zeros(n)  # step 1 assumes no credit
    trail: list[Any] = []  # the deduction each round priced
    full_ptc = np.zeros(n)  # the credit at the full deduction (round 1)
    done = np.zeros(n, dtype=bool)
    step = np.zeros(n, dtype=bool)  # cycling between two points that both get a credit
    for rounds in range(1, MAX_ROUNDS + 1):
        trail.append(deduction)
        sim = _sim(year, household, axes, omit, premiums=deduction)
        ptc = np.minimum(_f(sim, "aca_ptc", year), paid)
        if rounds == 1:
            full_ptc = ptc
        refigured = np.minimum(np.maximum(paid - ptc, 0.0), limit)
        done = (np.abs(refigured - deduction) < CONVERGED_WITHIN) & (
            np.abs(ptc - last_ptc) < CONVERGED_WITHIN
        )
        if done.all():
            return _Settled(sim, deduction, ptc, rounds, done)
        # a point whose figure returns to the one two rounds back is cycling
        # (the cliff), not converging: stop early rather than run the cap out
        if (
            rounds > 2
            and (done | (np.abs(refigured - trail[-2]) < CONVERGED_WITHIN)).all()
        ):
            step = ~done & (ptc > 0) & (last_ptc > 0)
            break
        deduction, last_ptc = refigured, ptc
    two_pass = np.minimum(np.maximum(paid - full_ptc, 0.0), limit)
    lower = np.minimum(trail[-1], trail[-2]) if len(trail) > 1 else trail[-1]
    final = np.where(done, trail[-1], np.where(step, lower, two_pass))
    sim = _sim(year, household, axes, omit, premiums=final)
    ptc = np.minimum(_f(sim, "aca_ptc", year), paid)
    return _Settled(sim, final, ptc, rounds + 1, done | step)


def _f(sim: Any, var: str, year: int) -> Any:
    return np.asarray(sim.calculate(var, year), dtype=float)


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
    settled = _settle(year, household)
    sim = settled.sim
    v = {
        name: float(_calc(sim, name, year)[0])
        for name in (
            "income_tax",
            "income_tax_refundable_credits",
            "net_investment_income_tax",
            "additional_medicare_tax",
            "self_employment_tax",
            "income_tax_before_credits",
            "state_income_tax",
            "adjusted_gross_income",
            "aca_magi",
            "aca_magi_fraction",
            "taxable_income",
            "qualified_business_income_deduction",
            "aca_ptc",
            "tax_unit_medicaid_income_level",
            "medicaid_magi",
            "tax_unit_fpg",
            "is_medicaid_eligible",
        )
    }
    aca_fpg = float(_calc(sim, "tax_unit_fpg", year - 1)[0])
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
        without = _sim(
            year,
            household,
            omit=PREFERENTIAL,
            premiums=settled.deduction if settled.ptc is not None else None,
        )
        ltcg_tax = v["income_tax_before_credits"] - float(
            _calc(without, "income_tax_before_credits", year)[0]
        )
    fpg = v["tax_unit_fpg"]
    # Form 8962 against the household's advance credit (Household.aptc). The
    # credit allowed is the settled one when premiums were settled (the engine's
    # credit is not limited to the premiums; line 11e is).
    ptc = float(settled.ptc[0]) if settled.ptc is not None else v["aca_ptc"]
    advance = float(household.aptc)
    excess = max(advance - ptc, 0.0)
    cap = repayment_cap(
        year, household.filing_status, poverty_percent(v["aca_magi_fraction"])
    )
    repayment = excess if cap is None else min(excess, cap)
    # The engine's income_tax is the tax net of refundable credits and holds the
    # net investment income tax; the draft's line 22 (planner.taxprep.draft) is
    # before both. Line 22 takes in the excess advance credit (8962 line 29, on
    # Schedule 2 line 1a); line 24 is line 22 plus Schedule 2 line 21: SE tax
    # (4), additional Medicare tax (11) and NIIT (12). The net credit is a
    # payment (Schedule 3 line 9), not a cut in line 24.
    line_22 = (
        v["income_tax"]
        + v["income_tax_refundable_credits"]
        - v["net_investment_income_tax"]
        + repayment
    )
    line_24 = (
        line_22
        + v["self_employment_tax"]
        + v["additional_medicare_tax"]
        + v["net_investment_income_tax"]
    )
    return TaxResult(
        year=year,
        engine_version=engine_version(),
        fed_income_tax_after_credits=r(line_22),
        se_tax=r(v["self_employment_tax"]),
        fed_total_tax=r(line_24),
        refundable_credits=r(v["income_tax_refundable_credits"]),
        ltcg_tax=r(max(ltcg_tax, 0.0)),
        state_tax=r(v["state_income_tax"]),
        agi=r(v["adjusted_gross_income"]),
        aca_magi=r(v["aca_magi"]),
        taxable_income=r(v["taxable_income"]),
        qualified_div_and_ltcg_in_taxable=r(max(pref, 0.0)),
        qbi_deduction=r(v["qualified_business_income_deduction"]),
        aca_ptc=r(ptc),
        aca_fpl_pct=r(100 * v["aca_magi"] / aca_fpg) if aca_fpg else 0.0,
        medicaid_fpl_pct=r(100 * v["tax_unit_medicaid_income_level"]),
        medicaid_eligible=v["is_medicaid_eligible"],
        medicaid_magi_monthly=r(v["medicaid_magi"] / 12),
        room_to_0pct_ltcg=r(th["ltcg_0pct_top"] - v["taxable_income"]),
        room_to_12pct_top=r(th["bracket_12pct_top"] - v["taxable_income"]),
        fpg=r(fpg),
        se_health_deduction=r(float(settled.deduction[0])),
        se_health_rounds=settled.rounds,
        se_health_converged=bool(settled.converged[0]),
        excess_aptc=r(excess),
        aptc_repayment=r(repayment),
        net_ptc=r(max(ptc - advance, 0.0)),
        aca_fpg=r(aca_fpg),
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
    (Form 8962 uses the prior year's poverty line). The figures come from the
    simulation at the settled health insurance deduction (``_settle``), and two
    keys report that settlement: ``se_health_converged`` (1 or 0) and
    ``se_health_ptc`` (the credit allowed, or -1 when no premiums were settled)."""
    settled = _settle(year, household)
    sim = settled.sim
    out = {name: float(_calc(sim, name, year)[0]) for name in names}
    out["se_health_converged"] = float(bool(settled.converged[0]))
    out["se_health_ptc"] = float(settled.ptc[0]) if settled.ptc is not None else -1.0
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
    settled = _settle(year, household, [axis], (variable,))
    sim = settled.sim
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
    if settled.ptc is not None:  # the credit allowed, as compute() reports it
        series["aca_ptc"] = settled.ptc
    series["se_health_deduction"] = settled.deduction
    xs = _calc(sim, variable, year)
    return [
        {
            variable: float(xs[i]),
            **{c: round(float(v[i]), ROUND) for c, v in series.items()},
        }
        for i in range(count)
    ]
