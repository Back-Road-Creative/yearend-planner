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

from planner import NOTICE, coverage
from planner.engine import tax
from planner.engine.household import Household
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
    "self_employment_tax",
    "additional_medicare_tax",
    "net_investment_income_tax",
    "eitc",
    "refundable_ctc",
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


def _form_8962(
    sheet: _Sheet, d: Draft, v: dict[str, float], facts: list[db.FactRow], fs: str
) -> tuple[float, float]:
    """Lay out Form 8962 from the 1095-A boxes summed across policies; returns
    (net credit for Schedule 3 line 9, excess repayment for Schedule 2 line 1a)."""
    box: dict[str, float] = {}
    for f in facts:
        if f.form == "1095-A":
            box[f.box] = box.get(f.box, 0.0) + f.value
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
    total_e = total_f = 0.0
    for m in MONTHS:
        a, b, f_ = (box.get(f"{k}_{m}", 0.0) for k in ("premium", "slcsp", "aptc"))
        if not (a or b or f_):
            continue
        most = max(b - monthly, 0.0)
        e = min(a, most) if eligible else 0.0
        ln = MONTH_LINE[m]
        sheet.add("8962", f"{ln}a", f"Month {m} enrollment premium", a, src)
        sheet.add("8962", f"{ln}b", f"Month {m} SLCSP premium", b, src)
        sheet.add("8962", f"{ln}d", f"Month {m} maximum assistance", most, "b - 8b")
        sheet.add("8962", f"{ln}e", f"Month {m} PTC allowed", e, "smaller of a, d")
        sheet.add("8962", f"{ln}f", f"Month {m} advance PTC", f_, src)
        total_e += e
        total_f += f_
    e24 = sheet.add("8962", "24", "Total PTC", total_e, "sum of column e")
    f25 = sheet.add("8962", "25", "Advance payment of PTC", total_f, "sum of column f")
    if e24 >= f25:
        return sheet.add("8962", "26", "Net PTC", e24 - f25, "24 - 25"), 0.0
    excess = sheet.add("8962", "27", "Excess advance PTC", f25 - e24, "25 - 24")
    cap = repayment_cap(d.year, fs, pct)
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
    if h.lines:
        other = dict(hh.other)
        if h.lines.get("16"):
            other["miscellaneous_income"] = other.get("miscellaneous_income", 0) + int(
                round(h.lines["16"])
            )
        hh = dataclasses.replace(
            hh, hsa_contribution=int(round(h.lines.get("13", 0.0))), other=other
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
    sheet = _Sheet(d)
    add = sheet.add

    def origin(key: str) -> str:
        if key in inp.unknown:
            return f"{key}: not given (left out, not zero)"
        return inp.origins.get(key, f"{key}: none")

    has_8962 = any(f.form == "1095-A" for f in facts)
    net_ptc, excess_aptc = (
        _form_8962(sheet, d, v, facts, hh.filing_status) if has_8962 else (0.0, 0.0)
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
    ses = [
        (form, *_schedule_se(sheet, d, v, who, form, gain, src, base))
        for who, form, gain, src, base in (
            ("you", "Sch SE", profit, profit_src, ss_wages),
            ("spouse", SCH_SE_SPOUSE, sp_profit, sp_src, ss_spouse),
        )
        if gain
    ]
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
    if h.lines.get("16"):
        add("Sch 1", "8f", "Income from Form 8889", h.lines["16"], "Form 8889 line 16")
        s1_9 = add("Sch 1", "9", "Total other income", h.lines["16"], "line 8f")
    s1_10 = add(
        "Sch 1", "10", "Additional income", s1_3 + s1_9, "3 + 9" if s1_9 else "line 3"
    )
    named = add(
        "Sch 1",
        "13",
        "HSA deduction",
        v["health_savings_account_ald"],
        "Form 8889 line 13" if h.lines else "engine health_savings_account_ald",
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
    if h.lines.get("17b"):
        add(
            "Sch 2",
            "17c",
            "Additional tax on HSA distributions",
            h.lines["17b"],
            "Form 8889 line 17b",
        )
        s2_18 = add("Sch 2", "18", "Total additional taxes", h.lines["17b"], "17c")
    s2_21 = add(
        "Sch 2",
        "21",
        "Total other taxes",
        s2_4 + s2_11 + s2_12 + s2_18,
        "4 + 11 + 12 + 18" if s2_18 else "4 + 11 + 12",
    )

    # Schedule 3
    s3_8 = add(
        "Sch 3",
        "8",
        "Nonrefundable credits (other than the child credit)",
        v["income_tax_non_refundable_credits"] - v["non_refundable_ctc"],
        "engine income_tax_non_refundable_credits less the child credit",
    )
    s3_9 = add("Sch 3", "9", "Net premium tax credit", net_ptc, "Form 8962 line 26")
    s3_15 = add("Sch 3", "15", "Other payments and refundable credits", s3_9, "line 9")

    # Form 1040 income
    f = "1040"
    l1z = add(f, "1z", "Wages", v["employment_income"], origin("wages"))
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
    ctc_14, ctc_27 = _schedule_8812(sheet, d, v, hh, l11, max(l18 - s3_8, 0.0))
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
    l27 = add(f, "27a", "Earned income credit", v["eitc"], "engine eitc")
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
        v["income_tax_refundable_credits"] - v["eitc"] - v["refundable_ctc"],
        "engine income_tax_refundable_credits less 27a and 28",
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
    d.notes.extend(h.notes)
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
            v["income_tax"] + table_gap,
            "line 22 (less 1a, plus NIIT, less refundable credits) against the "
            "engine's income tax (plus any Tax Table difference)",
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


def _schedule_8812(
    sheet: _Sheet,
    d: Draft,
    v: dict[str, float],
    hh: Household,
    agi: float,
    limit: float,
) -> tuple[float | None, float | None]:
    """Schedule 8812 for the household's dependents, and the 1040 dependents
    list as a note: (line 14, line 27), or None for a line not drafted. Lines
    4-17 are the form's own arithmetic, line 13 being `limit` (Credit Limit
    Worksheet A: 1040 line 18 less the Schedule 3 credits). Lines 18a-26
    (earned income, and Social Security tax with three or more children) are
    not drafted, so line 27 is the engine's, checked against line 17."""
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
    l27 = add(
        f,
        "27",
        "Additional child tax credit",
        v["refundable_ctc"],
        "engine refundable_ctc (lines 18a-26, earned income, not drafted)",
    )
    if l27 > l17 + TOLERANCE:
        d.notes.append(f"CHECK: Schedule 8812 line 27 {l27:,.2f} over line 17")
    return l14, l27


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
    "8889",
    "8962",
    d400.FORM,
    d400.SCHED,
)
HEADINGS = {
    "1040": "Form 1040",
    "8949": "Form 8949",
    "8889": "Form 8889 (health savings accounts)",
    "8962": "Form 8962",
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
    "8962": "aca_premium_tax_credit",
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
