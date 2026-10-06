"""Illinois Form IL-1040, laid onto the draft from the same engine run as the
federal return, for a full-year Illinois resident.

Line numbers, the exemption chart and income exceptions, the rate, the use tax
table and the rounding rule follow the 2025 IL-1040 and its instructions (IDOR,
il-1040.pdf and il-1040-instr.pdf, R-12/25); the safe harbor follows Form
IL-2210 and its instructions. Illinois starts from federal AGI (line 1), adds
federally tax-exempt interest (line 2), subtracts the retirement income and
taxable social security federal AGI counts (line 5), and taxes base income less
the exemption allowance at one flat rate. Every line is in whole dollars,
rounded half up after adding the cents (the instructions' "Should I round?").
The engine prices the exemptions (the dependent-claimed chart, the 65-or-older
boxes and Schedule IL-E/EITC dependents, all zero above the income
exceptions), the Schedule ICR property tax and K-12 credits, the use tax table
and the Illinois EITC and Child Tax Credit; this module adds what it does not
see: Schedule M items you type, IL withholding from the W-2 and 1099-R boxes
and the IL estimated payments recorded for the year. Not handled, and named:
part-year and nonresident returns (Schedule NR), the blind boxes (line 10c),
recapture (13), the credit for tax paid to another state (15, Schedule CR),
Schedule 1299-C (17), household employment tax (20), surcharges (22),
pass-through withholding and credits (27, 28), the IL-2210 penalty (34) and
donations (35).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from planner.engine import tax as engine
from planner.ledger import db

FORM = "IL-1040"
ENGINE = (
    "tax_exempt_interest_income",
    "taxable_social_security",
    "taxable_pension_income",
    "taxable_retirement_distributions",
    "taxable_roth_conversions",
    "salt_refund_income",
    "us_govt_interest",
    "il_529_plan_subtraction",
    "il_base_income",
    "il_is_exemption_eligible",
    "il_personal_exemption",
    "il_aged_blind_exemption",
    "il_dependent_exemption",
    "il_taxable_income",
    "il_income_tax_before_non_refundable_credits",
    "il_property_tax_credit",
    "il_k12_education_expense_credit",
    "il_use_tax",
    "il_eitc",
    "il_ctc",
)
KEYS = ("state_withheld", "il_additions", "il_subtractions", "il_use_tax")
ROUNDING = 3.0  # six lines rounded to the dollar before line 9

Add = Callable[..., float]


def _given(typed: Mapping[str, Any], key: str) -> tuple[float, str]:
    value = typed.get(key)
    if value is None:
        return 0.0, f"{key}: not given (left out, not zero)"
    return float(value), "typed"


def dollars(x: float) -> float:
    """Half up to the dollar: the IL-1040 instructions drop under 50 cents and
    raise 50 to 99 cents, rounding only a line's total."""
    return float(Decimal(str(round(x, 6))).quantize(Decimal(1), ROUND_HALF_UP))


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
    """Add the IL-1040 lines. ``paid`` is (date, amount, origin) for each IL
    estimated payment; ``agi`` is 1040 line 11a; ``status`` is the filing
    status (unused: the engine applies the income exceptions by status)."""
    # Step 2: income
    l1 = add(FORM, "1", "Federal adjusted gross income", dollars(agi), "1040 line 11a")
    l2 = add(
        FORM,
        "2",
        "Federally tax-exempt interest and dividend income",
        dollars(v["tax_exempt_interest_income"]),
        "1040 line 2a",
    )
    adds, adds_src = _given(typed, "il_additions")
    l3 = add(
        FORM,
        "3",
        "Other additions",
        dollars(adds),
        f"Schedule M il_additions ({adds_src})",
    )
    l4 = add(FORM, "4", "Total income", l1 + l2 + l3, "1 + 2 + 3")

    # Step 3: base income
    l5 = add(
        FORM,
        "5",
        "Social Security benefits and certain retirement plan income",
        dollars(
            v["taxable_retirement_distributions"]
            + v["taxable_roth_conversions"]
            + v["taxable_pension_income"]
            + v["taxable_social_security"]
        ),
        "1040 lines 4b, 5b and 6b (engine retirement distributions, Roth "
        "conversions, pensions, taxable social security)",
    )
    l6 = add(
        FORM,
        "6",
        "Illinois Income Tax overpayment",
        dollars(v["salt_refund_income"]),
        "1040 Schedule 1 line 1",
    )
    subs, subs_src = _given(typed, "il_subtractions")
    l7 = add(
        FORM,
        "7",
        "Other subtractions",
        dollars(v["us_govt_interest"] + v["il_529_plan_subtraction"] + subs),
        "Schedule M: engine US obligations interest + engine il_529_plan_subtraction "
        f"+ il_subtractions ({subs_src})",
    )
    l8 = add(FORM, "8", "Total subtractions", l5 + l6 + l7, "5 + 6 + 7")
    l9 = add(FORM, "9", "Illinois base income", max(l4 - l8, 0.0), "4 - 8, not below 0")

    # Step 4: exemptions, zero above the income exceptions
    eligible = bool(v["il_is_exemption_eligible"])
    l10a = add(
        FORM,
        "10a",
        "Exemption amount for yourself and your spouse",
        dollars(v["il_personal_exemption"]) if eligible else 0.0,
        "engine il_personal_exemption (Line 10a chart)",
    )
    l10b = add(
        FORM,
        "10b",
        "65 or older",
        dollars(v["il_aged_blind_exemption"]) if eligible else 0.0,
        "engine il_aged_blind_exemption: $1,000 a box",
    )
    l10d = add(
        FORM,
        "10d",
        "Dependents (Schedule IL-E/EITC Step 2 line 1)",
        dollars(v["il_dependent_exemption"]) if eligible else 0.0,
        "engine il_dependent_exemption",
    )
    l10 = add(
        FORM,
        "10",
        "Exemption allowance",
        l10a + l10b + l10d,
        "10a + 10b + 10d (10c not drafted)"
        if eligible
        else "0: federal AGI is over the income exception",
    )

    # Step 5: net income and tax
    l11 = add(FORM, "11", "Net income", max(l9 - l10, 0.0), "9 - 10, not below 0")
    rate = engine._param("gov.states.il.tax.income.rate", year)
    l12 = add(FORM, "12", "Tax", dollars(l11 * rate), f"11 x {rate:.2%}, to the dollar")
    l14 = add(FORM, "14", "Income tax", l12, "12 (13 recapture not drafted)")

    # Step 6: nonrefundable credits, the engine's already capped at the tax
    l16 = add(
        FORM,
        "16",
        "Property tax and K-12 education expense credits",
        dollars(v["il_property_tax_credit"] + v["il_k12_education_expense_credit"]),
        "engine il_property_tax_credit + il_k12_education_expense_credit "
        "(Schedule ICR)",
    )
    l18 = add(
        FORM,
        "18",
        "Total credits",
        min(l16, l14),
        "16 (15 and 17 not drafted), not above 14",
    )
    l19 = add(FORM, "19", "Tax after nonrefundable credits", l14 - l18, "14 - 18")

    # Step 7: other taxes; line 21 is never blank
    use = typed.get("il_use_tax")
    l21 = add(
        FORM,
        "21",
        "Use tax",
        dollars(float(use)) if use is not None else dollars(v["il_use_tax"]),
        "typed" if use is not None else "engine il_use_tax (UT Table by line 1)",
    )
    l23 = add(FORM, "23", "Total tax", l19 + l21, "19 + 21 (20 and 22 not drafted)")
    l24 = add(FORM, "24", "Total tax from Page 1", l23, "23")

    # Step 8: payments and refundable credits
    withheld = typed.get("state_withheld")
    l25 = add(
        FORM,
        "25",
        "Illinois Income Tax withheld",
        dollars(float(withheld or 0.0)),
        "W-2 box 17, 1099-R box 14 (Needed panel state_withheld; Schedule IL-WIT)"
        if withheld is not None
        else "state_withheld: not given (left out, not zero)",
    )
    l26 = add(
        FORM,
        "26",
        "Estimated payments",
        dollars(sum(a for _, a, _ in paid)),
        "; ".join(f"{d} {a:,.2f} ({o})" for d, a, o in paid)
        or "no IL payment recorded (planner paid)",
    )
    l29 = add(
        FORM,
        "29",
        "Earned Income Tax credit",
        dollars(v["il_eitc"]),
        "engine il_eitc (Schedule IL-E/EITC Step 4 line 9)",
    )
    l30 = add(
        FORM,
        "30",
        "Child Tax credit",
        dollars(v["il_ctc"]),
        "engine il_ctc (Schedule IL-E/EITC Step 5 line 12)",
    )
    l31 = add(
        FORM,
        "31",
        "Total payments and refundable credit",
        l25 + l26 + l29 + l30,
        "25 + 26 + 29 + 30 (27 and 28 not drafted)",
    )

    # Steps 9-11: the refund or the amount owed (34 and 35 not drafted)
    if l31 > l24:
        l32 = add(FORM, "32", "Overpayment", l31 - l24, "31 - 24")
        l37 = add(FORM, "37", "Overpayment after penalty and donations", l32, "32 - 36")
        add(FORM, "38", "Refund", l37, "37 (nothing credited forward on 40)")
    elif l24 > l31:
        l33 = add(FORM, "33", "Underpayment", l24 - l31, "24 - 31")
        add(FORM, "41", "Amount you owe", l33, "33 + 36")

    # Line 9 and line 14 against the engine, which rounds nothing and does not
    # see the typed Schedule M items: a gap past the rounding of the six income
    # and subtraction lines is a mapping the draft got wrong.
    want = max(v["il_base_income"] + adds - subs, 0.0)
    if abs(l9 - want) > ROUNDING:
        notes.append(
            f"CHECK: IL-1040 line 9 against the engine's IL base income (with "
            f"the typed Schedule M items): {l9:,.2f} vs {want:,.2f}"
        )
    tax = v["il_income_tax_before_non_refundable_credits"]
    if not (adds or subs) and abs(l14 - tax) > ROUNDING:
        notes.append(
            f"CHECK: IL-1040 line 14 against the engine's IL income tax: "
            f"{l14:,.2f} vs {tax:,.2f}"
        )
    if l6:
        notes.append(
            "IL-1040 line 6 is the whole state refund on Schedule 1 line 1; "
            "subtract only an Illinois overpayment (another state's refund stays in)"
        )
    if use is None:
        notes.append(
            "IL-1040 line 21 is the UT Table's figure for your income; type "
            "il_use_tax when your records show the use tax actually owed (0 when none)"
        )
    notes.append(
        "the IL-1040 draft is for a full-year IL resident: part-year and "
        "nonresident returns (Schedule NR), lines 10c (blind), 13, 15 (Schedule CR), "
        "17, 20, 22, 27, 28, 34 (IL-2210 penalty) and 35 are not drafted"
    )
    notes.append(
        "next year's IL safe harbor (IL-2210 Step 2) is 100% of this year's tax: "
        "lines 14 and 22 less the credits on 15, 16, 17, 28, 29 and 30; no "
        "penalty when the year's tax less withholding and credits is $1,000 or less"
    )
