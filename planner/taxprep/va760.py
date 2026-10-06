"""Virginia Form 760, laid onto the draft from the same engine run as the
federal return, for a full-year Virginia resident.

Line numbers, the rate schedule, the filing threshold, the Spouse Tax
Adjustment, the Schedule ADJ credit and deduction codes and the rounding rule
follow the 2025 Form 760, its Schedule ADJ and the Form 760 instructions
(Virginia Department of Taxation); the safe harbor follows Form 760C. Virginia
starts from federal AGI (line 1), adds (line 2) and subtracts (lines 4-7) to
reach VAGI (line 9), takes the Virginia itemized or standard deduction (lines
10, 11), the exemptions (line 12) and the Schedule ADJ deductions (line 13),
and taxes the rest on the Tax Rate Schedule (line 16): 2% to $3,000, 3% to
$5,000, 5% to $17,000 and 5.75% above. VAGI under the filing threshold is no
tax. "All amounts entered on your return must be rounded to the nearest
dollar", 50 cents and up rounded up. The engine prices the age deduction,
taxable Social Security, the state refund, the subtractions it models (US
obligations interest, unemployment compensation, the military, National Guard
and federal and state employee subtractions, disability income), the
deductions, the exemptions, the child and dependent care and Commonwealth
Savers (529) deductions, the Spouse Tax Adjustment and the line 23 choice
between the refundable earned income credit and the low-income credit; this
module moves the 529 contribution from the engine's subtractions to line 13
where the form takes it (deduction code 104) and adds what the engine does
not see: the additions, subtractions and deductions you type, VA withholding,
the VA estimated payments recorded for the year and use tax. Not handled, and
named: part-year and nonresident returns (Form 760PY, 763), the overpayment
credited from last year (line 21), extension payments (22), the credit for tax
paid to another state (24), Schedule CR credits (25), the credit to next year
and contributions (29-32), the Form 760C penalty (32) and the Schedule A
detail.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from planner.engine import tax as engine
from planner.ledger import db

FORM = "VA-760"
P = "gov.states.va.tax.income"
ENGINE = (
    "va_agi",
    "va_additions",
    "va_age_deduction",
    "taxable_social_security",
    "salt_refund_income",
    "va_subtractions",
    "va_529_plan_deduction",
    "tax_unit_itemizes",
    "va_itemized_deductions",
    "va_standard_deduction",
    "va_total_exemptions",
    "va_child_dependent_care_expense_deduction",
    "va_educator_expense_deduction",
    "va_income_tax_before_non_refundable_credits",
    "va_spouse_tax_adjustment",
    "va_claims_refundable_eitc",
    "va_refundable_eitc",
    "va_non_refundable_eitc_potential",
)
KEYS = (
    "state_withheld",
    "va_additions",
    "va_subtractions",
    "va_deductions",
    "va_use_tax",
)
ROUNDING = 4.0  # lines 1, 2 and 4-7 each rounded to the dollar

Add = Callable[..., float]


def _given(typed: Mapping[str, Any], key: str) -> tuple[float, str]:
    value = typed.get(key)
    if value is None:
        return 0.0, f"{key}: not given (left out, not zero)"
    return float(value), "typed"


def dollars(x: float) -> float:
    """Half up to the dollar: 50 cents and up rounded up."""
    return float(Decimal(str(round(x, 6))).quantize(Decimal(1), ROUND_HALF_UP))


def tax_on(income: float, year: int) -> float:
    """The Tax Rate Schedule on Virginia taxable income, to the dollar."""
    rows = engine.brackets(f"{P}.rates", year)
    tax = 0.0
    for (low, rate), (high, _) in zip(
        rows, [*rows[1:], (float("inf"), 0.0)], strict=True
    ):
        if income > low:
            tax += (min(income, high) - low) * rate
    return dollars(tax)


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
    """Add the Form 760 lines. ``paid`` is (date, amount, origin) for each VA
    estimated payment; ``agi`` is 1040 line 11a; ``status`` is the filing
    status."""
    del facts  # Virginia reads the federal return through the engine

    # Lines 1-9: VAGI
    l1 = add(
        FORM,
        "1",
        "Adjusted Gross Income from federal return",
        dollars(agi),
        "1040 line 11a",
    )
    adds, adds_src = _given(typed, "va_additions")
    l2 = add(
        FORM,
        "2",
        "Additions from Schedule ADJ, Line 3",
        dollars(v["va_additions"] + adds),
        f"engine va_additions + va_additions ({adds_src})",
    )
    l3 = add(FORM, "3", "Add Lines 1 and 2", l1 + l2, "1 + 2")
    l4 = add(
        FORM,
        "4",
        "Age Deduction",
        dollars(v["va_age_deduction"]),
        "engine va_age_deduction",
    )
    l5 = add(
        FORM,
        "5",
        "Social Security and Tier 1 Railroad Retirement benefits",
        dollars(v["taxable_social_security"]),
        "1040 line 6b",
    )
    l6 = add(
        FORM,
        "6",
        "State income tax refund or overpayment credit",
        dollars(v["salt_refund_income"]),
        "Schedule 1 line 1",
    )
    subs, subs_src = _given(typed, "va_subtractions")
    modeled = (
        v["va_subtractions"]
        - v["va_age_deduction"]
        - v["taxable_social_security"]
        - v["salt_refund_income"]
        - v["va_529_plan_deduction"]
    )
    l7 = add(
        FORM,
        "7",
        "Subtractions from Schedule ADJ, Line 7",
        dollars(modeled + subs),
        f"engine (US obligations interest, unemployment, military, National Guard, "
        f"federal and state employee, disability) + va_subtractions ({subs_src})",
    )
    l8 = add(FORM, "8", "Add Lines 4, 5, 6, and 7", l4 + l5 + l6 + l7, "4 + 5 + 6 + 7")
    l9 = add(FORM, "9", "Virginia Adjusted Gross Income (VAGI)", l3 - l8, "3 - 8")

    # Lines 10-15: the deduction, exemptions and taxable income
    if v["tax_unit_itemizes"]:
        deduction = add(
            FORM,
            "10",
            "Itemized Deductions from Virginia Schedule A",
            dollars(v["va_itemized_deductions"]),
            "engine va_itemized_deductions (you itemize federally, so Virginia too)",
        )
        ded_src = "10"
    else:
        deduction = add(
            FORM,
            "11",
            "Standard deduction",
            dollars(v["va_standard_deduction"]),
            "engine va_standard_deduction (the Virginia amount, not the federal)",
        )
        ded_src = "11"
    l12 = add(
        FORM,
        "12",
        "Exemptions",
        dollars(v["va_total_exemptions"]),
        "Section A (each person) + Section B (65 or older, blind)",
    )
    more, more_src = _given(typed, "va_deductions")
    l13 = add(
        FORM,
        "13",
        "Deductions from Schedule ADJ, Line 9",
        dollars(
            v["va_529_plan_deduction"]
            + v["va_child_dependent_care_expense_deduction"]
            + v["va_educator_expense_deduction"]
            + more
        ),
        f"code 104 Commonwealth Savers + code 101 child and dependent care "
        f"+ educator + va_deductions ({more_src})",
    )
    l14 = add(
        FORM,
        "14",
        "Add Lines 10, 11, 12, and 13",
        deduction + l12 + l13,
        f"{ded_src} + 12 + 13",
    )
    l15 = add(
        FORM, "15", "Virginia Taxable Income", max(l9 - l14, 0.0), "9 - 14, not below 0"
    )

    # Lines 16-18: the tax
    threshold = engine._param(f"{P}.filing_requirement.{status}", year)
    owes = l9 >= threshold
    l16 = add(
        FORM,
        "16",
        "Amount of Tax from Tax Rate Schedule",
        tax_on(l15, year) if owes else 0.0,
        "Tax Rate Schedule on 15"
        if owes
        else f"line 9 is less than the {threshold:,.0f} filing threshold: tax is $0",
    )
    sta = dollars(v["va_spouse_tax_adjustment"]) if status == "JOINT" and owes else 0.0
    l17 = add(
        FORM, "17", "Spouse Tax Adjustment (STA)", sta, "engine (Filing Status 2 only)"
    )
    l18 = add(FORM, "18", "Net Amount of Tax", max(l16 - l17, 0.0), "16 - 17")

    # Lines 19-28: payments and credits against the tax
    withheld = typed.get("state_withheld")
    l19 = add(
        FORM,
        "19a",
        "Virginia withholding",
        dollars(float(withheld or 0.0)),
        "W-2 box 17, 1099-R box 14 (Needed panel state_withheld; the filed form "
        "puts a spouse's on 19b)"
        if withheld is not None
        else "state_withheld: not given (left out, not zero)",
    )
    l20 = add(
        FORM,
        "20",
        "Estimated tax payments",
        dollars(sum(a for _, a, _ in paid)),
        "; ".join(f"{d} {a:,.2f} ({o})" for d, a, o in paid)
        or "no VA payment recorded (planner paid)",
    )
    if v["va_claims_refundable_eitc"]:
        credit = dollars(v["va_refundable_eitc"])
        credit_src = (
            "Schedule ADJ line 16b: 20% of the federal earned income credit, refundable"
        )
    else:
        credit = min(dollars(v["va_non_refundable_eitc_potential"]), l18)
        credit_src = (
            "Schedule ADJ line 18: the low-income credit ($300 an exemption), not "
            "more than 18"
        )
    l23 = add(
        FORM,
        "23",
        "Tax Credit for Low-Income Individuals or Earned Income Credit",
        credit,
        credit_src,
    )
    l26 = add(
        FORM,
        "26",
        "Add Lines 19a through 25",
        l19 + l20 + l23,
        "19a + 20 + 23 (21, 22, 24, 25 not drafted)",
    )
    use, use_src = _given(typed, "va_use_tax")
    l33 = add(FORM, "33", "Consumer's Use Tax", dollars(use), f"va_use_tax ({use_src})")
    l34 = add(
        FORM,
        "34",
        "Adjustments and Voluntary Contributions",
        l33,
        "33 (29-32 not drafted)",
    )
    if l18 > l26:
        l27 = add(FORM, "27", "Tax You Owe", l18 - l26, "18 - 26")
        add(FORM, "35", "Amount You Owe", l27 + l34, "27 + 34")
    else:
        l28 = add(FORM, "28", "Tax Overpayment", l26 - l18, "26 - 18")
        if l28 > l34:
            add(FORM, "36", "Your Refund", l28 - l34, "28 - 34")
        elif l34 > l28:
            add(FORM, "35", "Amount You Owe", l34 - l28, "34 - 28")

    # Line 9 and line 18 against the engine, which rounds nothing, takes the
    # 529 contribution as a subtraction and does not see the typed items.
    want = v["va_agi"] + v["va_529_plan_deduction"] + adds - subs
    if abs(l9 - want) > ROUNDING:
        notes.append(
            f"CHECK: VA Form 760 line 9 against the engine's Virginia AGI: "
            f"{l9:,.2f} vs {want:,.2f}"
        )
    tax = (
        v["va_income_tax_before_non_refundable_credits"] - v["va_spouse_tax_adjustment"]
    )
    if not (adds or subs or more) and abs(l18 - max(tax, 0.0)) > 1.0:
        notes.append(
            f"CHECK: VA Form 760 line 18 against the engine's Virginia tax: "
            f"{l18:,.2f} vs {max(tax, 0.0):,.2f}"
        )
    if v["tax_unit_itemizes"]:
        notes.append(
            "VA Schedule A starts from the federal itemized deductions and takes "
            "out the state and local income taxes in SALT; the draft carries the "
            "engine's total, not the Schedule A detail"
        )
    notes.append(
        "the VA Form 760 draft is for a full-year VA resident: part-year and "
        "nonresident returns (Form 760PY, 763), lines 21, 22, 24 (other state "
        "credit), 25 (Schedule CR), 29-32 and the Form 760C penalty are not drafted"
    )
    notes.append(
        "next year's VA safe harbor (Form 760C) is 90% of next year's tax or 100% "
        "of this year's line 18 less the credits on lines 23-25; no 760C at $150 "
        "or less; from 2026 installments are due when the tax over withholding "
        "and credits is more than $1,000"
    )
