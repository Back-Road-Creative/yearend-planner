"""Ohio Form IT 1040, laid onto the draft from the same engine run as the
federal return, for a full-year Ohio resident, with the Schedule of
Adjustments, the Schedule of Business Income and the Schedule of Credits lines
it reads.

Line numbers, the exemption amounts, the credits and their income tests, the
use tax worksheet and the rounding rule follow the 2025 IT 1040, its schedules
and the IT 1040 instructions (Ohio Department of Taxation, 1040-bundle.pdf and
it1040-booklet.pdf); the tax follows R.C. 5747.02(A)(3), the brackets the
booklet prints; the safe harbor follows Ohio IT/SD 2210. Ohio starts from
federal AGI (line 1), adds and deducts on the Schedule of Adjustments (lines
2a, 2b), subtracts the exemptions (line 4), taxes business income at 3% after
the business income deduction (lines 6, 8b) and the rest on the brackets
(line 8a). Every line is in whole dollars ("Round all figures to the nearest
dollar"), rounded half up. The engine prices the Schedule of Adjustments items
it models, the exemption count, the retirement income, senior citizen, child
care, exemption and earned income credits and the joint filing credit's
spouse test; this module adds what it does not see: the business income
deduction (from Schedule C and F, which the engine leaves out of Ohio MAGI's
add-back), Schedule of Adjustments and business income items you type, the use
tax you type, OH withholding and the OH estimated payments recorded for the
year. Not handled, and named: part-year and nonresident returns (IT NRC, IT
RC), Schedule of Business Income lines 1, 3-5 and 7-9 beyond the typed total,
the lump sum credits (Schedule of Credits lines 3, 5), lines 7, 8 and 14-35
past the earned income credit, the IT/SD 2210 interest penalty (IT 1040 line
11), late-payment interest (21), the carryforward (24) and donations (25).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from planner.engine import tax as engine
from planner.ledger import db

FORM = "OH IT 1040"
ADJ = "OH Sch ADJ"
BUS = "OH Sch BI"
CRED = "OH Sch CR"
# Schedule of Adjustments deductions the engine prices: (line, label, variable)
DEDUCTIONS = (
    ("15", "Taxable refunds of state and local income taxes", "salt_refund_income"),
    ("16", "Taxable Social Security benefits", "taxable_social_security"),
    (
        "26",
        "Federal interest and dividends exempt from state taxation",
        "us_govt_interest",
    ),
    (
        "27",
        "Deduction of prior year 168(k) and 179 depreciation add-backs",
        "oh_section_179_expense_add_back",
    ),
    (
        "34",
        "Uniformed services retirement income",
        "oh_uniformed_services_retirement_income_deduction",
    ),
    ("37", "Amounts contributed to a 529 Plan", "oh_529_plan_deduction_person"),
    ("38", "Pell/Ohio College Opportunity taxable grant amounts", "pell_grant"),
    ("39", "Ohio educator expenses", "oh_educator_expense_deduction_person"),
    ("42", "Disability benefits", "disability_benefits"),
    (
        "44",
        "Unreimbursed medical and health care expenses",
        "oh_unreimbursed_medical_care_expense_deduction_person",
    ),
)
ENGINE = (
    *(var for _, _, var in DEDUCTIONS),
    "self_employment_income",
    "farm_income",
    "oh_additions",
    "oh_agi",
    "oh_personal_exemptions_eligible_person",
    "oh_income_tax_before_non_refundable_credits",
    "oh_income_tax_before_refundable_credits",
    "oh_retirement_credit_potential",
    "oh_senior_citizen_credit_potential",
    "oh_cdcc_potential",
    "oh_exemption_credit_potential",
    "oh_joint_filing_credit_eligible",
    "oh_eitc_potential",
    "oh_refundable_credits",
)
KEYS = (
    "state_withheld",
    "oh_additions",
    "oh_deductions",
    "oh_business_income",
    "oh_use_tax",
)
# R.C. 5747.02(A)(3), the 2025 brackets the booklet prints: over 26,050, $342
# plus 2.75% of the excess; over 100,000, $2,394.32 plus 3.125%. The engine
# chains its own top base (2,375.63), $18.69 below the statute's.
BRACKETS = {2025: ((26_050.0, 342.0, 0.0275), (100_000.0, 2_394.32, 0.03125))}
BID_CAP = {"SEPARATE": 125_000.0}  # Schedule of Business Income line 12; else 250,000
# Schedule of Credits line 12, the booklet's table on MAGI less exemptions:
# 20% to 25,000, 15% to 50,000, 10% to 75,000, 5% below 750,000; up to $650.
JOINT = ((25_000.0, 0.20), (50_000.0, 0.15), (75_000.0, 0.10), (749_999.0, 0.05))
JOINT_CAP = 650.0
ROUNDING = 3.0  # lines 1, 2a and 2b each rounded to the dollar

Add = Callable[..., float]


def _given(typed: Mapping[str, Any], key: str) -> tuple[float, str]:
    value = typed.get(key)
    if value is None:
        return 0.0, f"{key}: not given (left out, not zero)"
    return float(value), "typed"


def dollars(x: float) -> float:
    """Half up to the dollar: "Round all figures to the nearest dollar."""
    return float(Decimal(str(round(x, 6))).quantize(Decimal(1), ROUND_HALF_UP))


def nonbusiness_tax(income: float, year: int) -> float:
    """IT 1040 line 8a on line 7a's taxable nonbusiness income: nothing up to
    26,050, then the bracket's base plus its rate on the excess."""
    tax = 0.0
    for over, base, rate in BRACKETS[year]:
        if income > over:
            tax = base + rate * (income - over)
    return tax


def joint_rate(income: float) -> float:
    """Schedule of Credits line 12's percentage of line 11."""
    return next((rate for top, rate in JOINT if income <= top), 0.0)


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
    """Add the IT 1040 lines and the schedule lines behind them. ``paid`` is
    (date, amount, origin) for each OH estimated payment; ``agi`` is 1040 line
    11a; ``status`` is the filing status (the business income deduction's cap)."""
    l1 = dollars(agi)

    # Schedule of Adjustments, additions
    adds, adds_src = _given(typed, "oh_additions")
    a12 = add(
        ADJ,
        "12",
        "Total additions",
        dollars(v["oh_additions"] + adds),
        f"engine oh_additions (lines 9, 11) + oh_additions ({adds_src})",
    )

    # Schedule of Business Income, parts 1 and 2
    sch_c = add(
        BUS, "2", "Schedule C", dollars(v["self_employment_income"]), "Sch C line 31"
    )
    sch_f = add(BUS, "6", "Schedule F", dollars(v["farm_income"]), "Sch F line 34")
    other, other_src = _given(typed, "oh_business_income")
    b10 = add(
        BUS,
        "10",
        "Total business income",
        sch_c + sch_f + dollars(other),
        f"2 + 6 + oh_business_income, lines 1, 3-5, 7-9 ({other_src})",
    )
    b11 = add(
        BUS,
        "11",
        "Lesser of line 10 or IT 1040 line 1",
        max(min(b10, l1), 0.0),
        "not below 0",
    )
    cap = BID_CAP.get(status, 250_000.0)
    b13 = add(
        BUS,
        "13",
        "Business income deduction",
        min(b11, cap),
        f"lesser of 11 and {cap:,.0f}",
    )

    # Schedule of Adjustments, deductions
    total = b13
    add(ADJ, "13", "Business income deduction", b13, "Sch BI line 13")
    for line, label, var in DEDUCTIONS:
        if v[var] or line in ("15", "16"):
            total += add(ADJ, line, label, dollars(v[var]), f"engine {var}")
    deds, deds_src = _given(typed, "oh_deductions")
    a47 = add(
        ADJ,
        "47",
        "Total deductions",
        total + dollars(deds),
        f"13 + the lines above + oh_deductions ({deds_src})",
    )

    # IT 1040 lines 1-7a
    add(FORM, "1", "Federal adjusted gross income", l1, "1040 line 11a")
    add(FORM, "2a", "Additions", a12, "Sch ADJ line 12")
    add(FORM, "2b", "Deductions", a47, "Sch ADJ line 47")
    l3 = add(FORM, "3", "Ohio adjusted gross income", l1 + a12 - a47, "1 + 2a - 2b")
    magi = l3 + b13
    count = v["oh_personal_exemptions_eligible_person"]
    each = float(
        engine._node("gov.states.oh.tax.income.exemptions.personal.amount", year).calc(
            magi
        )
    )
    l4 = add(
        FORM,
        "4",
        "Exemption amount",
        dollars(count * each),
        f"{count:.0f} x {each:,.0f} (modified AGI {magi:,.0f}: line 3 + Sch BI 13)",
    )
    l5 = add(FORM, "5", "Ohio income tax base", max(l3 - l4, 0.0), "3 - 4, not below 0")
    b15 = add(
        BUS,
        "15",
        "Taxable business income",
        min(b11 - b13, l5),
        "lesser of 11 - 13 and IT 1040 line 5",
    )
    b16 = add(
        BUS, "16", "Business income tax liability", dollars(b15 * 0.03), "15 x 3%"
    )
    l6 = add(FORM, "6", "Taxable business income", b15, "Sch BI line 15")
    l7 = add(
        FORM, "7", "Taxable nonbusiness income", max(l5 - l6, 0.0), "5 - 6, not below 0"
    )
    add(FORM, "7a", "Amount from line 7 on page 1", l7, "7")

    # Lines 8a-8c: the tax
    if year in BRACKETS:
        l8a = add(
            FORM,
            "8a",
            "Nonbusiness income tax liability",
            dollars(nonbusiness_tax(l7, year)),
            "R.C. 5747.02(A)(3) brackets on 7a",
        )
    else:
        l8a = add(
            FORM,
            "8a",
            "Nonbusiness income tax liability",
            dollars(v["oh_income_tax_before_non_refundable_credits"]),
            f"engine (no {year} brackets in the draft)",
        )
    l8b = add(FORM, "8b", "Business income tax liability", b16, "Sch BI line 16")
    l8c = add(FORM, "8c", "Income tax liability before credits", l8a + l8b, "8a + 8b")

    # Schedule of Credits, nonrefundable
    c1 = add(CRED, "1", "Tax liability before credits", l8c, "IT 1040 line 8c")
    c2 = add(
        CRED,
        "2",
        "Retirement income credit",
        dollars(v["oh_retirement_credit_potential"]),
        "engine (Table 2)",
    )
    c4 = add(
        CRED,
        "4",
        "Senior citizen credit",
        dollars(v["oh_senior_citizen_credit_potential"]),
        "engine: $50, 65 or older",
    )
    c6 = add(
        CRED,
        "6",
        "Child care and dependent care credit",
        dollars(v["oh_cdcc_potential"]),
        "engine oh_cdcc_potential",
    )
    c9 = add(
        CRED,
        "9",
        "Exemption credit",
        dollars(v["oh_exemption_credit_potential"]),
        "engine: $20 an exemption",
    )
    c10 = add(CRED, "10", "Total", c2 + c4 + c6 + c9, "2 + 4 + 6 + 9")
    c11 = add(CRED, "11", "Tax less credits", max(c1 - c10, 0.0), "1 - 10, not below 0")
    pct = joint_rate(magi - l4)
    if v["oh_joint_filing_credit_eligible"]:
        joint, joint_src = (
            min(dollars(c11 * pct), JOINT_CAP),
            f"{pct:.0%} of 11, up to $650",
        )
    else:
        joint, joint_src = 0.0, "not joint, or a spouse under $500 of qualifying income"
    c12 = add(CRED, "12", "Joint filing credit", joint, joint_src)
    c13 = add(
        CRED,
        "13",
        "Earned income credit",
        dollars(v["oh_eitc_potential"]),
        "30% of 1040 line 27a",
    )
    c36 = add(CRED, "36", "Total", c12 + c13, "12 + 13 (14-35 not drafted)")
    add(
        CRED,
        "37",
        "Tax less additional credits",
        max(c11 - c36, 0.0),
        "11 - 36, not below 0",
    )
    c40 = add(
        CRED,
        "40",
        "Total nonrefundable credits",
        c10 + c36,
        "10 + 36 (38, 39 not drafted)",
    )
    c47 = add(
        CRED,
        "47",
        "Total refundable credits",
        dollars(v["oh_refundable_credits"]),
        "engine oh_refundable_credits",
    )

    # IT 1040 lines 9-26
    add(FORM, "9", "Ohio nonrefundable credits", c40, "Sch CR line 40")
    l10 = add(
        FORM,
        "10",
        "Tax liability after nonrefundable credits",
        max(l8c - c40, 0.0),
        "8c - 9, not below 0",
    )
    use, use_src = _given(typed, "oh_use_tax")
    use_given = typed.get("oh_use_tax") is not None
    l12 = add(FORM, "12", "Unpaid use tax", dollars(use), f"oh_use_tax ({use_src})")
    l13 = add(
        FORM, "13", "Total Ohio tax liability", l10 + l12, "10 + 12 (11 not drafted)"
    )
    withheld = typed.get("state_withheld")
    l14 = add(
        FORM,
        "14",
        "Ohio income tax withheld",
        dollars(float(withheld or 0.0)),
        "W-2 box 17, 1099-R box 14 (Needed panel state_withheld)"
        if withheld is not None
        else "state_withheld: not given (left out, not zero)",
    )
    l15 = add(
        FORM,
        "15",
        "Estimated and extension payments",
        dollars(sum(a for _, a, _ in paid)),
        "; ".join(f"{d} {a:,.2f} ({o})" for d, a, o in paid)
        or "no OH payment recorded (planner paid)",
    )
    l16 = add(FORM, "16", "Refundable credits", c47, "Sch CR line 47")
    l17 = add(FORM, "17", "Total Ohio tax payments", l14 + l15 + l16, "14 + 15 + 16")
    l19 = add(
        FORM, "19", "Line 17 minus line 18", l17, "17 (18 is for an amended return)"
    )
    if l19 > l13:
        l23 = add(FORM, "23", "Overpayment", l19 - l13, "19 - 13")
        add(FORM, "26", "Refund", l23, "23 (24 and 25g not drafted)")
    elif l13 > l19:
        l20 = add(FORM, "20", "Tax due", l13 - l19, "13 - 19")
        add(FORM, "22", "Total amount due", l20, "20 (21 interest not drafted)")

    # Line 3 and line 10 against the engine, which rounds nothing, has no
    # business income deduction and does not see the typed items.
    want = v["oh_agi"] + adds - deds - b13
    if abs(l3 - want) > ROUNDING:
        notes.append(
            f"CHECK: OH IT 1040 line 3 against the engine's Ohio AGI less the "
            f"business income deduction: {l3:,.2f} vs {want:,.2f}"
        )
    tax = v["oh_income_tax_before_refundable_credits"]
    if (
        not (adds or deds or b13 or other)
        and l7 <= 100_000
        and abs(l10 - tax) > 2 * ROUNDING
    ):
        notes.append(
            f"CHECK: OH IT 1040 line 10 against the engine's Ohio income tax: "
            f"{l10:,.2f} vs {tax:,.2f}"
        )
    if l7 > 100_000:
        notes.append(
            "OH IT 1040 line 8a follows R.C. 5747.02(A)(3): $2,394.32 plus 3.125% "
            "over $100,000; the engine's base is $18.69 lower, so its estimate runs "
            "under the return"
        )
    if b10:
        notes.append(
            "OH Schedule of Business Income takes Schedule C and F as business "
            "income; type oh_business_income for the rest (K-1s, 4797, guaranteed "
            "payments) and check each amount is business income under R.C. 5747.01(B)"
        )
    if status == "JOINT" and not v["oh_joint_filing_credit_eligible"]:
        notes.append(
            "OH joint filing credit (Sch CR line 12) not claimed: each spouse needs "
            "$500 of qualifying income, Ohio AGI other than interest, dividends and "
            "distributions, capital gains, rents and royalties (amounts deducted on "
            "the Schedule of Adjustments, such as business income and Social "
            "Security, do not count); the engine sees one spouse short"
        )
    if not use_given:
        notes.append(
            "OH IT 1040 line 12 is use tax on purchases no Ohio sales tax was "
            "collected on (the booklet's worksheet: purchases x your county's rate); "
            "type oh_use_tax, 0 when none"
        )
    notes.append(
        "the OH IT 1040 draft is for a full-year OH resident: part-year and "
        "nonresident returns (IT NRC, IT RC), Schedule of Credits lines 3, 5, 7, 8 "
        "and 14-35, IT 1040 lines 11 (IT/SD 2210 interest penalty), 21, 24 and 25 "
        "are not drafted"
    )
    notes.append(
        "next year's OH safe harbor (IT/SD 2210) is 100% of this year's IT 1040 "
        "line 10 less line 16; no penalty when the year's tax less withholding is "
        "$500 or less"
    )
