"""New York Form IT-201, laid onto the draft from the same engine run as the
federal return, for a full-year New York State resident.

Line numbers, the Tax Table, the rate schedules, the tax computation worksheets
and the sales and use tax chart follow the 2025 Form IT-201 and its
instructions (NYS DTF, it201_fill_in.pdf and it201i.pdf). The engine prices the
subtractions it sees (lines 25-30), the deduction, the dependent exemption, the
household, solar and geothermal credits and the refundable credits; this module
adds what it does not see: the additions and other subtractions you type, the
$5,000 ($10,000 joint) cap on the 529 subtraction, the use tax (typed, else the
chart), NYS withholding from the W-2 and 1099-R boxes and the NY estimated
payments recorded for the year. Not handled, and named: part-year and
nonresident returns (Form IT-203), the resident credit (line 41, Form IT-112-R),
net other NYS taxes (line 45, Form IT-201-ATT), New York City and Yonkers taxes
and the MCTMT (lines 47-58; a New York City or Yonkers resident's local tax is
named by the local tax check), voluntary contributions (line 60), the
noncustodial parent EIC (line 66), the New York City credits (lines 69-70a),
other refundable credits (line 71), NYC and Yonkers withholding (lines 73-74),
the 529 account deposit (line 78a) and the penalties (lines 81-82).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from planner.engine import tax as engine
from planner.ledger import db

FORM = "NY IT-201"
ENGINE = (
    "salt_refund_income",
    "taxable_public_pension_income",
    "taxable_social_security",
    "us_govt_interest",
    "ny_pension_exclusion",
    "investment_in_529_plan",
    "ny_itemizes",
    "ny_deductions",
    "ny_exemptions",
    "ny_taxable_income",
    "ny_household_credit",
    "ny_solar_energy_systems_equipment_credit",
    "ny_geothermal_energy_system_credit",
    "ny_ctc",
    "ny_cdcc",
    "ny_eitc",
    "ny_supplemental_eitc",
    "ny_real_property_tax_credit",
    "ny_college_tuition_credit",
)
KEYS = ("state_withheld", "ny_additions", "ny_subtractions", "ny_use_tax")
SCHEDULE = {
    "SINGLE": "single",
    "SEPARATE": "separate",
    "JOINT": "joint",
    "SURVIVING_SPOUSE": "surviving_spouse",
    "HEAD_OF_HOUSEHOLD": "head_of_household",
}
CHECKED = 2025  # the year whose instructions the scales were checked against
TABLE_TOP = 65_000.0  # the Tax Table covers taxable income below $65,000
WORKSHEET_AGI = 107_650.0  # NYAGI above this: the tax computation worksheets
TOP_AGI = 25_000_000.0  # NYAGI above this: the top rate on all of line 38
PHASE_IN = 50_000.0  # each worksheet's phase-in, line 6 or 7 over $50,000
CAP_529 = 5_000.0  # line 30 per taxpayer; twice that on a joint return
# The sales and use tax chart (line 59) by federal AGI (line 19): (top, tax);
# above $200,000 it is 0.0175% of the income, $125 at most.
USE_TAX = (
    (15_000.0, 3.0),
    (30_000.0, 6.0),
    (50_000.0, 10.0),
    (75_000.0, 15.0),
    (100_000.0, 18.0),
    (150_000.0, 26.0),
    (200_000.0, 34.0),
)
# The 2025 Tax Table prices its joint column's 5.25% bracket from the unrounded
# base ($976.25 where the rate schedule prints $976): rows $23,600-$27,900 for
# married filing jointly and qualifying surviving spouse.
TABLE_BASE = {2025: {23_600.0: 976.25}}
JOINT_COLUMN = ("JOINT", "SURVIVING_SPOUSE")
TOLERANCE = 1.0

Add = Callable[..., float]


def _whole(x: float) -> float:
    return float(Decimal(str(round(x, 6))).quantize(Decimal(1), ROUND_HALF_UP))


def _scale(status: str, year: int) -> list[tuple[float, float]]:
    rows = engine.brackets(f"gov.states.ny.tax.income.main.{SCHEDULE[status]}", year)
    return [(low, rate) for low, rate in rows if math.isfinite(low)]


def schedule(
    scale: list[tuple[float, float]],
    income: float,
    bases: Mapping[float, float] | None = None,
) -> float:
    """The rate schedule as printed: each bracket's base is the exact tax below
    it rounded to whole dollars ("$600 plus 5.5% of the excess over $13,900");
    ``bases`` overrides a bracket's base by its lower edge."""
    base = 0.0
    for i, (low, rate) in enumerate(scale):
        high = scale[i + 1][0] if i + 1 < len(scale) else math.inf
        if income <= high:
            return (bases or {}).get(low, _whole(base)) + (income - low) * rate
        base += (high - low) * rate
    raise AssertionError("unreachable: the top bracket is open")


def _midpoint(income: float) -> float:
    """The Tax Table row's midpoint: $0-$13, $13-$25, $25-$50, then $50 wide
    ("at least / but less than"), the last $64,950-$65,000."""
    if income < 13:
        return 6.5
    if income < 25:
        return 19.0
    if income < 50:
        return 37.5
    return math.floor(income / 50) * 50 + 25.0


def _amounts(name: str, status: str, year: int) -> tuple[list[float], list[float]]:
    node = engine._node(
        f"gov.states.ny.tax.income.supplemental.{name}.{SCHEDULE[status]}", year
    )
    return [float(t) for t in node.thresholds], [float(a) for a in node.amounts]


def _fraction(excess: float) -> float:
    return round(min(max(excess, 0.0), PHASE_IN) / PHASE_IN, 4)


def worksheet(year: int, status: str, income: float, nyagi: float) -> float:
    """Line 39 by the tax computation worksheet for NYAGI over $107,650: the
    first worksheet phases line 38 into the flat rate of the bracket the
    phase-in starts in; each later one adds its recapture base and a fraction
    (rounded to four places) of its incremental benefit to the schedule tax."""
    scale = _scale(status, year)
    top = scale[-1][1]
    if nyagi > TOP_AGI:
        return income * top
    tops, recapture = _amounts("recapture_base", status, year)
    _, benefit = _amounts("incremental_benefit", status, year)
    sched = schedule(scale, income)
    k = max(i for i, t in enumerate(tops) if i == 0 or income > t)
    if k:
        return sched + recapture[k] + _fraction(nyagi - tops[k]) * benefit[k]
    flat = income * next(r for low, r in reversed(scale) if low < tops[1])
    if nyagi >= WORKSHEET_AGI + PHASE_IN:
        return flat
    return sched + (flat - sched) * _fraction(nyagi - WORKSHEET_AGI)


def tax(year: int, status: str, income: float, nyagi: float) -> tuple[float, str]:
    """Line 39: the Tax Table below $65,000 of taxable income (the schedule at
    the row's midpoint), the rate schedule from $65,000, and the worksheets when
    NYAGI is over $107,650; whole dollars."""
    where = f"{year} IT-201-I" if year == CHECKED else f"the engine's {year} schedule"
    if income <= 0:
        return 0.0, f"Tax Table ({where}): no taxable income"
    if nyagi > WORKSHEET_AGI:
        amount = worksheet(year, status, income, nyagi)
        return _whole(amount), f"tax computation worksheet ({where})"
    scale = _scale(status, year)
    if income < TABLE_TOP:
        bases = TABLE_BASE.get(year, {}) if status in JOINT_COLUMN else {}
        amount = schedule(scale, _midpoint(income), bases)
        return _whole(amount), f"Tax Table ({where})"
    return _whole(schedule(scale, income)), f"rate schedule ({where})"


def use_tax(fagi: float) -> float:
    """Line 59 from the sales and use tax chart, by federal AGI."""
    for top, owed in USE_TAX:
        if fagi <= top:
            return owed
    return _whole(min(fagi * 0.000175, 125.0))


def _given(typed: Mapping[str, Any], key: str) -> tuple[float, str]:
    value = typed.get(key)
    if value is None:
        return 0.0, f"{key}: not given (left out, not zero)"
    return float(value), "typed"


def lay_lines(
    add: Add,
    notes: list[str],
    v: Mapping[str, float],
    facts: list[db.FactRow],
    paid: list[tuple[str, float, str]],
    year: int,
    agi: float,
    typed: Mapping[str, Any],
    status: str,
) -> None:
    """Add the Form IT-201 lines. ``paid`` is (date, amount, origin) for each NY
    estimated payment; ``agi`` is 1040 line 11b."""
    # Federal income and the New York additions and subtractions
    more, more_src = _given(typed, "ny_additions")
    sub, sub_src = _given(typed, "ny_subtractions")
    l19 = add(
        FORM,
        "19",
        "Federal adjusted gross income",
        agi,
        "1040 line 11b (lines 1-18 copy the 1040's income lines)",
    )
    l23 = add(
        FORM,
        "23",
        "Additions (lines 20-23, Form IT-225)",
        more,
        f"ny_additions ({more_src}); the engine models none",
    )
    l24 = add(FORM, "24", "Lines 19 through 23", l19 + l23, "19 + 23")
    l25 = add(
        FORM,
        "25",
        "Taxable refunds, credits, or offsets of state and local income taxes",
        v["salt_refund_income"],
        "engine salt_refund_income (1040 Schedule 1 line 1)",
    )
    l26 = add(
        FORM,
        "26",
        "Pensions of NYS and local governments and the federal government",
        v["taxable_public_pension_income"],
        "engine taxable_public_pension_income",
    )
    l27 = add(
        FORM,
        "27",
        "Taxable amount of social security benefits",
        v["taxable_social_security"],
        "engine taxable_social_security (1040 line 6b)",
    )
    l28 = add(
        FORM,
        "28",
        "Interest income on U.S. government bonds",
        v["us_govt_interest"],
        "engine us_govt_interest",
    )
    l29 = add(
        FORM,
        "29",
        "Pension and annuity income exclusion",
        v["ny_pension_exclusion"],
        "engine ny_pension_exclusion",
    )
    cap = CAP_529 * (2 if status == "JOINT" else 1)
    l30 = add(
        FORM,
        "30",
        "New York's 529 college savings program deduction",
        min(v["investment_in_529_plan"], cap),
        f"engine investment_in_529_plan, ${cap:,.0f} at most (IT-201-I line 30)",
    )
    l31 = add(
        FORM,
        "31",
        "Other subtractions (Form IT-225)",
        sub,
        f"ny_subtractions ({sub_src})",
    )
    l32 = add(
        FORM,
        "32",
        "Lines 25 through 31",
        l25 + l26 + l27 + l28 + l29 + l30 + l31,
        "25 + 26 + 27 + 28 + 29 + 30 + 31",
    )
    l33 = add(FORM, "33", "New York adjusted gross income", l24 - l32, "24 - 32")
    itemized = bool(v["ny_itemizes"])
    l34 = add(
        FORM,
        "34",
        "NY itemized deduction (Form IT-196)" if itemized else "NY standard deduction",
        v["ny_deductions"],
        "engine ny_deductions (itemized, Form IT-196)"
        if itemized
        else "engine ny_deductions (standard)",
    )
    l35 = add(FORM, "35", "Line 33 less line 34", max(l33 - l34, 0.0), "33 - 34")
    l36 = add(
        FORM,
        "36",
        "Dependent exemption amount",
        v["ny_exemptions"],
        "engine ny_exemptions ($1,000 per dependent)",
    )
    l37 = add(FORM, "37", "Taxable income", max(l35 - l36, 0.0), "35 - 36")

    # Tax, credits and other taxes
    l38 = add(FORM, "38", "Taxable income", l37, "37")
    amount, t_src = tax(year, status, l38, l33)
    l39 = add(FORM, "39", "NYS tax on line 38 amount", amount, t_src)
    l40 = add(
        FORM,
        "40",
        "NYS household credit",
        v["ny_household_credit"],
        "engine ny_household_credit",
    )
    l42 = add(
        FORM,
        "42",
        "Other NYS nonrefundable credits (Form IT-201-ATT line 7)",
        v["ny_solar_energy_systems_equipment_credit"]
        + v["ny_geothermal_energy_system_credit"],
        "engine ny_solar_energy_systems_equipment_credit + "
        "ny_geothermal_energy_system_credit",
    )
    l43 = add(FORM, "43", "Lines 40, 41, and 42", l40 + l42, "40 + 42 (41 not drafted)")
    l44 = add(FORM, "44", "Line 39 less line 43", max(l39 - l43, 0.0), "39 - 43")
    l46 = add(FORM, "46", "Total New York State taxes", l44, "44 (line 45 not drafted)")
    use = typed.get("ny_use_tax")
    l59 = add(
        FORM,
        "59",
        "Sales or use tax",
        float(use) if use is not None else use_tax(l19),
        "typed"
        if use is not None
        else "the sales and use tax chart in IT-201-I, by line 19",
    )
    l61 = add(
        FORM,
        "61",
        "Total NYS, NYC, Yonkers, and sales or use taxes",
        l46 + l59,
        "46 + 59 (58 and 60 not drafted)",
    )
    l62 = add(FORM, "62", "Amount from line 61", l61, "61")

    # Refundable credits and payments
    l63 = add(FORM, "63", "Empire State child credit", v["ny_ctc"], "engine ny_ctc")
    l64 = add(
        FORM,
        "64",
        "NYS child and dependent care credit",
        v["ny_cdcc"],
        "engine ny_cdcc (Form IT-216)",
    )
    l65 = add(
        FORM,
        "65",
        "NYS earned income credit",
        v["ny_eitc"] + v["ny_supplemental_eitc"],
        "engine ny_eitc + ny_supplemental_eitc (Form IT-215)",
    )
    l67 = add(
        FORM,
        "67",
        "Real property tax credit",
        v["ny_real_property_tax_credit"],
        "engine ny_real_property_tax_credit (Form IT-214)",
    )
    l68 = add(
        FORM,
        "68",
        "College tuition credit",
        v["ny_college_tuition_credit"],
        "engine ny_college_tuition_credit (Form IT-272)",
    )
    withheld = typed.get("state_withheld")
    l72 = add(
        FORM,
        "72",
        "Total New York State tax withheld",
        float(withheld or 0.0),
        "W-2 box 17, 1099-R box 14 (Needed panel state_withheld)"
        if withheld is not None
        else "state_withheld: not given (left out, not zero)",
    )
    l75 = add(
        FORM,
        "75",
        "Total estimated tax payments",
        sum(a for _, a, _ in paid),
        "; ".join(f"{d} {a:,.2f} ({o})" for d, a, o in paid)
        or "no NY payment recorded (planner paid)",
    )
    l76 = add(
        FORM,
        "76",
        "Total payments",
        l63 + l64 + l65 + l67 + l68 + l72 + l75,
        "63 + 64 + 65 + 67 + 68 + 72 + 75 (66, 69-71, 73, 74 not drafted)",
    )

    # The refund or the amount owed
    if l76 > l62:
        l77 = add(FORM, "77", "Amount overpaid", l76 - l62, "76 - 62")
        add(FORM, "78", "Amount of line 77 available for refund", l77, "77 (79 left 0)")
    else:
        add(FORM, "80", "Amount you owe", l62 - l76, "62 - 76 (penalty 81 not drafted)")

    # Line 37 against the engine's NY taxable income: the engine does not see the
    # typed items or the 529 cap, so they are put back before comparing.
    want = max(
        v["ny_taxable_income"] + more - sub + v["investment_in_529_plan"] - l30, 0.0
    )
    if abs(l37 - want) > TOLERANCE:
        notes.append(
            f"CHECK: NY IT-201 line 37 against the engine's NY taxable income "
            f"(with the typed additions and subtractions): {l37:,.2f} vs {want:,.2f}"
        )
    if use is None:
        notes.append(
            "NY IT-201 line 59 is the sales and use tax chart's figure for your "
            "income; type ny_use_tax when your records show the tax actually owed "
            "(0 when none; the line is never left blank)"
        )
    notes.append(
        "the NY IT-201 draft is for a full-year NYS resident: a part-year or "
        "nonresident return is Form IT-203, which is not drafted; nor are the "
        "New York City and Yonkers taxes and MCTMT (lines 47-58), the resident "
        "credit (line 41) and the estimated tax penalty (line 81, Form IT-2105.9)"
    )
    notes.append(
        "next year's NY safe harbor (IT-2105.9 line 16 worksheet) is lines 46 and "
        "58 less the credits on lines 63-71, less any STAR credit check received "
        "in the year, which the draft does not see"
    )
