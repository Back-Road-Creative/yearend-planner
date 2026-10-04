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
from dataclasses import dataclass, field

from planner.engine import tax
from planner.ingest.needs import need_values
from planner.ledger import db
from planner.paths import Layout
from planner.plan import esttax, inputs
from planner.taxprep import capgains, d400, hsa
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
    "alternative_minimum_tax",
    "non_refundable_ctc",
    "income_tax_non_refundable_credits",
    "self_employment_tax",
    "additional_medicare_tax",
    "net_investment_income_tax",
    "eitc",
    "refundable_ctc",
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
# The Needed-panel keys a return reads (the others drive the plan, not the 1040).
TAX_KEYS = (*inputs.MONEY, "ordinary_dividends", "qualified_dividends")


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
            f"{f.form} box {f.box} ({f.issuer})"
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
    pct = tax.poverty_percent(v["aca_magi_fraction"])
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
    eligible = v["is_aca_ptc_eligible"] > 0
    if not eligible:
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
        typed = need_values(conn, lay, year, d400.KEYS)
        cg = capgains.build(conn, lay, year)
        h = hsa.build(conn, lay, year)
    finally:
        conn.close()
    paid = [p for p in pays if p.agency == "fed"]
    nc_paid = [(p.date, p.amount, p.origin) for p in pays if p.agency == "nc"]
    hh = inp.household
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
    v = tax.values(year, hh, (*ENGINE, *d400.ENGINE), PRIOR)
    d = Draft(
        year,
        tax.engine_version(),
        notes=list(inp.notes),
        estimates=list(inp.estimates),
        unknown=[k for k in inp.unknown if k in TAX_KEYS],
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

    # Schedule 1
    s1_3 = add(
        "Sch 1",
        "3",
        "Business income",
        v["self_employment_income"],
        origin("se_income"),
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
        src = (
            "premiums less the premium tax credit, settled with it (IRS Pub. 974)"
            if var == SEHI and hh.se_health_premiums
            else f"engine {var}"
        )
        named += add("Sch 1", ln, label, v[var], src)
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

    # Schedule SE
    if v["self_employment_income"]:
        add("Sch SE", "2", "Net profit", v["self_employment_income"], "Sch 1 line 3")
        add(
            "Sch SE",
            "4a",
            "Net earnings (x 92.35%)",
            v["taxable_self_employment_income"],
            "engine taxable_self_employment_income",
        )
        add(
            "Sch SE",
            "12",
            "Self-employment tax",
            v["self_employment_tax"],
            "engine self_employment_tax",
        )
        add(
            "Sch SE",
            "13",
            "Deduction for half of SE tax",
            v["self_employment_tax_ald"],
            "engine self_employment_tax_ald",
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
        "Sch 2", "4", "Self-employment tax", v["self_employment_tax"], "Sch SE line 12"
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
    l13b = add(
        f,
        "13b",
        "Schedule 1-A deductions (incl. the senior deduction)",
        max(v["taxable_income_deductions"] - l12 - l13a, 0.0),
        "engine taxable_income_deductions less 12e and 13a",
    )
    l14 = add(f, "14", "Total deductions", l12 + l13a + l13b, "12e + 13a + 13b")
    l15 = add(f, "15", "Taxable income", max(l11 - l14, 0.0), "11b - 14")

    # Form 1040 tax and credits
    l16 = add(
        f,
        "16",
        "Tax",
        v["income_tax_before_credits"] - v["alternative_minimum_tax"],
        "engine income_tax_before_credits less AMT",
    )
    l17 = add(f, "17", "Amount from Schedule 2, line 3", s2_3, "Sch 2 line 3")
    l18 = add(f, "18", "Lines 16 and 17", l16 + l17, "16 + 17")
    l19 = add(
        f,
        "19",
        "Child tax credit",
        v["non_refundable_ctc"],
        "engine non_refundable_ctc",
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
        v["refundable_ctc"],
        "engine refundable_ctc",
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
    if hh.state == "NC":
        married = hh.filing_status == "JOINT"
        d400.lay_lines(add, d.notes, v, facts, nc_paid, year, l11, typed, married)
    else:
        d.notes.append(f"no state return drafted for {hh.state}: only NC's D-400 is")

    # The lines against the engine's own totals: a gap is a mapping the draft
    # missed, and is said, never hidden.
    for got, want, what in (
        (
            l9,
            v["irs_gross_income"] - v["loss_ald"],
            "line 9 against the engine's gross income less its loss deduction",
        ),
        (l11, v["adjusted_gross_income"], "line 11a against the engine's AGI"),
        (l15, v["taxable_income"], "line 15 against the engine's taxable income"),
        (
            l22 - s2_1a + s2_12 - (l27 + l28 + l29),
            v["income_tax"],
            "line 22 (less 1a, plus NIIT, less refundable credits) against the "
            "engine's income tax",
        ),
    ):
        if abs(got - want) > TOLERANCE:
            d.notes.append(f"CHECK: {what}: {got:,.2f} vs {want:,.2f}")
    if exempt:
        d.notes.append(
            "tax-exempt interest (1040 line 2a) counts toward Social Security "
            "taxation and ACA MAGI"
        )
    return d


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
    "Sch 2",
    "Sch 3",
    "Sch D",
    "Sch SE",
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


def render(d: Draft) -> str:
    out = [
        f"Draft {d.year} return (policyengine-us {d.engine_version}); line "
        "numbers follow the 2025 forms. A draft to check against the forms, not "
        "a filing.",
    ]
    for form in (*ORDER, "Carryover"):
        lines = [ln for ln in d.lines if ln.form == form]
        if not lines:
            continue
        out.append("")
        out.append(HEADINGS.get(form, f"Schedule {form[4:]}"))
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
