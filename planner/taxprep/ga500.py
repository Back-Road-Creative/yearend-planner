"""Georgia Form 500, laid onto the draft from the same engine run as the
federal return, for a full-year Georgia resident, with the Schedule 1 lines it
reads.

Line numbers, the standard deduction, the dependent exemption, the low income
credit table, the itemizer and child care credits and the rounding rule follow
the 2025 Form 500, its Schedule 1 and the IT-511 instruction booklet (Georgia
Department of Revenue); the safe harbor follows Form 500 UET. Georgia starts
from federal AGI (line 8), adds and subtracts on Schedule 1 (line 9), takes the
Georgia standard deduction or the federal itemized deductions less the
booklet's adjustments (lines 11, 12c), subtracts $4,000 a dependent (line 14)
and taxes the rest at one flat rate (5.19% for 2025, line 16). Every line is in
whole dollars ("Round to the nearest dollar"), rounded half up. The engine
prices the Schedule 1 items it models (lump sum distributions, the retirement
and military retirement exclusions, taxable Social Security, Path2College 529
contributions and US obligations interest), the deductions, the exemptions,
the rate and the low income, itemizer and child and dependent care (IND-CR
202) credits; this module adds what it does not see: the Schedule 1 additions
and subtractions you type, the line 12b adjustment you type, GA withholding
and the GA estimated payments recorded for the year. Not handled, and named:
part-year and nonresident returns (Schedule 3), the net operating loss (line
15b), the other state(s) tax credit (18), IND-CR credits past 202 (20),
Schedule 2 and 2B credits (21, 27), G2 withholding (25), the credit to next
year (31), donations (32-41), the Form 500 UET penalty (42), late-payment
penalties and interest (43, 44).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from planner.engine import tax as engine
from planner.ledger import db

FORM = "GA Form 500"
SCHED = "GA Sch 1"
# Schedule 1 subtractions the engine prices: (line, label, variables)
SUBTRACTIONS = (
    (
        "7",
        "Retirement income exclusions (lines 7a-7f, each spouse on their own)",
        ("ga_retirement_exclusion", "ga_military_retirement_exclusion"),
    ),
    ("8", "Social Security benefits (taxable portion)", ("taxable_social_security",)),
    ("9", "Path2College 529 Plan", ("ga_investment_in_529_plan_deduction",)),
    ("10", "Interest on United States obligations", ("us_govt_interest",)),
)
ENGINE = (
    *(var for _, _, names in SUBTRACTIONS for var in names),
    "form_4972_lumpsum_distributions",
    "ga_additions",
    "ga_agi",
    "ga_standard_deduction",
    "tax_unit_itemizes",
    "itemized_taxable_income_deductions",
    "tax_unit_dependents",
    "ga_exemptions",
    "ga_taxable_income",
    "ga_low_income_credit_potential",
    "ga_itemizer_credit_potential",
    "ga_cdcc_potential",
    "ga_income_tax_before_refundable_credits",
)
KEYS = (
    "state_withheld",
    "ga_additions",
    "ga_subtractions",
    "ga_itemized_adjustment",
)
ROUNDING = 3.0  # lines 8, 9 and 11 or 12c each rounded to the dollar
EXEMPT_INTEREST = (("1099-INT", "8"), ("1099-DIV", "12"))  # tax-exempt interest

Add = Callable[..., float]


def _given(typed: Mapping[str, Any], key: str) -> tuple[float, str]:
    value = typed.get(key)
    if value is None:
        return 0.0, f"{key}: not given (left out, not zero)"
    return float(value), "typed"


def dollars(x: float) -> float:
    """Half up to the dollar: "Round to the nearest dollar"."""
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
    """Add the Form 500 lines and the Schedule 1 lines behind them. ``paid`` is
    (date, amount, origin) for each GA estimated payment; ``agi`` is 1040 line
    11a; ``status`` is the filing status."""
    del status  # the engine prices the deduction and credits by status
    l8 = dollars(agi)

    # Schedule 1, additions
    lump = add(
        SCHED,
        "2",
        "Lump sum distributions",
        dollars(v["form_4972_lumpsum_distributions"]),
        "engine (Form 4972)",
    )
    adds, adds_src = _given(typed, "ga_additions")
    s6 = add(
        SCHED,
        "6",
        "Total additions",
        lump + dollars(v["ga_additions"] - v["form_4972_lumpsum_distributions"] + adds),
        f"2 + ga_additions, lines 1, 3-5 ({adds_src})",
    )

    # Schedule 1, subtractions
    total = 0.0
    for line, label, names in SUBTRACTIONS:
        amount = sum(v[n] for n in names)
        if amount or line == "8":
            total += add(
                SCHED, line, label, dollars(amount), "engine " + " + ".join(names)
            )
    subs, subs_src = _given(typed, "ga_subtractions")
    s12 = add(
        SCHED,
        "12",
        "Other adjustments",
        dollars(subs),
        f"ga_subtractions, line 11 and the rest ({subs_src})",
    )
    s13 = add(SCHED, "13", "Total subtractions", total + s12, "7-12")
    s14 = add(SCHED, "14", "Net adjustments", s6 - s13, "6 - 13")

    # Form 500 lines 8-13: income and the deduction
    add(FORM, "8", "Federal adjusted gross income", l8, "1040 line 11a")
    l9 = add(FORM, "9", "Adjustments from Schedule 1", s14, "Sch 1 line 14")
    l10 = add(FORM, "10", "Georgia adjusted gross income", l8 + l9, "8 + 9")
    adj, adj_src = _given(typed, "ga_itemized_adjustment")
    if v["tax_unit_itemizes"]:
        a = add(
            FORM,
            "12a",
            "Federal itemized deductions",
            dollars(v["itemized_taxable_income_deductions"]),
            "Schedule A line 17",
        )
        b = add(
            FORM,
            "12b",
            "Less adjustments",
            dollars(adj),
            f"ga_itemized_adjustment ({adj_src})",
        )
        deduction = add(
            FORM,
            "12c",
            "Georgia total itemized deductions",
            max(a - b, 0.0),
            "12a - 12b",
        )
        ded_src = "12c (you itemize federally, so Georgia too)"
    else:
        deduction = add(
            FORM,
            "11",
            "Standard deduction",
            dollars(v["ga_standard_deduction"]),
            "engine ga_standard_deduction (the Georgia amount, not the federal)",
        )
        ded_src = "11"
    l13 = add(
        FORM, "13", "Income after the deduction", l10 - deduction, f"10 - {ded_src}"
    )

    # Lines 14-16: the exemption and the tax
    count = v["tax_unit_dependents"]
    each = engine._param("gov.states.ga.tax.income.exemptions.dependent", year)
    l14 = add(
        FORM,
        "14",
        "Dependent exemption",
        dollars(v["ga_exemptions"]),
        f"{count:.0f} (line 7c) x {each:,.0f}",
    )
    l15a = add(FORM, "15a", "Income before GA NOL", l13 - l14, "13 - 14")
    l15c = add(
        FORM,
        "15c",
        "Georgia taxable income",
        l15a,
        "15a (15b, the net operating loss, not drafted)",
    )
    rate = engine._param("gov.states.ga.tax.income.main.flat_rate", year)
    l16 = add(FORM, "16", "Tax", dollars(max(l15c, 0.0) * rate), f"15c x {rate:.2%}")

    # Lines 17-23: the credits
    per = float(
        engine._node("gov.states.ga.tax.income.credits.low_income.amount", year).calc(
            l8
        )
    )
    low = v["ga_low_income_credit_potential"]
    if per and low:
        add(
            FORM,
            "17a",
            "Low income credit exemptions",
            float(round(low / per)),
            "self, spouse and children, one more for each 65 or older",
        )
        add(FORM, "17b", "Low income credit amount", per, "the worksheet's table")
    l17 = add(
        FORM,
        "17c",
        "Low income credit",
        dollars(low),
        "17a x 17b (federal AGI under 20,000)",
    )
    l19 = add(
        FORM,
        "19",
        "Georgia eligible itemizer tax credit",
        dollars(v["ga_itemizer_credit_potential"]),
        "engine: $300 a taxpayer who itemizes",
    )
    l20 = add(
        FORM,
        "20",
        "Credits used from IND-CR Summary Worksheet",
        dollars(v["ga_cdcc_potential"]),
        "IND-CR 202: 50% of the federal child and dependent care credit",
    )
    l22 = add(
        FORM,
        "22",
        "Total credits used",
        min(l17 + l19 + l20, l16),
        "17c + 19 + 20, not more than 16 (18, 21 not drafted)",
    )
    l23 = add(FORM, "23", "Balance", max(l16 - l22, 0.0), "16 - 22, not below 0")

    # Lines 24-46: payments, the balance due or the refund
    withheld = typed.get("state_withheld")
    l24 = add(
        FORM,
        "24",
        "Georgia income tax withheld on wages and 1099s",
        dollars(float(withheld or 0.0)),
        "W-2 box 17, 1099-R box 14 (Needed panel state_withheld)"
        if withheld is not None
        else "state_withheld: not given (left out, not zero)",
    )
    l26 = add(
        FORM,
        "26",
        "Estimated tax paid",
        dollars(sum(a for _, a, _ in paid)),
        "; ".join(f"{d} {a:,.2f} ({o})" for d, a, o in paid)
        or "no GA payment recorded (planner paid)",
    )
    l28 = add(
        FORM,
        "28",
        "Total prepayment credits",
        l24 + l26,
        "24 + 26 (25, 27 not drafted)",
    )
    if l23 > l28:
        l29 = add(FORM, "29", "Balance due", l23 - l28, "23 - 28")
        add(FORM, "45", "Amount you owe", l29, "29 (32-44 not drafted)")
    elif l28 > l23:
        l30 = add(FORM, "30", "Overpayment", l28 - l23, "28 - 23")
        add(FORM, "46", "Refund", l30, "30 (31-44 not drafted)")

    # Line 10 and line 23 against the engine, which rounds nothing and does
    # not see the typed items.
    want = v["ga_agi"] + adds - subs
    if abs(l10 - want) > ROUNDING:
        notes.append(
            f"CHECK: GA Form 500 line 10 against the engine's Georgia AGI: "
            f"{l10:,.2f} vs {want:,.2f}"
        )
    tax = v["ga_income_tax_before_refundable_credits"]
    if not (adds or subs or adj) and abs(l23 - tax) > ROUNDING:
        notes.append(
            f"CHECK: GA Form 500 line 23 against the engine's Georgia income tax: "
            f"{l23:,.2f} vs {tax:,.2f}"
        )
    exempt = sum(
        f.value for f in facts if (f.form, f.box) in EXEMPT_INTEREST and f.value
    )
    if exempt and typed.get("ga_additions") is None:
        notes.append(
            f"GA Schedule 1 line 1 adds interest on other states' municipal bonds: "
            f"of the {exempt:,.2f} tax-exempt interest, type the non-Georgia part "
            f"in ga_additions (0 when it is all Georgia bonds)"
        )
    if v["tax_unit_itemizes"] and typed.get("ga_itemized_adjustment") is None:
        notes.append(
            "GA Form 500 line 12b takes out income taxes other than Georgia's in "
            "Schedule A's SALT (their share of the capped line 5e) and interest "
            "spent to earn Georgia-exempt income; type ga_itemized_adjustment, "
            "0 when none"
        )
    notes.append(
        "the GA Form 500 draft is for a full-year GA resident: part-year and "
        "nonresident returns (Schedule 3), lines 15b, 18, 21, 25, 27 and 31-44 "
        "and IND-CR credits other than 202 are not drafted"
    )
    notes.append(
        "next year's GA safe harbor (Form 500 UET) is the lesser of 100% of this "
        "year's Form 500 line 23 less line 27 and 70% of next year's; Georgia has "
        "no 110% rule for higher incomes"
    )
