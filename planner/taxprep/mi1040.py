"""Michigan MI-1040, laid onto the draft from the same engine run as the
federal return, for a full-year Michigan resident, with the Schedule 1 lines
it reads.

Line numbers, the exemption amounts, the 4.25% rate, the Michigan earned
income tax credit and the rounding rule follow the 2025 MI-1040, its Schedule
1 (Form 3423) and the MI-1040 instruction book (Michigan Department of
Treasury); the safe harbor follows MI-2210. Michigan starts from federal AGI
(line 10), adds and subtracts on Schedule 1 (lines 11, 13), takes $5,800 an
exemption plus the special and disabled veteran exemptions (line 9f, carried
to line 15) and taxes the rest at one flat rate (line 17). Every line is in
whole dollars ("Round down amounts of 49 cents or less. Round up amounts of 50
cents or more"). The engine prices the Schedule 1 items it models (the
deduction for self-employment tax, US obligations interest, military
retirement pay, taxable Social Security and military pay, Michigan income tax
refunds, Michigan 529 contributions, the Schedule R credit base, the Tier 2
and Tier 3 Michigan Standard Deductions, the Form 4884 retirement and pension
deduction and the senior investment income deduction), the exemptions, the
rate and the earned income tax credit; this module adds what it does not see:
the Schedule 1 additions and subtractions you type, the MI-1040CR property tax
credit you type, MI withholding and the MI estimated payments recorded for the
year. Not handled, and named: part-year and nonresident returns (Schedule NR),
the Schedule 1 year of birth boxes (24A-24H), the Michigan net operating loss
(Schedule 1 line 30), the nonrefundable credits (lines 18-20), voluntary
contributions, the home buyer savings penalty and use tax (22-24), the
farmland preservation, historic preservation and flow-through entity credits
(27, 29, 30), amended returns (33), the credit forward (38), the MI-2210
penalty and interest (36) and the home heating credit (MI-1040CR-7, filed
apart).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from planner.engine import tax as engine
from planner.ledger import db

FORM = "MI-1040"
SCHED = "MI Sch 1"
EX = "gov.states.mi.tax.income.exemptions"
RB = "gov.states.mi.tax.income.deductions.retirement_benefits"
# Schedule 1 subtractions the engine prices: (line, label, variables)
SUBTRACTIONS = (
    ("10", "Income from U.S. government bonds and obligations", ("us_govt_interest",)),
    (
        "11",
        "Military retirement and railroad retirement benefits",
        ("military_retirement_pay",),
    ),
    (
        "14",
        "Taxable Social Security benefits or military pay",
        ("taxable_social_security", "military_service_income"),
    ),
    ("16", "Michigan state and local income tax refunds", ("salt_refund_income",)),
    ("17", "Michigan 529 and MiABLE contributions", ("mi_529_deduction",)),
    (
        "23",
        "Miscellaneous subtractions: the Schedule R credit base",
        ("section_22_income",),
    ),
)
# Lines 25 and 26, the Michigan Standard Deduction by the older spouse's
# birth year; the engine takes the larger of each and Form 4884 Section D
# (Worksheet 3.3), which the form puts on line 27 instead.
STANDARD = (
    ("25", "Tier 2 Michigan Standard Deduction", "mi_standard_deduction_tier_two"),
    ("26", "Tier 3 Michigan Standard Deduction", "mi_standard_deduction_tier_three"),
)
SENIOR = (
    "28",
    "Dividend/interest/capital gains deduction (born before 1946)",
    "mi_interest_dividends_capital_gains_deduction",
)
ENGINE = (
    *(var for _, _, names in SUBTRACTIONS for var in names),
    *(var for _, _, var in STANDARD),
    "mi_expanded_retirement_benefits_deduction",
    "mi_pension_benefit",
    SENIOR[2],
    "self_employment_tax_ald",
    "mi_additions",
    "mi_subtractions",
    "tax_unit_size",
    "tax_unit_stillborn_children",
    "mi_disabled_exemption_eligible_person",
    "is_fully_disabled_service_connected_veteran",
    "mi_exemptions",
    "mi_taxable_income",
    "mi_income_tax_before_refundable_credits",
    "eitc",
    "mi_eitc",
    "mi_homestead_property_tax_credit",
    "mi_home_heating_credit",
)
KEYS = (
    "state_withheld",
    "mi_additions",
    "mi_subtractions",
    "mi_property_tax_credit",
)
ROUNDING = 3.0  # lines 10, 11 and 13 each rounded to the dollar
EXEMPT_INTEREST = (("1099-INT", "8"), ("1099-DIV", "12"))  # tax-exempt interest

Add = Callable[..., float]


def _given(typed: Mapping[str, Any], key: str) -> tuple[float, str]:
    value = typed.get(key)
    if value is None:
        return 0.0, f"{key}: not given (left out, not zero)"
    return float(value), "typed"


def dollars(x: float) -> float:
    """Half up to the dollar: 49 cents or less down, 50 cents or more up."""
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
    """Add the MI-1040 lines and the Schedule 1 lines behind them. ``paid`` is
    (date, amount, origin) for each MI estimated payment; ``agi`` is 1040 line
    11a; ``status`` is the filing status."""
    del status  # the engine prices the deductions and exemptions by status

    # Schedule 1, additions
    se = add(
        SCHED,
        "2",
        "Deduction for taxes on or measured by income",
        dollars(v["self_employment_tax_ald"]),
        "engine self_employment_tax_ald (the federal deduction for half the "
        "self-employment tax)",
    )
    adds, adds_src = _given(typed, "mi_additions")
    s9 = add(
        SCHED,
        "9",
        "Total additions",
        se + dollars(v["mi_additions"] - v["self_employment_tax_ald"] + adds),
        f"2 + mi_additions, lines 1 and 3-8 ({adds_src})",
    )

    # Schedule 1, subtractions
    total = 0.0
    for line, label, names in SUBTRACTIONS:
        amount = sum(v[n] for n in names)
        if amount:
            total += add(
                SCHED, line, label, dollars(amount), "engine " + " + ".join(names)
            )
    expanded = v["mi_expanded_retirement_benefits_deduction"]
    section_d = 0.0
    for line, label, var in STANDARD:
        amount = v[var]
        if amount and expanded and abs(amount - expanded) < 0.005:
            section_d = amount  # Form 4884 line 19, not the standard deduction
        elif amount:
            total += add(SCHED, line, label, dollars(amount), f"engine {var}")
    pension = v["mi_pension_benefit"] + section_d
    if pension:
        total += add(
            SCHED,
            "27",
            "Retirement/pension benefits (Form 4884)",
            dollars(pension),
            "engine mi_pension_benefit"
            + (
                " + Form 4884 Section D line 19 (Worksheet 3.3: up to "
                f"{engine._param(f'{RB}.expanded.rate', year):.0%} of the "
                "Section A limit)"
                if section_d
                else ""
            ),
        )
    if v[SENIOR[2]]:
        total += add(
            SCHED, SENIOR[0], SENIOR[1], dollars(v[SENIOR[2]]), f"engine {SENIOR[2]}"
        )
    subs, subs_src = _given(typed, "mi_subtractions")
    s29 = add(
        SCHED,
        "29",
        "Subtotal",
        total + dollars(subs),
        f"10-28; mi_subtractions, lines 12, 13, 15 and 18-22 ({subs_src})",
    )
    s31 = add(
        SCHED,
        "31",
        "Total subtractions",
        s29,
        "29 (30, the Michigan net operating loss, not drafted)",
    )

    # Line 9: exemptions
    count = v["tax_unit_size"]
    personal = engine._param(f"{EX}.personal", year)
    add(
        FORM,
        "9a",
        "Exemptions",
        dollars(count * personal),
        f"{count:.0f} (you, spouse and dependents) x {personal:,.0f}",
    )
    parts = count * personal
    for line, label, var, path in (
        (
            "9b",
            "Special exemptions (deaf, blind, disabled)",
            "mi_disabled_exemption_eligible_person",
            "disabled.amount.base",
        ),
        (
            "9c",
            "Qualified disabled veterans",
            "is_fully_disabled_service_connected_veteran",
            "disabled.amount.veteran",
        ),
        (
            "9d",
            "Certificates of Stillbirth",
            "tax_unit_stillborn_children",
            "personal",
        ),
    ):
        n = v[var]
        if n:
            each = engine._param(f"{EX}.{path}", year)
            parts += n * each
            add(FORM, line, label, dollars(n * each), f"{n:.0f} x {each:,.0f}")
    l9f = add(
        FORM,
        "9f",
        "Total exemptions",
        dollars(v["mi_exemptions"]),
        "engine mi_exemptions (9a-9e)",
    )

    # Lines 10-17: income and the tax
    l10 = add(FORM, "10", "Adjusted gross income", dollars(agi), "1040 line 11a")
    l11 = add(FORM, "11", "Additions from Schedule 1", s9, "Sch 1 line 9")
    l12 = add(FORM, "12", "Total", l10 + l11, "10 + 11")
    l13 = add(FORM, "13", "Subtractions from Schedule 1", s31, "Sch 1 line 31")
    l14 = add(
        FORM, "14", "Income subject to tax", max(l12 - l13, 0.0), "12 - 13, not below 0"
    )
    l15 = add(FORM, "15", "Exemption allowance", l9f, "9f")
    l16 = add(FORM, "16", "Taxable income", max(l14 - l15, 0.0), "14 - 15, not below 0")
    rate = engine._param("gov.states.mi.tax.income.rate", year)
    l17 = add(FORM, "17", "Tax", dollars(l16 * rate), f"16 x {rate:.2%}")
    l21 = add(FORM, "21", "Income tax", l17, "17 (credits on 18-20 not drafted)")
    l25 = add(
        FORM,
        "25",
        "Total tax liability",
        l21,
        "21 (22-24: contributions, the home buyer savings penalty and use tax, "
        "not drafted)",
    )

    # Lines 26-34: refundable credits and payments
    ptc, ptc_src = _given(typed, "mi_property_tax_credit")
    l26 = add(
        FORM,
        "26",
        "Property tax credit",
        dollars(ptc),
        f"MI-1040CR or MI-1040CR-2 (mi_property_tax_credit, {ptc_src})",
    )
    l28 = 0.0
    if v["eitc"]:
        add(
            FORM,
            "28a",
            "Federal earned income tax credit",
            dollars(v["eitc"]),
            "1040 line 27a",
        )
        match = engine._param("gov.states.mi.tax.income.credits.eitc.match", year)
        l28 = add(
            FORM,
            "28b",
            "Michigan earned income tax credit",
            dollars(v["mi_eitc"]),
            f"28a x {match:.0%}",
        )
    withheld = typed.get("state_withheld")
    l31 = add(
        FORM,
        "31",
        "Michigan tax withheld",
        dollars(float(withheld or 0.0)),
        "Schedule W line 6: W-2 box 17, 1099-R box 14 (Needed panel state_withheld)"
        if withheld is not None
        else "state_withheld: not given (left out, not zero)",
    )
    l32 = add(
        FORM,
        "32",
        "Estimated tax payments",
        dollars(sum(a for _, a, _ in paid)),
        "; ".join(f"{d} {a:,.2f} ({o})" for d, a, o in paid)
        or "no MI payment recorded (planner paid)",
    )
    l34 = add(
        FORM,
        "34",
        "Total refundable credits and payments",
        l26 + l28 + l31 + l32,
        "26 + 28b + 31 + 32 (27, 29, 30, 33c not drafted)",
    )

    # Lines 36-39: the tax due or the refund
    if l25 > l34:
        add(
            FORM,
            "36",
            "You owe",
            l25 - l34,
            "25 - 34 (interest and penalty not drafted)",
        )
    elif l34 > l25:
        l37 = add(FORM, "37", "Overpayment", l34 - l25, "34 - 25")
        add(FORM, "39", "Refund", l37, "37 (38, the credit forward, not drafted)")

    # The Schedule 1 totals and line 21 against the engine, which rounds
    # nothing and does not see the typed items.
    want = v["mi_additions"] - v["mi_subtractions"] + adds - subs
    if abs((s9 - s31) - want) > ROUNDING:
        notes.append(
            f"CHECK: MI Schedule 1 additions less subtractions against the "
            f"engine's: {s9 - s31:,.2f} vs {want:,.2f}"
        )
    if abs(parts - v["mi_exemptions"]) > 0.5:
        notes.append(
            f"CHECK: MI-1040 lines 9a-9d against the engine's exemptions: "
            f"{parts:,.2f} vs {v['mi_exemptions']:,.2f}"
        )
    tax = v["mi_income_tax_before_refundable_credits"]
    if not (adds or subs) and abs(l21 - tax) > ROUNDING:
        notes.append(
            f"CHECK: MI-1040 line 21 against the engine's Michigan income tax: "
            f"{l21:,.2f} vs {tax:,.2f}"
        )
    exempt = sum(
        f.value for f in facts if (f.form, f.box) in EXEMPT_INTEREST and f.value
    )
    if exempt and typed.get("mi_additions") is None:
        notes.append(
            f"MI Schedule 1 line 1 adds interest and dividends from other states' "
            f"bonds: of the {exempt:,.2f} tax-exempt interest, type the "
            f"non-Michigan part in mi_additions (0 when it is all Michigan bonds)"
        )
    if (
        typed.get("mi_property_tax_credit") is None
        and v["mi_homestead_property_tax_credit"]
    ):
        notes.append(
            f"the engine estimates a {v['mi_homestead_property_tax_credit']:,.2f} "
            f"Michigan homestead property tax credit: work MI-1040CR (or "
            f"MI-1040CR-2) and type mi_property_tax_credit for MI-1040 line 26"
        )
    if v["mi_home_heating_credit"]:
        notes.append(
            f"the engine estimates a {v['mi_home_heating_credit']:,.2f} Michigan "
            f"home heating credit: it is claimed on MI-1040CR-7, filed apart, "
            f"not on the MI-1040"
        )
    notes.append(
        "the MI-1040 draft is for a full-year MI resident: part-year and "
        "nonresident returns (Schedule NR), Schedule 1 lines 24A-24H (the year "
        "of birth boxes: fill them in) and 30, MI-1040 lines 18-20, 22-24, 27, "
        "29, 30, 33 and 38 and the MI-2210 penalty are not drafted"
    )
    notes.append(
        "next year's MI safe harbor (MI-2210) is the lesser of 90% of next "
        "year's tax and 100% of this year's MI-1040 line 21 less lines 26, 27, "
        "28b, 29 and 30 (110% when this year's AGI is over 150,000, 75,000 "
        "married filing separately); no penalty when what is left after "
        "withholding and credits is 500 or less"
    )
