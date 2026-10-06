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
credit, the draft's Schedule 1 line 17) is consistent with it. The engine's
28% rate and unrecaptured section 1250 gain path is replaced by the Schedule D
Tax Worksheet (``_sdtw_tax``).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
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
    itemizes: bool = False  # Schedule A beats the standard deduction
    itemized_deductions: float = 0.0  # Schedule A line 17
    standard_deduction: float = 0.0


# IRC 36B(c)(1)(A): the premium tax credit needs household income that "does not
# exceed 400 percent" of the poverty line, and i8962 Worksheet 2 puts 401 on line 5
# only when income is MORE than 4 x the guideline, in dollars. policyengine-us floors
# the ratio to whole percents and ends eligibility at a floored ratio >= 4.00
# (parameters/gov/aca/ptc_income_eligibility.yaml), so it pays nothing at exactly
# 400.00%. The planner moves that edge just past 4.00 (exactly 400.00% is eligible)
# and zeroes the credit on income more than 4 x the guideline (_aca_ptc), which cuts
# 400.01% to 400.99% the engine's floor would otherwise let through.
ACA_PTC_LINE = 4.0
_EDGE = 1e-9  # past 4.00 by less than a cent of MAGI moves the ratio
_CENT = 0.005  # half a cent: the engine's float32 MAGI against 4 x the guideline


def _aca_400_edge(system: Any) -> None:
    from policyengine_core.periods import instant

    for bracket in system.parameters.gov.aca.ptc_income_eligibility.brackets:
        if float(bracket.threshold("2026-01-01")) == ACA_PTC_LINE:
            bracket.threshold.update(
                start=instant("2014-01-01"), value=ACA_PTC_LINE + _EDGE
            )


def _rate_schedule(amount: Any, fs: Any, income: Any) -> Any:
    """Tax on ``amount`` by the rate schedule, for an array of tax units."""
    from policyengine_us.model_api import max_, min_

    tax, low = 0.0, 0.0
    for n in range(1, len(list(income.bracket.rates.__iter__())) + 1):
        top = max_(low, income.bracket.thresholds[str(n)][fs])
        tax = tax + income.bracket.rates[str(n)] * max_(0.0, min_(amount, top) - low)
        low = top
    return tax


def _sdtw(tax_unit: Any, period: Any, parameters: Any) -> tuple[Any, Any, Any]:
    """The Schedule D Tax Worksheet (2025 Schedule D instructions), used when
    Schedule D line 18 (28% rate gain) or line 19 (unrecaptured section 1250
    gain) is more than zero. Returns whether it applies, the part of taxable
    income kept off the rate schedule (line 1 less line 21) and the tax on it
    (lines 31 + 34 + 40 + 43); both are 0 when line 46, the tax on all taxable
    income, is the smaller (line 47)."""
    from policyengine_us.model_api import add, max_, min_, where

    p = parameters(period).gov.irs
    cg = p.capital_gains
    fs = tax_unit("filing_status", period)
    l1 = max_(tax_unit("taxable_income", period), 0.0)
    s19 = add(tax_unit, period, ["unrecaptured_section_1250_gain"])
    l11 = s19 + add(tax_unit, period, ["capital_gains_28_percent_rate_gain"])
    l9 = tax_unit("dwks09", period)
    l10 = tax_unit("dwks10", period)
    l13 = l10 - min_(l9, l11)
    l14 = max_(l1 - l13, 0.0)
    l16 = min_(l1, cg.thresholds["1"][fs])
    l17 = min_(l14, l16)
    l20 = min_(l14, min_(l1, p.income.bracket.thresholds["4"][fs]))  # 24% top
    l21 = max_(max_(l1 - l10, 0.0), l20)
    l22 = l16 - l17  # taxed at 0%
    l23 = min_(l1, l13)
    l25 = max_(l23 - l22, 0.0)
    l29 = max_(min_(l1, cg.thresholds["2"][fs]) - (l21 + l22), 0.0)
    l30 = min_(l25, l29)  # taxed at 15%
    l33 = l23 - (l22 + l30)  # taxed at 20%
    l39 = max_(min_(l9, s19) - max_(l10 + l21 - l1, 0.0), 0.0)  # at 25%
    l42 = l1 - (l21 + l22 + l30 + l33 + l39)  # at 28%
    gains_tax = (
        cg.rates["2"] * l30
        + cg.rates["3"] * l33
        + cg.unrecaptured_s_1250_rate * l39
        + cg.other_cg_rate * l42
    )
    l45 = gains_tax + _rate_schedule(l21, fs, p.income)
    worksheet = l45 <= _rate_schedule(l1, fs, p.income)
    applies = tax_unit("has_qdiv_or_ltcg", period) & (l11 > 0) & (l1 > 0)
    return applies, where(worksheet, l1 - l21, 0.0), where(worksheet, gains_tax, 0.0)


SDTW_VARS = (
    "capital_gains_28_percent_rate_gain",
    "unrecaptured_section_1250_gain",
    "capital_gains_excluded_from_taxable_income",
)


def _sdtw_tax(system: Any) -> None:
    """Price 28% rate and unrecaptured section 1250 gain by the Schedule D Tax
    Worksheet. The engine's own path keeps the 28% gain in the rate schedule's
    base below the 0% threshold and also adds 28% of all of it, bands the
    gains at the 0% threshold instead of the 24% bracket top (line 19) and
    never takes line 46 when it is smaller, so a filer in the 12% bracket with
    $10,000 of collectibles gain is charged $2,800 more than the worksheet.
    The engine's tax is income_tax_main_rates (the rate schedule on taxable
    income less capital_gains_excluded_from_taxable_income) plus
    capital_gains_tax; with the worksheet those are lines 44 and 31 + 34 + 40 +
    43, and regular_tax_before_credits (Form 6251 line 10's regular tax, less
    capital_gains_tax) is the main-rates tax. Without such gain the engine's
    formulas run unchanged."""
    from policyengine_us.model_api import Variable, where

    def pick(name: str, branch: Any) -> None:
        engine = system.variables[name].formulas["0001-01-01"]

        def formula(tax_unit: Any, period: Any, parameters: Any) -> Any:
            applies, excluded, gains_tax = _sdtw(tax_unit, period, parameters)
            mine = branch(tax_unit, period, excluded, gains_tax)
            return where(applies, mine, engine(tax_unit, period, parameters))

        system.update_variable(type(name, (Variable,), {"formula": formula}))

    pick("capital_gains_excluded_from_taxable_income", lambda tu, pe, x, g: x)
    pick("capital_gains_tax", lambda tu, pe, x, g: g)
    pick(
        "regular_tax_before_credits",
        lambda tu, pe, x, g: tu("income_tax_main_rates", pe),
    )


@lru_cache(maxsize=1)
def _system() -> Any:
    from policyengine_us import CountryTaxBenefitSystem

    system = CountryTaxBenefitSystem()
    _aca_400_edge(system)
    _sdtw_tax(system)
    return system


def _ptc_capped(year: int) -> bool:
    """Whether the year ends the credit above 400% (no cap 2021-2025, P.L. 117-169)."""
    node = _system().parameters.gov.aca.ptc_income_eligibility
    return not bool(node(f"{year}-01-01").calc(ACA_PTC_LINE + 0.5))


def over_ptc_line(year: int, magi: float, prior_fpg: float) -> bool:
    """i8962 Worksheet 2: income more than 4 x last year's guideline in a capped
    year, so Form 8962 line 5 is 401 and no credit is allowed."""
    return _ptc_capped(year) and magi > ACA_PTC_LINE * prior_fpg + _CENT


def _aca_ptc(sim: Any, year: int) -> Any:
    """The engine's credit, zero where income is more than 4 x last year's
    guideline in a capped year (i8962 Worksheet 2)."""
    ptc = _calc(sim, "aca_ptc", year)
    if not _ptc_capped(year):
        return ptc
    line = ACA_PTC_LINE * _calc(sim, "tax_unit_fpg", year - 1)
    return np.where(_calc(sim, "aca_magi", year) > line + _CENT, 0.0, ptc)


def engine_version() -> str:
    from importlib.metadata import version

    return version("policyengine-us")


def _node(path: str, year: int) -> Any:
    node = _system().parameters
    for part in path.split("."):
        node = getattr(node, part)
    return node(f"{year}-01-01")


def _param(path: str, year: int) -> float:
    return float(_node(path, year))


def brackets(path: str, year: int) -> list[tuple[float, float]]:
    """A marginal-rate scale parameter as (threshold, rate) rows, lowest first."""
    scale = _node(path, year)
    return [
        (float(t), float(r)) for t, r in zip(scale.thresholds, scale.rates, strict=True)
    ]


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


# Form 1040 line 16: under this taxable income the Tax Table, not the rate
# schedule, sets the tax (2025 Form 1040 instructions, Tax Table).
TAX_TABLE_TOP = 100_000.0


def bracket_tax(amount: float, year: int, filing_status: str) -> float:
    """Tax on ordinary income by the year's rate schedule, from the engine's
    own bracket rates and edges."""
    tax, low = 0.0, 0.0
    for n in range(1, 8):
        if amount <= low:
            break
        top = _param(f"gov.irs.income.bracket.thresholds.{n}.{filing_status}", year)
        tax += (min(amount, top) - low) * _param(
            f"gov.irs.income.bracket.rates.{n}", year
        )
        low = top
    return tax


def table_tax(amount: float, year: int, filing_status: str) -> float:
    """Tax as the Tax Table prints it: rows of $5 under $5, $10 to $25, $25 to
    $3,000 and $50 to $100,000, each the rate-schedule tax on the row's
    midpoint rounded to whole dollars (an amount under $5 owes nothing). From
    $100,000 up the rate schedule applies to the cent."""
    if amount >= TAX_TABLE_TOP:
        return bracket_tax(amount, year, filing_status)
    if amount < 5:
        return 0.0
    if amount < 25:
        low, width = (5.0, 10.0) if amount < 15 else (15.0, 10.0)
    else:
        width = 25.0 if amount < 3000 else 50.0
        low = math.floor(amount / width) * width
    exact = bracket_tax(low + width / 2, year, filing_status)
    return float(Decimal(f"{exact:.2f}").quantize(Decimal("1"), ROUND_HALF_UP))


def preferential(v: dict[str, float]) -> float:
    """The part of taxable income line 16 keeps off the rate schedule: the
    adjusted net capital gain (Qualified Dividends and Capital Gain Tax
    Worksheet), or with 28% rate or unrecaptured section 1250 gain the
    Schedule D Tax Worksheet's line 1 less line 21 (0 when line 46 is the
    smaller). ``v`` holds the engine's ``SDTW_VARS`` and
    adjusted_net_capital_gain."""
    if v[SDTW_VARS[0]] + v[SDTW_VARS[1]] > 0:
        return v[SDTW_VARS[2]]
    return v["adjusted_net_capital_gain"]


def line_16(
    regular: float, taxable: float, gains: float, year: int, filing_status: str
) -> tuple[float, float]:
    """Form 1040 line 16 from the engine's rate-schedule tax (its
    income_tax_before_credits less AMT): the ordinary part of taxable income
    (``gains`` is ``preferential``) under $100,000 is taxed by the Tax Table
    (the Qualified Dividends and Capital Gain Tax Worksheet, line 22; the
    Schedule D Tax Worksheet, line 44), and that worksheet's total is capped
    at the tax on all taxable income (line 24; line 47). Returns the line and
    its gap from the engine's figure."""
    ordinary = taxable - min(max(gains, 0.0), taxable)
    line = regular
    if ordinary < TAX_TABLE_TOP:
        line += table_tax(ordinary, year, filing_status) - bracket_tax(
            ordinary, year, filing_status
        )
    if ordinary < taxable:
        line = min(line, table_tax(taxable, year, filing_status))
    return line, round(line - regular, 2)


IRMAA_STATUS = {
    "SINGLE": "single",
    "JOINT": "joint",
    "SEPARATE": "separate",
    "HEAD_OF_HOUSEHOLD": "head_of_household",
    "SURVIVING_SPOUSE": "surviving_spouse",
}


def irmaa_first_tier(year: int, filing_status: str) -> float:
    """The top of Medicare Part B's no-surcharge bracket (CMS, by filing
    status): MAGI above it two years earlier adds the first IRMAA."""
    irmaa = _system().parameters.gov.hhs.medicare.part_b.irmaa
    scale = getattr(irmaa, IRMAA_STATUS[filing_status])(f"{year}-01-01")
    return float(scale.thresholds[1]) - 1


def ss_taxation_thresholds(year: int, filing_status: str) -> tuple[float, float]:
    """IRC 86(c): provisional income above the first makes up to 50% of Social
    Security taxable, above the second up to 85%."""
    base = "gov.irs.social_security.taxability.threshold"
    return (
        _param(f"{base}.base.main.{filing_status}", year),
        _param(f"{base}.adjusted_base.main.{filing_status}", year),
    )


def self_employment_parameters(year: int) -> dict[str, float]:
    """The Schedule SE constants the engine prices with, so the draft's Part I
    lines use the same wage base and rates: the Social Security wage base (line
    7), the 12.4% and 2.9% rates (lines 10 and 11), the $400 floor (line 4c),
    the share of the tax deducted (line 13) and the share of net profit left
    after the employer-equivalent half (the 92.35% of line 4a)."""
    ss = _param("gov.irs.self_employment.rate.social_security", year)
    medicare = _param("gov.irs.self_employment.rate.medicare", year)
    return {
        "wage_base": _param("gov.irs.payroll.social_security.cap", year),
        "social_security_rate": ss,
        "medicare_rate": medicare,
        "floor": _param("gov.irs.self_employment.net_earnings_exemption", year),
        "net_earnings_share": 1
        - _param("gov.irs.ald.misc.employer_share", year) * (ss + medicare),
        "deductible_share": _param(
            "gov.irs.ald.self_employment_tax.percent_deductible", year
        ),
    }


def savers_credit_table(year: int, filing_status: str) -> list[tuple[float, float]]:
    """Form 8880 line 9's table for one filing status: (top AGI, rate) rows in
    order, a rate applying up to and including its top; above the last row the
    rate is 0. The engine keeps the joint brackets (each the first dollar of the
    next rate) and scales them by filing status, as the form's columns do."""
    p = _system().parameters.gov.irs.credits.retirement_saving.rate
    at = f"{year}-01-01"
    scale = float(getattr(p.threshold_adjustment, filing_status)(at))
    rows = p.joint.brackets
    return [
        ((float(nxt.threshold(at)) - 1) * scale, float(b.amount(at)))
        for b, nxt in zip(rows, rows[1:], strict=False)
    ]


def savers_credit_cap(year: int) -> float:
    """Form 8880 line 6's per-person cap on the contributions ($2,000)."""
    return _param("gov.irs.credits.retirement_saving.contributions_cap", year)


EIC_ROW = 50.0  # the EIC Table's rows are $50 wide


def eic_parameters(year: int) -> dict[str, float]:
    """The EIC's tests the Form 1040 line 27a instructions print (2025: the
    $11,950 investment income limit in Step 2, the 25-64 age band for a filer
    without a qualifying child in Step 4), from the engine's parameters."""
    p = _system().parameters.gov.irs.credits.eitc
    at = f"{year}-01-01"
    return {
        "investment_income": float(p.phase_out.max_investment_income(at)),
        "min_age": float(p.eligibility.age.min(at)),
        "max_age": float(p.eligibility.age.max(at)),
    }


def _eic_terms(year: int, children: int, joint: bool) -> tuple[float, ...]:
    """(maximum, phase-in rate, phase-out rate, phase-out start) for an EIC
    Table column: ``children`` qualifying children, 3 or more the last."""
    p = _system().parameters.gov.irs.credits.eitc
    at = f"{year}-01-01"
    k = min(children, 3)
    start = float(p.phase_out.start(at).calc(k))
    if joint:
        start += float(p.phase_out.joint_bonus(at).calc(k))
    return (
        float(p.max(at).calc(k)),
        float(p.phase_in_rate(at).calc(k)),
        float(p.phase_out.rate(at).calc(k)),
        start,
    )


def eic_phaseout_start(year: int, children: int, joint: bool) -> float:
    """Worksheet A line 5's (B line 10's) AGI below which AGI is not looked up
    (2025: $10,620, $17,730 joint, with no child; $23,350, $30,470 joint)."""
    return _eic_terms(year, children, joint)[3]


def eic_table(year: int, children: int, joint: bool, amount: float) -> int:
    """The EIC Table's credit for an amount a worksheet looks up (2025 Form 1040
    instructions, line 27a), in the column for ``children`` and joint or not.
    The table prices each $50 row at its midpoint, rounded to the dollar; a row
    holding the end of the phase-in or the start of the phase-out gets the
    maximum, and the last row stops where the credit ends (the starred notes)."""
    if amount < 1:
        return 0
    most, rise, fall, start = _eic_terms(year, children, joint)
    end = round(start + most / fall)
    if amount >= end:
        return 0
    base = amount // EIC_ROW * EIC_ROW
    low, high = max(base, 1.0), min(base + EIC_ROW, end)
    mid = (low + high) / 2
    phased_in = most if low < most / rise < high else min(most, rise * mid)
    cut = 0.0 if low < start < high else fall * max(mid - start, 0.0)
    credit = Decimal(f"{phased_in - cut:.6f}").quantize(Decimal(1), ROUND_HALF_UP)
    return max(int(credit), 0)


def actc_phase_in(year: int) -> tuple[float, float, int]:
    """Schedule 8812 lines 19-20 and the gate to Part II-B (2025: earned income
    over $2,500, at 15%; three or more qualifying children may use the Social
    Security tax less the EIC instead; 26 U.S.C. 24(d)(1)(B)), from the
    engine's parameters."""
    p = _system().parameters.gov.irs.credits.ctc.refundable.phase_in
    at = f"{year}-01-01"
    return (
        float(p.threshold(at)),
        float(p.rate(at)),
        int(p.min_children_for_ss_taxes_minus_eitc(at)),
    )


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


def poverty_line(year: int, size: int, state: str) -> float:
    """Form 8962 line 4 for a family of ``size``: the year before's HHS
    guideline (i8962 Table 1-1), with Alaska's and Hawaii's own. Pub. 974's
    year-of-marriage Worksheets I and III read it for each spouse's family."""
    fpg = _system().parameters(f"{year - 1}-01-01").gov.hhs.fpg
    group = state if state in ("AK", "HI") else "CONTIGUOUS_US"
    first, more = fpg.first_person[group], fpg.additional_person[group]
    return float(first + more * (size - 1))


def applicable_figure(year: int, pct: int) -> float | None:
    """Form 8962 line 7 at a whole-percent line 5 (i8962 Table 2): the IRC
    36B(b)(3)(A) table, run straight between the band ends and shown to four
    places (375% in 2025 is 0.0788). None when the year allows no credit over
    400% and ``pct`` is 401."""
    if pct > 100 * ACA_PTC_LINE and _ptc_capped(year):
        return None
    p = _system().parameters(f"{year}-01-01").gov.aca.required_contribution_percentage
    ends, start, end = list(p.threshold), list(p.initial), list(p.final)
    x = pct / 100
    band = max(i for i in range(len(start)) if ends[i] <= x)
    top = ends[band + 1] if band + 1 < len(ends) else ends[band]
    where = min((x - ends[band]) / (top - ends[band]), 1.0) if top > ends[band] else 0
    exact = Decimal(str(start[band])) + Decimal(str(where)) * (
        Decimal(str(end[band])) - Decimal(str(start[band]))
    )
    return float(exact.quantize(Decimal("0.0001"), ROUND_HALF_UP))


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
        np.asarray(_head(first, "self_employment_income", year), dtype=float)
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
        ptc = np.minimum(_aca_ptc(sim, year), paid)
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
    ptc = np.minimum(_aca_ptc(sim, year), paid)
    return _Settled(sim, final, ptc, rounds + 1, done | step)


def _f(sim: Any, var: str, year: int) -> Any:
    return np.asarray(_calc(sim, var, year), dtype=float)


# Person figures read for the head alone, not summed over the tax unit:
# Medicaid eligibility is the head's own (the plan prices their coverage).
HEAD_ONLY = ("is_medicaid_eligible",)


def _calc(sim: Any, var: str, year: int) -> Any:
    """One figure per tax unit (per axis point). A person figure is summed over
    the unit's members, so a joint return's lines are the couple's (unit 3a-1),
    except a HEAD_ONLY one."""
    if sim.tax_benefit_system.variables[var].entity.key != "person":
        return sim.calculate(var, year)
    if var in HEAD_ONLY:
        return _head(sim, var, year)
    return sim.calculate(var, year, map_to="tax_unit")


def _head(sim: Any, var: str, year: int) -> Any:
    """A person figure for the head of each tax unit (the first person)."""
    heads = np.asarray(sim.calculate("is_tax_unit_head", year), dtype=bool)
    return np.asarray(sim.calculate(var, year))[heads]


def r(x: float) -> float:
    return round(x, ROUND)


EXCLUSION = "dependent_care_assistance_exclusion"


@dataclass(frozen=True)
class DependentCare:
    """Form 2441 Part III (2025 Form 2441 and its instructions, lines 12-31):
    the employer's dependent care benefits, what of them is excluded and taxed,
    and the care left for the credit in Part II. Lines 22 and 24 (benefits from
    your own sole proprietorship or partnership) are taken as 0."""

    line12: float  # W-2 box 10, both spouses
    line13: float  # grace-period carryover used
    line14: float  # forfeited or carried forward
    line15: float  # 12 + 13 - 14
    line16: float  # care incurred
    line17: float  # smaller of 15 and 16
    line18: float  # your earned income, not counting line 12
    line19: float  # the spouse's (yours again when not joint)
    line20: float  # smallest of 17, 18 and 19
    line21: float  # the exclusion's dollar cap
    line25: float  # excluded: smaller of 20 and 21
    line26: float  # taxable (Form 1040 line 1e): 15 - 25
    line27: float  # $3,000 a qualifying person, two at most
    line29: float  # 27 - 28 (line 28 = line 25 here)
    line30: float  # care paid less line 28
    line31: float  # smaller of 29 and 30: Part II line 3


_CARE: dict[tuple[int, str], tuple[Household, DependentCare | None]] = {}


def dependent_care(
    year: int, household: Household
) -> tuple[Household, DependentCare | None]:
    """The household the engine prices once Form 2441 Part III is worked out,
    and the form's lines (None, and the household unchanged, without W-2 box 10
    benefits). The engine caps the exclusion at the dollar cap and the lower
    earner's income but not at the care incurred (line 17), never taxes the
    excess (line 26), and claims care up to the $3,000 limit less the
    exclusion where the form claims the care not paid with benefits (line 30).
    So the exclusion is pinned to line 25, the care to line 30 and line 26 is
    added to the wages of whoever received the benefits (pro rata between
    spouses). The returned household holds no benefits, so a second call
    leaves it as it is. Earned income is the engine's, read before line 26
    joins it (lines 18-19 leave the benefits out; Part II then counts them)."""
    sp = household.spouse
    mine = household.dependent_care_benefits
    theirs = sp.dependent_care_benefits if sp is not None else 0
    if not mine + theirs:
        return household, None
    key = (year, repr(household))
    if (hit := _CARE.get(key)) is not None:
        return hit
    base = replace(
        household, tax_unit_inputs={**household.tax_unit_inputs, EXCLUSION: 0.0}
    )
    sim = _sim(year, base)
    head = float(_calc(sim, "head_earned", year)[0])
    spouse = float(_calc(sim, "spouse_earned", year)[0])
    limit = float(_calc(sim, "cdcc_limit", year)[0])  # line 27: nothing excluded
    p = _system().parameters(f"{year}-01-01").gov.irs.gross_income
    cap = float(
        getattr(
            p.dependent_care_assistance_programs.reduction_amount,
            household.filing_status,
        )
    )
    l12 = float(mine + theirs)
    l15 = max(
        l12 + household.dependent_care_grace - household.dependent_care_forfeited, 0.0
    )
    l16 = float(household.care_expenses)
    l17 = min(l15, l16)
    l19 = max(spouse, 0.0) if household.filing_status == "JOINT" else max(head, 0.0)
    l18 = max(head, 0.0)
    l20 = min(l17, l18, l19)
    l25 = min(l20, cap)
    l26 = max(l15 - l25, 0.0)
    l29 = max(limit - l25, 0.0)
    l30 = max(l16 - l25, 0.0)
    care = DependentCare(
        l12,
        float(household.dependent_care_grace),
        float(household.dependent_care_forfeited),
        l15,
        l16,
        l17,
        l18,
        l19,
        l20,
        cap,
        l25,
        l26,
        limit,
        l29,
        l30,
        min(l29, l30),
    )
    taxed = int(round(l26))
    yours = int(round(taxed * mine / l12))
    out = replace(
        household,
        wages=household.wages + yours,
        care_expenses=int(round(l30)),
        dependent_care_benefits=0,
        dependent_care_grace=0,
        dependent_care_forfeited=0,
        tax_unit_inputs={**household.tax_unit_inputs, EXCLUSION: l25},
        spouse=(
            replace(sp, wages=sp.wages + taxed - yours, dependent_care_benefits=0)
            if sp is not None
            else None
        ),
    )
    if len(_CARE) >= MEMO_SIZE:
        _CARE.clear()
    _CARE[key] = (out, care)
    return out, care


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
        hit = _MEMO[key] = _compute(year, dependent_care(year, household)[0])
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
            "alternative_minimum_tax",
            "adjusted_net_capital_gain",
            *SDTW_VARS,
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
            "tax_unit_itemizes",
            "itemized_taxable_income_deductions",
            "standard_deduction",
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
    ptc = (
        float(settled.ptc[0])
        if settled.ptc is not None
        else float(_aca_ptc(sim, year)[0])
    )
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
    # payment (Schedule 3 line 9), not a cut in line 24. The engine prices
    # ordinary income by the rate schedule; under $100,000 the form uses the
    # Tax Table, and that gap carries into lines 22 and 24.
    regular = v["income_tax_before_credits"] - v["alternative_minimum_tax"]
    _, table_gap = line_16(
        regular,
        v["taxable_income"],
        preferential(v),
        year,
        household.filing_status,
    )
    line_22 = (
        table_gap
        + v["income_tax"]
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
        itemizes=bool(v["tax_unit_itemizes"]),
        itemized_deductions=r(v["itemized_taxable_income_deductions"]),
        standard_deduction=r(v["standard_deduction"]),
    )


def values(
    year: int,
    household: Household,
    names: Iterable[str],
    prior: Iterable[str] = (),
    own: Iterable[str] = (),
) -> dict[str, float]:
    """Named engine variables for one household-year, in one run (the draft
    return reads its lines from these). Unrounded; a person figure is the tax
    unit's sum (``_calc``).
    ``prior`` names are read for the year before, keyed ``<name>@prior``
    (Form 8962 uses the prior year's poverty line). ``own`` person figures are
    also read for each spouse alone, keyed ``<name>@you`` and ``<name>@spouse``
    (0 with no spouse): each has their own Schedule SE (unit 3a-5). The
    figures come from the simulation at the settled health insurance deduction
    (``_settle``), and two keys report that settlement: ``se_health_converged``
    (1 or 0) and ``se_health_ptc`` (the credit allowed, or -1 when no premiums
    were settled). Form 2441 Part III is worked out first (``dependent_care``)."""
    household = dependent_care(year, household)[0]
    settled = _settle(year, household)
    sim = settled.sim
    out = {name: float(_calc(sim, name, year)[0]) for name in names}
    if "aca_ptc" in out:
        out["aca_ptc"] = float(_aca_ptc(sim, year)[0])
    if "is_aca_ptc_eligible" in out and over_ptc_line(
        year,
        float(_calc(sim, "aca_magi", year)[0]),
        float(_calc(sim, "tax_unit_fpg", year - 1)[0]),
    ):
        out["is_aca_ptc_eligible"] = 0.0
    out["se_health_converged"] = float(bool(settled.converged[0]))
    out["se_health_ptc"] = float(settled.ptc[0]) if settled.ptc is not None else -1.0
    for name in prior:
        out[f"{name}@prior"] = float(_calc(sim, name, year - 1)[0])
    for name in own:
        for who, role in (
            ("you", "is_tax_unit_head"),
            ("spouse", "is_tax_unit_spouse"),
        ):
            mask = np.asarray(sim.calculate(role, year), dtype=bool)
            mine = np.asarray(sim.calculate(name, year), dtype=float)[mask]
            out[f"{name}@{who}"] = float(mine[0]) if mine.size else 0.0
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
    """Sweep one of the first person's inputs in one engine run (engine axes).
    Form 2441 Part III is worked out at the base household and held across the
    sweep (the taxable benefits and the exclusion do not move with it)."""
    if step <= 0 or hi < lo:
        raise ValueError("sweep needs step > 0 and hi >= lo")
    household = dependent_care(year, household)[0]
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
    series["aca_ptc"] = _aca_ptc(sim, year)
    if settled.ptc is not None:  # the credit allowed, as compute() reports it
        series["aca_ptc"] = settled.ptc
    series["se_health_deduction"] = settled.deduction
    xs = _head(sim, variable, year)  # the axis moves the first person only
    return [
        {
            variable: float(xs[i]),
            **{c: round(float(v[i]), ROUND) for c, v in series.items()},
        }
        for i in range(count)
    ]
