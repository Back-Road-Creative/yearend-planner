"""The draft return: Form 1040 with Schedules 1, 2, 3, D and SE and Forms
8949, 8889 and 8962, and for a North Carolina resident the D-400 with its
Schedule S, every line priced by the engine from the household the Needed panel
confirmed, and every line naming where its figure came from.

The engine prices the year; this module lays its figures onto the form lines
(line numbers follow the 2025 forms) and adds what the engine does not see:
withholding from the W-2 and 1099 boxes, the estimated payments recorded for
the year, and the premium tax credit reconciled month by month from the 1095-A
(Form 8962). A line the forms do not settle is left out of the household,
never zeroed, and the draft names it. It is a draft to check against the
forms, not a filing.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from datetime import date

from planner import NOTICE, coverage
from planner.engine import tax
from planner.engine.household import Dependent, Household, Person
from planner.ingest.needs import need_values, schedule_b_required
from planner.ledger import db
from planner.paths import Layout
from planner.plan import esttax, inputs
from planner.taxprep import capgains, d400, hsa, schedule_c
from planner.taxprep.expected import inventory

ENGINE = (
    "employment_income",
    "taxable_interest_income",
    "qualified_dividend_income",
    "dividend_income",
    "taxable_ira_distributions",
    "taxable_roth_conversions",
    "social_security",
    "taxable_social_security",
    "loss_limited_net_capital_gains",
    "self_employment_income",
    "irs_gross_income",
    "above_the_line_deductions",
    "loss_ald",
    "adjusted_gross_income",
    "standard_deduction",
    "tax_unit_itemizes",
    "itemized_taxable_income_deductions",
    "qualified_business_income_deduction",
    "taxable_income_deductions",
    "taxable_income",
    "income_tax_before_credits",
    "adjusted_net_capital_gain",
    "alternative_minimum_tax",
    "non_refundable_ctc",
    "income_tax_non_refundable_credits",
    "cdcc",
    "cdcc_rate",
    "cdcc_limit",
    "cdcc_relevant_expenses",
    "count_cdcc_eligible",
    "refundable_american_opportunity_credit",
    "non_refundable_american_opportunity_credit",
    "lifetime_learning_credit",
    "savers_credit",
    "savers_credit_potential",
    "head_earned",
    "spouse_earned",
    "self_employment_tax",
    "additional_medicare_tax",
    "net_investment_income_tax",
    "eitc",
    "eitc_child_count",
    "refundable_ctc",
    "employee_social_security_tax",
    "employee_medicare_tax",
    "ctc",
    "ctc_qualifying_children",
    "income_tax_refundable_credits",
    "income_tax",
    "health_savings_account_ald",
    "self_employment_tax_ald",
    "self_employed_pension_contribution_ald",
    "self_employed_health_insurance_ald",
    "taxable_self_employment_income",
    "aca_magi",
    "aca_magi_fraction",
    "tax_unit_size",
    "aca_required_contribution_percentage",
    "is_aca_ptc_eligible",
    "aca_ptc",
    "tip_income_deduction",
    "overtime_income_deduction",
    "auto_loan_interest_deduction",
    "additional_senior_deduction",
)
PRIOR = ("tax_unit_fpg",)
WITHHELD = (
    ("W-2", "2"),
    ("1099-R", "4"),
    ("1099-INT", "4"),
    ("1099-DIV", "4"),
    ("1099-NEC", "4"),
    ("1099-K", "4"),
    ("1099-B", "4"),
    ("SSA-1099", "6"),
)
# Form 8962 line 28's repayment cap lives with the engine's credit settlement.
repayment_cap = tax.repayment_cap
UNCAPPED_FROM = tax.UNCAPPED_FROM
MONTHS = tuple(f"{m:02d}" for m in range(1, 13))
MONTH_LINE = {m: str(11 + n) for n, m in enumerate(MONTHS, 1)}  # Jan = line 12
TOLERANCE = 1.0
# Form 8962 rounds the monthly contribution (line 8b) to whole dollars: twelve
# months can sit up to $6 from the annual figure the engine prices.
ROUNDING = 12.0
SEHI = "self_employed_health_insurance_ald"
# Schedule 1-A (Form 1040) 2025, amounts as printed on the form. P.L. 119-21
# allows the four deductions for tax years 2025 through 2028 (the engine's
# deductions list ends with 2028).
SCH_1A_YEARS = range(2025, 2029)
SCH_1A = "Sch 1-A"
TIPS_CAP = 25_000.0  # line 7
OVERTIME_CAP = (12_500.0, 25_000.0)  # line 15: single or other, joint
CAR_LOAN_CAP = 10_000.0  # line 24
SENIOR_AMOUNT = 6_000.0  # line 35
SENIOR_RATE = 0.06  # line 34
SENIOR_AGE = 65  # born before January 2 of year - 64, so 65 on December 31
# MAGI where each phase-out starts (lines 9, 17, 26, 32): single or other, joint.
TIPS_FROM = (150_000.0, 300_000.0)
OVERTIME_FROM = (150_000.0, 300_000.0)
CAR_LOAN_FROM = (100_000.0, 200_000.0)
SENIOR_FROM = (75_000.0, 150_000.0)
# Lines 11-12 and 19-20 cut $100 for each whole $1,000 of MAGI over the start;
# lines 28-29 cut $200 for each $1,000 or part of one. The engine cuts 10% of
# the exact excess for tips and overtime, so the form can leave up to $100 more.
CUT_TIPS = CUT_OVERTIME = 100.0
CUT_CAR_LOAN = 200.0
ROUNDING_GAP = 100.0
# The engine variable behind each Schedule 1-A deduction line.
ENGINE_LINE = {
    "13": "tip_income_deduction",
    "21": "overtime_income_deduction",
    "30": "auto_loan_interest_deduction",
    "37": "additional_senior_deduction",
}
SS_WAGES = (("W-2", "3"), ("W-2", "7"))  # Social Security wages and tips
# Social Security and Medicare tax withheld (Schedule 8812 line 21)
PAYROLL = (("W-2", "4"), ("W-2", "6"))
SCH_SE_SPOUSE = "Sch SE (spouse)"
# Read for each spouse alone: each has their own Schedule SE (unit 3a-5)
OWN = ("self_employment_income", "employment_income", "self_employment_tax")
SCH_8812 = "Sch 8812"
# Schedule 8812 per year: (a child under 17, its refundable most). P.L. 119-21
# sec. 70104 sets $2,200 from 2025; Rev. Proc. 2024-40 (2025) and Rev. Proc.
# 2025-32 (2026) hold the refundable part at $1,700.
CTC_AMOUNTS = {2025: (2_200.0, 1_700.0), 2026: (2_200.0, 1_700.0)}
CTC_CHILD_UNDER = 17  # line 4: under 17 at the end of the year
ODC_AMOUNT = 500.0  # line 7, IRC 24(h)(4)
CTC_FROM = (200_000.0, 400_000.0)  # line 9: other, married filing jointly
CTC_CUT_PER = 1_000.0  # line 10 rounds the excess up to whole $1,000s
CTC_CUT_RATE = 0.05  # line 11
SCH_EIC = "Sch EIC"
EIC_CHILD_UNDER = 19  # Step 3: under 19 at the end of the year,
EIC_STUDENT_UNDER = 24  # or under 24 and a full-time student
# The EIC Table prices a $50 row at its midpoint and the engine the exact
# amount: at the steepest rate (45%, three children) that is $22.50 apart.
EIC_SLACK = 25.0
# The Needed-panel keys behind Schedule 1-A Parts II to IV.
PART_KEYS = ("qualified_tips", "qualified_overtime", "car_loan_interest")
TAX_KEYS = inputs.TAX_KEYS  # the Needed-panel keys a return reads
NOT_READY = (
    "NOT READY for a preparer: it rests on unknown figures (left out, not zero): "
)


@dataclass(frozen=True)
class Line:
    form: str  # a key of HEADINGS
    line: str
    label: str
    value: float
    source: str


@dataclass
class Draft:
    year: int
    engine_version: str
    lines: list[Line] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    estimates: list[str] = field(default_factory=list)  # keys standing in from YTD
    unknown: list[str] = field(default_factory=list)  # keys left out (not zero)
    missing: list[str] = field(default_factory=list)  # forms still to come
    coverage: list[coverage.Gap] = field(default_factory=list)  # the gate's gaps
    statuses: dict[str, str] = field(default_factory=dict)  # capability rows

    @property
    def not_ready(self) -> str | None:
        """The line that heads every copy of a draft resting on an unknown or
        on a gap that touches the return (unit 2a)."""
        parts = [g.reason for g in self.coverage if "draft" in g.touches]
        if self.unknown:
            parts.insert(0, NOT_READY + ", ".join(self.unknown))
        elif parts:
            parts[0] = "NOT READY for a preparer: " + parts[0]
        return "; ".join(parts) or None

    def tag(self, form: str) -> str:
        """verified, estimated or not handled for one form (unit 2a)."""
        row = FORM_CAPABILITY.get(form, "draft_return")
        return coverage.tag(self.coverage, "draft", self.statuses.get(row))

    def get(self, form: str, line: str) -> float | None:
        for ln in self.lines:
            if (ln.form, ln.line) == (form, line):
                return ln.value
        return None


def _sum(facts: list[db.FactRow], pairs: tuple[tuple[str, str], ...]) -> float:
    return round(sum(f.value for f in facts if (f.form, f.box) in pairs), 2)


def _cited(facts: list[db.FactRow], pairs: tuple[tuple[str, str], ...]) -> str:
    hits = sorted(
        {
            f"{f.form} box {f.box} ({f.issuer}; {f.file_name} p.{f.page})"
            for f in facts
            if (f.form, f.box) in pairs
        }
    )
    return ", ".join(hits) if hits else "no form shows any"


class _Sheet:
    def __init__(self, out: Draft) -> None:
        self.out = out

    def add(
        self,
        form: str,
        line: str,
        label: str,
        value: float,
        source: str,
        digits: int = 2,
    ) -> float:
        value = round(value, digits)
        self.out.lines.append(Line(form, line, label, value, source))
        return value


@dataclass(frozen=True)
class Marriage:
    """A joint return's marriage in the year, for Form 8962's alternative
    calculation (Pub. 974, Alternative Calculation for Year of Marriage)."""

    day: date
    spouse_kids: int  # dependents in the spouse's alternative family size
    kids: int  # every dependent on the return
    state: str  # the poverty table: Alaska and Hawaii have their own


@dataclass
class _Alternative:
    """Pub. 974 Worksheets I-V: per pre-marriage month the column (c) and (e)
    the election puts on Form 8962, and each spouse's Part V line."""

    months: dict[str, tuple[float, float]]  # month -> (column c, column e)
    part_v: list[tuple[str, int, float, int, int]]  # line, size, 7, start, stop


def _alternative(
    year: int,
    rows: dict[str, list[float]],
    own: dict[str, dict[str, float]],
    married: Marriage,
    income: float,
) -> _Alternative | None:
    """Worksheets I-IV for each spouse's family before the marriage (Pub. 974):
    half the household income over the poverty line for that family's size,
    its own applicable figure and monthly contribution, and its own 1095-A from
    the first month of coverage to the month of marriage. None when neither
    spouse had coverage then."""
    half = math.floor(income / 2 + 0.5)  # Worksheet I line 2
    wed = married.day.month
    out = _Alternative({}, [])
    sides = (
        ("35", "you", 1 + married.kids - married.spouse_kids),
        ("36", "spouse", 1 + married.spouse_kids),
    )
    for line, owner, size in sides:
        box = own.get(owner, {})
        covered = [int(m) for m in MONTHS if box.get(f"premium_{m}")]
        if not covered or covered[0] > wed:
            continue
        start, stop = covered[0], min(covered[-1], wed)
        fpl = tax.poverty_line(year, size, married.state)  # line 3
        pct = 401 if half > 4 * fpl else math.floor(100 * half / fpl)  # Step 1
        figure = tax.applicable_figure(year, pct)  # line 5
        contribution = None if figure is None else round(round(half * figure) / 12)
        for n in range(start, stop + 1):  # Worksheet II or IV
            m = f"{n:02d}"
            a, b = box.get(f"premium_{m}", 0.0), box.get(f"slcsp_{m}", 0.0)
            c = b if contribution is None else float(contribution)
            c0, e0 = out.months.get(m, (0.0, 0.0))
            out.months[m] = (c0 + c, e0 + min(a, max(b - c, 0.0)))
        out.part_v.append((line, size, float(contribution or 0), start, stop))
    return out if out.part_v else None


def _form_8962(
    sheet: _Sheet,
    d: Draft,
    v: dict[str, float],
    facts: list[db.FactRow],
    fs: str,
    married: Marriage | None = None,
) -> tuple[float, float]:
    """Lay out Form 8962 from the 1095-A boxes summed across policies; returns
    (net credit for Schedule 3 line 9, excess repayment for Schedule 2 line 1a).
    A joint return married during the year elects the alternative calculation
    for the year of marriage when it lowers the repayment (Pub. 974)."""
    box: dict[str, float] = {}
    own: dict[str, dict[str, float]] = {}  # each spouse's own 1095-As
    for f in facts:
        if f.form == "1095-A":
            box[f.box] = box.get(f.box, 0.0) + f.value
            mine = own.setdefault(f.owner, {})
            mine[f.box] = mine.get(f.box, 0.0) + f.value
    src = "1095-A " + ", ".join(sorted({f.issuer for f in facts if f.form == "1095-A"}))
    fpl = v["tax_unit_fpg@prior"]
    over = tax.over_ptc_line(d.year, v["aca_magi"], fpl)
    # i8962 Worksheet 2: income more than 4 x the guideline is 401, not truncated
    pct = 401 if over else tax.poverty_percent(v["aca_magi_fraction"])
    figure = round(v["aca_required_contribution_percentage"], 4)
    sheet.add(
        "8962", "1", "Tax family size", v["tax_unit_size"], "engine tax_unit_size"
    )
    magi = sheet.add("8962", "2a", "Modified AGI", v["aca_magi"], "engine aca_magi")
    sheet.add("8962", "3", "Household income", magi, "line 2a (no dependents)")
    sheet.add(
        "8962", "4", "Federal poverty line", fpl, "engine tax_unit_fpg, prior year"
    )
    sheet.add("8962", "5", "Household income as % of poverty line", pct, "3 / 4")
    sheet.add("8962", "7", "Applicable figure", figure, "IRC 36B(b)(3)(A) table", 4)
    annual = round(magi * figure)
    sheet.add("8962", "8a", "Annual contribution amount", annual, "3 x 7")
    monthly = round(annual / 12)
    sheet.add("8962", "8b", "Monthly contribution amount", monthly, "8a / 12")
    eligible = v["is_aca_ptc_eligible"] > 0 and not over
    if over:
        d.notes.append(
            "Form 8962: household income is over 400% of the poverty line (more "
            "than 4 x line 4), so line 5 is 401 and no credit is allowed; every "
            "month's credit is 0 and the advance is repaid"
        )
    elif not eligible:
        d.notes.append(
            "Form 8962: the engine finds no premium tax credit eligibility this year "
            "(income under the poverty line or in the Medicaid band, or Medicare "
            "age); every month's credit is 0 and the advance is repaid"
        )
    rows: dict[str, list[float]] = {}  # month -> columns a, b, c, d, e, f
    for m in MONTHS:
        a, b, f_ = (box.get(f"{k}_{m}", 0.0) for k in ("premium", "slcsp", "aptc"))
        if not (a or b or f_):
            continue
        most = max(b - monthly, 0.0)
        rows[m] = [a, b, float(monthly), most, min(a, most) if eligible else 0.0, f_]
    total_f = sum(r[5] for r in rows.values())
    regular = sum(r[4] for r in rows.values())
    cap = repayment_cap(d.year, fs, pct)

    def repaid(e24: float) -> float:
        excess = max(total_f - e24, 0.0)
        return excess if cap is None else min(excess, cap)

    alt = None
    if (
        married is not None
        and eligible
        and fs == "JOINT"
        and married.day.year == d.year
        and married.day > date(d.year, 1, 1)  # Table 4: unmarried on January 1
        and regular < total_f  # Worksheet 3 line 14: excess advance paid
        # Table 4 question 4: coverage before the first full month of marriage
        and min(int(m) for m in rows) < married.day.month + (married.day.day > 1)
    ):
        alt = _alternative(d.year, rows, own, married, magi)
        wed = married.day.isoformat()
        if alt is not None and sum(e for _, e in alt.months.values()) <= sum(
            rows[m][4] for m in alt.months if m in rows
        ):  # Worksheet V line 14
            alt = None
            d.notes.append(
                f"Form 8962: married {wed}; the alternative calculation for the "
                "year of marriage (Pub. 974 Worksheet V) does not lower the "
                "excess advance credit, so line 9 is No and Part V is blank"
            )
    if alt is not None:
        for m, (c, e) in alt.months.items():
            if m in rows:
                b = rows[m][1]
                rows[m][2:5] = [c, max(b - c, 0.0), e]
        alt_e = sum(r[4] for r in rows.values())
        d.notes.append(
            f"Form 8962: married {wed}; the alternative calculation for the year "
            "of marriage (Pub. 974, Worksheets I-V) lowers the excess advance "
            f"credit repaid from {repaid(regular):,.0f} to {repaid(alt_e):,.0f}: "
            "check Yes on line 9 and No on line 10; Part V lines 35-36 hold each "
            "spouse's family before the marriage"
        )
    for m, (a, b, c, most, e, f_) in rows.items():
        ln = MONTH_LINE[m]
        sheet.add("8962", f"{ln}a", f"Month {m} enrollment premium", a, src)
        sheet.add("8962", f"{ln}b", f"Month {m} SLCSP premium", b, src)
        if alt is not None and m in alt.months:
            how = "Pub. 974 Worksheets II + IV column C"
            sheet.add("8962", f"{ln}c", f"Month {m} contribution amount", c, how)
            sheet.add("8962", f"{ln}d", f"Month {m} maximum assistance", most, "b - c")
            how = "Pub. 974 Worksheet V column A"
            sheet.add("8962", f"{ln}e", f"Month {m} PTC allowed", e, how)
        else:
            sheet.add("8962", f"{ln}d", f"Month {m} maximum assistance", most, "b - 8b")
            sheet.add("8962", f"{ln}e", f"Month {m} PTC allowed", e, "smaller of a, d")
        sheet.add("8962", f"{ln}f", f"Month {m} advance PTC", f_, src)
    for line, size, contribution, start, stop in alt.part_v if alt else ():
        how = "Pub. 974 Worksheet " + ("I" if line == "35" else "III")
        sheet.add("8962", f"{line}a", "Alternative family size", size, how)
        sheet.add(
            "8962", f"{line}b", "Alternative monthly contribution", contribution, how
        )
        sheet.add("8962", f"{line}c", "Alternative start month", start, how)
        sheet.add("8962", f"{line}d", "Alternative stop month", stop, how)
    e24 = sheet.add(
        "8962", "24", "Total PTC", sum(r[4] for r in rows.values()), "sum of column e"
    )
    f25 = sheet.add("8962", "25", "Advance payment of PTC", total_f, "sum of column f")
    if alt is not None:  # Pub. 974 Step 8: line 26 is zero under the election
        sheet.add("8962", "26", "Net PTC", 0.0, "Pub. 974 Step 8")
        if e24 >= f25:
            return 0.0, 0.0
    elif e24 >= f25:
        return sheet.add("8962", "26", "Net PTC", e24 - f25, "24 - 25"), 0.0
    excess = sheet.add("8962", "27", "Excess advance PTC", f25 - e24, "25 - 24")
    if cap is None:
        why = (
            "no cap at 400% and over"
            if pct >= 400
            else "no cap from 2026 (P.L. 119-21 sec. 71305)"
            if d.year >= UNCAPPED_FROM
            else f"cap for {d.year} not in the planner's table: repaid in full"
        )
        sheet.add("8962", "28", "Repayment limitation", excess, why)
        repay = excess
    else:
        sheet.add("8962", "28", "Repayment limitation", cap, "Rev. Proc. 2024-35")
        repay = min(excess, cap)
    return 0.0, sheet.add(
        "8962", "29", "Excess APTC repayment", repay, "smaller of 27, 28"
    )


def build(lay: Layout, year: int) -> Draft:
    """The draft return for a tax year, from the ledger and the Needed panel."""
    inp = inputs.build(lay, year)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        facts = db.facts_for(conn, year)
        pays = esttax.payments(conn, lay, year)
        typed = need_values(conn, lay, year, (*d400.KEYS, "foreign_accounts"))
        cg = capgains.build(conn, lay, year)
        h = hsa.build(conn, lay, year)
        hs = hsa.build(conn, lay, year, "spouse")  # a joint spouse's own 8889
        sc = schedule_c.build(conn, lay, year)
    finally:
        conn.close()
    paid = [p for p in pays if p.agency == "fed"]
    nc_paid = [(p.date, p.amount, p.origin) for p in pays if p.agency == "nc"]
    # The draft is a tax return: a filer born January 1 is 65 for the year
    # before (Pub. 501), which the standard deduction and Part V both count.
    hh = dataclasses.replace(
        inp.household, age=inp.tax_age if inp.tax_age is not None else inp.household.age
    )
    if hh.spouse is not None and inp.spouse_tax_age is not None:
        hh = dataclasses.replace(
            hh, spouse=dataclasses.replace(hh.spouse, age=inp.spouse_tax_age)
        )

    def hsa_sum(line: str) -> float:  # both spouses' Forms 8889
        return h.lines.get(line, 0.0) + hs.lines.get(line, 0.0)

    hsa_src = " + the spouse's" if hs.lines else ""
    if h.lines or hs.lines:
        other = dict(hh.other)
        if hsa_sum("16"):
            other["miscellaneous_income"] = other.get("miscellaneous_income", 0) + int(
                round(hsa_sum("16"))
            )
        hh = dataclasses.replace(
            hh, hsa_contribution=int(round(hsa_sum("13"))), other=other
        )
    exempt = float(hh.tax_exempt_interest)  # the Needed panel: boxes, then typed
    us = _sum(facts, d400.US_INTEREST)
    if us:  # taxable federally, subtracted on NC Schedule S line 18
        hh = dataclasses.replace(
            hh, other={**hh.other, "us_govt_interest_person": int(round(us))}
        )
    # Schedule SE line 8a is W-2 boxes 3 and 7, which pre-tax 401(k) deferrals
    # put above box 1. The engine takes box 1 as the wage base's earnings unless
    # told: give it the form's figure so its SE tax is the line 12 below.
    # Each spouse's schedule reads their own W-2s (owners: unit 3a-4).
    ss_wages, ss_spouse = (_ss_wages(facts, who) for who in ("you", "spouse"))
    cap = tax.self_employment_parameters(year)["wage_base"]
    if ss_wages is not None:
        hh = dataclasses.replace(
            hh,
            other={
                **hh.other,
                "taxable_earnings_for_social_security": int(round(min(ss_wages, cap))),
            },
        )
    if ss_spouse is not None and hh.spouse is not None:
        sp = dataclasses.replace(hh.spouse, ss_wages=int(round(min(ss_spouse, cap))))
        hh = dataclasses.replace(hh, spouse=sp)
    # Tips and overtime need a joint return if married (Schedule 1-A lines
    # 4-5 and 14-15; P.L. 119-21 secs. 70201-70202). The engine does not apply
    # that rule, so a separate filer's engine run holds neither: its taxable
    # income, tax and credits are then the return's own.
    priced = hh
    if hh.filing_status == "SEPARATE" and year in SCH_1A_YEARS:
        priced = dataclasses.replace(hh, qualified_tips=0, qualified_overtime=0)
    _, care = tax.dependent_care(year, priced)  # Form 2441 Part III
    v = tax.values(year, priced, (*ENGINE, *d400.ENGINE), PRIOR, OWN)
    d = Draft(
        year,
        tax.engine_version(),
        notes=list(inp.notes),
        estimates=list(inp.estimates),
        unknown=inp.tax_unknown,
        coverage=list(inp.coverage),
        statuses=coverage.statuses(lay),
    )
    d.missing = [f"{e.form} from {e.issuer}" for e in inventory(lay, year).outstanding]
    if inp.spouse_death:  # 2025 Form 1040 instructions, Death of a Taxpayer
        died = date.fromisoformat(inp.spouse_death).strftime("%m/%d/%Y")
        d.notes.append(
            f"Form 1040: check the spouse's Deceased box and enter {died}; write "
            '"Filing as surviving spouse" where you sign (a personal representative '
            "other than you signs too)"
        )
    sheet = _Sheet(d)
    add = sheet.add

    def origin(key: str) -> str:
        if key in inp.unknown:
            return f"{key}: not given (left out, not zero)"
        return inp.origins.get(key, f"{key}: none")

    has_8962 = any(f.form == "1095-A" for f in facts)
    wedding = (
        Marriage(
            date.fromisoformat(inp.married),
            inp.spouse_kids,
            len(hh.dependents),
            hh.state,
        )
        if inp.married
        else None
    )
    net_ptc, excess_aptc = (
        _form_8962(sheet, d, v, facts, hh.filing_status, wedding)
        if has_8962
        else (0.0, 0.0)
    )
    if not has_8962 and v["aca_ptc"] > 0:
        d.notes.append(
            f"the engine prices a {v['aca_ptc']:,.0f} premium tax credit; it is "
            "claimed (and any advance reconciled) only on Form 8962 from the 1095-A"
        )
    if hh.se_health_premiums and v["se_health_ptc"] >= 0:
        premiums = float(hh.se_health_premiums)
        if not v["se_health_converged"]:
            d.notes.append(
                "the self-employed health deduction and the premium tax credit have "
                "no settled answer here (income sits at the 400% cliff, where the "
                "credit ends as the deduction falls); Schedule 1 line 17 is the "
                "planner's two-pass figure, not a fixed point of the Pub. 974 "
                "iteration: check it with a preparer"
            )
        elif has_8962 and abs(premiums - v["se_health_ptc"] - v[SEHI]) < TOLERANCE:
            # the deduction is premiums less the credit; the engine priced that
            # credit, Form 8962 reads it month by month from the 1095-A
            e24 = d.get("8962", "24") or 0.0
            if abs(premiums - e24 - v[SEHI]) > ROUNDING:
                d.notes.append(
                    f"CHECK Schedule 1 line 17 ({v[SEHI]:,.0f}) is the premiums "
                    f"({premiums:,.0f}) less the engine's credit "
                    f"{v['se_health_ptc']:,.0f}; "
                    f"Form 8962 line 24 from the 1095-A is {e24:,.0f} (IRS Pub. 974 "
                    "wants the two to agree): the Needed-panel premiums, benchmark "
                    "or advance credit differ from the 1095-A"
                )

    # Schedule C from the categorised bank rows, then Schedule SE from its line
    # 31; Schedule 1 carries both results.
    for line, label, value, src in schedule_c.sheet(sc):
        add("Sch C", line, label, value, src)
    profit, profit_src = v["self_employment_income@you"], origin("se_income")
    line_31 = sc.lines.get("31")
    if line_31 is not None:
        d.notes.extend(sc.notes)
        d.notes.append(schedule_c.NOT_BUILT)
        if abs(line_31 - profit) <= TOLERANCE:
            profit, profit_src = line_31, "Sch C line 31"
        else:
            d.notes.append(
                f"CHECK: Schedule C line 31 {line_31:,.2f} vs the household's "
                f"self-employment income {profit:,.2f} on Schedule 1 line 3 (typed, "
                "a 1099 estimate, or Schedule C facts out of date: run "
                f"planner categorize --year {year})"
            )
    sp_profit, sp_src = v["self_employment_income@spouse"], origin("spouse_se_income")
    owners = [
        (who, form, gain, src, base)
        for who, form, gain, src, base in (
            ("you", "Sch SE", profit, profit_src, ss_wages),
            ("spouse", SCH_SE_SPOUSE, sp_profit, sp_src, ss_spouse),
        )
        if gain
    ]
    ses = [
        (form, *_schedule_se(sheet, d, v, who, form, gain, src, base))
        for who, form, gain, src, base in owners
    ]
    # The EIC's Worksheet B: each Schedule SE's form, profit and line 13.
    eic_se = [(s[0], o[2], s[2]) for s, o in zip(ses, owners, strict=True)]
    se = (sum(s[1] for s in ses), sum(s[2] for s in ses)) if ses else None
    if se and abs(se[1] - v["self_employment_tax_ald"]) > TOLERANCE:
        d.notes.append(
            f"CHECK: Schedule SE line 13 against the engine's deduction: "
            f"{se[1]:,.2f} vs {v['self_employment_tax_ald']:,.2f}"
        )
    se_src = " + ".join(s[0] for s in ses)

    # Schedule 1
    s1_3 = add(
        "Sch 1",
        "3",
        "Business income",
        profit + sp_profit,
        f"{profit_src} + the spouse's {sp_src}" if sp_profit else profit_src,
    )
    s1_9 = 0.0
    if hsa_sum("16"):
        add(
            "Sch 1",
            "8f",
            "Income from Form 8889",
            hsa_sum("16"),
            f"Form 8889 line 16{hsa_src}",
        )
        s1_9 = add("Sch 1", "9", "Total other income", hsa_sum("16"), "line 8f")
    s1_10 = add(
        "Sch 1", "10", "Additional income", s1_3 + s1_9, "3 + 9" if s1_9 else "line 3"
    )
    named = add(
        "Sch 1",
        "13",
        "HSA deduction",
        v["health_savings_account_ald"],
        f"Form 8889 line 13{hsa_src}"
        if h.lines or hs.lines
        else "engine health_savings_account_ald",
    )
    for ln, label, var in (
        ("15", "Deductible part of SE tax", "self_employment_tax_ald"),
        (
            "16",
            "SEP, SIMPLE and qualified plans",
            "self_employed_pension_contribution_ald",
        ),
        ("17", "Self-employed health insurance", SEHI),
    ):
        value, src = v[var], f"engine {var}"
        if var == SEHI and hh.se_health_premiums:
            src = "premiums less the premium tax credit, settled with it (IRS Pub. 974)"
        elif var == "self_employment_tax_ald" and se:
            value, src = se[1], f"{se_src} line 13"
        named += add("Sch 1", ln, label, value, src)
    add(
        "Sch 1",
        "20",
        "IRA deduction",
        max(v["above_the_line_deductions"] - v["loss_ald"] - named, 0.0),
        "engine above_the_line_deductions less loss_ald and lines 13-17",
    )
    # The engine counts only gains in gross income and deducts the allowed
    # capital and business loss as loss_ald; the forms carry those losses on
    # 1040 line 7a and Schedule 1 line 3 instead.
    s1_26 = add(
        "Sch 1",
        "26",
        "Adjustments to income",
        v["above_the_line_deductions"] - v["loss_ald"],
        "engine above_the_line_deductions less loss_ald",
    )

    # Schedule 2
    s2_1a = add(
        "Sch 2", "1a", "Excess advance PTC repayment", excess_aptc, "Form 8962 line 29"
    )
    s2_2 = add(
        "Sch 2",
        "2",
        "Alternative minimum tax",
        v["alternative_minimum_tax"],
        "engine alternative_minimum_tax",
    )
    s2_3 = add("Sch 2", "3", "Lines 1z and 2", s2_1a + s2_2, "1z + 2")
    s2_4 = add(
        "Sch 2",
        "4",
        "Self-employment tax",
        se[0] if se else v["self_employment_tax"],
        f"{se_src} line 12" if se else "engine self_employment_tax",
    )
    s2_11 = add(
        "Sch 2",
        "11",
        "Additional Medicare tax",
        v["additional_medicare_tax"],
        "engine additional_medicare_tax",
    )
    s2_12 = add(
        "Sch 2",
        "12",
        "Net investment income tax",
        v["net_investment_income_tax"],
        "engine net_investment_income_tax",
    )
    s2_18 = 0.0
    if hsa_sum("17b"):
        add(
            "Sch 2",
            "17c",
            "Additional tax on HSA distributions",
            hsa_sum("17b"),
            f"Form 8889 line 17b{hsa_src}",
        )
        s2_18 = add("Sch 2", "18", "Total additional taxes", hsa_sum("17b"), "17c")
    s2_21 = add(
        "Sch 2",
        "21",
        "Total other taxes",
        s2_4 + s2_11 + s2_12 + s2_18,
        "4 + 11 + 12 + 18" if s2_18 else "4 + 11 + 12",
    )

    # Form 1040 income
    f = "1040"
    wage_src = origin("wages")
    if care is not None and care.line26 > 0:
        add(
            f, "1e", "Taxable dependent care benefits", care.line26, "Form 2441 line 26"
        )
        wage_src += "; line 1e"
    l1z = add(f, "1z", "Wages", v["employment_income"], wage_src)
    add(f, "2a", "Tax-exempt interest", exempt, origin("tax_exempt_interest"))
    l2b = add(
        f, "2b", "Taxable interest", v["taxable_interest_income"], origin("interest")
    )
    add(
        f,
        "3a",
        "Qualified dividends",
        v["qualified_dividend_income"],
        origin("qualified_dividends"),
    )
    l3b = add(
        f,
        "3b",
        "Ordinary dividends",
        v["dividend_income"],
        origin("ordinary_dividends"),
    )
    l4b = v["taxable_ira_distributions"] + v["taxable_roth_conversions"]
    gross_r = _sum(facts, (("1099-R", "1"),))
    add(f, "4a", "IRA distributions", gross_r or l4b, _cited(facts, (("1099-R", "1"),)))
    l4b = add(
        f,
        "4b",
        "IRA distributions, taxable",
        l4b,
        f"{origin('ira_distributions')}; {origin('roth_conversion')}",
    )
    add(
        f,
        "6a",
        "Social security benefits",
        v["social_security"],
        origin("social_security"),
    )
    l6b = add(
        f,
        "6b",
        "Social security, taxable",
        v["taxable_social_security"],
        "engine taxable_social_security",
    )
    l7 = add(
        f,
        "7a",
        "Capital gain or (loss)",
        v["loss_limited_net_capital_gains"],
        "Sch D line 16 (21 if a loss)"
        if cg.lines
        else f"{origin('short_term_gains')}; {origin('long_term_gains')}",
    )
    l8 = add(f, "8", "Additional income from Schedule 1", s1_10, "Sch 1 line 10")
    l9 = add(
        f,
        "9",
        "Total income",
        l1z + l2b + l3b + l4b + l6b + l7 + l8,
        "1z + 2b + 3b + 4b + 6b + 7a + 8",
    )
    l10 = add(f, "10", "Adjustments from Schedule 1", s1_26, "Sch 1 line 26")
    l11 = add(f, "11a", "Adjusted gross income", l9 - l10, "9 - 10")
    add(f, "11b", "Adjusted gross income (page 2)", l11, "line 11a")
    itemizes = v["tax_unit_itemizes"] > 0
    l12 = add(
        f,
        "12e",
        "Itemized deductions" if itemizes else "Standard deduction",
        v["itemized_taxable_income_deductions"]
        if itemizes
        else v["standard_deduction"],
        "engine "
        + ("itemized_taxable_income_deductions" if itemizes else "standard_deduction"),
    )
    l13a = add(
        f,
        "13a",
        "Qualified business income deduction",
        v["qualified_business_income_deduction"],
        "engine qualified_business_income_deduction",
    )
    if year in SCH_1A_YEARS:
        total_1a, form_rounded = _schedule_1a(sheet, d, v, hh, l11)
        if form_rounded:
            # The form's rounded phase-out leaves more deduction than the
            # engine's: price again on the form's figure, so line 15 and the
            # tax on it (line 16) and the credits and taxes that follow agree.
            v = tax.values(
                year,
                dataclasses.replace(priced, tax_unit_inputs=form_rounded),
                (*ENGINE, *d400.ENGINE),
                PRIOR,
            )
        l13b = add(
            f,
            "13b",
            "Additional deductions (Schedule 1-A)",
            total_1a,
            "Sch 1-A line 38",
        )
    else:
        l13b = add(
            f,
            "13b",
            "Other deductions",
            max(v["taxable_income_deductions"] - l12 - l13a, 0.0),
            f"engine taxable_income_deductions less 12e and 13a (Schedule 1-A "
            f"covers {SCH_1A_YEARS[0]} through {SCH_1A_YEARS[-1]}, not {year})",
        )
    l14 = add(f, "14", "Total deductions", l12 + l13a + l13b, "12e + 13a + 13b")
    l15 = add(f, "15", "Taxable income", max(l11 - l14, 0.0), "11b - 14")

    # Form 1040 tax and credits
    regular = v["income_tax_before_credits"] - v["alternative_minimum_tax"]
    tax_16, table_gap = tax.line_16(
        regular, l15, v["adjusted_net_capital_gain"], year, hh.filing_status
    )
    l16 = add(
        f,
        "16",
        "Tax",
        tax_16,
        "engine income_tax_before_credits less AMT"
        + (", the ordinary part by the Tax Table" if table_gap else ""),
    )
    if table_gap:
        d.notes.append(
            f"1040 line 16 follows the Tax Table (income under "
            f"${tax.TAX_TABLE_TOP:,.0f} is taxed by its $50 rows): {l16:,.2f}, "
            f"{table_gap:+,.2f} from the rate schedule's exact {regular:,.2f}"
        )
    l17 = add(f, "17", "Amount from Schedule 2, line 3", s2_3, "Sch 2 line 3")
    l18 = add(f, "18", "Lines 16 and 17", l16 + l17, "16 + 17")

    # Form 2441 and Schedule 3
    s3_2 = _form_2441(sheet, d, v, hh, care, l11, l18)
    if s3_2 is not None:
        add("Sch 3", "2", "Child and dependent care credit", s3_2, "Form 2441 line 11")
    education = _form_8863(sheet, d, v, hh, l11, l18 - (s3_2 or 0.0))
    if education is not None:
        add("Sch 3", "3", "Education credits", education[1], "Form 8863 line 19")
    savers = _form_8880(
        sheet,
        d,
        v,
        hh,
        l11,
        l18 - (s3_2 or 0.0) - (education[1] if education is not None else 0.0),
    )
    if savers is not None:
        add(
            "Sch 3",
            "4",
            "Retirement savings contributions credit",
            savers,
            "Form 8880 line 12",
        )
    engine_education = (
        v["non_refundable_american_opportunity_credit"] + v["lifetime_learning_credit"]
    )
    s3_8 = add(
        "Sch 3",
        "8",
        "Nonrefundable credits (other than the child credit)",
        v["income_tax_non_refundable_credits"]
        - v["non_refundable_ctc"]
        - v["cdcc"]
        - engine_education
        - v["savers_credit"]
        + (s3_2 or 0.0)
        + (education[1] if education is not None else engine_education)
        + (v["savers_credit"] if savers is None else savers),
        "engine income_tax_non_refundable_credits less the child credit"
        + (", with line 2 from Form 2441" if s3_2 is not None else "")
        + (", line 3 from Form 8863" if education is not None else "")
        + (", line 4 from Form 8880" if savers is not None else ""),
    )
    s3_9 = add("Sch 3", "9", "Net premium tax credit", net_ptc, "Form 8962 line 26")
    s3_15 = add("Sch 3", "15", "Other payments and refundable credits", s3_9, "line 9")
    eic = _eic(sheet, d, v, hh, l1z, eic_se, l11, exempt + l2b + l3b + max(l7, 0.0))
    ctc_14, ctc_27 = _schedule_8812(
        sheet,
        d,
        v,
        hh,
        l11,
        max(l18 - s3_8, 0.0),
        _actc_earned(d, eic, eic_se, l1z, profit + sp_profit),
        _payroll(facts, v),
        eic,
    )
    l19 = add(
        f,
        "19",
        "Child tax credit",
        v["non_refundable_ctc"] if ctc_14 is None else ctc_14,
        "engine non_refundable_ctc" if ctc_14 is None else "Sch 8812 line 14",
    )
    l20 = add(f, "20", "Amount from Schedule 3, line 8", s3_8, "Sch 3 line 8")
    l21 = add(f, "21", "Lines 19 and 20", l19 + l20, "19 + 20")
    l22 = add(f, "22", "Tax after credits", max(l18 - l21, 0.0), "18 - 21")
    l23 = add(f, "23", "Other taxes from Schedule 2, line 21", s2_21, "Sch 2 line 21")
    l24 = add(f, "24", "Total tax", l22 + l23, "22 + 23")

    # Form 1040 payments
    w2 = _sum(facts, (("W-2", "2"),))
    l25a = add(f, "25a", "Withheld, Forms W-2", w2, _cited(facts, (("W-2", "2"),)))
    other_pairs = WITHHELD[1:]
    l25b = add(
        f,
        "25b",
        "Withheld, Forms 1099 and SSA-1099",
        _sum(facts, other_pairs),
        _cited(facts, other_pairs),
    )
    l25d = add(f, "25d", "Total withholding", l25a + l25b, "25a + 25b")
    est_src = (
        "; ".join(f"{p.date} {p.amount:,.2f} ({p.origin})" for p in paid)
        or "no federal payment recorded (planner paid)"
    )
    l26 = add(f, "26", "Estimated tax payments", sum(p.amount for p in paid), est_src)
    l27 = add(f, "27a", "Earned income credit", eic, "EIC Worksheet")
    l28 = add(
        f,
        "28",
        "Additional child tax credit",
        v["refundable_ctc"] if ctc_27 is None else ctc_27,
        "engine refundable_ctc" if ctc_27 is None else "Sch 8812 line 27",
    )
    l29 = add(
        f,
        "29",
        "American opportunity and other refundable credits",
        v["income_tax_refundable_credits"]
        - v["eitc"]
        - v["refundable_ctc"]
        - v["refundable_american_opportunity_credit"]
        + (
            education[0]
            if education is not None
            else v["refundable_american_opportunity_credit"]
        ),
        "engine income_tax_refundable_credits less 27a and 28"
        + (", with Form 8863 line 8" if education is not None else ""),
    )
    l31 = add(f, "31", "Amount from Schedule 3, line 15", s3_15, "Sch 3 line 15")
    l32 = add(
        f,
        "32",
        "Other payments and refundable credits",
        l27 + l28 + l29 + l31,
        "27a + 28 + 29 + 31",
    )
    l33 = add(f, "33", "Total payments", l25d + l26 + l32, "25d + 26 + 32")
    if l33 >= l24:
        add(f, "34", "Overpaid (refund)", l33 - l24, "33 - 24")
    else:
        add(f, "37", "Amount you owe", l24 - l33, "24 - 33")

    for line, value in h.lines.items():
        add("8889", line, hsa.LABELS[line], value, h.sources[line])
    for line, value in hs.lines.items():
        add(hsa.SPOUSE_FORM, line, hsa.LABELS[line], value, hs.sources[line])
    d.notes.extend(h.notes)
    d.notes.extend(f"the spouse's Form 8889: {n}" for n in hs.notes)
    if cg.lots or cg.lines:
        limit = 1500.0 if hh.filing_status == "SEPARATE" else 3000.0
        _schedule_d(sheet, d, cg, l7, l11 - l14, limit)
    _schedule_b(sheet, d, facts, l2b, l3b, typed["foreign_accounts"])
    if hh.state == "NC":
        married = hh.filing_status == "JOINT"
        d400.lay_lines(add, d.notes, v, facts, nc_paid, year, l11, typed, married)

    # The lines against the engine's own totals: a gap is a mapping the draft
    # missed, and is said, never hidden.
    sb_4, sb_6 = d.get("Sch B", "4"), d.get("Sch B", "6")
    # The Tax Table's difference reaches line 22 only past the nonrefundable
    # credits: credits the tax limits (Form 8880 line 11, say) absorb it.
    passed = 0.0 if l22 <= 0 else min(l22, table_gap)
    # Line 27a is the EIC Table's and the engine's eitc exact: their gap is
    # checked at 27a, so the tie-out takes the table's.
    eic_gap = l27 - v["eitc"]
    # Part II-B takes 27a too: Schedule 8812 line 27 is checked on its own.
    ctc_gap = l28 - v["refundable_ctc"]
    for got, want, what in (
        (
            l9,
            v["irs_gross_income"] - v["loss_ald"],
            "line 9 against the engine's gross income less its loss deduction",
        ),
        (l11, v["adjusted_gross_income"], "line 11a against the engine's AGI"),
        (sb_4, l2b, "Schedule B line 4 against 1040 line 2b"),
        (sb_6, l3b, "Schedule B line 6 against 1040 line 3b"),
        (l15, v["taxable_income"], "line 15 against the engine's taxable income"),
        (
            l22 - s2_1a + s2_12 - (l27 + l28 + l29),
            v["income_tax"] + passed - eic_gap - ctc_gap,
            "line 22 (less 1a, plus NIIT, less refundable credits) against the "
            "engine's income tax (plus any Tax Table difference left after the "
            "credits, less the EIC Table's and Schedule 8812's)",
        ),
    ):
        if got is not None and abs(got - want) > TOLERANCE:
            d.notes.append(f"CHECK: {what}: {got:,.2f} vs {want:,.2f}")
    if exempt:
        d.notes.append(
            "tax-exempt interest (1040 line 2a) counts toward Social Security "
            "taxation and ACA MAGI"
        )
    return d


def _form_2441(
    sheet: _Sheet,
    d: Draft,
    v: dict[str, float],
    hh: Household,
    care: tax.DependentCare | None,
    agi: float,
    l18: float,
) -> float | None:
    """Form 2441 (2025 Form 2441 and its instructions; unit 3c-1): Part III
    (dependent care benefits) when W-2 box 10 shows any, then Part II (the
    credit). Returns line 11, Schedule 3 line 2; None when the return has
    neither care nor benefits. The engine counts the qualifying persons (a
    dependent under 13) and the line 8 decimal; Part III's lines come from
    ``tax.dependent_care``, which the engine was priced on."""
    if care is None and not hh.care_expenses:
        return None
    add = sheet.add
    f = "2441"
    count = add(
        f,
        "2",
        "Qualifying persons (dependents under 13)",
        v["count_cdcc_eligible"],
        "engine count_cdcc_eligible",
    )
    claim = count > 0 and (care.line16 if care is not None else hh.care_expenses) > 0
    if care is not None:
        add(f, "12", "Dependent care benefits", care.line12, "W-2 box 10, both spouses")
        add(f, "13", "Grace-period carryover used", care.line13, "dependent_care_grace")
        add(
            f,
            "14",
            "Forfeited or carried forward",
            care.line14,
            "dependent_care_forfeited",
        )
        add(f, "15", "Lines 12 and 13 less line 14", care.line15, "12 + 13 - 14")
        add(f, "16", "Qualified expenses incurred", care.line16, "care_expenses")
        add(f, "17", "Smaller of line 15 or 16", care.line17, "min(15, 16)")
        add(f, "18", "Your earned income", care.line18, "engine head_earned")
        add(
            f,
            "19",
            "Spouse's earned income" if hh.filing_status == "JOINT" else "Line 18",
            care.line19,
            "engine spouse_earned" if hh.filing_status == "JOINT" else "line 18",
        )
        add(f, "20", "Smallest of line 17, 18 or 19", care.line20, "min(17, 18, 19)")
        add(f, "21", "Exclusion limit", care.line21, "IRC 129(a)(2)(A)")
        add(f, "22", "Benefits from your sole proprietorship", 0.0, "taken as none")
        add(f, "23", "Line 15 less line 22", care.line15, "15 - 22")
        add(f, "24", "Deductible benefits", 0.0, "none: line 22 is 0")
        add(f, "25", "Excluded benefits", care.line25, "min(20, 21)")
        add(f, "26", "Taxable benefits", care.line26, "23 - 25; Form 1040 line 1e")
        if claim:
            add(f, "27", "$3,000, or $6,000 for two or more", care.line27, "line 2")
            add(f, "28", "Lines 24 and 25", care.line25, "24 + 25")
            add(f, "29", "Line 27 less line 28", care.line29, "27 - 28")
            add(f, "30", "Care paid, not counting line 28", care.line30, "16 - 28")
            add(f, "31", "Smaller of line 29 or 30", care.line31, "min(29, 30)")
    if not claim:
        if hh.care_expenses and not count:
            d.notes.append(
                "Form 2441: care was typed but no dependent is under 13, so "
                "no one qualifies for the credit (a dependent or spouse who "
                "cannot care for themselves also qualifies: the draft does not "
                "count them; see your preparer)"
            )
        return 0.0 if care is not None else None
    if care is not None:
        l3 = add(f, "3", "Qualified expenses", care.line31, "line 31")
    else:
        l3 = add(
            f,
            "3",
            "Qualified expenses, up to $3,000 ($6,000 for two or more)",
            min(float(hh.care_expenses), v["cdcc_limit"]),
            "care_expenses; engine cdcc_limit",
        )
    l4 = add(f, "4", "Your earned income", v["head_earned"], "engine head_earned")
    joint = hh.filing_status == "JOINT"
    l5 = add(
        f,
        "5",
        "Spouse's earned income" if joint else "Line 4",
        v["spouse_earned"] if joint else l4,
        "engine spouse_earned" if joint else "line 4",
    )
    l6 = add(f, "6", "Smallest of line 3, 4 or 5", min(l3, l4, l5), "min(3, 4, 5)")
    add(f, "7", "Adjusted gross income", agi, "Form 1040 line 11")
    l8 = add(
        f, "8", "Decimal amount", v["cdcc_rate"], "engine cdcc_rate (line 8 table)"
    )
    l9a = add(f, "9a", "Line 6 times line 8", l6 * l8, "6 x 8")
    l9c = add(f, "9c", "Lines 9a and 9b", l9a, "9a + 9b (9b not drafted)")
    l10 = add(f, "10", "Tax limit", l18, "Credit Limit Worksheet: 1040 line 18")
    allowed = hh.filing_status != "SEPARATE"
    l11 = add(f, "11", "Credit", min(l9c, l10) if allowed else 0.0, "min(9c, 10)")
    if not allowed:
        d.notes.append(
            "Form 2441: a married person filing separately takes the credit only "
            "if they lived apart from their spouse the last 6 months of the year "
            "(2025 i2441, Married Persons Filing Separately); the draft takes none"
        )
    if abs(l6 - v["cdcc_relevant_expenses"]) > TOLERANCE:
        d.notes.append(
            f"CHECK: Form 2441 line 6 against the engine's cdcc_relevant_expenses: "
            f"{l6:,.2f} vs {v['cdcc_relevant_expenses']:,.2f}"
        )
    d.notes.append(
        "Form 2441: Part I (each care provider's name, address, taxpayer ID and "
        "amount paid) is filled in from the providers' receipts; line 9b (2024 "
        "care paid in 2025, Worksheet A) is not drafted"
    )
    return l11


# Form 8863 (2025): the American opportunity credit's expenses per student
# (line 27's cap, all of the first $2,000 and a quarter of the next), the
# modified AGI phase-out both credits share (lines 2 and 5; IRC 25A(d), fixed
# since 2021), the refundable part (line 8) and the lifetime learning credit's
# expense cap and rate (lines 11 and 12; IRC 25A(c)).
AOTC_CAP, AOTC_FULL, AOTC_REST = 4000.0, 2000.0, 0.25
EDUCATION_MAGI = {"JOINT": (180_000.0, 20_000.0)}
EDUCATION_MAGI_OTHER = (90_000.0, 10_000.0)
AOTC_REFUNDABLE = 0.40
LLC_CAP, LLC_RATE = 10_000.0, 0.20


def _student_name(who: str) -> str:
    return {"p": "you", "s": "the spouse"}.get(who, f"dependent {who[1:]}")


def _form_8863(
    sheet: _Sheet,
    d: Draft,
    v: dict[str, float],
    hh: Household,
    magi: float,
    limit: float,
) -> tuple[float, float] | None:
    """Form 8863, education credits (2025 Form 8863 and its instructions; unit
    3c-2): Part III per student (lines 27-30, or 31 for the lifetime learning
    credit), Part I (the American opportunity credit and its refundable line 8)
    and Part II with the Credit Limit Worksheet. ``limit`` is 1040 line 18 less
    Schedule 3 lines 1 and 2 (worksheet line 6). Returns (line 8, line 19), or
    None when the return has no student."""
    if not hh.students:
        return None
    add = sheet.add
    f = "8863"
    if hh.filing_status == "SEPARATE":
        d.notes.append(
            "Form 8863: a married person filing separately cannot take either "
            "education credit (2025 i8863, Who Can Claim); the draft takes none"
        )
        return 0.0, 0.0
    aotc = llc = 0.0
    for n, st in enumerate(hh.students, 1):
        who = _student_name(st.who)
        if st.credit == "llc":
            llc += add(
                f,
                f"S{n}-31",
                f"Student {n} ({who}): lifetime learning expenses",
                float(st.expenses),
                "education: paid less tax-free assistance",
            )
            continue
        l27 = add(
            f,
            f"S{n}-27",
            f"Student {n} ({who}): expenses, up to $4,000",
            min(float(st.expenses), AOTC_CAP),
            "education: paid less tax-free assistance",
        )
        l28 = add(
            f,
            f"S{n}-28",
            "Line 27 less $2,000",
            max(l27 - AOTC_FULL, 0.0),
            "27 - 2,000",
        )
        l29 = add(f, f"S{n}-29", "Line 28 times 25%", l28 * AOTC_REST, "28 x 0.25")
        aotc += add(
            f,
            f"S{n}-30",
            "American opportunity credit for the student",
            l27 if l28 == 0 else AOTC_FULL + l29,
            "27 if 28 is zero, else 2,000 + 29",
        )
    top, span = EDUCATION_MAGI.get(hh.filing_status, EDUCATION_MAGI_OTHER)

    def phase(first: int, total: float) -> float:
        """Lines 2-7 (or 13-18): ``total`` times the share left unphased."""
        n = [str(first + i) for i in range(6)]
        add(f, n[0], "Phase-out top", top, "$180,000 joint, else $90,000")
        add(f, n[1], "Modified adjusted gross income", magi, "Form 1040 line 11")
        room = add(f, n[2], "Line 2 less line 3", max(top - magi, 0.0), "2 - 3")
        add(f, n[3], "Phase-out range", span, "$20,000 joint, else $10,000")
        share = add(
            f, n[4], "Line 4 over line 5 (at most 1)", min(room / span, 1.0), "4 / 5"
        )
        return add(f, n[5], "Credit after the phase-out", total * share, "1 x 6")

    l7 = 0.0
    if any(st.credit == "aotc" for st in hh.students):
        add(
            f,
            "1",
            "American opportunity credit, all students",
            aotc,
            "Part III line 30",
        )
        l7 = phase(2, aotc)
    barred = hh.aotc_refundable_barred
    l8 = add(
        f,
        "8",
        "Refundable American opportunity credit",
        0.0 if barred else l7 * AOTC_REFUNDABLE,
        "none: the line 7 conditions apply" if barred else "7 x 0.40",
    )
    l9 = add(f, "9", "Nonrefundable American opportunity credit", l7 - l8, "7 - 8")
    l18 = 0.0
    if llc:
        add(
            f, "10", "Lifetime learning expenses, all students", llc, "Part III line 31"
        )
        l11 = add(
            f,
            "11",
            "Smaller of line 10 or $10,000",
            min(llc, LLC_CAP),
            "min(10, 10,000)",
        )
        l12 = add(f, "12", "Line 11 times 20%", l11 * LLC_RATE, "11 x 0.20")
        l18 = phase(13, l12)
    l19 = add(
        f,
        "19",
        "Nonrefundable education credits",
        max(min(l18 + l9, limit), 0.0),
        "Credit Limit Worksheet: smaller of 18 + 9 or 1040 line 18 less Sch 3 "
        "lines 1-2",
    )
    for got, want, what in (
        (l8, v["refundable_american_opportunity_credit"], "line 8"),
        (
            l19,
            v["non_refundable_american_opportunity_credit"]
            + v["lifetime_learning_credit"],
            "line 19",
        ),
    ):
        if abs(got - want) > TOLERANCE:
            d.notes.append(
                f"CHECK: Form 8863 {what} against the engine's: {got:,.2f} vs "
                f"{want:,.2f}"
            )
    d.notes.append(
        "Form 8863: Part III lines 20-26 (each student's name, school, its EIN "
        "and the yes-or-no answers behind the credit typed) are filled in from "
        "the 1098-T and the student's records"
    )
    return l8, l19


def _form_8880(
    sheet: _Sheet,
    d: Draft,
    v: dict[str, float],
    hh: Household,
    agi: float,
    limit: float,
) -> float | None:
    """Form 8880, the saver's credit (2025 Form 8880, which carries its own
    instructions; unit 3c-3): column (a) the head's and (b) a joint spouse's
    lines 1-6, then lines 7-12 with the Credit Limit Worksheet. ``limit`` is 1040
    line 18 less Schedule 3 lines 1-3. Returns line 12, or None when no one made
    a contribution the form counts."""
    people: list[tuple[str, str, Household | Person]] = [("a", "you", hh)]
    if hh.spouse is not None and hh.filing_status == "JOINT":
        people.append(("b", "your spouse", hh.spouse))
    if not any(
        x.traditional_ira_contribution + x.roth_ira_contribution + x.elective_deferrals
        for _, _, x in people
    ):
        return None
    add = sheet.add
    f = "8880"
    cap = tax.savers_credit_cap(d.year)
    # Line 4 is never below the year's own IRA distributions, both spouses' on
    # a joint return (the form: both spouses' go in both columns).
    floor = sum(x.ira_distributions for _, _, x in people)
    l7 = 0.0
    for col, who, x in people:
        l1 = add(
            f,
            f"1({col})",
            f"Traditional and Roth IRA and ABLE contributions ({who})",
            float(x.traditional_ira_contribution + x.roth_ira_contribution),
            "traditional_ira_contribution + roth_ira_contribution",
        )
        l2 = add(
            f,
            f"2({col})",
            f"Elective deferrals and voluntary contributions ({who})",
            float(x.elective_deferrals),
            "elective_deferrals (W-2 box 12)",
        )
        l3 = add(f, f"3({col})", "Lines 1 and 2", l1 + l2, "1 + 2")
        l4 = add(
            f,
            f"4({col})",
            "Distributions in the testing period",
            float(max(x.savers_distributions, floor)),
            "savers_distributions, at least the year's IRA distributions",
        )
        l5 = add(f, f"5({col})", "Line 3 less line 4", max(l3 - l4, 0.0), "3 - 4")
        barred = x.savers_eligible is False or (
            x.savers_eligible is None and x.age < 18
        )
        l7 += add(
            f,
            f"6({col})",
            f"Smaller of line 5 or ${cap:,.0f}",
            0.0 if barred else min(l5, cap),
            "none: under 18, a student or claimed as a dependent"
            if barred
            else f"min(5, {cap:,.0f})",
        )
    add(f, "7", "Line 6, both columns", l7, "6(a) + 6(b)")
    add(f, "8", "Adjusted gross income", agi, "Form 1040 line 11a")
    table = tax.savers_credit_table(d.year, hh.filing_status)
    rate = next((r for top, r in table if agi <= top), 0.0)
    top = table[-1][0]
    l9 = add(
        f,
        "9",
        "Decimal from the table",
        rate,
        f"line 8 against the table; over ${top:,.0f} it is 0",
    )
    l10 = add(f, "10", "Line 7 times line 9", l7 * l9, "7 x 9")
    l11 = add(
        f,
        "11",
        "Credit Limit Worksheet",
        max(limit, 0.0),
        "1040 line 18 less Sch 3 lines 1-3",
    )
    l12 = add(
        f, "12", "Credit for qualified retirement savings", min(l10, l11), "min(10, 11)"
    )
    # Line 10 against the engine's credit before the limit: line 11 follows the
    # Tax Table's 1040 line 16, the engine the exact rate schedule.
    if abs(l10 - v["savers_credit_potential"]) > TOLERANCE:
        d.notes.append(
            f"CHECK: Form 8880 line 10 against the engine's saver's credit: "
            f"{l10:,.2f} vs {v['savers_credit_potential']:,.2f}"
        )
    return l12


def _eic_children(hh: Household) -> list[tuple[int, Dependent]]:
    """The dependents who are qualifying children for the EIC (2025 Form 1040
    instructions, line 27a, Step 3): under 19 at the end of the year, or under
    24 and a full-time student, and younger than the filer (or either spouse on
    a joint return), or permanently and totally disabled at any age. Each is
    taken to have a valid SSN and to have lived with the filer in the United
    States for more than half the year, as the dependents list notes."""
    oldest = hh.age
    if hh.filing_status == "JOINT" and hh.spouse is not None:
        oldest = max(oldest, hh.spouse.age)
    return [
        (n, dep)
        for n, dep in enumerate(hh.dependents, 1)
        if dep.disabled
        or (
            dep.age < (EIC_STUDENT_UNDER if dep.full_time_student else EIC_CHILD_UNDER)
            and dep.age < oldest
        )
    ]


def _schedule_eic(sheet: _Sheet, d: Draft, kids: list[tuple[int, Dependent]]) -> None:
    """Schedule EIC lines 3-4b for the first three qualifying children (the
    schedule lists three; more do not raise the credit); lines 1, 2, 5 and 6
    (name, SSN, relationship, months lived with you) are the preparer's."""
    add = sheet.add
    for c, (n, dep) in enumerate(kids[:3], 1):
        add(
            SCH_EIC,
            f"3-{c}",
            f"Child {c} (dependent {n}): year of birth",
            d.year - dep.age,
            f"dependent {n} age {dep.age} at year end",
            0,
        )
        if dep.age < EIC_CHILD_UNDER:
            continue  # under 19 and younger than you: lines 4a and 4b are skipped
        student = dep.full_time_student and dep.age < EIC_STUDENT_UNDER
        add(
            SCH_EIC,
            f"4a-{c}",
            f"Under 24, a student and younger than you: {'Yes' if student else 'No'}",
            0.0,
            f"dependent {n}",
        )
        if not student:
            add(
                SCH_EIC,
                f"4b-{c}",
                f"Permanently and totally disabled: {'Yes' if dep.disabled else 'No'}",
                0.0,
                f"dependent {n}",
            )
    d.notes.append(
        "Schedule EIC: type each child's name and SSN (lines 1-2), the "
        "relationship (line 5) and the months the child lived with you in the "
        "United States (line 6; over half the year is the draft's assumption)"
    )


def _eic(
    sheet: _Sheet,
    d: Draft,
    v: dict[str, float],
    hh: Household,
    wages: float,
    se: list[tuple[str, float, float]],
    agi: float,
    investment: float,
) -> float:
    """1040 line 27a by the EIC worksheet (2025 Form 1040 instructions, line
    27a): Worksheet A, or B when anyone on the return was self-employed (``se``
    holds each Schedule SE's form, Schedule C profit and line 13), the EIC Table
    looked up on earned income and, when they differ and AGI is past the
    phase-out start, on AGI; the smaller is the credit. Steps 1-4's tests that
    the draft knows (AGI and investment income limits, the separated-spouse and
    age rules) are applied; the engine's eitc is checked against the result."""
    add = sheet.add
    f = "EIC"
    p = tax.eic_parameters(d.year)
    joint = hh.filing_status == "JOINT"
    kids = _eic_children(hh)
    n = min(len(kids), 3)
    if len(kids) != round(v["eitc_child_count"]):
        d.notes.append(
            f"CHECK: the EIC counts {len(kids)} qualifying children; the engine "
            f"counts {v['eitc_child_count']:.0f}"
        )

    def stop(why: str) -> float:
        d.notes.append(f"EIC: {why}, so 1040 line 27a is 0 (check the box on 27c)")
        _check(0.0)
        return 0.0

    def _check(credit: float) -> None:
        if abs(credit - v["eitc"]) > EIC_SLACK:
            d.notes.append(
                f"CHECK: 1040 line 27a {credit:,.2f} (the EIC Table) vs engine "
                f"eitc {v['eitc']:,.2f}"
            )

    if investment > p["investment_income"]:
        return stop(
            f"investment income {investment:,.2f} (1040 lines 2a + 2b + 3b + 7a) "
            f"is over ${p['investment_income']:,.0f} (Step 2)"
        )
    if not kids:
        if hh.filing_status == "SEPARATE":
            return stop("married filing separately without a qualifying child (Step 4)")
        ages = [hh.age] + ([hh.spouse.age] if joint and hh.spouse is not None else [])
        if not any(p["min_age"] <= a <= p["max_age"] for a in ages):
            return stop(
                f"without a qualifying child the filer (or a joint spouse) must be "
                f"{p['min_age']:.0f}-{p['max_age']:.0f} at the end of the year (Step 4)"
            )
    elif hh.filing_status == "SEPARATE":
        d.notes.append(
            "EIC on a separate return: you qualify only under the special rule for "
            "separated spouses (Step 3 questions 3-5: apart for the last 6 months, "
            "or legally separated); check its box in the Dependents section"
        )
    if se:
        form = "B"
        l1c = add(
            f,
            "B1c",
            "Schedule C profit (Sch SE line 3, or Sch C line 31)",
            sum(s[1] for s in se),
            " + ".join(f"{s[0]} line 3" for s in se),
        )
        l1d = add(
            f,
            "B1d",
            "Deduction for half of SE tax",
            sum(s[2] for s in se),
            " + ".join(f"{s[0]} line 13" for s in se),
        )
        l1e = add(f, "B1e", "Line 1c less line 1d", l1c - l1d, "1c - 1d")
        l4a = add(f, "B4a", "Earned income from Step 5", wages, "1040 line 1z")
        earned = add(f, "B4b", "Total earned income", l1e + l4a, "1e + 4a")
    else:
        form = "A"
        earned = add(f, "A1", "Earned income from Step 5", wages, "1040 line 1z")
    num = {"A": ("2", "3", "5", "6"), "B": ("7", "8", "10", "11")}[form]
    if earned <= 0:
        return stop("earned income is zero or less")
    column = f"{'joint' if joint else 'single'} column, {n} children"
    by_earned = add(
        f,
        form + num[0],
        "EIC Table on earned income",
        tax.eic_table(d.year, n, joint, earned),
        column,
    )
    add(f, form + num[1], "Adjusted gross income", agi, "1040 line 11b")
    start = tax.eic_phaseout_start(d.year, n, joint)
    credit = by_earned
    if agi != earned and agi >= start:
        by_agi = add(
            f,
            form + num[2],
            "EIC Table on AGI",
            tax.eic_table(d.year, n, joint, agi),
            column,
        )
        credit = min(by_earned, by_agi)
    credit = add(
        f,
        form + num[3],
        "Earned income credit",
        credit,
        f"line {num[0]}"
        if credit == by_earned
        else f"smaller of {num[0]} and {num[2]}",
    )
    if kids:
        _schedule_eic(sheet, d, kids)
    _check(credit)
    return credit


def _schedule_8812(
    sheet: _Sheet,
    d: Draft,
    v: dict[str, float],
    hh: Household,
    agi: float,
    limit: float,
    earned: tuple[float, str],
    payroll: tuple[float, str],
    eic: float,
) -> tuple[float | None, float | None]:
    """Schedule 8812 for the household's dependents, and the 1040 dependents
    list as a note: (line 14, line 27), or None for a line not drafted. Lines
    4-27 are the form's own arithmetic: line 13 is `limit` (Credit Limit
    Worksheet A: 1040 line 18 less the Schedule 3 credits), line 18a `earned`
    (the Earned Income Chart, ``_actc_earned``), and Part II-B, with three or
    more children, takes line 21 from `payroll` and line 24 from `eic` (1040
    line 27a). Line 27 is checked against the engine's refundable_ctc."""
    if not hh.dependents:
        return None, None
    listed = []
    for n, dep in enumerate(hh.dependents, 1):
        marks = [
            m
            for m, on in (
                ("full-time student", dep.full_time_student),
                ("disabled", dep.disabled),
            )
            if on
        ]
        credit = (
            "child tax credit"
            if dep.age < CTC_CHILD_UNDER
            else "credit for other dependents"
        )
        listed.append(f"({n}) age {', '.join([str(dep.age), *marks])}, {credit}")
    d.notes.append(
        f"1040 dependents: {'; '.join(listed)}; type each name, SSN and "
        "relationship from the Social Security card (the credits assume each "
        "dependent and filer has a valid SSN and lived with you over half the year)"
    )
    amounts = CTC_AMOUNTS.get(d.year)
    if amounts is None:
        d.notes.append(
            f"Schedule 8812 is not drafted: the {d.year} credit amounts are not "
            "on file here; 1040 lines 19 and 28 are the engine's"
        )
        return None, None
    child, refundable = amounts
    add = sheet.add
    f = SCH_8812
    k = 1 if hh.filing_status == "JOINT" else 0
    children = sum(dep.age < CTC_CHILD_UNDER for dep in hh.dependents)
    others = len(hh.dependents) - children
    if children != round(v["ctc_qualifying_children"]):
        d.notes.append(
            f"CHECK: Schedule 8812 line 4 counts {children} children under "
            f"{CTC_CHILD_UNDER}; the engine counts "
            f"{v['ctc_qualifying_children']:.0f}"
        )
    add(f, "1", "Adjusted gross income", agi, "1040 line 11b")
    l3 = add(f, "3", "Modified AGI", agi, "line 1 (no income excluded on 2a-2c)")
    add(f, "4", "Qualifying children under 17", children, "dependents under 17", 0)
    l5 = add(
        f, "5", "Line 4 times the child amount", children * child, f"4 x {child:,.0f}"
    )
    add(f, "6", "Other dependents", others, "dependents 17 or over", 0)
    l7 = add(f, "7", "Line 6 times $500", others * ODC_AMOUNT, f"6 x {ODC_AMOUNT:,.0f}")
    l8 = add(f, "8", "Lines 5 and 7", l5 + l7, "5 + 7")
    l9 = add(f, "9", "Phase-out starts", CTC_FROM[k], "as printed")
    over = l3 - l9
    cut = 0.0
    if over > 0:
        l10 = add(
            f,
            "10",
            "MAGI over the start, rounded up",
            -(-over // CTC_CUT_PER) * CTC_CUT_PER,
            "3 - 9, up to the next $1,000",
        )
        cut = add(f, "11", "Reduction", l10 * CTC_CUT_RATE, f"10 x {CTC_CUT_RATE:.0%}")
    l12 = add(f, "12", "Credit before the tax limit", max(l8 - cut, 0.0), "8 - 11")
    if abs(l12 - v["ctc"]) > TOLERANCE:
        d.notes.append(
            f"CHECK: Schedule 8812 line 12 {l12:,.2f} vs engine ctc {v['ctc']:,.2f}"
        )
    l13 = add(
        f,
        "13",
        "Credit Limit Worksheet A",
        limit,
        "1040 line 18 less Sch 3 line 8 (each Schedule 3 credit taken as one "
        "the worksheet subtracts)",
    )
    l14 = add(
        f,
        "14",
        "Child tax credit and credit for other dependents",
        min(l12, l13),
        "min(12, 13)",
    )
    l16a = l12 - l14
    if not children or l16a <= TOLERANCE:
        if v["refundable_ctc"] > TOLERANCE:
            d.notes.append(
                f"CHECK: the engine's refundable_ctc {v['refundable_ctc']:,.2f} "
                "where Schedule 8812 Part II-A has nothing to refund"
            )
        return l14, 0.0
    l16a = add(f, "16a", "Line 12 less line 14", l16a, "12 - 14")
    l16b = add(
        f,
        "16b",
        "Children times the refundable most",
        children * refundable,
        f"4 x {refundable:,.0f}",
    )
    l17 = add(f, "17", "Smaller of 16a and 16b", min(l16a, l16b), "min(16a, 16b)")
    floor, rate, many = tax.actc_phase_in(d.year)
    l18a = add(f, "18a", "Earned income", *earned)
    l20 = 0.0
    if l18a > floor:
        l19 = add(
            f, "19", f"Line 18a less ${floor:,.0f}", l18a - floor, f"18a - {floor:,.0f}"
        )
        l20 = l19 * rate
    l20 = add(f, "20", f"Line 19 times {rate:.0%}", l20, f"19 x {rate:g}")
    slack = TOLERANCE
    if l16b >= many * refundable and l20 < l17:
        # Part II-B: the Social Security and Medicare tax less the EIC.
        l21 = add(f, "21", "Social Security and Medicare tax withheld", *payroll)
        if v["additional_medicare_tax"] > TOLERANCE:
            d.notes.append(
                "Schedule 8812 line 21: the Additional Medicare Tax and RRTA Tax "
                "Worksheet is not drafted (Additional Medicare Tax is owed); the "
                "line is boxes 4 and 6 as they stand"
            )
        l22 = add(
            f,
            "22",
            "Schedule 1 line 15; Schedule 2 lines 5, 6 and 13",
            d.get("Sch 1", "15") or 0.0,
            "Sch 1 line 15 (Schedule 2 lines 5, 6 and 13, Forms 4137 and 8919 "
            "and W-2 box 12 codes A, B, M and N, are not drafted)",
        )
        l23 = add(f, "23", "Lines 21 and 22", l21 + l22, "21 + 22")
        l24 = add(
            f,
            "24",
            "1040 line 27a and Schedule 3 line 11",
            eic,
            "1040 line 27a (Schedule 3 line 11, excess Social Security tax "
            "withheld, is not drafted)",
        )
        l25 = add(f, "25", "Line 23 less line 24", max(l23 - l24, 0.0), "23 - 24")
        l26 = add(f, "26", "Larger of lines 20 and 25", max(l20, l25), "max(20, 25)")
        refund, how = min(l17, l26), "min(17, 26)"
        # The engine takes its exact eitc where line 24 has the EIC Table's.
        slack = EIC_SLACK
    else:
        refund, how = min(l17, l20), "min(17, 20)"
    l27 = add(f, "27", "Additional child tax credit", refund, how)
    if abs(l27 - v["refundable_ctc"]) > slack:
        d.notes.append(
            f"CHECK: Schedule 8812 line 27 {l27:,.2f} vs engine refundable_ctc "
            f"{v['refundable_ctc']:,.2f}"
        )
    return l14, l27


def _actc_earned(
    d: Draft,
    eic: float,
    se: list[tuple[str, float, float]],
    wages: float,
    profit: float,
) -> tuple[float, str]:
    """Schedule 8812 line 18a by its Earned Income Chart (2025 instructions):
    taking the EIC, its earned income (Worksheet B line 4b, or Step 5's, which
    is Worksheet A line 1); otherwise the Earned Income Worksheet: 1040 line 1z
    plus Schedule C line 31 less Schedule 1 line 15 (line 7), and 0 when that
    is zero or less. Neither the optional methods, statutory employee income,
    combat pay nor Medicaid waiver payments are drafted."""
    if eic > 0:
        if se:
            return d.get("EIC", "B4b") or 0.0, "EIC Worksheet B line 4b"
        return d.get("EIC", "A1") or 0.0, "EIC Worksheet A line 1 (Step 5)"
    half = d.get("Sch 1", "15") or 0.0
    return (
        max(wages + profit - half, 0.0),
        "Earned Income Worksheet: 1040 line 1z + Schedule C line 31 - "
        "Schedule 1 line 15",
    )


def _payroll(facts: list[db.FactRow], v: dict[str, float]) -> tuple[float, str]:
    """Schedule 8812 line 21: the W-2s' boxes 4 and 6 on file, both spouses';
    with neither box on file, the engine's employee Social Security and Medicare
    tax on the wages, which is what an employer withholds."""
    if any((f.form, f.box) in PAYROLL for f in facts):
        return _sum(facts, PAYROLL), _cited(facts, PAYROLL)
    return (
        v["employee_social_security_tax"] + v["employee_medicare_tax"],
        "engine employee Social Security and Medicare tax (W-2 boxes 4 and 6 "
        "not on file)",
    )


def _ss_wages(facts: list[db.FactRow], owner: str) -> float | None:
    """The owner's W-2 boxes 3 and 7 on file (Schedule SE line 8a), or None."""
    mine = [f for f in facts if f.owner == owner]
    if not any((f.form, f.box) in SS_WAGES for f in mine):
        return None
    return _sum(mine, SS_WAGES)


def _schedule_se(
    sheet: _Sheet,
    d: Draft,
    v: dict[str, float],
    who: str,
    form: str,
    profit: float,
    profit_src: str,
    ss_wages: float | None,
) -> tuple[float, float]:
    """One person's Schedule SE Part I, line by line (``who`` is you or spouse;
    each spouse files their own: unit 3a-5): (self-employment tax on line 12,
    the deduction for half of it on line 13). The Social Security wage base,
    the rates and the $400 floor are the engine's own parameters; the engine's
    tax for that person is checked against line 12. ``ss_wages`` is the sum of
    their W-2 boxes 3 and 7 on file, or None."""
    p = tax.self_employment_parameters(d.year)
    add = sheet.add

    share = p["net_earnings_share"]
    l2 = add(form, "2", "Net profit or (loss) from Schedule C", profit, profit_src)
    l3 = add(form, "3", "Combine lines 1a, 1b and 2", l2, "line 2 (no farm profit)")
    l4a = add(
        form,
        "4a",
        f"Net earnings (x {share:.2%})",
        l3 * share if l3 > 0 else l3,
        f"3 x {share:.2%}" if l3 > 0 else "line 3 (a loss)",
    )
    l4c = add(
        form,
        "4c",
        "Combine lines 4a and 4b",
        l4a,
        "line 4a (no optional method)",
    )
    if l4c < p["floor"]:
        why = f"line 4c under ${p['floor']:,.0f}: no self-employment tax"
        l12 = add(form, "12", "Self-employment tax", 0.0, why)
        l13 = add(form, "13", "Deduction for half of SE tax", 0.0, why)
    else:
        l6 = add(form, "6", "Add lines 4c and 5b", l4c, "line 4c (no church pay)")
        l7 = add(
            form,
            "7",
            "Social Security wage base",
            p["wage_base"],
            "Schedule SE line 7 (engine gov.irs.payroll.social_security.cap)",
        )
        l8a = add(
            form,
            "8a",
            "Social Security wages and tips",
            v[f"employment_income@{who}"] if ss_wages is None else ss_wages,
            "wages: W-2 box 1 stands in for boxes 3 and 7 (no W-2 box 3 or 7 on file)"
            if ss_wages is None
            else "W-2 boxes 3 and 7",
        )
        l8d = add(
            form,
            "8d",
            "Add lines 8a, 8b and 8c",
            l8a,
            "line 8a (no Form 4137 or 8919)",
        )
        l9 = add(form, "9", "Wage base left", max(l7 - l8d, 0.0), "7 - 8d")
        ss = p["social_security_rate"]
        l10 = add(
            form,
            "10",
            f"Social Security part (x {ss:.1%})",
            min(l6, l9) * ss,
            f"smaller of 6 and 9, x {ss:.1%}",
        )
        md = p["medicare_rate"]
        l11 = add(form, "11", f"Medicare part (x {md:.1%})", l6 * md, f"6 x {md:.1%}")
        l12 = add(form, "12", "Self-employment tax", l10 + l11, "10 + 11")
        l13 = add(
            form,
            "13",
            "Deduction for half of SE tax",
            l12 * p["deductible_share"],
            f"12 x {p['deductible_share']:.0%}",
        )
    want = v[f"self_employment_tax@{who}"]
    if abs(l12 - want) > TOLERANCE:
        d.notes.append(
            f"CHECK: {form} line 12 against the engine's SE tax: "
            f"{l12:,.2f} vs {want:,.2f}"
        )
    return l12, l13


def _phased(
    sheet: _Sheet,
    lines: tuple[str, ...],
    amount: float,
    cap: float,
    magi: float,
    start: float,
    cut: float,
    *,
    up: bool,
    what: str,
) -> float:
    """One Schedule 1-A part's lines: the amount held to its cap, the MAGI
    over the phase-out start in whole $1,000s (rounded up or down as the part
    says), and the cut taken for each. ``lines`` are the form's cap, MAGI,
    start, excess, thousands, cut and result lines, in that order."""
    cap_ln, magi_ln, start_ln, over_ln, count_ln, cut_ln, out_ln = lines
    add = sheet.add
    held = add(
        SCH_1A,
        cap_ln,
        f"Smaller of the {what} or ${cap:,.0f}",
        min(amount, cap),
        "as printed",
    )
    add(SCH_1A, magi_ln, "Modified AGI", magi, "line 3")
    add(SCH_1A, start_ln, "Phase-out starts", start, "as printed")
    taken = 0.0
    over = magi - start
    if over > 0:
        add(SCH_1A, over_ln, "MAGI over the start", over, f"{magi_ln} - {start_ln}")
        steps = math.ceil(round(over / 1000, 9)) if up else math.floor(over / 1000)
        add(
            SCH_1A,
            count_ln,
            "Thousands of dollars over",
            steps,
            f"{over_ln} / 1,000, whole number",
        )
        taken = add(
            SCH_1A, cut_ln, "Reduction", steps * cut, f"{count_ln} x ${cut:,.0f}"
        )
    return add(
        SCH_1A,
        out_ln,
        f"Deduction for {what}",
        max(held - taken, 0.0),
        f"{cap_ln} - {cut_ln}"
        if over > 0
        else f"line {cap_ln} (MAGI is not over the start)",
    )


def _schedule_1a(
    sheet: _Sheet, d: Draft, v: dict[str, float], hh: Household, agi: float
) -> tuple[float, dict[str, float]]:
    """Schedule 1-A (Form 1040), whose line 38 is 1040 line 13b: (the total, the
    engine variables whose figure the form's rounding moves, with the form's
    figure, for the caller to price again). Part II to IV lines are drawn for
    the figures the household carries; Part V for a filer 65 or over at year
    end. Each part is checked against the engine's; a gap that is not the
    form's rounding is a CHECK, never absorbed. The engine run `v` holds no
    tips or overtime for a married filing separately return."""
    add = sheet.add
    magi = add(SCH_1A, "1", "Adjusted gross income", agi, "1040 line 11b")
    add(SCH_1A, "3", "Modified AGI", magi, "line 1 (no income excluded on lines 2a-2d)")
    k = 1 if hh.filing_status == "JOINT" else 0
    separate = hh.filing_status == "SEPARATE"
    form: dict[str, float] = {}
    skipped: set[str] = set()
    if hh.qualified_tips and not hh.tipped_occupation_code:
        d.notes.append(
            "Schedule 1-A Part II is not drafted: tips need a Treasury tipped "
            "occupation code (IRS.gov/TippedOccupations), and none is on file"
        )
    elif hh.qualified_tips:
        if separate:
            skipped.add("13")
        else:
            add(
                SCH_1A,
                "6",
                "Qualified tips",
                hh.qualified_tips,
                "household qualified tips (lines 4a-5 not split)",
            )
            form["13"] = _phased(
                sheet,
                ("7", "8", "9", "10", "11", "12", "13"),
                hh.qualified_tips,
                TIPS_CAP,
                magi,
                TIPS_FROM[k],
                CUT_TIPS,
                up=False,
                what="qualified tips",
            )
    if hh.qualified_overtime:
        if separate:
            skipped.add("21")
        else:
            add(
                SCH_1A,
                "14c",
                "Qualified overtime compensation",
                hh.qualified_overtime,
                "household qualified overtime (lines 14a-14b not split)",
            )
            form["21"] = _phased(
                sheet,
                ("15", "16", "17", "18", "19", "20", "21"),
                hh.qualified_overtime,
                OVERTIME_CAP[k],
                magi,
                OVERTIME_FROM[k],
                CUT_OVERTIME,
                up=False,
                what="qualified overtime",
            )
    if hh.car_loan_interest:
        add(
            SCH_1A,
            "23",
            "Qualified passenger vehicle loan interest",
            hh.car_loan_interest,
            "household car loan interest (line 22 columns not split)",
        )
        form["30"] = _phased(
            sheet,
            ("24", "25", "26", "27", "28", "29", "30"),
            hh.car_loan_interest,
            CAR_LOAN_CAP,
            magi,
            CAR_LOAN_FROM[k],
            CUT_CAR_LOAN,
            up=True,
            what="car loan interest",
        )
    unasked = [k for k in PART_KEYS if k in d.unknown]
    if unasked:
        d.notes.append(
            "Schedule 1-A Parts II to IV (tips, overtime, car loan interest) leave "
            f"out {', '.join(unasked)}: no figure is on file, so none is deducted; "
            "type each (0 if none) in the Needed panel"
        )
    spouse_age = hh.spouse.age if hh.spouse is not None else None
    head_65 = hh.age >= SENIOR_AGE
    spouse_65 = bool(k) and spouse_age is not None and spouse_age >= SENIOR_AGE
    if (head_65 or spouse_65) and not separate:
        add(SCH_1A, "31", "Modified AGI", magi, "line 3")
        add(SCH_1A, "32", "Phase-out starts", SENIOR_FROM[k], "as printed")
        over = magi - SENIOR_FROM[k]
        cut = 0.0
        if over > 0:
            add(SCH_1A, "33", "MAGI over the start", over, "31 - 32")
            cut = add(
                SCH_1A, "34", "Reduction", over * SENIOR_RATE, f"33 x {SENIOR_RATE:.0%}"
            )
        l35 = add(
            SCH_1A,
            "35",
            "Deduction before the age test",
            max(SENIOR_AMOUNT - cut, 0.0),
            f"{SENIOR_AMOUNT:,.0f} - 34"
            if over > 0
            else f"{SENIOR_AMOUNT:,.0f} (line 33 is zero or less)",
        )
        l36a = add(
            SCH_1A,
            "36a",
            "Your deduction",
            l35 if head_65 else 0.0,
            f"line 35 (65 by year end, age {hh.age}; a valid SSN is assumed)"
            if head_65
            else f"0 (age {hh.age} at year end, under {SENIOR_AGE})",
        )
        l36b = 0.0
        if k and spouse_age is not None:
            l36b = add(
                SCH_1A,
                "36b",
                "Your spouse's deduction",
                l35 if spouse_65 else 0.0,
                f"line 35 (65 by year end, age {spouse_age}; a valid SSN is assumed)"
                if spouse_65
                else f"0 (age {spouse_age} at year end, under {SENIOR_AGE})",
            )
        form["37"] = add(
            SCH_1A,
            "37",
            "Enhanced deduction for seniors",
            l36a + l36b,
            "36a + 36b" if k and spouse_age is not None else "36a",
        )
    elif head_65:
        skipped.add("37")
    if k and spouse_age is None:  # the spouse may be 65 whatever the filer's age
        d.notes.append(
            f"Schedule 1-A line 36b: a joint return adds the spouse's "
            f"${SENIOR_AMOUNT:,.0f} if born before January 2, {d.year - 64}; the "
            "spouse's birth date is not on file, so it is not drafted (planner "
            "enter spouse_birth_date)"
        )
    if skipped:
        d.notes.append(
            "Schedule 1-A: tips, overtime and the senior deduction need a joint "
            "return if married; none is taken for married filing separately"
        )
    total = add(
        SCH_1A,
        "38",
        "Total additional deductions",
        sum(form.values()),
        "13 + 21 + 30 + 37",
    )
    engine = {line: v[name] for line, name in ENGINE_LINE.items()}
    form_rounded: dict[str, float] = {}  # engine variable -> the form's figure
    for line, want in engine.items():
        got = form.get(line, 0.0)
        gap = got - want
        if abs(gap) <= TOLERANCE:
            continue
        if line in ("13", "21") and abs(gap) <= ROUNDING_GAP:
            form_rounded[ENGINE_LINE[line]] = got
            d.notes.append(
                f"Schedule 1-A line {line}: the form rounds the phase-out down to "
                f"whole $1,000s ({got:,.2f}); the engine phases out smoothly "
                f"({want:,.2f}); the draft follows the form, so line 15 is "
                f"{abs(gap):,.2f} lower and line 16 is priced on that lower income"
            )
        else:
            d.notes.append(
                f"CHECK: Schedule 1-A line {line} {got:,.2f} vs engine {want:,.2f}"
            )
    return total, form_rounded


INTEREST_BOXES = (("1099-INT", "1"), ("1099-INT", "3"))
DIVIDEND_BOXES = (("1099-DIV", "1a"),)


def _payers(
    sheet: _Sheet,
    line: str,
    facts: list[db.FactRow],
    pairs: tuple[tuple[str, str], ...],
    total: float,
    origin: str,
) -> float:
    """One Schedule B row per payer from its forms' boxes; a figure the forms
    do not show (typed on the Needed panel) is one more row to name."""
    payers = sorted({f.issuer for f in facts if (f.form, f.box) in pairs})
    shown = 0.0
    for name in payers:
        mine = [f for f in facts if f.issuer == name]
        shown += sheet.add("Sch B", line, name, _sum(mine, pairs), _cited(mine, pairs))
    rest = round(total - shown, 2)
    if abs(rest) > TOLERANCE:
        sheet.add(
            "Sch B",
            line,
            "No form names this payer: write the payer's name",
            rest,
            f"{origin} less the forms",
        )
        shown += rest
    return round(shown, 2)


def _schedule_b(
    sheet: _Sheet,
    d: Draft,
    facts: list[db.FactRow],
    l2b: float,
    l3b: float,
    foreign: object,
) -> None:
    """Schedule B when interest or ordinary dividends are over $1,500: a row
    per payer, lines 4 and 6 tied to 1040 lines 2b and 3b, Part III from the
    Needed panel's typed answer."""
    if not schedule_b_required(l2b, l3b):
        d.notes.append(
            f"Schedule B is not required: taxable interest {l2b:,.2f} and ordinary "
            f"dividends {l3b:,.2f} are each $1,500 or less"
        )
        return
    add = sheet.add
    l1 = _payers(sheet, "1", facts, INTEREST_BOXES, l2b, "1040 line 2b")
    l2 = add("Sch B", "2", "Total interest", l1, "sum of line 1")
    l3 = add(
        "Sch B",
        "3",
        "Excludable savings bond interest (Form 8815)",
        0.0,
        "Form 8815 is not drafted: no education exclusion is taken",
    )
    add("Sch B", "4", "Taxable interest", l2 - l3, "2 - 3; to 1040 line 2b")
    l5 = _payers(sheet, "5", facts, DIVIDEND_BOXES, l3b, "1040 line 3b")
    add("Sch B", "6", "Total ordinary dividends", l5, "sum of line 5; to 1040 line 3b")
    if foreign not in ("yes", "no"):
        d.unknown.append("foreign_accounts")
        d.notes.append(
            "Schedule B Part III (foreign accounts and trusts, lines 7a-8) is "
            "unanswered: type foreign_accounts on the Needed panel"
        )
        return
    word = "Yes" if foreign == "yes" else "No"
    src = "Needed panel foreign_accounts"
    add(
        "Sch B",
        "7a",
        f"Foreign account interest or signature authority: {word}",
        0.0,
        src,
    )
    add(
        "Sch B",
        "8",
        f"Foreign trust distribution, grantor or transferor: {word}",
        0.0,
        src,
    )
    if foreign == "yes":
        d.notes.append(
            "Schedule B Part III: a yes needs line 7a's FinCEN Form 114 (FBAR) "
            "question, line 7b's country and line 8 settled account by account; "
            "an FBAR is due when the accounts together top $10,000 at any time"
        )


def _schedule_d(
    sheet: _Sheet,
    d: Draft,
    cg: capgains.CapGains,
    l7a: float,
    taxable: float,
    limit: float,
) -> None:
    """Form 8949 rows, the Schedule D lines and, for a loss beyond the yearly
    limit, the carryover worksheet for next year."""
    seen = {"A": 0, "D": 0}
    for lot in cg.lots:
        seen[lot.box] += 1
        wash = f"; W +{lot.adjustment:,.2f}" if lot.code else ""
        sheet.add(
            "8949",
            f"{lot.box}{seen[lot.box]}",
            f"{lot.description} {lot.acquired} to {lot.sold}",
            lot.gain,
            f"proceeds {lot.proceeds:,.2f} basis {lot.basis:,.2f}{wash}",
        )
    for line, value in cg.lines.items():
        sheet.add("Sch D", line, capgains.LABELS[line], value, cg.sources[line])
    d.notes.extend(cg.notes)
    d.unknown.extend(cg.unknown)
    l16 = cg.lines.get("16", 0.0)
    want = l16 if l16 >= 0 else max(l16, -limit)
    if l16 < 0:
        sheet.add("Sch D", "21", "Loss allowed this year", l7a, "1040 line 7a")
        st, lt = capgains.carryover(cg.lines["7"], cg.lines["15"], l16, l7a, taxable)
        for line, label, value in (("8", "Short-term", st), ("13", "Long-term", lt)):
            sheet.add(
                "Carryover",
                line,
                f"{label} loss carried to {cg.year + 1}",
                value,
                "Capital Loss Carryover Worksheet (Schedule D instructions)",
            )
    if abs(l7a - want) > TOLERANCE:
        d.notes.append(
            f"CHECK: 1040 line 7a {l7a:,.2f} vs Schedule D line 16 {l16:,.2f}: the "
            "household's gains are not this Schedule D (typed or still an estimate)"
        )


ORDER = (
    "1040",
    "Sch 1",
    SCH_1A,
    "Sch 2",
    "Sch 3",
    SCH_8812,
    "Sch B",
    "Sch C",
    "Sch D",
    "Sch SE",
    SCH_SE_SPOUSE,
    "8949",
    "2441",
    "8863",
    "8880",
    "EIC",
    SCH_EIC,
    "8889",
    hsa.SPOUSE_FORM,
    "8962",
    d400.FORM,
    d400.SCHED,
)
HEADINGS = {
    "1040": "Form 1040",
    "8949": "Form 8949",
    "8889": "Form 8889 (health savings accounts)",
    hsa.SPOUSE_FORM: "Form 8889 (health savings accounts), the spouse's",
    "8962": "Form 8962",
    "2441": "Form 2441 (child and dependent care expenses)",
    "8863": "Form 8863 (education credits)",
    "8880": "Form 8880 (credit for qualified retirement savings contributions)",
    "EIC": "EIC Worksheet (Form 1040 line 27a: A, or B when self-employed)",
    SCH_EIC: "Schedule EIC (qualifying child information)",
    d400.FORM: "NC Form D-400",
    d400.SCHED: "NC D-400 Schedule S (additions and deductions)",
    "Carryover": "Capital loss carryover to next year",
}
# The capability row behind each form's tag; any other form is draft_return's.
FORM_CAPABILITY = {
    SCH_1A: "schedule_1a",
    "Sch B": "schedule_b",
    "Sch C": "schedule_c",
    "Sch D": "schedule_d",
    "8949": "schedule_d",
    "Sch SE": "self_employment_tax",
    SCH_SE_SPOUSE: "self_employment_tax",
    "8889": "form_8889",
    hsa.SPOUSE_FORM: "form_8889",
    "8962": "aca_premium_tax_credit",
    "2441": "child_dependent_care_credit",
    "8863": "education_credits",
    "8880": "savers_credit",
    "EIC": "earned_income_credit",
    SCH_EIC: "earned_income_credit",
    d400.FORM: "nc_d400_draft",
    d400.SCHED: "nc_d400_draft",
    "Carryover": "schedule_d",
}


def render(d: Draft) -> str:
    out = [
        f"Draft {d.year} return (policyengine-us {d.engine_version}); line "
        "numbers follow the 2025 forms. A draft to check against the forms, not "
        "a filing.",
    ]
    if d.not_ready:
        out.append(d.not_ready)  # the blocker first, then the standing notice
    out.append(NOTICE)
    for form in (*ORDER, "Carryover"):
        lines = [ln for ln in d.lines if ln.form == form]
        if not lines:
            continue
        out.append("")
        out.append(f"{HEADINGS.get(form, f'Schedule {form[4:]}')} [{d.tag(form)}]")
        for ln in lines:
            places = 2 if round(ln.value, 2) == ln.value else 4
            out.append(
                f"  {ln.line:5} {ln.label:52.52} {ln.value:>14,.{places}f}  {ln.source}"
            )
    if d.estimates:
        out.append(f"\nestimates (YTD stands in): {', '.join(d.estimates)}")
    if d.unknown:
        out.append(f"unknown (left out, not zero): {', '.join(d.unknown)}")
    if d.missing:
        out.append(f"forms still to come: {'; '.join(d.missing)}")
    out.extend(f"note: {n}" for n in d.notes)
    return "\n".join(out) + "\n"
