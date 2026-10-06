"""New Jersey Form NJ-1040, laid onto the draft from the same engine run as
the federal return, for a full-year New Jersey resident.

Line numbers, the exemptions, the pension and other retirement exclusions,
the property tax deduction or credit (Worksheet H), the Tax Table and the
rounding rule follow the 2025 NJ-1040 and its instructions (NJ Division of
Taxation, 1040.pdf and 1040i.pdf); fund distributions follow GIT-5 and the
safe harbor NJ-2210. New Jersey does not start from federal AGI: it adds its
own categories of income (lines 15-26), never nets a loss in one category
against another (a loss on line 18, 19, 21, 22 or 23 is no entry), allows no
capital loss carryover, and taxes what is left after the retirement
exclusions, exemptions and deductions by the Tax Table (under $100,000) or
the Tax Rate Schedules. Every line is in whole dollars: rounding is optional,
but a return that rounds rounds every line, 50 cents or more up. The engine
prices the categories it models, the pension and other retirement
exclusions, the exemptions but the veteran's, the NJBEST deduction and the
earned income, child and dependent care and child tax credits; this module
adds what it does not see: W-2 box 16 state wages, the other states' bond
interest and fund dividends, veteran exemptions, medical expenses (Worksheet
F; until typed, the engine's estimate), other deductions, property taxes (or
18% of rent) and use tax you type, NJ
withholding and the NJ estimated payments recorded for the year. Not
handled, and named: part-year and nonresident returns, lines 20b and 37b-37c,
the credit for taxes paid to other jurisdictions (44, Schedule NJ-COJ), lines
46-48, the NJ-2210 interest (52), the shared responsibility payment (53c,
Schedule NJ-HCC), excess UI/WF/SWF, disability and family leave withholding
(59-61, NJ-2450), lines 62 and 63, the credit forward and contributions
(69-78) and the Senior Freeze base year.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from planner.engine import tax as engine
from planner.ledger import db
from planner.taxprep import pa40

FORM = "NJ-1040"
P = "gov.states.nj.tax.income"
ENGINE = (
    "us_govt_interest",
    "tax_exempt_interest_income",
    "short_term_capital_gains",
    "long_term_capital_gains",
    "taxable_pension_income",
    "partnership_s_corp_income",
    "rental_income",
    "gambling_winnings",
    "alimony_income",
    "miscellaneous_income",
    "nj_regular_exemption",
    "nj_senior_exemption",
    "nj_blind_or_disabled_exemption",
    "nj_dependents_exemption",
    "nj_dependents_attending_college_exemption",
    "is_qualifying_child_dependent",
    "nj_pension_retirement_exclusion",
    "nj_other_retirement_income_exclusion",
    "nj_other_retirement_special_exclusion",
    "nj_agi",
    "nj_medical_expense_deduction",
    "nj_529_deduction",
    "real_estate_taxes",
    "nj_eitc",
    "nj_cdcc",
    "nj_ctc",
    "nj_income_tax_before_refundable_credits",
)
KEYS = (
    "state_withheld",
    "nj_other_interest",
    "nj_other_dividends",
    "nj_veterans",
    "nj_other_deductions",
    "nj_medical_expenses",
    "nj_property_taxes",
    "nj_use_tax",
)
# Line 15: "contributions to retirement plans (other than 401(k) Plans) are
# included in State wages" (box 16); with no box 16, box 1 plus these.
DEFERRALS = ("12E", "12F", "12G", "12H", "12S")
CARRY_LINES = ("6", "14")  # Schedule D carryovers, which NJ does not allow
VETERAN = 6_000.0  # line 9, "x $6,000"
# Worksheet H: the deduction's cap and the credit, halved for spouses filing
# separately who kept the same main home; the deduction is taken when it
# saves at least the credit.
PROPERTY_TAX_CAP = 15_000.0
PROPERTY_TAX_CREDIT = 50.0
TABLE_UNDER = 100_000.0  # line 43: the Tax Table under this, else the schedules
TABLE_STEP = 50.0  # the Tax Table's rows
SCHEDULE = {
    "SINGLE": "single",
    "JOINT": "joint",
    "SEPARATE": "separate",
    "HEAD_OF_HOUSEHOLD": "head_of_household",
    "SURVIVING_SPOUSE": "surviving_spouse",
}
ROUNDING = 6.0  # lines 15-26 and 28a-28b each rounded to the dollar
TAX_ROUNDING = 5.0  # the Tax Table's $50 rows and the rounded lines above
EXEMPT_INTEREST = ("1099-INT", "8")
EXEMPT_DIVIDENDS = ("1099-DIV", "12")

Add = Callable[..., float]


def _given(typed: Mapping[str, Any], key: str) -> tuple[float, str]:
    value = typed.get(key)
    if value is None:
        return 0.0, f"{key}: not given (left out, not zero)"
    return float(value), "typed"


def dollars(x: float) -> float:
    """Half up to the dollar: 50 cents or more up, less than 50 cents down."""
    return float(Decimal(str(round(x, 6))).quantize(Decimal(1), ROUND_HALF_UP))


def tax_on(income: float, status: str, year: int) -> float:
    """Line 43: the Tax Table under $100,000, whose rows are $50 wide and
    price each at its midpoint ($39,850-$39,900 joint is $628), else the Tax
    Rate Schedule; both to the dollar."""
    rows = engine.brackets(f"{P}.main.{SCHEDULE[status]}", year)
    income = max(income, 0.0)
    if income < TABLE_UNDER:
        income = math.floor(income / TABLE_STEP) * TABLE_STEP + TABLE_STEP / 2
    tax = 0.0
    for (start, rate), (end, _) in zip(rows, [*rows[1:], (math.inf, 0.0)], strict=True):
        tax += max(min(income, end) - start, 0.0) * rate
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
    """Add the NJ-1040 lines. ``paid`` is (date, amount, origin) for each NJ
    estimated payment; ``agi`` (1040 line 11a) is unused, since NJ starts from
    its own categories of income; ``status`` is the filing status."""
    del agi

    # Lines 6-13: exemptions
    vets, vets_src = _given(typed, "nj_veterans")
    children = int(round(v["is_qualifying_child_dependent"]))
    each = engine._param(f"{P}.exemptions.dependents.amount", year)
    qualified = min(children * each, v["nj_dependents_exemption"])
    exemptions = (
        ("6", "Regular", v["nj_regular_exemption"], "engine: $1,000 each"),
        ("7", "Senior 65+", v["nj_senior_exemption"], "engine: $1,000 each"),
        (
            "8",
            "Blind/Disabled",
            v["nj_blind_or_disabled_exemption"],
            "engine: $1,000 each",
        ),
        ("9", "Veteran", vets * VETERAN, f"nj_veterans x $6,000 ({vets_src})"),
        (
            "10",
            "Qualified Dependent Children",
            qualified,
            f"{children} qualifying children x {each:,.0f}",
        ),
        (
            "11",
            "Other Dependents",
            v["nj_dependents_exemption"] - qualified,
            f"the other dependents x {each:,.0f}",
        ),
        (
            "12",
            "Dependents Attending Colleges",
            v["nj_dependents_attending_college_exemption"],
            "engine: $1,000 each full-time student under 22",
        ),
    )
    l13 = 0.0
    for line, label, amount, src in exemptions:
        if amount or line == "6":
            l13 += add(FORM, line, label, dollars(amount), src)
    l13 = add(FORM, "13", "Total Exemption Amount", l13, "6 through 12")

    # Lines 15-27: income, by category; a loss is no entry
    w2 = pa40.compensation(facts, DEFERRALS)
    wages = w2[0] if w2 else v["employment_income"]
    l15 = add(
        FORM,
        "15",
        "Wages, salaries, tips, and other employee compensation",
        dollars(wages),
        ", ".join(w2[1]) if w2 else "engine employment_income (no W-2 on file)",
    )
    other, other_src = _given(typed, "nj_other_interest")
    l16a = add(
        FORM,
        "16a",
        "Taxable interest income",
        dollars(max(v["taxable_interest_income"] - v["us_govt_interest"], 0.0) + other),
        "1040 line 2b less US obligations interest, plus nj_other_interest "
        f"({other_src})",
    )
    exempt = max(v["tax_exempt_interest_income"] - other, 0.0)
    if exempt:
        add(
            FORM,
            "16b",
            "Tax-exempt interest income",
            dollars(exempt),
            "1040 line 2a less the part on 16a",
        )
    fund, fund_src = _given(typed, "nj_other_dividends")
    l17 = add(
        FORM,
        "17",
        "Dividends",
        dollars(v["dividend_income"] + fund),
        f"1040 line 3b, plus nj_other_dividends ({fund_src}); capital gain "
        "distributions are on 19",
    )
    business = v["self_employment_income"]
    carry = -sum(f.value for f in facts if f.form == "SCH-D" and f.box in CARRY_LINES)
    gains = v["short_term_capital_gains"] + v["long_term_capital_gains"] + carry
    pension = (
        v["taxable_pension_income"]
        + v["taxable_ira_distributions"]
        + v["taxable_roth_conversions"]
    )
    rows = (
        (
            "18",
            "Net profits from business",
            business,
            "engine self_employment_income (Schedule C; NJ-BUS-1 Part I)",
        ),
        (
            "19",
            "Net gains or income from disposition of property",
            gains,
            "Schedule NJ-DOP: Schedule D sales and capital gain distributions"
            + (f", federal carryover {carry:,.2f} added back" if carry else ""),
        ),
        (
            "20a",
            "Taxable pension, annuity, and IRA distributions/withdrawals",
            pension,
            "1040 lines 4b and 5b (Roth conversions included)",
        ),
        (
            "21",
            "Distributive Share of Partnership Income",
            v["partnership_s_corp_income"],
            "engine partnership_s_corp_income (an S corporation's share is line 22's)",
        ),
        (
            "23",
            "Net gains or income from rents, royalties, patents, and copyrights",
            v["rental_income"],
            "engine rental_income (NJ-BUS-1 Part IV)",
        ),
        ("24", "Net gambling winnings", v["gambling_winnings"], "engine"),
        (
            "25",
            "Alimony and separate maintenance payments received",
            v["alimony_income"],
            "engine",
        ),
        ("26", "Other", v["miscellaneous_income"], "engine miscellaneous_income"),
    )
    l27 = l15 + l16a + l17
    for line, label, amount, src in rows:
        if amount > 0:
            l27 += add(FORM, line, label, dollars(amount), src)
        elif amount < 0:
            notes.append(
                f"NJ-1040 line {line} is a loss of {-amount:,.2f}: New Jersey "
                "makes no entry and nets it against no other category"
            )
    l27 = add(FORM, "27", "Total Income", l27, "15, 16a, 17 through 20a, 21 through 26")

    # Lines 28a-29: the retirement exclusions and NJ gross income
    l28a = add(
        FORM,
        "28a",
        "Pension/Retirement Exclusion",
        dollars(v["nj_pension_retirement_exclusion"]),
        "engine (age 62 or disabled, line 27 not over $150,000)",
    )
    l28b = add(
        FORM,
        "28b",
        "Other Retirement Income Exclusion",
        dollars(
            v["nj_other_retirement_income_exclusion"]
            + v["nj_other_retirement_special_exclusion"]
        ),
        "engine (Worksheet D)",
    )
    l28c = add(FORM, "28c", "Total Exclusion Amount", l28a + l28b, "28a + 28b")
    l29 = add(FORM, "29", "New Jersey Gross Income", max(l27 - l28c, 0.0), "27 - 28c")
    threshold = engine._param(f"{P}.filing_threshold.{status}", year)
    owes = l29 > threshold

    # Lines 30-39: exemptions, deductions and taxable income
    l30 = add(FORM, "30", "Exemption Amount", l13, "13")
    medical = typed.get("nj_medical_expenses")
    if medical is not None:
        floor = engine._param(f"{P}.deductions.medical_expenses.rate", year)
        insurance = v["self_employed_health_insurance_ald"]
        l31 = add(
            FORM,
            "31",
            "Medical Expenses",
            dollars(max(float(medical) - l29 * floor, 0.0) + insurance),
            f"Worksheet F: nj_medical_expenses (typed) over {floor:.0%} of line "
            "29, plus the self-employed health insurance deduction",
        )
    else:
        l31 = add(
            FORM,
            "31",
            "Medical Expenses",
            dollars(v["nj_medical_expense_deduction"]),
            "engine estimate (Worksheet F: over 2% of line 29; nj_medical_expenses "
            "not given)",
        )
    ded, ded_src = _given(typed, "nj_other_deductions")
    l36 = add(
        FORM,
        "36",
        "Other deductions (lines 32-36)",
        dollars(ded),
        f"nj_other_deductions ({ded_src})",
    )
    l37a = add(
        FORM,
        "37a",
        "NJBEST Deduction",
        dollars(v["nj_529_deduction"]),
        "engine (up to $10,000, NJ gross income $200,000 or less)",
    )
    l38 = add(
        FORM,
        "38",
        "Total Exemptions and Deductions",
        l30 + l31 + l36 + l37a,
        "30 through 37c (37b, 37c not drafted)",
    )
    l39 = add(FORM, "39", "Taxable Income", max(l29 - l38, 0.0), "29 - 38")

    # Lines 40a-43 and Worksheet H: the property tax deduction or credit
    share = 2.0 if status == "SEPARATE" else 1.0
    ptax = typed.get("nj_property_taxes")
    taxes = float(ptax) if ptax is not None else v["real_estate_taxes"]
    credit = 0.0
    l41 = 0.0
    if owes and taxes:
        add(
            FORM,
            "40a",
            "Total Property Taxes (18% of Rent) Paid",
            dollars(taxes),
            "nj_property_taxes (typed)"
            if ptax is not None
            else "the real estate taxes on the Needed panel",
        )
        deduction = min(dollars(taxes), PROPERTY_TAX_CAP / share)
        saved = tax_on(l39, status, year) - tax_on(l39 - deduction, status, year)
        if saved >= PROPERTY_TAX_CREDIT / share:
            l41 = add(
                FORM,
                "41",
                "Property Tax Deduction",
                deduction,
                f"Worksheet H: saves {saved:,.0f} of tax, more than the "
                f"{PROPERTY_TAX_CREDIT / share:,.0f} credit",
            )
        else:
            credit = PROPERTY_TAX_CREDIT / share
    l42 = add(FORM, "42", "New Jersey Taxable Income", max(l39 - l41, 0.0), "39 - 41")
    if owes:
        l43 = add(
            FORM,
            "43",
            "Tax on amount on line 42",
            tax_on(l42, status, year),
            "Tax Table" if l42 < TABLE_UNDER else "Tax Rate Schedules",
        )
    else:
        l43 = add(
            FORM,
            "43",
            "Tax on amount on line 42",
            0.0,
            f"line 29 is not over the {threshold:,.0f} filing threshold: no tax",
        )

    # Lines 45-54: the tax after credits and the total due
    l45 = add(FORM, "45", "Balance of Tax", l43, "43 (44 not drafted)")
    l50 = add(
        FORM,
        "50",
        "Balance of Tax After Credits",
        max(l45, 0.0),
        "45 (46-49 not drafted)",
    )
    use, use_src = _given(typed, "nj_use_tax")
    l51 = add(FORM, "51", "Use Tax Due", dollars(use), f"nj_use_tax ({use_src})")
    l54 = add(FORM, "54", "Total Tax Due", l50 + l51, "50 + 51 (52, 53c not drafted)")

    # Lines 55-66: withholding, credits and payments
    withheld = typed.get("state_withheld")
    l55 = add(
        FORM,
        "55",
        "Total NJ Income Tax Withheld",
        dollars(float(withheld or 0.0)),
        "W-2 box 17, 1099-R box 14 (Needed panel state_withheld)"
        if withheld is not None
        else "state_withheld: not given (left out, not zero)",
    )
    l56 = 0.0
    if credit:
        l56 = add(
            FORM,
            "56",
            "Property Tax Credit",
            credit,
            "Worksheet H: the deduction saves less than the credit",
        )
    l57 = add(
        FORM,
        "57",
        "New Jersey Estimated Tax Payments",
        dollars(sum(a for _, a, _ in paid)),
        "; ".join(f"{d} {a:,.2f} ({o})" for d, a, o in paid)
        or "no NJ payment recorded (planner paid)",
    )
    refundable = (
        (
            "58",
            "New Jersey Earned Income Tax Credit",
            "nj_eitc",
            "40% of the federal EIC",
        ),
        ("64", "Child and Dependent Care Credit", "nj_cdcc", "Worksheet J"),
        (
            "65",
            "New Jersey Child Tax Credit",
            "nj_ctc",
            "line 42 $80,000 or less, each dependent 5 or younger",
        ),
    )
    l66 = l55 + l56 + l57
    for line, label, name, src in refundable:
        if v[name]:
            l66 += add(FORM, line, label, dollars(v[name]), f"engine {name} ({src})")
    l66 = add(
        FORM,
        "66",
        "Total Withholdings, Credits, and Payments",
        l66,
        "55 through 65 (59-63 not drafted)",
    )
    if l66 < l54:
        l67 = add(FORM, "67", "Amount you owe", l54 - l66, "54 - 66")
        add(FORM, "79", "Balance due", l67, "67 (78 not drafted)")
    elif l66 > l54:
        l68 = add(FORM, "68", "Overpayment", l66 - l54, "66 - 54")
        add(FORM, "80", "Refund amount", l68, "68 (69-78 not drafted)")

    # Line 29 and line 50 against the engine, which rounds nothing, prices
    # the exact schedule, reads wages from box 1 and does not see the typed
    # items.
    want = v["nj_agi"] + (wages - v["employment_income"]) + other + fund
    want += max(gains, 0.0) - max(gains - carry, 0.0)
    if abs(l29 - want) > ROUNDING:
        notes.append(
            f"CHECK: NJ-1040 line 29 against the engine's New Jersey gross income: "
            f"{l29:,.2f} vs {want:,.2f}"
        )
    tax = v["nj_income_tax_before_refundable_credits"]
    plain = all(typed.get(k) is None for k in KEYS[1:7]) and not w2
    if plain and abs(l50 - tax) > TAX_ROUNDING:
        notes.append(
            f"CHECK: NJ-1040 line 50 against the engine's New Jersey income tax: "
            f"{l50:,.2f} vs {tax:,.2f}"
        )
    exempt_int = sum(f.value for f in facts if (f.form, f.box) == EXEMPT_INTEREST)
    if exempt_int and typed.get("nj_other_interest") is None:
        notes.append(
            f"NJ-1040 line 16a includes interest on other states' bonds: of the "
            f"{exempt_int:,.2f} tax-exempt interest, type the non-New Jersey part in "
            "nj_other_interest (0 when it is all New Jersey bonds)"
        )
    exempt_div = sum(f.value for f in facts if (f.form, f.box) == EXEMPT_DIVIDENDS)
    if exempt_div and typed.get("nj_other_dividends") is None:
        notes.append(
            f"NJ-1040 line 17: of the {exempt_div:,.2f} exempt-interest dividends, "
            "a fund that is not a New Jersey qualified investment fund pays "
            "taxable dividends (GIT-5); type that part in nj_other_dividends "
            "(0 when none)"
        )
    if not w2 and v["employment_income"]:
        notes.append(
            "NJ-1040 line 15 is the wages you typed: NJ state wages (W-2 box 16) "
            "include retirement plan contributions other than 401(k); drop the "
            "W-2s in so their box 16 is read"
        )
    if medical is None and l31:
        notes.append(
            f"NJ-1040 line 31 is the engine's estimate of {l31:,.0f} (from 65 it "
            "counts the standard Medicare Part B premium): type nj_medical_expenses, "
            "the unreimbursed medical expenses and premiums for Worksheet F line 1"
        )
    if owes and ptax is None:
        notes.append(
            "NJ-1040 line 40a: type nj_property_taxes, the property taxes on your "
            "New Jersey main home, or 18% of the rent a tenant paid (0 when none); "
            "the draft read the real estate taxes on the Needed panel"
        )
    if status == "SEPARATE" and owes and taxes:
        notes.append(
            "NJ-1040 Worksheet H uses $7,500 and $25 for spouses filing separately "
            "who kept the same main home; living apart, each uses $15,000 and $50"
        )
    if not owes:
        notes.append(
            f"NJ-1040: line 29 is not over the {threshold:,.0f} filing threshold, "
            "so there is no tax and no property tax deduction or credit; seniors "
            "and blind or disabled residents can still claim the credit"
        )
    notes.append(
        "the NJ-1040 draft is for a full-year NJ resident: part-year and "
        "nonresident returns, lines 20b, 37b-37c, 44 (Schedule NJ-COJ), 46-48, "
        "52, 53c (Schedule NJ-HCC), 59-63 and 69-78 are not drafted"
    )
    notes.append(
        "next year's NJ safe harbor (NJ-2210) is the lesser of 80% of next year's "
        "line 50 and 100% of this year's; withholding and the refundable credits "
        "(lines 55, 56, 58-65) count as paid; no installment is due under $400"
    )
