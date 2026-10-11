"""Form 1116, Foreign Tax Credit (Individual), for the foreign tax a fund or
bank withheld on your Forms 1099-DIV (box 7) and 1099-INT (box 6) (unit 3e-7).

Line numbers and rules follow the 2025 Form 1116, its instructions and Pub.
514:

- Election: with all foreign income passive, all of it on payee statements
  (Forms 1099-DIV and 1099-INT) and creditable foreign tax of $300 or less
  ($600 married filing jointly), you can take the credit without Form 1116:
  Schedule 3 line 1 is the smaller of the tax and Form 1116 line 20's tax. No
  carryover reaches or leaves that year, so with a carryover typed the
  planner files Form 1116 instead.
- Otherwise one Form 1116, passive category income, in a single column: a
  regulated investment company's (mutual fund's) foreign income is totaled,
  not reported country by country.
- Part I: the foreign source gross income you type (from each fund's foreign
  source statement) is line 1a and line 3d. Line 3a is the standard deduction,
  or for an itemizer the real estate taxes' share of the capped state and
  local tax deduction (medical expenses and general sales tax are not
  modeled); line 3b is Schedule 1 line 26. Line 3e, gross income from all
  sources, is Form 1040 line 9: the planner does not hold gains before losses
  or receipts before costs, and the smaller figure apportions more of the
  deductions to foreign income, which can only lower the credit. Lines 4a and
  4b are 0 when foreign gross income is $5,000 or less (all interest expense
  can go to U.S. source); over that, line 4a is deductible home mortgage
  interest times 3d / 3e (Worksheet for Home Mortgage Interest).
- Part III: line 18 is taxable income plus Schedule 1-A line 37. When the
  Qualified Dividends and Capital Gain Tax Worksheet (line 5 more than zero,
  line 23 less than 24) or the Schedule D Tax Worksheet (line 18 more than
  zero, line 45 less than 46) taxes dividends and gains at lower rates,
  foreign qualified dividends on line 1a are multiplied by 0.4054 (taxed at
  15%), 0.5405 (20%) or left out (0%), and line 18 comes from the Worksheet
  for Line 18. Under the adjustment exception (line 5 or 18 not more than the
  top of the 24% bracket, $394,600 joint and $197,300 otherwise for 2025, and
  foreign qualified dividends under $20,000) you can elect not to adjust; the
  planner takes whichever credit is larger.
- Line 20 is Form 1040 line 16 plus Schedule 2 line 1z (not the net
  investment income tax). Line 24, the smaller of the taxes (line 14) and the
  limit (line 23), is the passive category's credit; line 35 reaches Schedule
  3 line 1. Tax over the limit carries back 1 year and forward 10 (Schedule B
  (Form 1116)).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from planner.engine import tax

FORM = "1116"
W18 = "1116 Wks 18"
BOXES = (("1099-DIV", "7"), ("1099-INT", "6"))
KEYS = ("foreign_source_income", "foreign_qualified_dividends", "foreign_tax_carryover")
ELECTION = 300.0  # $600 married filing jointly
INTEREST_FLOOR = 5_000.0  # lines 4a, 4b
EXCEPTION_DIVIDENDS = 20_000.0
QD_15, QD_20 = 0.4054, 0.5405  # line 1a adjustment
WKS_28, WKS_25, WKS_20, WKS_15 = 0.2432, 0.3243, 0.4595, 0.5946
NOT_DRAFTED = (
    "Form 1116 is drafted for passive category income on Forms 1099-DIV and "
    "1099-INT only: other categories, foreign tax on Schedules K-1 and K-3, "
    "foreign capital gains and losses (Worksheets A and B, line 5), expenses "
    "definitely related to the income (line 2), the line 12 reduction (taxes "
    "on dividends held 16 days or less, among others), lines 13, 16, 22 and "
    "34, and Schedules B and C (Form 1116) are not"
)

Add = Callable[..., float]
Line = tuple[str, str, str, float, str, int]


@dataclass(frozen=True)
class Bands:
    """The worldwide rate groups the Worksheet for Line 18 reads: amounts taxed
    at 0%, 15%, 20%, 25% and 28%, the ordinary part (Qualified Dividends and
    Capital Gain Tax Worksheet line 5, Schedule D Tax Worksheet line 18), the
    worksheet's name and whether it taxes less than the rate schedule."""

    zero: float
    fifteen: float
    twenty: float
    twenty_five: float
    twenty_eight: float
    ordinary: float
    sheet: str
    lower: bool


@dataclass
class Facts:
    paid: float  # 1099-DIV box 7 + 1099-INT box 6
    income: float | None  # foreign source gross income, passive (typed)
    qualified: float | None  # the qualified dividends in it (typed)
    carryover: float | None  # unused foreign tax from earlier years (typed)
    joint: bool
    line20: float  # 1040 line 16 + Schedule 2 line 1z
    taxable: float  # 1040 line 15
    senior: float  # Schedule 1-A line 37
    deduction: float  # line 3a
    deduction_src: str
    adjustments: float  # line 3b: Schedule 1 line 26
    gross: float  # line 3e: 1040 line 9
    mortgage: float  # Schedule A line 8e, 0 for a non-itemizer
    other_interest: float  # line 4b interest (Schedule 1-A line 30)
    top24: float  # top of the 24% bracket, the adjustment exception
    bands: Bands | None  # None: no dividends or gains at lower rates


@dataclass
class Result:
    credit: float  # Schedule 3 line 1
    form: bool  # Form 1116 filed (False: the election)
    lines: list[Line] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def bands(
    year: int, status: str, taxable: float, v: Mapping[str, float]
) -> Bands | None:
    """The rate groups from the engine's figures: the Schedule D Tax Worksheet
    with 28% rate or unrecaptured section 1250 gain, else the Qualified
    Dividends and Capital Gain Tax Worksheet (2025 Form 1040 instructions)."""
    l1 = max(taxable, 0.0)
    zero_top = tax._param(f"gov.irs.capital_gains.thresholds.1.{status}", year)
    fifteen_top = tax._param(f"gov.irs.capital_gains.thresholds.2.{status}", year)
    top24 = tax._param(f"gov.irs.income.bracket.thresholds.4.{status}", year)
    s28, s19 = v[tax.SDTW_VARS[0]], v[tax.SDTW_VARS[1]]
    if s28 + s19 > 0:
        b = {
            k: float(x)
            for k, x in tax.sdtw_bands(
                l1,
                s19,
                s19 + s28,
                v["dwks09"],
                v["dwks10"],
                zero_top,
                fifteen_top,
                top24,
            ).items()
        }
        l45 = (
            0.15 * b["30"]
            + 0.20 * b["33"]
            + 0.25 * b["39"]
            + 0.28 * b["42"]
            + tax.table_tax(b["21"], year, status)
        )
        return Bands(
            b["22"],
            b["30"],
            b["33"],
            b["39"],
            b["42"],
            max(l1 - v["dwks10"], 0.0),
            "Schedule D Tax Worksheet",
            l45 < tax.table_tax(l1, year, status),
        )
    l4 = v["adjusted_net_capital_gain"]
    if l4 <= 0:
        return None
    l5 = max(l1 - l4, 0.0)
    l7 = min(l1, zero_top)
    l9 = l7 - min(l5, l7)
    l10 = min(l1, l4)
    l17 = min(l10 - l9, max(min(l1, fifteen_top) - (l5 + l9), 0.0))
    l20 = l10 - (l9 + l17)
    l23 = tax.table_tax(l5, year, status) + 0.15 * l17 + 0.20 * l20
    return Bands(
        l9,
        l17,
        l20,
        0.0,
        0.0,
        l5,
        "Qualified Dividends and Capital Gain Tax Worksheet",
        l23 < tax.table_tax(l1, year, status),
    )


def _limit(f: Facts, adjust: bool) -> tuple[float, list[Line], list[str]]:
    """Form 1116 lines 1a-35 with or without the qualified dividend
    adjustment; the credit, the lines and the notes."""
    out: list[Line] = []
    notes: list[str] = []

    def add(
        form: str, line: str, label: str, value: float, src: str, digits: int = 2
    ) -> float:
        value = round(value, digits)
        out.append((form, line, label, value, src, digits))
        return value

    gross_foreign = max(f.income or 0.0, 0.0)
    qd = min(max(f.qualified or 0.0, 0.0), gross_foreign)
    if (f.qualified or 0.0) > gross_foreign:
        notes.append(
            f"CHECK: foreign_qualified_dividends {f.qualified:,.2f} is more than "
            f"foreign_source_income {gross_foreign:,.2f}; Form 1116 counts "
            f"{qd:,.2f}"
        )
    b = f.bands
    if adjust and b is not None and qd:
        pref = b.zero + b.fifteen + b.twenty
        share = (lambda x: x / pref) if pref > 0 else (lambda x: 0.0)
        adjusted = (
            gross_foreign
            - qd
            + qd * (share(b.fifteen) * QD_15 + share(b.twenty) * QD_20)
        )
        src = (
            f"foreign_source_income less {qd:,.2f} foreign qualified dividends, "
            f"plus them x {QD_15} at 15% and x {QD_20} at 20%, none at 0%"
        )
        if sum(1 for x in (b.zero, b.fifteen, b.twenty) if x > 0) > 1:
            notes.append(
                "CHECK: Form 1116 line 1a splits the foreign qualified dividends "
                "across the 0%, 15% and 20% rates in proportion to the "
                f"{b.sheet}'s amounts at each rate; the instructions set no order, "
                "so confirm the split"
            )
    else:
        adjusted, src = gross_foreign, "foreign_source_income (passive, RIC column)"
    l1a = add(
        FORM, "1a", "Gross income from foreign sources (passive, RIC)", adjusted, src
    )
    l2 = add(FORM, "2", "Expenses definitely related", 0.0, "none drafted")
    l3a = add(
        FORM, "3a", "Deductions not definitely related", f.deduction, f.deduction_src
    )
    l3b = add(FORM, "3b", "Other deductions", f.adjustments, "Sch 1 line 26")
    l3c = add(FORM, "3c", "Lines 3a and 3b", l3a + l3b, "3a + 3b")
    l3d = add(
        FORM, "3d", "Gross foreign source income", gross_foreign, "line 1a unadjusted"
    )
    l3e = add(
        FORM,
        "3e",
        "Gross income from all sources",
        f.gross,
        "1040 line 9 (smaller than gross income, so the credit is not overstated)",
    )
    ratio = 1.0 if l3e <= 0 else min(round(l3d / l3e, 4), 1.0)
    l3f = add(FORM, "3f", "Line 3d divided by 3e", ratio, "3d / 3e, not more than 1", 4)
    l3g = add(FORM, "3g", "Line 3c times 3f", l3c * l3f, "3c x 3f")
    if l3d <= INTEREST_FLOOR:
        l4a = add(
            FORM,
            "4a",
            "Home mortgage interest",
            0.0,
            "foreign gross income <= 5,000: all to U.S. source",
        )
        l4b = add(
            FORM,
            "4b",
            "Other interest expense",
            0.0,
            "foreign gross income <= 5,000: all to U.S. source",
        )
    else:
        l4a = add(
            FORM,
            "4a",
            "Home mortgage interest",
            f.mortgage * l3f,
            f"Sch A line 8e {f.mortgage:,.2f} x 3d / 3e "
            "(Worksheet for Home Mortgage Interest)",
        )
        l4b = add(FORM, "4b", "Other interest expense", 0.0, "asset method not drafted")
        if f.other_interest:
            notes.append(
                "CHECK: Form 1116 line 4b takes a share of the "
                f"{f.other_interest:,.2f} "
                "vehicle loan and other interest by the asset method (Pub. 514): "
                "not drafted, so line 4b is 0"
            )
    l5 = add(FORM, "5", "Losses from foreign sources", 0.0, "not drafted")
    l6 = add(
        FORM,
        "6",
        "Total deductions and losses",
        l2 + l3g + l4a + l4b + l5,
        "2 + 3g + 4a + 4b + 5",
    )
    l7 = add(FORM, "7", "Foreign source taxable income", l1a - l6, "1a - 6")
    l8 = add(
        FORM, "8", "Total foreign taxes paid", f.paid, "1099-DIV box 7 + 1099-INT box 6"
    )
    l9 = add(FORM, "9", "Foreign taxes paid", l8, "line 8")
    carry = max(f.carryover or 0.0, 0.0)
    l10 = add(
        FORM,
        "10",
        "Carryback or carryover",
        carry,
        "foreign_tax_carryover (Schedule B (Form 1116) line 3)",
    )
    l11 = add(FORM, "11", "Lines 9 and 10", l9 + l10, "9 + 10")
    l14 = add(FORM, "14", "Taxes available for credit", l11, "11 (12, 13 not drafted)")
    l15 = add(FORM, "15", "Foreign source taxable income", l7, "line 7")
    l17 = add(FORM, "17", "Lines 15 and 16", l15, "15 (16 not drafted)")
    taxable = f.taxable + f.senior
    if adjust and b is not None and b.lower:
        w1 = add(
            W18,
            "1",
            "Taxable income plus Sch 1-A line 37",
            taxable,
            "1040 11b - 14 + Sch 1-A 37",
        )
        w3 = w5 = 0.0
        if b.sheet.startswith("Schedule D"):
            w2 = add(
                W18, "2", "Worldwide 28% gains", b.twenty_eight, f"{b.sheet} line 42"
            )
            w3 = add(W18, "3", "Line 2 times 0.2432", w2 * WKS_28, "2 x 0.2432")
            w4 = add(
                W18, "4", "Worldwide 25% gains", b.twenty_five, f"{b.sheet} line 39"
            )
            w5 = add(W18, "5", "Line 4 times 0.3243", w4 * WKS_25, "4 x 0.3243")
            lines = ("33", "30", "22")
        else:
            lines = ("20", "17", "9")
        w6 = add(
            W18,
            "6",
            "Worldwide 20% gains and qualified dividends",
            b.twenty,
            f"{b.sheet} line {lines[0]}",
        )
        w7 = add(W18, "7", "Line 6 times 0.4595", w6 * WKS_20, "6 x 0.4595")
        w8 = add(
            W18,
            "8",
            "Worldwide 15% gains and qualified dividends",
            b.fifteen,
            f"{b.sheet} line {lines[1]}",
        )
        w9 = add(W18, "9", "Line 8 times 0.5946", w8 * WKS_15, "8 x 0.5946")
        w10 = add(
            W18,
            "10",
            "Worldwide 0% gains and qualified dividends",
            b.zero,
            f"{b.sheet} line {lines[2]}",
        )
        w11 = add(
            W18,
            "11",
            "Lines 3, 5, 7, 9 and 10",
            w3 + w5 + w7 + w9 + w10,
            "3 + 5 + 7 + 9 + 10",
        )
        l18 = add(
            FORM,
            "18",
            "Individuals: taxable income, adjusted",
            max(w1 - w11, 0.0),
            "Worksheet for Line 18, line 12",
        )
    else:
        l18 = add(
            FORM,
            "18",
            "Individuals: taxable income",
            max(taxable, 0.0),
            "1040 11b - 14 + Sch 1-A line 37",
        )
    if l17 <= 0 or l18 <= 0:
        ratio = 0.0
    else:
        ratio = min(l17 / l18, 1.0)
    l19 = add(FORM, "19", "Line 17 divided by 18", ratio, "17 / 18, not more than 1", 4)
    l20 = add(
        FORM,
        "20",
        "U.S. income tax",
        max(f.line20, 0.0),
        "1040 line 16 + Sch 2 line 1z",
    )
    l21 = add(FORM, "21", "Line 20 times 19 (the limit)", l20 * l19, "20 x 19")
    l23 = add(FORM, "23", "Lines 21 and 22", l21, "21 (22 not drafted)")
    l24 = add(FORM, "24", "Smaller of 14 or 23", min(l14, l23), "smaller of 14, 23")
    l27 = add(FORM, "27", "Credit for taxes on passive category income", l24, "line 24")
    l32 = add(FORM, "32", "Lines 25 through 31", l27, "27")
    l33 = add(FORM, "33", "Smaller of 20 or 32", min(l20, l32), "smaller of 20, 32")
    l35 = add(FORM, "35", "Foreign tax credit", l33, "33 (34 not drafted)")
    if l14 > l24:
        notes.append(
            f"Form 1116: {l14 - l24:,.2f} of foreign tax is over the limit; it carries "
            "back 1 year and forward 10 (Schedule B (Form 1116)): type the carryover "
            "into next year's foreign_tax_carryover"
        )
    return l35, out, notes


def compute(f: Facts) -> Result:
    """Schedule 3 line 1 by the election or by Form 1116."""
    limit = ELECTION * (2 if f.joint else 1)
    if f.paid <= 0 and not f.carryover:
        return Result(0.0, False)
    if f.paid <= limit and not f.carryover:
        credit = round(min(f.paid, max(f.line20, 0.0)), 2)
        return Result(
            credit,
            False,
            notes=[
                f"foreign tax credit without Form 1116: {f.paid:,.2f} of foreign tax, "
                f"all passive on Forms 1099-DIV and 1099-INT, is not more than "
                f"{limit:,.0f}; Schedule 3 line 1 is the smaller of it and Form 1040 "
                "line 16 plus Schedule 2 line 1z, and no carryover reaches or leaves "
                "this year"
            ],
        )
    plain = _limit(f, adjust=False)
    b = f.bands
    if b is None or not b.lower or b.ordinary <= 0:
        credit, lines, notes = plain
    else:
        adjusted = _limit(f, adjust=True)
        exception = b.ordinary <= f.top24 and (f.qualified or 0.0) < EXCEPTION_DIVIDENDS
        if exception and plain[0] >= adjusted[0]:
            credit, lines, notes = plain
            notes = [
                *notes,
                f"Form 1116: the adjustment exception applies ({b.sheet} ordinary "
                f"income {b.ordinary:,.2f} <= {f.top24:,.0f}, foreign qualified "
                f"dividends under {EXCEPTION_DIVIDENDS:,.0f}); not adjusting gives "
                f"{plain[0]:,.2f} against {adjusted[0]:,.2f}",
            ]
        else:
            credit, lines, notes = adjusted
    out = Result(credit, True, lines, list(notes))
    if f.income is None:
        out.notes.append(
            "CHECK: foreign_source_income not given: Form 1116 line 1a is 0, so the "
            "credit is 0; type it from each fund's foreign source income statement"
        )
    out.notes.append(NOT_DRAFTED)
    return out


def lay(add: Add, notes: list[str], r: Result) -> float:
    """Lay a result on the draft; returns Schedule 3 line 1."""
    for form, line, label, value, src, digits in r.lines:
        add(form, line, label, value, src, digits)
    notes.extend(r.notes)
    return r.credit
