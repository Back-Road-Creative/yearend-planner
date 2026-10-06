"""California Form 540, laid onto the draft from the same engine run as the
federal return, for a full-year California resident.

Line numbers, the Tax Table, the rate schedules, the standard deduction and the
exemption credits follow the 2025 Form 540 and its booklet (FTB 2025-540.pdf,
2025-540-booklet.pdf). The engine prices the Schedule CA subtractions and
additions it sees, the deduction, the exemption, dependent care and renter's
credits, the AMT, the Behavioral Health Services Tax and the refundable
credits; this module adds what it does not see: the Schedule CA items you type,
the use tax you type, CA withholding from the W-2 and 1099-R boxes and the CA
estimated payments recorded for the year. Not handled, and named: part-year
and nonresident returns (Form 540NR), the Schedule G-1 and FTB 5870A tax (line
34), the other credits (lines 43-45), other taxes (line 63), 592-B and 593
withholding (line 73), the film credit (line 74), the estimated tax penalty
(line 92, FTB 3853 and line 113) and interest and penalties (line 112).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from planner.engine import tax as engine
from planner.ledger import db

FORM = "CA 540"
ENGINE = (
    "ca_agi_subtractions",
    "ca_additions",
    "ca_agi",
    "ca_deductions",
    "ca_itemized_deductions",
    "ca_standard_deduction",
    "ca_taxable_income",
    "ca_exemptions",
    "ca_cdcc",
    "ca_renter_credit",
    "ca_amt",
    "ca_mental_health_services_tax",
    "ca_eitc",
    "ca_yctc",
    "ca_foster_youth_tax_credit",
    "ca_use_tax",
)
KEYS = ("state_withheld", "ca_additions", "ca_subtractions", "ca_use_tax")
# Schedule X (single, separate), Y (joint, surviving spouse), Z (head of
# household): the engine's own scales, which match the 2025 booklet.
SCHEDULE = {
    "SINGLE": "single",
    "SEPARATE": "separate",
    "JOINT": "joint",
    "SURVIVING_SPOUSE": "surviving_spouse",
    "HEAD_OF_HOUSEHOLD": "head_of_household",
}
CHECKED = 2025  # the year whose booklet the scales were checked against
TABLE_TOP = 100_000.0  # the Tax Table covers taxable income up to $100,000
TOLERANCE = 1.0

Add = Callable[..., float]


def _schedule(scale: list[tuple[float, float]], income: float) -> float:
    owed = 0.0
    for i, (low, rate) in enumerate(scale):
        high = scale[i + 1][0] if i + 1 < len(scale) else math.inf
        if income > low:
            owed += (min(income, high) - low) * rate
    return owed


def _whole(x: float) -> float:
    return float(Decimal(str(round(x, 6))).quantize(Decimal(1), ROUND_HALF_UP))


def _midpoint(income: float) -> float:
    """The Tax Table row's midpoint: rows are $1-$50, then $100 wide ($51-$150,
    $151-$250, ...), with $99,951-$100,000 the last."""
    if income <= 50:
        return 25.5
    if income > 99_950:
        return 99_975.5
    return math.ceil((income - 50) / 100) * 100 + 0.5


def tax(year: int, status: str, income: float) -> tuple[float, str]:
    """Line 31: the Tax Table up to $100,000 of taxable income (the schedule at
    the row's midpoint), the rate schedule above it; whole dollars."""
    name = SCHEDULE[status]
    scale = engine.brackets(f"gov.states.ca.tax.income.rates.{name}", year)
    letter = {"JOINT": "Y", "SURVIVING_SPOUSE": "Y", "HEAD_OF_HOUSEHOLD": "Z"}
    where = (
        f"{year} booklet"
        if year == CHECKED
        else f"the engine's {year} schedule, a CPI projection from 2025"
        if year > CHECKED
        else f"the engine's {year} schedule"
    )
    if income <= 0:
        return 0.0, f"Tax Table ({where}): no taxable income"
    if income <= TABLE_TOP:
        return _whole(_schedule(scale, _midpoint(income))), f"Tax Table ({where})"
    sched = letter.get(status, "X")
    return _whole(_schedule(scale, income)), f"Schedule {sched} ({where})"


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
    """Add the Form 540 lines. ``paid`` is (date, amount, origin) for each CA
    estimated payment; ``agi`` is 1040 line 11b."""
    # Income (Schedule CA Part I line 27, columns B and C)
    sub, sub_src = _given(typed, "ca_subtractions")
    more, more_src = _given(typed, "ca_additions")
    l13 = add(FORM, "13", "Federal adjusted gross income", agi, "1040 line 11b")
    l14 = add(
        FORM,
        "14",
        "California adjustments, subtractions (Schedule CA col B)",
        v["ca_agi_subtractions"] + sub,
        "engine ca_agi_subtractions (taxable Social Security, unemployment, US "
        f"obligations interest, state refund) + ca_subtractions ({sub_src})",
    )
    l15 = add(FORM, "15", "Line 13 less line 14", l13 - l14, "13 - 14")
    l16 = add(
        FORM,
        "16",
        "California adjustments, additions (Schedule CA col C)",
        v["ca_additions"] + more,
        f"engine ca_additions (HSA) + ca_additions ({more_src})",
    )
    l17 = add(FORM, "17", "California adjusted gross income", l15 + l16, "15 + 16")
    itemized = v["ca_itemized_deductions"] > v["ca_standard_deduction"]
    l18 = add(
        FORM,
        "18",
        "CA itemized deductions" if itemized else "CA standard deduction",
        v["ca_deductions"],
        "engine ca_itemized_deductions (Schedule CA Part II)"
        if itemized
        else "engine ca_standard_deduction",
    )
    l19 = add(FORM, "19", "Taxable income", max(l17 - l18, 0.0), "17 - 18")

    # Tax
    amount, t_src = tax(year, status, l19)
    l31 = add(FORM, "31", "Tax", amount, t_src)
    l32 = add(
        FORM, "32", "Exemption credits", v["ca_exemptions"], "engine ca_exemptions"
    )
    l33 = add(FORM, "33", "Line 31 less line 32", max(l31 - l32, 0.0), "31 - 32")
    l35 = add(
        FORM, "35", "Lines 33 and 34", l33, "33 (line 34, Schedule G-1, not drafted)"
    )
    l40 = add(
        FORM,
        "40",
        "Nonrefundable child and dependent care expenses credit",
        v["ca_cdcc"],
        "engine ca_cdcc (FTB 3506)",
    )
    l46 = add(
        FORM,
        "46",
        "Nonrefundable renter's credit",
        v["ca_renter_credit"],
        "engine ca_renter_credit",
    )
    l47 = add(FORM, "47", "Total credits", l40 + l46, "40 + 46 (43-45 not drafted)")
    l48 = add(FORM, "48", "Line 35 less line 47", max(l35 - l47, 0.0), "35 - 47")
    l61 = add(
        FORM, "61", "Alternative minimum tax", v["ca_amt"], "engine ca_amt (Schedule P)"
    )
    l62 = add(
        FORM,
        "62",
        "Behavioral Health Services Tax",
        v["ca_mental_health_services_tax"],
        "engine ca_mental_health_services_tax",
    )
    l64 = add(FORM, "64", "Total tax", l48 + l61 + l62, "48 + 61 + 62 (63 not drafted)")

    # Payments
    withheld = typed.get("state_withheld")
    l71 = add(
        FORM,
        "71",
        "California income tax withheld",
        float(withheld or 0.0),
        "W-2 box 17, 1099-R box 14 (Needed panel state_withheld)"
        if withheld is not None
        else "state_withheld: not given (left out, not zero)",
    )
    l72 = add(
        FORM,
        "72",
        f"{year} California estimated tax",
        sum(a for _, a, _ in paid),
        "; ".join(f"{d} {a:,.2f} ({o})" for d, a, o in paid)
        or "no CA payment recorded (planner paid)",
    )
    l75 = add(FORM, "75", "Earned income tax credit", v["ca_eitc"], "engine ca_eitc")
    l76 = add(
        FORM, "76", "Young child tax credit", v["ca_yctc"], "engine ca_yctc (FTB 3514)"
    )
    l77 = add(
        FORM,
        "77",
        "Foster youth tax credit",
        v["ca_foster_youth_tax_credit"],
        "engine ca_foster_youth_tax_credit (FTB 3514)",
    )
    l78 = add(
        FORM,
        "78",
        "Total payments",
        l71 + l72 + l75 + l76 + l77,
        "71 + 72 + 75 + 76 + 77 (73, 74 not drafted)",
    )

    # Use tax and the balance
    use = typed.get("ca_use_tax")
    l91 = add(
        FORM,
        "91",
        "Use tax",
        float(use) if use is not None else v["ca_use_tax"],
        "typed"
        if use is not None
        else "engine ca_use_tax (the booklet's use tax table by income)",
    )
    l93 = add(FORM, "93", "Payments balance", max(l78 - l91, 0.0), "78 - 91")
    l94 = add(FORM, "94", "Use tax balance", max(l91 - l78, 0.0), "91 - 78")
    l95 = add(
        FORM,
        "95",
        "Payments after the ISR penalty",
        l93,
        "93 (line 92, the health coverage penalty, not drafted)",
    )
    if l95 > l64:
        l97 = add(FORM, "97", "Overpaid tax", l95 - l64, "95 - 64")
        l99 = add(
            FORM, "99", "Overpaid tax available", l97, "97 (nothing applied on 98)"
        )
        add(
            FORM,
            "115",
            "Refund or no amount due",
            l99,
            "99 (contributions 110, penalties 112-113 not drafted)",
        )
    else:
        l100 = add(FORM, "100", "Tax due", l64 - l95, "64 - 95")
        add(
            FORM,
            "111",
            "Amount you owe",
            l94 + l100,
            "94 + 100 (96, 110 not drafted)",
        )

    # Line 19 against the engine's CA taxable income: the engine does not see
    # the typed Schedule CA items, so they are put back before comparing.
    want = max(v["ca_taxable_income"] + more - sub, 0.0)
    if abs(l19 - want) > TOLERANCE:
        notes.append(
            f"CHECK: CA 540 line 19 against the engine's CA taxable income "
            f"(with the typed Schedule CA items): {l19:,.2f} vs {want:,.2f}"
        )
    if use is None and v["ca_use_tax"]:
        notes.append(
            "CA 540 line 91 is the use tax table's estimate for your income; type "
            "ca_use_tax when your records show the use tax actually owed (the "
            "line is never left blank)"
        )
    notes.append(
        "the CA 540 draft is for a full-year CA resident: a part-year or "
        "nonresident return is Form 540NR, which is not drafted; nor are the "
        "health coverage penalty (line 92, FTB 3853) and the estimated tax "
        "penalty (line 113, FTB 5805)"
    )
