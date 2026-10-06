"""The ledger as an engine household.

Every input comes from the Needed panel (a form, a YTD estimate or a typed
answer), so the household the planners price is the one the intake loop has
confirmed. Unknown is never zero: a missing item is left out and named in
``Inputs.unknown``; unknown qualified dividends are taxed as ordinary (the
worse case) and said so. Overrides are the few planning numbers no document
can supply: the Q4 dividend estimate, planned sales, a planned conversion (typed,
or ``conversion_target="auto"`` to adopt the recommended one), a planned HSA
contribution and a typed total income for the year, put on the line the owner
names. Each income stream's year-to-date figure is carried to December 31 by
its typed forecast (``planner.plan.forecast``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from planner import coverage
from planner.engine import tax
from planner.engine.household import (
    PERSON_INPUTS,
    PERSON_SAVERS,
    Dependent,
    Household,
    MissingInputError,
    Person,
    Student,
)
from planner.ingest.derive import CountyError, resolve_county
from planner.ingest.needs import _needed
from planner.ledger import db
from planner.paths import Layout
from planner.plan import forecast
from planner.taxprep import capgains, f8582, hsa, k1, sche, schf

FILING = {
    "single": "SINGLE",
    "married_joint": "JOINT",
    "married_separate": "SEPARATE",
    "head_of_household": "HEAD_OF_HOUSEHOLD",
    "qualifying_surviving_spouse": "SURVIVING_SPOUSE",
}
REQUIRED = ("birth_date", "filing_status", "state")
# Needed-panel key -> Household field, dollars rounded to whole dollars.
MONEY = {
    "wages": "wages",
    "se_income": "se_income",
    "interest": "interest",
    "tax_exempt_interest": "tax_exempt_interest",
    "unemployment": "unemployment",
    "state_refund_taxable": "salt_refund",
    "cancelled_debt": "cancelled_debt",
    "other_income": "other_income",
    "stock_option_income": "stock_option_income",
    "short_term_gains": "short_term_gains",
    "long_term_gains": "long_term_gains",
    "ira_distributions": "ira_distributions",
    "roth_conversion": "roth_conversion",
    "social_security": "social_security",
    "traditional_ira_contribution": "traditional_ira_contribution",
    "se_health_premiums": "se_health_premiums",
    "slcsp_monthly": "slcsp_monthly",
    "aptc": "aptc",
    "hsa_contribution": "hsa_contribution",
    "qualified_tips": "qualified_tips",
    "qualified_overtime": "qualified_overtime",
    "car_loan_interest": "car_loan_interest",
    "planned_giving": "charitable_cash",
    "real_estate_taxes": "real_estate_taxes",
    "mortgage_interest": "mortgage_interest",
    "care_expenses": "care_expenses",
    "dependent_care_benefits": "dependent_care_benefits",
    "dependent_care_grace": "dependent_care_grace",
    "dependent_care_forfeited": "dependent_care_forfeited",
    "roth_ira_contribution": "roth_ira_contribution",
    "elective_deferrals": "elective_deferrals",
    "savers_distributions": "savers_distributions",
}
# The four states a value can be in. Known includes a known zero; unknown is
# left out of the arithmetic (never priced as zero without saying so); not
# applicable is an item another answer makes moot, so it is never asked.
KNOWN, ESTIMATE, UNKNOWN, NOT_APPLICABLE = (
    "known",
    "estimate",
    "unknown",
    "not applicable",
)
# The income lines a typed total_income is measured against: Form 1040 line 9
# less wages (the line the total sets) and Social Security (the engine decides
# how much of it is taxable, so it is never part of a typed total).
TOTAL_INCOME_LINES = (
    "se_income",
    "interest",
    "non_qualified_dividends",
    "qualified_dividends",
    "ira_distributions",
    "roth_conversion",
    "unemployment",
    "salt_refund",
    "cancelled_debt",
    "other_income",
    "stock_option_income",
)
# The lines a typed total_income can be put on: the residual is ordinary income
# of the owner's own naming, never wages by default when wages are known.
TOTAL_INCOME_TO = (
    "wages",
    "se_income",
    "interest",
    "non_qualified_dividends",
    "ira_distributions",
)
# Short- and long-term gains reach Form 1040 line 7 as one net figure, and a net
# loss counts only up to this much a year (half for married filing separately):
# IRC 1211(b); Schedule D instructions, line 21.
CAPITAL_LOSS_LIMIT = 3000
CAPITAL_LOSS_LIMIT_MFS = 1500
CONVERSION_TARGETS = ("manual", "auto")
# Needed-panel key -> Household field, a whole-number code rather than dollars.
CODES = {"tipped_occupation_code": "tipped_occupation_code"}
# The Needed-panel keys a tax figure reads (the others drive the plan, not the tax).
TAX_KEYS = (
    *MONEY,
    *CODES,
    "ordinary_dividends",
    "qualified_dividends",
    "education",
    "rentals",
    "k1s",
    "farms",
    "collectibles",
    "unrecaptured_1250",
    "rental_active",
    "lived_apart",
    "rental_qbi",
    "aotc_refundable_barred",
    "savers_barred",
)
# A joint spouse's own Form 8880 column rests the credit on them as the head's does.
TAX_KEYS = (*TAX_KEYS, *(f"spouse_{k}" for k in (*PERSON_SAVERS, "savers_barred")))
# Schedule D line -> the engine's tax unit input
RATE_GAINS = {
    "18": "capital_gains_28_percent_rate_gain",
    "19": "unrecaptured_section_1250_gain",
}
OVERRIDES = (
    "q4_dividend_estimate",
    "planned_st_sales",
    "planned_lt_sales",
    "planned_conversion",
    "planned_hsa",
)


class OverrideError(ValueError):
    """A typed planning number that cannot be applied, with what to change."""


@dataclass(frozen=True)
class Overrides:
    """Planning numbers added on top of the ledger (dollars). ``planned_hsa``
    replaces the year's HSA figure; the others add to it.

    ``total_income`` is the year's total income before the planned items above
    (the owner's own full-year figure, for when the YTD ledger lags): wages
    become the total less the other income lines the ledger counts; with
    ``total_income_line`` that residual goes on the named line instead.
    ``conversion_target`` is ``manual`` (the typed ``planned_conversion``) or
    ``auto`` (the plan adopts the recommended conversion; typing one as well
    is refused)."""

    q4_dividend_estimate: float = 0.0
    planned_st_sales: float = 0.0
    planned_lt_sales: float = 0.0
    planned_conversion: float = 0.0
    planned_hsa: float | None = None
    total_income: float | None = None
    conversion_target: str = "manual"
    total_income_line: str = "wages"

    def __post_init__(self) -> None:
        if self.total_income_line not in TOTAL_INCOME_TO:
            raise OverrideError(
                f"total_income_line must be one of {', '.join(TOTAL_INCOME_TO)}, "
                f"got {self.total_income_line!r}"
            )
        if self.conversion_target not in CONVERSION_TARGETS:
            raise OverrideError(
                f"conversion_target must be {' or '.join(CONVERSION_TARGETS)}, "
                f"got {self.conversion_target!r}"
            )

    def describe(self) -> list[str]:
        out = [
            f"{k} {getattr(self, k):,.0f}" for k in OVERRIDES[:4] if getattr(self, k)
        ]
        if self.planned_hsa is not None:
            out.append(f"planned_hsa {self.planned_hsa:,.0f}")
        if self.total_income is not None:
            out.append(f"total_income {self.total_income:,.0f}")
            if self.total_income_line != "wages":
                out.append(f"total_income_line {self.total_income_line}")
        if self.conversion_target == "auto":
            out.append("conversion_target auto")
        return out


@dataclass
class Inputs:
    year: int
    household: Household
    origins: dict[str, str] = field(default_factory=dict)  # key -> where it came from
    estimates: list[str] = field(default_factory=list)  # keys standing in from YTD
    unknown: list[str] = field(default_factory=list)  # keys left out (not zero)
    # Age for the tax tests that count a filer as one year older on the day
    # before the birthday (65 and over: the standard deduction, Schedule 1-A).
    tax_age: int | None = None
    spouse_tax_age: int | None = None  # the same, for a joint return's spouse
    spouse_death: str | None = None  # a joint spouse who died in the year (3b-3)
    married: str | None = None  # a joint return's marriage date in the year (3b-4)
    spouse_kids: int = 0  # dependents on the spouse's side before the marriage
    notes: list[str] = field(default_factory=list)
    overrides: Overrides = field(default_factory=Overrides)
    forecast: list[forecast.Stream] = field(default_factory=list)
    ranges: dict[str, tuple[float, float]] = field(default_factory=dict)
    scope: list[str] = field(default_factory=list)  # Not handled lines (also notes)
    coverage: list[coverage.Gap] = field(default_factory=list)  # every gap (2a)
    schedule_e: sche.Result | None = None  # Schedule E Part I (3e-2b)
    schedule_k1: k1.Result | None = None  # Schedule E Parts II, III (3e-3a)
    form_8582: f8582.Result | None = None  # passive activity losses (3e-4)
    schedule_f: schf.Result | None = None  # farming (3e-5)
    # (word, payer, amount, source) for each K-1 portfolio box (3e-3b)
    k1_portfolio: list[tuple[str, str, float, str]] = field(default_factory=list)

    def state(self, key: str) -> str:
        """KNOWN, ESTIMATE, UNKNOWN or NOT_APPLICABLE (an item never asked)."""
        if key in self.unknown:
            return UNKNOWN
        if key in self.estimates:
            return ESTIMATE
        return KNOWN if key in self.origins else NOT_APPLICABLE

    @property
    def tax_unknown(self) -> list[str]:
        """The unknown items a tax figure rests on (left out, not zero)."""
        return [k for k in self.unknown if k in TAX_KEYS]


def age_on(birth: str, day: date) -> int:
    b = date.fromisoformat(birth)
    return day.year - b.year - ((day.month, day.day) < (b.month, b.day))


def age_at_year_end(birth: str, year: int) -> int:
    return age_on(birth, date(year, 12, 31))


def tax_age(birth: str, year: int) -> int:
    """Age on December 31 for the tests that count a filer as reaching an age
    on the day before the birthday (IRS Pub. 501, "considered 65 on the day
    before your 65th birthday"): someone born January 1 is a year older than
    the calendar says. Contribution limits, glide paths and the plan keep
    ``age_at_year_end``."""
    b = date.fromisoformat(birth)
    return age_at_year_end(birth, year) + ((b.month, b.day) == (1, 1))


def _recorded_conversions(conn: sqlite3.Connection, year: int) -> float:
    return round(
        sum(c.amount for c in db.conversions(conn) if c.date.startswith(f"{year}-")),
        2,
    )


def _county_key(lay: Layout, value: dict[str, Any]) -> str | None:
    """The engine's name for the household's county, or None when unknown (the
    engine then prices the state's first county; the Needed panel asks)."""
    if value.get("county") is None:
        return None
    try:
        return resolve_county(lay.config, str(value["county"]), str(value["state"]))
    except CountyError as exc:
        raise MissingInputError(f"county: {exc} (planner enter county)") from exc


def _students(
    value: dict[str, Any], fields: dict[str, Any], notes: list[str]
) -> tuple[Student, ...]:
    """The typed students (the ``education`` answer) as the engine's people:
    the expenses less the tax-free assistance (Form 8863's adjusted qualified
    education expenses, never below zero). A student the return does not
    have (a spouse off a joint return, a dependent past the last) is left
    out, with a note."""
    who = {"you": "p"} | ({"spouse": "s"} if "spouse" in fields else {})
    who |= {f"dependent {i}": f"d{i}" for i in range(1, len(fields["dependents"]) + 1)}
    out = []
    for e in value.get("education") or ():
        if e["student"] not in who:
            notes.append(
                f"education: {e['student']} is not on this return (the spouse is "
                "only on a joint return; dependents count from 1 in the order "
                f"typed): no credit for that student; check education"
            )
            continue
        out.append(
            Student(
                who[e["student"]], max(int(e["paid"]) - int(e["aid"]), 0), e["credit"]
            )
        )
    return tuple(out)


def build(
    lay: Layout, year: int, overrides: Overrides | None = None, *, with_db: bool = True
) -> Inputs:
    """The household the planners price, from the Needed panel plus overrides."""
    ov = overrides or Overrides()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        cg = capgains.store(conn, lay, year)  # typed carryovers reach Schedule D
        for who in hsa.WHO:  # and typed HSA answers reach each Form 8889
            hsa.store(conn, lay, year, who=who)
        report = _needed(conn, lay, year)
        recorded = _recorded_conversions(conn, year)
        through = forecast.ytd_through(conn, year)
    finally:
        conn.close()
    value: dict[str, Any] = {}
    out = Inputs(year, None, overrides=ov)  # type: ignore[arg-type]
    for st in report.items:
        key = st.need.key
        if st.state in ("actual", "estimate"):
            value[key] = st.value
            out.origins[key] = st.origin
            if st.state == "estimate":
                out.estimates.append(key)
        elif not key.startswith("account:"):
            out.unknown.append(key)
    _forecast(lay, year, report, value, out, through)
    tips = value.get("qualified_tips")
    if not (tips and float(tips) > 0) and "tipped_occupation_code" in out.unknown:
        out.unknown.remove("tipped_occupation_code")  # moot without tips
    missing = [k for k in REQUIRED if value.get(k) is None]
    if missing:
        raise MissingInputError(
            f"the plan needs {', '.join(missing)} (planner needed --year {year})"
        )
    fields: dict[str, Any] = {
        "age": age_at_year_end(str(value["birth_date"]), year),
        "filing_status": FILING[str(value["filing_status"])],
        "state": str(value["state"]),
        "county": _county_key(lay, value),
    }
    for key, name in MONEY.items():
        if value.get(key) is not None:
            fields[name] = int(round(float(value[key])))
    for key, name in CODES.items():
        if value.get(key) is not None:
            fields[name] = int(value[key])
    out.tax_age = tax_age(str(value["birth_date"]), year)
    if fields["filing_status"] == "JOINT" and value.get("spouse_birth_date"):
        spouse = str(value["spouse_birth_date"])
        money: dict[str, Any] = {
            k: int(round(float(value[f"spouse_{k}"])))
            for k in (*PERSON_INPUTS, *PERSON_SAVERS)
            if value.get(f"spouse_{k}") is not None
        }
        fields["spouse"] = Person(age=age_at_year_end(spouse, year), **money)
        out.spouse_tax_age = tax_age(spouse, year)
        died = value.get("spouse_death_date")
        if died and died != "none" and date.fromisoformat(str(died)).year == year:
            # A spouse who died in the year is the age they were at death, and
            # 65 only if 65 then, reached the day before the birthday (Pub. 501).
            death = date.fromisoformat(str(died))
            fields["spouse"] = replace(fields["spouse"], age=age_on(spouse, death))
            out.spouse_tax_age = age_on(spouse, death + timedelta(days=1))
            out.spouse_death = death.isoformat()
            out.notes.append(
                f"the spouse died {death.isoformat()}: the joint return carries the "
                f"spouse's income to that date and yours for all of {year}; type "
                "the spouse's figures to that date. For "
                f"{year + 1} and {year + 2} the status is qualifying_surviving_spouse "
                "while a dependent child lives at home, otherwise single or "
                "head_of_household (2025 Form 1040 instructions)"
            )
    fields["dependents"] = tuple(
        Dependent(
            age=age_at_year_end(str(d["birth_date"]), year),
            full_time_student=bool(d.get("student")),
            disabled=bool(d.get("disabled")),
        )
        for d in value.get("dependents") or ()
    )
    # Form 8880: the credit needs 18 (born on or before January 1, 18 years
    # back) and neither a dependent on another return nor a student.
    fields["savers_eligible"] = (
        out.tax_age >= 18 and value.get("savers_barred") != "yes"
    )
    if fields.get("spouse") is not None and out.spouse_tax_age is not None:
        fields["spouse"] = replace(
            fields["spouse"],
            savers_eligible=out.spouse_tax_age >= 18
            and value.get("spouse_savers_barred") != "yes",
        )
    fields["students"] = _students(value, fields, out.notes)
    fields["aotc_refundable_barred"] = value.get("aotc_refundable_barred") == "yes"
    wed = value.get("marriage_date")
    if (
        fields["filing_status"] == "JOINT"
        and wed
        and wed != "none"
        and date.fromisoformat(str(wed)).year == year
    ):
        out.married = str(wed)
        kids, n = (
            int(value.get("spouse_premarriage_dependents") or 0),
            len(fields["dependents"]),
        )
        out.spouse_kids = min(kids, n)
        if kids > n:
            out.notes.append(
                f"spouse_premarriage_dependents is {kids} but the return has {n} "
                f"dependents: the year-of-marriage calculation counts {n} on your "
                "spouse's side; check spouse_premarriage_dependents"
            )
    out.coverage = coverage.gate(
        lay,
        fields["state"],
        fields["filing_status"],
        spouse="spouse" in fields,
        dependents=len(fields["dependents"]),
        death_year=coverage.death_year(value.get("spouse_death_date")),
        year=year,
        residency=value.get("state_residency"),
        local=value.get("local_income_tax"),
    )
    out.scope = [g.reason for g in out.coverage if g.area == "household"]
    out.notes.extend(g.reason for g in out.coverage)
    ordinary = value.get("ordinary_dividends")
    qualified = value.get("qualified_dividends")
    if ordinary is not None and qualified is None:
        fields["non_qualified_dividends"] = int(round(float(ordinary)))
        out.notes.append(
            "qualified_dividends unknown: all dividends priced as ordinary "
            "(the worse case) until the 1099-DIV or a typed figure arrives"
        )
    elif ordinary is not None and qualified is not None:
        q = int(round(float(qualified)))
        fields["qualified_dividends"] = q
        fields["non_qualified_dividends"] = max(int(round(float(ordinary))) - q, 0)
    elif qualified is not None:
        fields["qualified_dividends"] = int(round(float(qualified)))
    if value.get("k1s"):
        _k1_portfolio(out, fields, value["k1s"])
    if recorded > fields.get("roth_conversion", 0):
        fields["roth_conversion"] = int(round(recorded))
        out.origins["roth_conversion"] = "conversions recorded this year"
    if ov.total_income is not None:
        line = ov.total_income_line
        if line in {s.key for s in out.forecast} or (
            line == "non_qualified_dividends"
            and "ordinary_dividends" in {s.key for s in out.forecast}
        ):
            raise OverrideError(
                f"total_income_line {line} has its own forecast; drop one of them"
            )
        others = _counted(fields, skip=line)
        rest = int(round(ov.total_income)) - others
        if rest < 0:
            raise OverrideError(
                f"total_income {ov.total_income:,.0f} is below the other income "
                f"already counted ({others:,.0f}); type a total of at least that"
            )
        fields[line] = rest
        key = "ordinary_dividends" if line == "non_qualified_dividends" else line
        out.origins[key] = "total_income override (total less the other income)"
        if key in out.unknown:
            out.unknown.remove(key)
        if key in out.estimates:
            out.estimates.remove(key)
        out.notes.append(
            f"total_income {ov.total_income:,.0f} typed: {line} {rest:,} = the total "
            f"less {others:,} of other income counted (Social Security and "
            "Schedule E excluded); "
            "planned items are added on top"
        )
    # overrides
    if ov.q4_dividend_estimate and any(
        s.key == "ordinary_dividends" for s in out.forecast
    ):
        raise OverrideError(
            "q4_dividend_estimate and a dividend forecast would count the same "
            "dividends twice; drop the q4_dividend_estimate"
        )
    if ov.q4_dividend_estimate:
        target = (
            "qualified_dividends"
            if qualified is not None
            else "non_qualified_dividends"
        )
        fields[target] = fields.get(target, 0) + int(round(ov.q4_dividend_estimate))
    if ov.planned_st_sales:
        fields["short_term_gains"] = fields.get("short_term_gains", 0) + int(
            round(ov.planned_st_sales)
        )
    if ov.planned_lt_sales:
        fields["long_term_gains"] = fields.get("long_term_gains", 0) + int(
            round(ov.planned_lt_sales)
        )
    if ov.planned_conversion:
        fields["roth_conversion"] = fields.get("roth_conversion", 0) + int(
            round(ov.planned_conversion)
        )
    if ov.planned_hsa is not None:
        fields["hsa_contribution"] = int(round(ov.planned_hsa))
    if value.get("rentals") or value.get("k1s") or value.get("farms"):
        _schedule_e(out, fields, value)
    # Schedule D lines 18 and 19 price the 28% and 25% gains by the Schedule D
    # Tax Worksheet (planner.engine.tax, unit 3e-6b1)
    rate_gains = {
        var: cg.lines[line] for line, var in RATE_GAINS.items() if line in cg.lines
    }
    # Form 4797 line 18b, Schedule 1 line 4 (unit 3e-6c)
    if cg.form4797 is not None and cg.form4797.line18b:
        rate_gains["other_net_gain"] = cg.form4797.line18b
    # The foreign tax credit (unit 3e-7): the tax itself, the engine capping
    # it at the whole tax; the draft applies Form 1116's limit
    foreign = float(value.get("foreign_tax_paid") or 0)
    if foreign:
        rate_gains["foreign_tax_credit_potential"] = foreign + float(
            value.get("foreign_tax_carryover") or 0
        )
    # Form 6251 line 2i (unit 3e-8): the incentive stock options exercised
    iso = float(value.get("iso_amt_adjustment") or 0)
    if iso:
        rate_gains[tax.ISO] = iso
    if rate_gains:
        fields["tax_unit_inputs"] = {**fields.get("tax_unit_inputs", {}), **rate_gains}
    out.household = Household(**fields)
    return out


def _schedule_e(out: Inputs, fields: dict[str, Any], value: dict[str, Any]) -> None:
    """Schedule E: Part I from the rentals (unit 3e-2b), Parts II and III from
    the K-1s (unit 3e-3a), Schedule F from the farms (unit 3e-5), their
    passive losses through one Form 8582 (unit 3e-4)."""
    cols = sche.columns(
        (value.get("rentals") or []) + k1.royalties(value.get("k1s") or [])
    )
    rows = k1.rows(value.get("k1s") or [])
    fs = schf.farms(value.get("farms") or [])
    active = value.get("rental_active")
    acts = (
        sche.activities(cols, active=active == "yes")
        + k1.activities(rows)
        + schf.activities(fs)
    )
    magi = (
        _counted(fields)
        + sche.magi_part(cols)
        + k1.nonpassive_net(rows)
        + schf.magi_part(fs)
        - fields.get("hsa_contribution", 0)
        - fields.get("se_health_premiums", 0)
    )
    separate = None
    if fields["filing_status"] == "SEPARATE":
        separate = "apart" if value.get("lived_apart") == "yes" else "together"
    form = f8582.compute(acts, magi=magi, separate=separate)
    if acts:
        out.form_8582 = form
        out.notes.extend(form.notes)
        if active is None and any(a.name.startswith("rental") for a in acts):
            out.notes.append(
                "Form 8582 puts the rentals in Part V, with no special allowance, "
                "until rental_active is answered"
            )
        if separate == "together" and value.get("lived_apart") is None:
            out.notes.append(
                "Form 8582 takes married filing separately as living together "
                "(no special allowance) until lived_apart is answered"
            )
    if cols:
        out.schedule_e = sche.schedule(cols, form.allowed)
        fields["rental_income"] = int(round(out.schedule_e.total))
        fields["rental_qbi"] = value.get("rental_qbi") == "yes"
        if fields["rental_qbi"] and any(c.kind == "royalty" for c in cols):
            out.notes.append(
                "the engine's QBI deduction counts the royalties' net with the "
                "rentals; Form 8995 line 1 takes only the qualifying rentals "
                "(royalties are investment income, not a trade or business)"
            )
        out.notes.extend(out.schedule_e.notes)
    if rows:
        out.schedule_k1 = k1.schedule(rows, form.allowed)
        _k1_fields(out, fields, value["k1s"], out.schedule_k1)
    if fs:
        _farm_fields(out, fields, schf.schedule(fs, form.allowed))
    if sum(a.overall for a in acts) < 0:
        out.notes.append(
            f"Form 8582 line 6 modified AGI {magi:,.0f}: AGI without the "
            "passive losses, taxable Social Security, the IRA deduction "
            "or the deductible part of SE tax, from the household's own "
            "income lines"
        )


def _farm_fields(out: Inputs, fields: dict[str, Any], r: schf.Result) -> None:
    """Each owner's Schedule F line 34s, the engine's farm operations income."""
    out.schedule_f = r
    out.notes.extend(r.notes)
    fields["farm_income"] = int(round(r.by_owner["you"]))
    spouse = int(round(r.by_owner["spouse"]))
    if spouse and fields.get("spouse") is not None:
        fields["spouse"] = replace(fields["spouse"], farm_income=spouse)
    elif spouse:
        out.notes.append(
            "a farm marked spouse is not on this return (the spouse is not on "
            "it): its Schedule F is drafted but left out of the tax"
        )


def _k1_portfolio(out: Inputs, fields: dict[str, Any], entries: list[Any]) -> None:
    """The K-1 interest and dividends on top of the 1099 figures (unit 3e-3b).
    The K-1 gains reach Schedule D lines 5 and 12 (planner.taxprep.capgains),
    the royalties Schedule E line 4."""
    out.k1_portfolio = k1.portfolio(entries)
    got = dict.fromkeys(k1.PORTFOLIO, 0.0)
    for word, _, amount, _ in out.k1_portfolio:
        got[word] += amount
    if got["interest"]:
        fields["interest"] = fields.get("interest", 0) + int(round(got["interest"]))
    if got["dividends"]:
        q = int(round(got["qualified"]))
        ordinary = int(round(got["dividends"])) - q
        fields["qualified_dividends"] = fields.get("qualified_dividends", 0) + q
        fields["non_qualified_dividends"] = (
            fields.get("non_qualified_dividends", 0) + ordinary
        )
    if any(got[w] for w in ("interest", "dividends")):
        out.notes.append(
            "the K-1 interest and dividends are added to the interest and "
            "ordinary_dividends answers, which take the 1099s only"
        )
    if any(got[w] for w in ("stgain", "ltgain")) and any(
        k in out.origins and "typed" in out.origins[k]
        for k in ("short_term_gains", "long_term_gains")
    ):
        out.notes.append(
            "CHECK: a typed short_term_gains or long_term_gains replaces Schedule "
            "D lines 7 and 15, which carry the K-1 gains (lines 5 and 12)"
        )


def _k1_fields(
    out: Inputs, fields: dict[str, Any], entries: list[dict[str, Any]], r: k1.Result
) -> None:
    """The engine's pass-through inputs from the drafted K-1 rows."""
    guaranteed = sum(e.get("guaranteed", 0) for e in entries)
    fields["partnership_income"] = int(round(r.by_kind["partnership"] - guaranteed))
    fields["s_corp_income"] = int(round(r.by_kind["scorp"]))
    fields["trust_income"] = int(round(r.by_kind["trust"]))
    fields["guaranteed_payments"] = guaranteed
    fields["passive_pass_through"] = int(round(r.passive_pships))
    you, spouse = k1.se_earnings(entries)
    fields["k1_se"] = int(round(you))
    if spouse and fields.get("spouse") is not None:
        fields["spouse"] = replace(fields["spouse"], k1_se=int(round(spouse)))
    elif spouse:
        out.notes.append(
            "a K-1 marked spouse has box 14 code A, but the spouse is not on "
            "this return: their self-employment earnings are left out"
        )
    stated = k1.qbi(entries, ("partnership", "scorp"))
    fields["k1_qbi"] = any("qbi" in e for e in entries if e["kind"] != "trust")
    fields["trust_qbi"] = any("qbi" in e for e in entries if e["kind"] == "trust")
    if fields["k1_qbi"] or fields["trust_qbi"]:
        every = k1.qbi(entries, tuple(k1.FORMS))
        fields["qbi_w2_wages"] = int(round(every["w2wages"]))
        fields["qbi_ubia"] = int(round(every["ubia"]))
    base = fields["partnership_income"] + fields["s_corp_income"]
    if fields["k1_qbi"] and abs(stated["qbi"] - base) > 1:
        out.notes.append(
            f"the engine counts the partnership and S corporation income after "
            f"the passive-loss limit ({base:,}) as QBI; the section 199A "
            f"statements give {stated['qbi']:,.0f}, the figure Form 8995 line 1 "
            "takes"
        )
    if fields["trust_qbi"] and fields["trust_income"] > 0:
        out.notes.append(
            "the engine's QBI deduction leaves out the estate or trust's section "
            "199A amount (it is not in the engine's gross income); Form 8995 "
            "line 1 adds it from the 1041 K-1 box 14 code I statement"
        )
    if not (fields["k1_qbi"] or fields["trust_qbi"]):
        out.notes.append(
            "no K-1 section 199A statement (qbi) is typed, so no K-1 income "
            "counts toward the QBI deduction"
        )
    if r.passive_pships:
        out.notes.append(
            "the passive partnership and S corporation income is counted as net "
            "investment income (Form 8960 line 4a)"
        )
    if any(e["kind"] == "trust" for e in entries):
        out.notes.append(
            "the engine leaves the estate or trust K-1 out of net investment "
            "income; Form 8960 lines 4a and 5 add its passive and portfolio "
            "parts (the 1041 K-1 box 14 code H statement)"
        )


def _forecast(
    lay: Layout,
    year: int,
    report: Any,
    value: dict[str, Any],
    out: Inputs,
    through: dict[str, str],
) -> None:
    """Carry each income stream to December 31 (planner.plan.forecast)."""
    typed = forecast.load(lay, year)
    states = {st.need.key: st for st in report.items}
    joint = value.get("filing_status") == "married_joint"
    for key in forecast.STREAMS:
        st = states.get(key)
        state = st.state if st and st.state in ("actual", "estimate") else None
        t = typed.get(key)
        if t is not None and t.owner == "spouse" and not joint:
            raise OverrideError(
                f"{key}: a spouse's income is on this return only when filing "
                "married_joint; change the forecast's owner or the filing status"
            )
        try:
            if t is not None:
                t.check(key, year)
            s, origin, note = forecast.resolve(
                key,
                state,
                value.get(key),
                out.origins.get(key, ""),
                t,
                through.get(key),
                year,
            )
        except forecast.ForecastError as exc:
            raise OverrideError(str(exc)) from exc
        if note:
            out.notes.append(note)
        if s is None:
            continue
        out.forecast.append(s)
        value[key] = s.full_year
        out.origins[key] = origin
        if key in out.unknown:
            out.unknown.remove(key)
        if key not in out.estimates:
            out.estimates.append(key)
        if s.low is not None and s.high is not None:
            out.ranges[key] = (s.low, s.high)
            out.notes.append(
                f"{key} full year {s.full_year:,.0f} (low {s.low:,.0f}, "
                f"high {s.high:,.0f})"
            )


def _counted(fields: dict[str, Any], skip: str | None = None) -> int:
    """Form 1040 line 9 less Social Security, Schedule E and the ``skip`` line:
    wages and the other income lines, the net capital gain or the allowed loss,
    and a joint spouse's wages and income lines."""
    net = int(fields.get("short_term_gains", 0)) + int(fields.get("long_term_gains", 0))
    limit = (
        CAPITAL_LOSS_LIMIT_MFS
        if fields["filing_status"] == "SEPARATE"
        else CAPITAL_LOSS_LIMIT
    )
    lines = ("wages", *TOTAL_INCOME_LINES)
    others = sum(int(fields.get(k, 0)) for k in lines if k != skip) + max(net, -limit)
    if (sp := fields.get("spouse")) is not None:  # a joint total is the couple's
        others += sp.wages + sum(
            getattr(sp, k) for k in TOTAL_INCOME_LINES if k in PERSON_INPUTS
        )
    return others
