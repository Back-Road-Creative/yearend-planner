# ruff: noqa: E501
"""The Needed panel: what the plan still lacks for a year, and where it comes from.

Every fact a planner or tax line relies on is declared once here, with the
document that supplies it. ``needed()`` diffs that list against the ledger
(form boxes, YTD facts), the profile and the typed answers, and reports each
item as actual (a form or an answer), estimate (year-to-date rows), missing,
or don't-have. Only facts no dropped document supplied are ever asked.

Each item names the document that supplies it (``Need.doc``, a key of ``DOCS``
with its exact download path) and the plan outputs it unlocks (``unlocks``,
from ``OUTPUTS``). ``group_by_document`` folds a list of items into one entry
per document, so one download closes several at once; items no document
supplies group last as typed answers.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from planner import states
from planner.config import ASSUMPTION_FIELDS, load_assumptions
from planner.engine.household import PERSON_INPUTS, PERSON_SAVERS
from planner.ingest.derive import county_from_zip
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.taxprep import k1, refund, sche, schf, statereturn

PROFILE = "profile"
PRIOR = "prior"  # the year before the plan year (the filed return)
YEAR = "year"

TYPED = "Typed answers"  # the group of items no document supplies
MANUAL_VALUES = "values"
MANUAL_DONT_HAVE = "dont_have"
ACCOUNT = "account:"  # dynamic keys: account:<number>, account:<number>:death
SCHEDULE_B_OVER = 1500.0  # interest or ordinary dividends over this need Schedule B
FILING = (
    "single",
    "married_joint",
    "married_separate",
    "head_of_household",
    "qualifying_surviving_spouse",  # unit 3b-1
)


# The Medicaid work requirement's first year (config/thresholds.yaml
# medicaid_work_requirement_start; tests/test_config.py holds them equal).
MEDICAID_WORK_FROM = 2027

# State withholding: W-2 box 17, 1099-R box 14, 1099-G box 11, 1099-MISC box 16.
STATE_WITHHELD = (
    ("W-2", "17"),
    ("1099-R", "14"),
    ("1099-G", "11"),
    ("1099-MISC", "16"),
)


@dataclass(frozen=True)
class Need:
    key: str
    label: str
    why: str
    source: str  # the document that supplies it, with where to get it
    kind: str  # date | int | money | fraction | enum | monthly | dependents |
    # education | rentals | k1s | farms | str
    scope: str = YEAR
    # (form, box) ledger lookups, summed; a "-" before the box subtracts it
    boxes: tuple[tuple[str, str], ...] = ()
    estimate: tuple[tuple[str, str], ...] = ()  # YTD facts that stand in meanwhile
    choices: tuple[str, ...] = ()
    # An estimate computed from the ledger when the boxes give nothing:
    # (conn, plan year) -> (value, origin) or None.
    derive: Callable[[sqlite3.Connection, int], tuple[float, str] | None] | None = None
    # Words or amounts worked out from other answers and the ledger (the county
    # from the return's ZIP, a refund's taxable part from last year's return):
    # (config folder, conn, plan year, values so far) ->
    # (value or None, origin or the reason there is none), or None for no lead.
    # A value is an actual answer; a None value leaves the item missing, with
    # the origin as its note.
    derive_text: (
        Callable[
            [Path, sqlite3.Connection, int, dict[str, Any]],
            tuple[str | float | None, str] | None,
        ]
        | None
    ) = None
    doc: str = ""  # key of DOCS: the document that supplies it ("" = typed)
    unlocks: tuple[str, ...] = ()  # plan outputs (OUTPUTS) this item feeds
    # Asked only when this holds for the values so far (Schedule B's Part III
    # only when Schedule B is required); None = always asked.
    asked: Callable[[dict[str, Any]], bool] | None = None
    # Whose documents the boxes sum: "you" or "spouse" for a per-person line
    # (db.OWNERS), None for the household's.
    owner: str | None = None

    def __post_init__(self) -> None:
        if self.doc and self.doc not in DOCS:
            raise ValueError(f"{self.key}: unknown document {self.doc!r}")
        if not set(self.unlocks) <= set(OUTPUTS):
            raise ValueError(f"{self.key}: unknown outputs {self.unlocks}")

    @property
    def where(self) -> str:
        """One line: the document's download path, then where in it to look."""
        if not self.doc:
            return self.source
        path = DOCS[self.doc].path
        return f"{path}; look for: {self.source}" if self.source else path


@dataclass(frozen=True)
class Doc:
    key: str
    name: str
    path: str  # the exact clicks (or place) to get it


@dataclass(frozen=True)
class Status:
    need: Need
    state: str  # actual | estimate | missing | dont_have
    value: Any = None
    origin: str = ""  # "SSA 2026", "YTD vanguard_income", "typed", ...


@dataclass
class NeedsReport:
    year: int
    items: list[Status] = field(default_factory=list)

    def by_state(self, state: str) -> list[Status]:
        return [s for s in self.items if s.state == state]

    @property
    def done(self) -> bool:
        return not self.by_state("missing")


# Every output a missing item can hold back: the dashboard's planners, the
# draft return and the forms and schedules built from them.
OUTPUTS = (
    "Glide path",
    "Spending band",
    "MAGI headroom",
    "Levers",
    "Roth conversion",
    "Withdrawal plan",
    "Estimated tax",
    "Cash buffer",
    "Draft 1040",
    "State return draft",
    "Schedule D",
    "Schedule C",
    "Form 8889",
    "ACA credit",
    "Expected forms",
    "Year rollover",
)

# The documents the Needed panel sends you to, each with the exact download
# path. The Vanguard clicks follow the page names the CSV templates cite
# (templates/csv/*.yaml); the rest are the issuers' own document pages.
DOCS: dict[str, Doc] = {
    d.key: d
    for d in (
        Doc(
            "vg_tax",
            "Vanguard tax forms (1099-INT, 1099-DIV, 1099-B)",
            "Vanguard > My Accounts > Tax center > download the forms for the tax year (PDF)",
        ),
        Doc(
            "vg_realized",
            "Vanguard realized gains export",
            "Vanguard > Cost basis > Realized gains/losses > Export CSV",
        ),
        Doc(
            "f1099r",
            "Form 1099-R from each IRA custodian",
            "Vanguard > My Accounts > Tax center > the 1099-R in the forms list; "
            "another custodian: its tax documents page",
        ),
        Doc(
            "f5498",
            "Form 5498 from each IRA custodian",
            "Vanguard > My Accounts > Tax center > the 5498 in the forms list "
            "(arrives in May); another custodian: its tax documents page",
        ),
        Doc(
            "f5498sa",
            "Form 5498-SA from your HSA custodian",
            "your HSA custodian's website > tax documents > Form 5498-SA "
            "(arrives in May)",
        ),
        Doc(
            "w2",
            "Form W-2 from each employer",
            "your employer's payroll or HR portal > tax documents",
        ),
        Doc(
            "ssa_statement",
            "Social Security Statement",
            "ssa.gov/myaccount > Your Social Security Statement (PDF)",
        ),
        Doc("ssa_1099", "Form SSA-1099", "ssa.gov/myaccount > replacement documents"),
        Doc(
            "filed_return",
            "Last year's filed return",
            "your preparer's portal or tax software > Print or Export > PDF of "
            "last year's filed return",
        ),
        Doc(
            "f1095a",
            "Form 1095-A",
            "HealthCare.gov (or your state's marketplace) > your application > "
            "tax forms",
        ),
        Doc(
            "f1098",
            "Form 1098 (two years in a row)",
            "your loan servicer's website > documents or tax forms",
        ),
        Doc(
            "f1098t",
            "Form 1098-T from each school",
            "the school's student account or bursar portal > tax forms (1098-T)",
        ),
        Doc(
            "insurer",
            "Marketplace or insurer billing statement",
            "HealthCare.gov (or your insurer's website) > your account > "
            "billing or invoices",
        ),
        Doc(
            "bank_csv",
            "Bank transaction export (CSV)",
            "your bank's website > the account > download transactions (CSV)",
        ),
    )
}
_NONE = "no document supplies this; type it"
_1040_ID = "the filing-status check boxes and address line; or type it"


def _passive(s: dict[str, Any]) -> tuple[float, bool]:
    """(Form 8582 line 3 across Schedule E Part I and the K-1s, whether any
    rental is a passive activity)."""
    rentals = sche.activities(sche.columns(s.get("rentals") or []), active=True)
    acts = rentals + k1.activities(k1.rows(s.get("k1s") or []))
    return sum(a.overall for a in acts), bool(rentals)


NEEDS: tuple[Need, ...] = (
    Need(
        "birth_date",
        "Birth date",
        "age for IRA access, Medicare and Social Security",
        _NONE,
        "date",
        PROFILE,
        unlocks=(
            "Glide path",
            "Spending band",
            "Roth conversion",
            "Form 8889",
            "Expected forms",
        ),
    ),
    Need(
        "filing_status",
        "Filing status",
        "every bracket and threshold",
        _1040_ID,
        "enum",
        PROFILE,
        boxes=(("1040", "filing_status"),),
        choices=FILING,
        doc="filed_return",
        unlocks=(
            "MAGI headroom",
            "Roth conversion",
            "Levers",
            "Draft 1040",
            "Form 8889",
        ),
    ),
    Need(
        "state",
        "State",
        "state income tax and Medicaid rules",
        _1040_ID,
        "str",
        PROFILE,
        boxes=(("1040", "state"),),
        choices=tuple(states.STATES),
        doc="filed_return",
        unlocks=("MAGI headroom", "Levers", "Draft 1040", "Expected forms"),
    ),
    Need(
        "state_residency",
        "Where you lived and earned this year",
        "a part-year or nonresident state return the plan does not price",
        "full_year: lived in the state all year with no income from another "
        "state; moved: moved into or out of it this year; other_state: wages or "
        "other income earned in, or taxed by, another state",
        "enum",
        choices=("full_year", "moved", "other_state"),
        unlocks=("Estimated tax", "State return draft"),
        asked=lambda s: _known_state(s),
    ),
    Need(
        "local_income_tax",
        "City, county or school district income tax",
        "a local income tax the plan does not price",
        "yes when a city, county or school district taxes your income: local "
        "tax withheld on a W-2 (box 19, the locality in box 20) or a local "
        "return filed last year",
        "enum",
        choices=("no", "yes"),
        unlocks=("Estimated tax",),
        asked=lambda s: _known_state(s),
    ),
    Need(
        "county",
        "County",
        "the ACA benchmark (SLCSP) premium",
        "the ZIP in the address block when it lies in one county; otherwise type it",
        "str",
        PROFILE,
        derive_text=lambda config, conn, year, values: _county_from_return(
            config, conn, year, values
        ),
        doc="filed_return",
        unlocks=("ACA credit", "MAGI headroom"),
    ),
    Need(
        "spouse_birth_date",
        "Spouse's birth date",
        "a joint return is the couple's: the spouse's age sets the extra standard "
        "deduction at 65 and the senior deduction",
        _NONE,
        "date",
        PROFILE,
        asked=lambda s: s.get("filing_status") == "married_joint",
        unlocks=("MAGI headroom", "Roth conversion", "Levers", "Draft 1040"),
    ),
    Need(
        "dependents",
        "Dependents (birth dates; mark student or disabled; or none)",
        "the child tax credit, the credit for other dependents, head of household "
        "and the household size for the ACA credit; update the student marks each "
        "year",
        "each dependent's birth date, with student (full-time this year) or "
        "disabled after it: 2018-03-02, 2006-07-01 student; or none",
        "dependents",
        PROFILE,
        asked=lambda s: (
            s.get("filing_status")
            in ("married_joint", "head_of_household", "qualifying_surviving_spouse")
        ),
        unlocks=("MAGI headroom", "Roth conversion", "Levers", "Draft 1040"),
    ),
    Need(
        "spouse_death_date",
        "Date your spouse died (or none)",
        "a spouse who died during the year is on a joint return with income to the "
        "date of death and counts as 65 only if 65 at death; a qualifying surviving "
        "spouse files at joint rates only for the two years after the year of "
        "death, while a dependent child lives at home",
        "the date as YYYY-MM-DD, or none",
        "date_or_none",
        PROFILE,
        asked=lambda s: (
            s.get("filing_status") in ("married_joint", "qualifying_surviving_spouse")
        ),
        unlocks=("MAGI headroom", "Roth conversion", "Levers", "Draft 1040"),
    ),
    Need(
        "marriage_date",
        "Date you married during the year (or none)",
        "a couple married during the year who each had a marketplace plan before "
        "it may repay less excess advance credit: Form 8962's alternative "
        "calculation for the year of marriage (Pub. 974)",
        "the date as YYYY-MM-DD, or none",
        "date_or_none",
        PROFILE,
        asked=lambda s: s.get("filing_status") == "married_joint",
        unlocks=("Draft 1040",),
    ),
    Need(
        "spouse_premarriage_dependents",
        "How many of the dependents were your spouse's before the marriage",
        "the year-of-marriage calculation splits the household into your family "
        "and your spouse's before the marriage: a child counts on your spouse's "
        "side only if your spouse could claim them (Pub. 974, Alternative Family "
        "Size); one either of you could claim may go on either side",
        "a whole number, 0 when every dependent was yours",
        "int",
        PROFILE,
        asked=lambda s: (
            s.get("filing_status") == "married_joint"
            and s.get("marriage_date") not in (None, "none")
            and bool(s.get("dependents"))
        ),
        unlocks=("Draft 1040",),
    ),
    Need(
        "spending_floor",
        "Spending floor (annual $)",
        "the lowest the household can run on",
        _NONE,
        "money",
        PROFILE,
        unlocks=("Spending band",),
    ),
    Need(
        "spending_ceiling",
        "Spending ceiling (annual $)",
        "comfortable spending",
        _NONE,
        "money",
        PROFILE,
        unlocks=("Spending band",),
    ),
    Need(
        "cash_target",
        "Cash buffer target ($)",
        "the cash line on the dashboard",
        _NONE,
        "money",
        PROFILE,
        unlocks=("Cash buffer", "Glide path", "Withdrawal plan"),
    ),
    Need(
        "mortgage_monthly",
        "Mortgage P&I and escrow per month ($)",
        "the month-by-month cash line",
        "two years of Form 1098 give the principal and interest; escrow is "
        "typed (the mortgage statement shows it)",
        "money",
        PROFILE,
        derive=lambda conn, year: _mortgage_pi(conn, year),
        doc="f1098",
        unlocks=("Glide path", "Cash buffer"),
    ),
    Need(
        "premium_monthly",
        "Health premium per month ($, net of the advance credit)",
        "the month-by-month cash line",
        "the amount due each month, net of the advance credit",
        "money",
        PROFILE,
        doc="insurer",
        unlocks=("Glide path", "Cash buffer", "Levers"),
    ),
    Need(
        "spending_actual",
        "What the household spent last year (annual $)",
        "the spending band's check against what the year really cost",
        "everything that left the household in the year that ended: bank and "
        "card statements, less transfers between your own accounts",
        "money",
        PRIOR,
        unlocks=("Spending band", "Year rollover"),
    ),
    Need(
        "withdrawal_rate",
        "Withdrawal rate",
        "the glide path, e.g. 0.035",
        _NONE,
        "fraction",
        PROFILE,
        unlocks=("Spending band",),
    ),
    Need(
        "inflation",
        "Inflation",
        "planning assumption, e.g. 0.025",
        _NONE,
        "fraction",
        PROFILE,
        unlocks=("Spending band", "Glide path"),
    ),
    Need(
        "return_floor",
        "Pessimistic real return",
        "sequence-risk band",
        _NONE,
        "fraction",
        PROFILE,
        unlocks=("Spending band", "Glide path"),
    ),
    Need(
        "return_track",
        "Planning real return",
        "glide path",
        _NONE,
        "fraction",
        PROFILE,
        unlocks=("Spending band", "Glide path"),
    ),
    Need(
        "ss_claim_age",
        "Planned Social Security claim age",
        "the age/year table",
        _NONE,
        "int",
        PROFILE,
        unlocks=("Glide path", "Expected forms"),
    ),
    Need(
        "conversion_margin",
        "Conversion margin ($)",
        "room kept below a threshold",
        _NONE,
        "money",
        PROFILE,
        unlocks=("Roth conversion",),
    ),
    Need(
        "conversion_cap",
        "Conversion cap ($)",
        "hard cap on one year's Roth conversion",
        _NONE,
        "money",
        PROFILE,
        unlocks=("Roth conversion",),
    ),
    Need(
        "conversion_objective",
        "Conversion objective",
        "which candidate the planner recommends",
        _NONE,
        "enum",
        PROFILE,
        choices=(
            "ltcg_0pct",
            "bracket_12",
            "aca_400",
            "medicaid_under",
            "medicaid_over",
        ),
        unlocks=("Roth conversion", "Levers"),
    ),
    Need(
        "se_hours",
        "Self-employment hours by month (month:hours, ...)",
        "the Medicaid work requirement asks 80 hours a month from "
        f"{MEDICAID_WORK_FROM}; the plan watches the thinnest month",
        "your own time log, month by month (1:85, 2:60)",
        "monthly",
        asked=lambda s: (
            s.get("_year", 0) >= MEDICAID_WORK_FROM
            and s.get("conversion_objective") == "medicaid_under"
        ),
        unlocks=("MAGI headroom",),
    ),
    Need(
        "roth_basis_contributions",
        "Roth IRA contributions, lifetime total ($)",
        "the part of the Roth balance that is spendable at any age",
        "the sum of every year's box 10, or the Roth custodian's contribution "
        "history (Vanguard: Balances & holdings > the Roth account > Contributions)",
        "money",
        PROFILE,
        estimate=(("5498", "10"),),
        doc="f5498",
        unlocks=("Withdrawal plan", "Year rollover"),
    ),
    Need(
        "hsa_coverage",
        "High-deductible health plan this year (none, self or family)",
        "whether an HSA contribution is a lever, and its limit",
        "the plan's summary of benefits (an HSA-eligible plan says so) or the "
        "marketplace plan listing",
        "enum",
        PROFILE,
        choices=("none", "self", "family"),
        unlocks=("Form 8889", "Levers", "Expected forms"),
    ),
    Need(
        "workplace_plan",
        "Covered by a retirement plan at work this year (yes or no)",
        "whether a traditional IRA contribution is deductible in full",
        "box 13 'Retirement plan' (checked = yes); a SEP or solo 401(k) of "
        "your own also counts",
        "enum",
        PROFILE,
        choices=("yes", "no"),
        doc="w2",
        unlocks=("Levers",),
    ),
    Need(
        "ss_estimate_62",
        "SS monthly estimate at 62",
        "claim-age comparison",
        "the retirement estimate at age 62",
        "money",
        PROFILE,
        boxes=(("SSA", "monthly_62"),),
        doc="ssa_statement",
        unlocks=("Glide path",),
    ),
    Need(
        "ss_estimate_67",
        "SS monthly estimate at 67",
        "claim-age comparison",
        "the retirement estimate at age 67",
        "money",
        PROFILE,
        boxes=(("SSA", "monthly_67"),),
        doc="ssa_statement",
        unlocks=("Glide path",),
    ),
    Need(
        "ss_estimate_70",
        "SS monthly estimate at 70",
        "claim-age comparison",
        "the retirement estimate at age 70",
        "money",
        PROFILE,
        boxes=(("SSA", "monthly_70"),),
        doc="ssa_statement",
        unlocks=("Glide path",),
    ),
    Need(
        "prior_agi",
        "Prior-year AGI (1040 line 11)",
        "safe harbor and the delta report",
        "Form 1040 line 11",
        "money",
        PRIOR,
        boxes=(("1040", "11"),),
        estimate=(("CARRY-EST", "agi"),),
        doc="filed_return",
        unlocks=("Estimated tax", "Year rollover"),
    ),
    Need(
        "prior_total_tax",
        "Prior-year total tax (1040 line 24)",
        "the 100%/110% safe harbor",
        "Form 1040 line 24",
        "money",
        PRIOR,
        boxes=(("1040", "24"),),
        estimate=(("CARRY-EST", "total_tax"),),
        doc="filed_return",
        unlocks=("Estimated tax", "Year rollover"),
    ),
    Need(
        "prior_nc_tax",
        "Prior-year NC income tax (D-400 line 15)",
        "the NC safe harbor",
        "Form D-400 line 15",
        "money",
        PRIOR,
        boxes=(("NC-D400", "15"),),
        estimate=(("CARRY-EST", "nc_tax"),),
        doc="filed_return",
        unlocks=("Estimated tax", "Year rollover"),
        asked=lambda s: s.get("state") == "NC",
    ),
    Need(
        "prior_state_tax",
        "Prior-year state income tax (a state other than NC)",
        "the state's safe harbor",
        "the tax line of last year's return for the state you live in",
        "money",
        PRIOR,
        boxes=statereturn.prior_boxes("state_tax"),
        estimate=(("CARRY-EST", "state_tax"),),
        doc="filed_return",
        unlocks=("Estimated tax", "Year rollover"),
        asked=lambda s: _other_taxing_state(s),
    ),
    Need(
        "prior_capital_loss_carryforward",
        "Capital loss carried into this year ($)",
        "offsets this year's gains before any is taxed",
        "the Capital Loss Carryover Worksheet in the Schedule D "
        "instructions (0 when Schedule D line 16 was not a loss); planner "
        "rollover carries it",
        "money",
        PRIOR,
        boxes=(("CARRY", "st"), ("CARRY", "lt")),
        estimate=(("CARRY-EST", "st"), ("CARRY-EST", "lt")),
        doc="filed_return",
        unlocks=("Levers", "Withdrawal plan", "Year rollover"),
    ),
    Need(
        "fed_withheld",
        "Federal income tax withheld",
        "credited evenly to the four installments",
        "W-2 box 2, 1099-R box 4, 1099-G box 4, 1099-MISC box 4 (zero when "
        "nothing withholds)",
        "money",
        boxes=(("W-2", "2"), ("1099-R", "4"), ("1099-G", "4"), ("1099-MISC", "4")),
        doc="w2",
        unlocks=("Estimated tax",),
    ),
    Need(
        "nc_withheld",
        "NC income tax withheld",
        "credited evenly to the four installments",
        "W-2 box 17, 1099-R box 14, 1099-G box 11, 1099-MISC box 16 (zero when "
        "nothing withholds)",
        "money",
        boxes=STATE_WITHHELD,
        doc="w2",
        unlocks=("Estimated tax", "State return draft"),
        asked=lambda s: s.get("state") == "NC",
    ),
    Need(
        "state_withheld",
        "State income tax withheld (a state other than NC)",
        "credited to the state's installments",
        "W-2 box 17, 1099-R box 14, 1099-G box 11, 1099-MISC box 16 (zero when "
        "nothing withholds)",
        "money",
        boxes=STATE_WITHHELD,
        doc="w2",
        unlocks=("Estimated tax", "State return draft"),
        asked=lambda s: _other_taxing_state(s),
    ),
    Need(
        "ca_subtractions",
        "Other CA subtractions (Schedule CA (540) Part I line 27, column B)",
        "subtracted from federal AGI on Form 540 line 14",
        "Schedule CA (540) column B, less what the planner fills itself (taxable "
        "Social Security, unemployment, US obligations interest, a state refund); "
        "type 0 when none",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "CA",
    ),
    Need(
        "ca_additions",
        "Other CA additions (Schedule CA (540) Part I line 27, column C)",
        "added to federal AGI on Form 540 line 16",
        "Schedule CA (540) column C, less the HSA deduction the planner adds "
        "back itself (e.g. other states' municipal bond interest); type 0 when "
        "none",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "CA",
    ),
    Need(
        "ca_use_tax",
        "CA use tax owed (Form 540 line 91)",
        "tax on purchases no sales tax was collected on",
        "your purchase records, or the use tax table in the 540 booklet (the "
        "draft uses the table until you type it)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "CA",
    ),
    Need(
        "ny_subtractions",
        "Other NY subtractions (Form IT-225 subtractions)",
        "subtracted from federal AGI on IT-201 line 31",
        "Form IT-225, less what the planner fills itself (a state refund, NY and "
        "federal pensions, taxable Social Security, US bond interest, the pension "
        "exclusion and the 529 deduction); type 0 when none",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "NY",
    ),
    Need(
        "ny_additions",
        "NY additions to federal AGI (IT-201 lines 20-23, Form IT-225)",
        "added to federal AGI on IT-201 line 23",
        "IT-201 lines 20-23 and Form IT-225 (e.g. other states' municipal bond "
        "interest, public employee 414(h) contributions); type 0 when none",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "NY",
    ),
    Need(
        "ny_use_tax",
        "NY sales or use tax owed (IT-201 line 59)",
        "tax on purchases no sales tax was collected on",
        "your purchase records, or the sales and use tax chart in IT-201-I (the "
        "draft uses the chart until you type it)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "NY",
    ),
    Need(
        "pa_ube",
        "PA unreimbursed employee business expenses (PA-40 line 1b)",
        "subtracted from PA compensation on PA-40 line 1b",
        "PA Schedule UE (most returns have none; type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "PA",
    ),
    Need(
        "pa_other_interest",
        "Tax-exempt interest PA taxes (other states' bonds)",
        "added to PA interest income on PA-40 line 2",
        "1099-INT box 8 or 1099-DIV box 12 from bonds of states other than PA "
        "(a fund's statement gives the PA share); type 0 when none",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "PA",
    ),
    Need(
        "pa_deductions",
        "Other PA deductions (PA-40 line 10: MSA, ABLE, student loan interest)",
        "subtracted on PA-40 line 10 with the HSA and 529 deductions",
        "PA Schedule O, less the HSA and 529 contributions the planner fills "
        "itself; type 0 when none",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "PA",
    ),
    Need(
        "pa_sp_income",
        "Other PA eligibility income (Schedule SP Section III lines 3-10)",
        "added to eligibility income for tax forgiveness (PA-40 line 21)",
        "Schedule SP lines 3-10 (alimony, gifts and inheritances over $5,000, "
        "income from outside PA, ...); it matters only near the forgiveness "
        "limits; type 0 when none",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "PA",
    ),
    Need(
        "pa_use_tax",
        "PA use tax owed (PA-40 line 25)",
        "tax on purchases no sales tax was collected on",
        "your purchase records, or the PA-40 IN use tax table (the draft uses the "
        "table until you type it)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "PA",
    ),
    Need(
        "il_additions",
        "IL other additions (IL-1040 line 3, Schedule M)",
        "added to federal AGI on IL-1040 line 3",
        "IL Schedule M Step 2 (a child's tax-exempt interest from Form 8814, "
        "pass-through additions, ...; most returns have none; type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "IL",
    ),
    Need(
        "il_subtractions",
        "IL other subtractions beyond US bond interest and 529 contributions "
        "(IL-1040 line 7, Schedule M)",
        "subtracted on IL-1040 line 7 with the US obligations interest and 529 "
        "contributions the draft already counts",
        "IL Schedule M Step 3 (military pay, ...; type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "IL",
    ),
    Need(
        "il_use_tax",
        "IL use tax owed (IL-1040 line 21)",
        "tax on purchases no sales tax was collected on",
        "your purchase records (the UT Worksheet), or the UT Table in the IL-1040 "
        "instructions (the draft uses the table until you type it)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "IL",
    ),
    Need(
        "oh_additions",
        "OH additions beyond the depreciation add-back (Schedule of Adjustments "
        "lines 1-11)",
        "added to federal AGI on OH IT 1040 line 2a",
        "OH Schedule of Adjustments (non-Ohio municipal bond interest, 529 funds "
        "used for other expenses, ...; most returns have none; type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "OH",
    ),
    Need(
        "oh_deductions",
        "OH deductions the draft does not already count (Schedule of Adjustments "
        "lines 14-46)",
        "subtracted on OH IT 1040 line 2b with the state refund, taxable social "
        "security, US obligations interest and business income deduction",
        "OH Schedule of Adjustments (railroad benefits, military pay, ...; type 0 "
        "when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "OH",
    ),
    Need(
        "oh_business_income",
        "OH business income beyond Schedule C and F (Schedule of Business Income "
        "lines 1, 3-5 and 7-9)",
        "business income: the business income deduction and the 3% rate",
        "K-1s, Form 4797 and guaranteed payments that are business income under "
        "R.C. 5747.01(B) (type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "OH",
    ),
    Need(
        "oh_use_tax",
        "OH unpaid use tax (IT 1040 line 12)",
        "tax on purchases no Ohio sales tax was collected on",
        "your purchase records times your county's rate (the IT 1040 instructions' "
        "use tax worksheet; type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "OH",
    ),
    Need(
        "ga_additions",
        "GA additions to federal AGI (Form 500 Schedule 1 lines 1, 3-5)",
        "added to federal AGI on GA Form 500 line 9 with the lump sum "
        "distributions the planner counts",
        "Form 500 Schedule 1 additions: interest on other states' and their "
        "cities' bonds, the bonus depreciation add-back, ... (type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "GA",
    ),
    Need(
        "ga_subtractions",
        "GA subtractions the draft does not already count (Form 500 Schedule 1 "
        "lines 11-12)",
        "subtracted on GA Form 500 line 9 with the retirement exclusion, taxable "
        "social security, Path2College 529 and US obligations interest",
        "Form 500 Schedule 1 lines 11 and 12 (the IT-511 booklet's list of other "
        "adjustments; type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "GA",
    ),
    Need(
        "ga_itemized_adjustment",
        "GA adjustments to federal itemized deductions (Form 500 line 12b)",
        "taken off the federal itemized deductions on GA Form 500 line 12c",
        "the IT-511 booklet's line 12b worksheet: other states' income taxes in "
        "Schedule A's SALT, ... (type 0 when none; asked only when you itemize)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "GA",
    ),
    Need(
        "mi_additions",
        "MI additions to federal AGI (MI-1040 Schedule 1 lines 1, 3-8)",
        "added to federal AGI on MI-1040 line 11 with the self-employment tax "
        "deduction the planner counts (Schedule 1 line 2)",
        "MI-1040 Schedule 1 additions: interest and dividends from other "
        "states' bonds, the federal net operating loss deduction, ... (type 0 "
        "when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "MI",
    ),
    Need(
        "mi_subtractions",
        "MI subtractions the draft does not already count (MI-1040 Schedule 1 "
        "lines 12, 13, 15, 18-22)",
        "subtracted on MI-1040 line 13 with US obligations interest, military "
        "retirement, taxable Social Security, Michigan tax refunds, Michigan 529 "
        "contributions, the Michigan Standard Deduction and the retirement and "
        "senior investment deductions",
        "MI-1040 Schedule 1 lines 12, 13, 15 and 18-22 (the instruction book's "
        "Schedule 1 lines; type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "MI",
    ),
    Need(
        "mi_property_tax_credit",
        "MI homestead property tax credit (MI-1040CR line 44 or MI-1040CR-2)",
        "a refundable credit on MI-1040 line 26",
        "your MI-1040CR (or MI-1040CR-2) line carried to MI-1040 line 26 "
        "(type 0 when you do not claim it)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "MI",
    ),
    Need(
        "nc_additions",
        "NC additions to federal AGI (D-400 Schedule S line 16)",
        "added to federal AGI on D-400 line 7",
        "D-400 Schedule S Part A (most returns have none; type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "NC",
    ),
    Need(
        "nc_other_deductions",
        "Other NC deductions (D-400 Schedule S lines 20-40)",
        "subtracted from federal AGI on D-400 line 9: Bailey and uniformed "
        "services retirement, and the other Schedule S Part B items",
        "D-400 Schedule S Part B; the planner fills lines 18 and 19 itself "
        "(type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "NC",
    ),
    Need(
        "nc_use_tax",
        "NC consumer use tax owed (D-400 line 18)",
        "tax on purchases no sales tax was collected on",
        "your purchase records, or the use tax table in the D-400 instructions "
        "(the draft uses the table until you type it)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "NC",
    ),
    Need(
        "nj_other_interest",
        "Interest from other states' bonds (NJ-1040 line 16a)",
        "added to NJ-1040 line 16a: New Jersey taxes interest on other states' "
        "and their localities' obligations, and takes it out of line 16b",
        "the tax-exempt interest on your 1099-INT box 8 and 1099-DIV box 12 that "
        "is not from New Jersey bonds or a New Jersey qualified investment fund "
        "(type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "NJ",
    ),
    Need(
        "nj_other_dividends",
        "Taxable fund dividends federal law exempts (NJ-1040 line 17)",
        "added to NJ-1040 line 17: a fund that is not a New Jersey qualified "
        "investment fund pays taxable dividends, even when federal law exempts "
        "them (GIT-5)",
        "1099-DIV box 12 from funds that did not certify as a New Jersey "
        "qualified investment fund; the fund's year-end notice says (type 0 "
        "when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "NJ",
    ),
    Need(
        "nj_veterans",
        "How many of you and your spouse are honorably discharged veterans "
        "(NJ-1040 line 9)",
        "a $6,000 exemption each on NJ-1040 line 9",
        "a whole number: 0, 1, or 2 on a joint return; proof (DD-214) goes in "
        "with the first return that claims it",
        "int",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "NJ",
    ),
    Need(
        "nj_other_deductions",
        "Other NJ deductions (NJ-1040 lines 32-36)",
        "subtracted on NJ-1040 line 38: alimony paid, a qualified conservation "
        "contribution, the Health Enterprise Zone deduction, the Alternative "
        "Business Calculation Adjustment and the organ and bone marrow donation "
        "deduction",
        "NJ-1040 lines 32-36 and their worksheets in the instructions; the draft "
        "fills lines 30, 31 and 37a itself (type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "NJ",
    ),
    Need(
        "nj_medical_expenses",
        "Unreimbursed medical expenses and premiums (NJ-1040 Worksheet F line 1)",
        "the part over 2% of NJ-1040 line 29 is deducted on line 31, with the "
        "self-employed health insurance deduction the planner counts",
        "doctor, dental, hospital, prescription and insurance costs you paid and "
        "were not repaid for, Medicare premiums included (type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "NJ",
    ),
    Need(
        "nj_property_taxes",
        "Property taxes on your NJ main home, or 18% of the rent (NJ-1040 line 40a)",
        "the property tax deduction (line 41) or the $50 credit (line 56), "
        "whichever Worksheet H says saves more",
        "the property taxes paid on your New Jersey main home this year, or 18% "
        "of the rent a tenant paid; share them with co-owners by ownership "
        "(type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "NJ",
    ),
    Need(
        "nj_use_tax",
        "NJ use tax owed (NJ-1040 line 51)",
        "tax on internet, mail-order and other out-of-state purchases no sales "
        "tax was collected on",
        "your purchase records, or the use tax chart in the NJ-1040 instructions "
        "(type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "NJ",
    ),
    Need(
        "va_additions",
        "VA additions to federal AGI (Schedule ADJ lines 1-2c)",
        "added to federal AGI on Form 760 line 2: interest on other states' "
        "bonds, the conformity addition and the other addition codes",
        "Schedule ADJ lines 1 and 2a-2c and the addition codes in the Form 760 "
        "instructions (type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "VA",
    ),
    Need(
        "va_subtractions",
        "VA subtractions the draft does not already count (Schedule ADJ lines 5a-6d)",
        "subtracted on Form 760 line 7 with US obligations interest, the "
        "unemployment compensation and the military and federal and state "
        "employee subtractions the planner counts",
        "Schedule ADJ lines 5a-6d: the disability income subtraction and the "
        "subtraction codes in the Form 760 instructions that the planner does "
        "not price (type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "VA",
    ),
    Need(
        "va_deductions",
        "VA deductions other than child care and Commonwealth Savers (Schedule "
        "ADJ line 8)",
        "subtracted on Form 760 line 13 with the child and dependent care "
        "expenses and Commonwealth Savers contributions the planner counts",
        "Schedule ADJ line 8 deduction codes 102, 103 and 105-109 in the Form "
        "760 instructions: foster care, long-term care premiums, ... (type 0 "
        "when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "VA",
    ),
    Need(
        "va_use_tax",
        "VA consumer's use tax owed (Form 760 line 33)",
        "tax on internet, mail-order and out-of-state purchases no sales tax was "
        "collected on",
        "your purchase records, or the Sales Tax Estimation Table in the Form "
        "760 instructions (type 0 when none)",
        "money",
        unlocks=("State return draft",),
        asked=lambda s: s.get("state") == "VA",
    ),
    Need(
        "wages",
        "Wages",
        "ordinary income",
        "box 1; type 0 if none",
        "money",
        boxes=(("W-2", "1"),),
        doc="w2",
        unlocks=("MAGI headroom", "Levers", "Draft 1040", "Expected forms"),
    ),
    Need(
        "dependent_care_benefits",
        "Dependent care benefits from an employer",
        "Form 2441 Part III: benefits above the care incurred, or above the "
        "lower earner's income, are taxable (line 26); the rest lowers the "
        "credit's $3,000 or $6,000 limit (line 28)",
        "box 10; type 0 if none",
        "money",
        boxes=(("W-2", "10"),),
        doc="w2",
        asked=lambda s: bool(s.get("dependents")),
        unlocks=("Draft 1040",),
    ),
    Need(
        "care_expenses",
        "Care paid this year for a child under 13, so you (and your spouse) could work",
        "the credit for child and dependent care expenses (Form 2441 Part II, "
        "column d) and the benefits test (line 16): up to $3,000 for one child, "
        "$6,000 for two or more",
        "the care provider's receipts or your dependent care account's claims "
        "history; type 0 if none",
        "money",
        asked=lambda s: bool(s.get("dependents")),
        unlocks=("Draft 1040",),
    ),
    Need(
        "dependent_care_grace",
        "Dependent care benefits carried from last year and used in this year's "
        "grace period",
        "Form 2441 line 13: added to this year's benefits",
        "your dependent care account's statement; type 0 if none",
        "money",
        asked=lambda s: _dependent_care(s),
        unlocks=("Draft 1040",),
    ),
    Need(
        "dependent_care_forfeited",
        "Dependent care benefits forfeited, or carried to next year",
        "Form 2441 line 14: what you did not receive is not taxable",
        "your dependent care account's year-end statement (box 10 less what it "
        "reimbursed); type 0 if none",
        "money",
        asked=lambda s: _dependent_care(s),
        unlocks=("Draft 1040",),
    ),
    Need(
        "education",
        "Students: qualified education expenses and the credit for each (or none)",
        "Form 8863: the American opportunity credit (up to $2,500 a student, 40% "
        "refundable) or the lifetime learning credit (20% of up to $10,000 a "
        "return); tuition, fees and course materials paid, less the tax-free "
        "scholarships and grants that paid them",
        "each student's 1098-T (box 1 paid, box 5 scholarships and grants) and "
        "receipts for required books and supplies: who (you, spouse, or "
        "dependent N in the order the dependents were typed), the expenses paid, "
        "aid and the tax-free assistance, then aotc or llc: dependent 1 6500 aid "
        "1500 aotc; you 3000 llc; or none. aotc is only for a student in the "
        "first four years of college, at least half-time toward a degree or "
        "credential, with the credit claimed for 3 or fewer earlier years, no "
        "felony drug conviction, and the school's EIN",
        "education",
        doc="f1098t",
        unlocks=("Draft 1040",),
    ),
    Need(
        "aotc_refundable_barred",
        "Do the Form 8863 line 7 conditions all apply to you (yes or no)",
        "a filer under 24 at the end of the year who meets all three gets no "
        "refundable American opportunity credit: all of it is nonrefundable "
        "(2025 Form 8863 line 7)",
        "yes if all three hold: (1) you were under 18, or 18 with earned income "
        "under half your support, or a full-time student over 18 and under 24 "
        "with earned income under half your support; (2) a parent was alive at "
        "the end of the year; (3) you are not filing a joint return. Otherwise "
        "no (always no at 24 or older)",
        "enum",
        choices=("yes", "no"),
        asked=lambda s: (
            s.get("filing_status") != "married_joint"
            and any(e.get("credit") == "aotc" for e in s.get("education") or ())
        ),
        unlocks=("Draft 1040",),
    ),
    Need(
        "qualified_tips",
        "Qualified tips (already in wages)",
        "the Schedule 1-A tips deduction, tax years 2025 to 2028 (P.L. 119-21 sec. 70201)",
        "the employer's W-2, or the separate statement of qualified tips an "
        "employer may give for 2025 (IRS Notice 2025-62), or your own tip records; "
        "type 0 if none",
        "money",
        unlocks=("Draft 1040",),
    ),
    Need(
        "tipped_occupation_code",
        "Treasury tipped-occupation code for those tips (3 digits)",
        "the tips deduction is allowed only for an occupation on the Treasury list",
        "the code for your occupation on IRS.gov/TippedOccupations; type 0 if "
        "none or no tips",
        "int",
        unlocks=("Draft 1040",),
    ),
    Need(
        "qualified_overtime",
        "Qualified overtime premium (already in wages)",
        "the Schedule 1-A overtime deduction, tax years 2025 to 2028 (P.L. 119-21 sec. 70202)",
        "the employer's W-2, or the separate statement of qualified overtime "
        "compensation an employer may give for 2025 (IRS Notice 2025-62), or your "
        "pay stubs: only the extra half in time-and-a-half pay counts; type 0 if none",
        "money",
        unlocks=("Draft 1040",),
    ),
    Need(
        "car_loan_interest",
        "Interest paid on a qualifying new-vehicle loan",
        "the Schedule 1-A car loan interest deduction, tax years 2025 to 2028 "
        "(P.L. 119-21 sec. 70203, IRC sec. 163(h)(4))",
        "the lender's year-end statement of interest paid on a loan taken out "
        "after 2024 to buy a new car, minivan, SUV, pickup or motorcycle made in the "
        "United States; type 0 if none",
        "money",
        unlocks=("Draft 1040",),
    ),
    Need(
        "planned_giving",
        "Gifts to charity this year (cash, planned or made)",
        "Schedule A gifts, and the donate-shares and donor-advised-fund levers",
        "your giving records or receipts for the year; type 0 if none",
        "money",
        unlocks=("Levers", "Draft 1040"),
    ),
    Need(
        "real_estate_taxes",
        "Real estate taxes paid this year",
        "Schedule A line 5b: whether itemizing beats the standard deduction",
        "the county tax bill, or Form 1098 box 10 when the lender pays it from "
        "escrow; type 0 if none",
        "money",
        unlocks=("Levers", "Draft 1040"),
    ),
    Need(
        "mortgage_interest",
        "Home mortgage interest paid this year",
        "Schedule A line 8a: whether itemizing beats the standard deduction",
        "the lender's Form 1098 box 1; type 0 if none",
        "money",
        unlocks=("Levers", "Draft 1040"),
    ),
    Need(
        "se_income",
        "Self-employment net income",
        "SE tax, QBI, Schedule C",
        "Schedule C line 31 from the bank CSV once its rows are categorised "
        "(planner categorize); 1099-NEC / 1099-K from each payer stand in",
        "money",
        boxes=(("SCH-C", "31"),),
        estimate=(("1099-NEC", "1"), ("1099-K", "1a")),
        doc="bank_csv",
        unlocks=(
            "MAGI headroom",
            "Levers",
            "Draft 1040",
            "Schedule C",
            "Expected forms",
        ),
    ),
    Need(
        "interest",
        "Taxable interest",
        "ordinary income",
        "1099-INT boxes 1 and 3",
        "money",
        boxes=(("1099-INT", "1"), ("1099-INT", "3")),
        estimate=(("YTD", "interest"),),
        doc="vg_tax",
        unlocks=("MAGI headroom", "Glide path", "Draft 1040", "State return draft"),
    ),
    Need(
        "unemployment",
        "Unemployment compensation",
        "ordinary income (Schedule 1 line 7)",
        "Form 1099-G box 1, from the state's unemployment agency; less any you "
        "repaid this year; type 0 if none",
        "money",
        boxes=(("1099-G", "1"),),
        unlocks=("MAGI headroom", "Draft 1040", "State return draft"),
    ),
    Need(
        "state_refund",
        "State or local income tax refund received",
        "taxable only as far as last year's itemized deduction of it cut your tax",
        "Form 1099-G box 2, from the state or city tax department (a refund "
        "applied to this year's estimated tax counts); type 0 if none",
        "money",
        boxes=(("1099-G", "2"),),
        unlocks=("Draft 1040",),
    ),
    Need(
        "state_refund_taxable",
        "Taxable part of the state or local income tax refund",
        "Schedule 1 line 1",
        "line 9 of the State and Local Income Tax Refund Worksheet in the Form "
        "1040 instructions (Schedule 1, line 1); Pub. 525's Itemized Deduction "
        "Recoveries when one of the instructions' exceptions applies",
        "money",
        derive_text=lambda config, conn, year, values: _refund_taxable(
            conn, year, values
        ),
        unlocks=("MAGI headroom", "Draft 1040", "State return draft"),
        asked=lambda s: bool(s.get("state_refund")),
    ),
    Need(
        "cancelled_debt",
        "Canceled debt",
        "ordinary income (Schedule 1 line 8c) unless an exclusion applies",
        "Form 1099-C box 2; the insolvency, bankruptcy and qualified principal "
        "residence exclusions (Form 982, Pub. 4681) take out what is not "
        "taxable, so type the taxable part; type 0 if none",
        "money",
        boxes=(("1099-C", "2"),),
        unlocks=("MAGI headroom", "Draft 1040", "State return draft"),
    ),
    Need(
        "other_income",
        "Other income (prizes, awards, and other payments not wages or business)",
        "ordinary income (Schedule 1 line 8z)",
        "Form 1099-MISC box 3, from whoever paid you; prizes and awards, "
        "punitive damages and other taxable income no other line takes; not "
        "business income (Schedule C) or rents and royalties (Schedule E); "
        "type 0 if none",
        "money",
        boxes=(("1099-MISC", "3"),),
        unlocks=("MAGI headroom", "Draft 1040", "State return draft"),
    ),
    Need(
        "rentals",
        "Rental real estate and royalties (Schedule E Part I), or none",
        "Schedule 1 line 5: each property's rents or royalties less its expenses, "
        "with the vacation-home and passive-loss limits",
        "your rent roll, leases and receipts; 1099-MISC box 1 (rents) and box 2 "
        "(royalties); the lender's 1098 for the rental's mortgage; last year's "
        "Form 4562 for depreciation. One entry a property, separated by "
        "semicolons: rental rents 18000 mortgage 4000 taxes 2000 expenses 3000 "
        "depreciation 3000 days 300 personal 0; royalty royalties 1200 expenses "
        "100 depletion 50. days are the fair rental days and personal the "
        "personal-use days (Schedule E line 2); expenses are the other operating "
        "costs together; direct is a rental-only cost not split by days; "
        "carryover and carrydep are last year's Pub. 527 Worksheet 5-1 lines 7a "
        "and 7b; prior is the property's prior-year unallowed passive loss (last "
        "year's Form 8582 Part VII column (c)). Type none if there are none",
        "rentals",
        unlocks=("MAGI headroom", "Draft 1040", "State return draft"),
    ),
    Need(
        "rental_qbi",
        "Is your rental real estate a trade or business for the QBI deduction "
        "(yes or no)",
        "Form 1040 line 13: rental income counts for the 20% qualified business "
        "income deduction only when the rental is a trade or business",
        "yes if the rentals meet the Rev. Proc. 2019-38 safe harbor (separate "
        "books and records for each enterprise, 250 or more hours of rental "
        "services a year, contemporaneous logs of those hours) or are otherwise "
        "a section 162 trade or business. No for a triple-net lease, a home you "
        "used personally more than 14 days, or a rental you just hold",
        "enum",
        choices=("yes", "no"),
        asked=lambda s: any(p["kind"] == "rental" for p in s.get("rentals") or ()),
        unlocks=("Draft 1040",),
    ),
    Need(
        "k1s",
        "Schedules K-1 from partnerships, S corporations, estates and trusts "
        "(Schedule E Parts II and III), or none",
        "Schedule 1 line 5: each K-1's business and rental income or loss, with "
        "the passive-loss limit; box 14 code A goes to Schedule SE",
        "each Schedule K-1 (Form 1065, 1120-S or 1041). One entry a K-1, "
        "separated by semicolons: the kind (partnership, scorp or trust), then "
        "passive, or nonpassive when you materially participated (Pub. 925: "
        "500 hours, or the other tests), then each box as a word and amount: "
        "ordinary (box 1, or 1041 box 6), rental (box 2, or 1041 box 7), "
        "otherrental (box 3, or 1041 box 8), guaranteed (1065 box 4c), "
        "section179 (1065 box 12, 1120-S box 11), se (1065 box 14 code A), "
        "portfolio (1041 box 5), deductions (1041 box 9) and the section 199A "
        "statement's qbi, w2wages and ubia; and the portfolio boxes interest "
        "(1065 box 5, 1120-S 4, 1041 1), dividends and qualified (1065 6a and "
        "6b, 1120-S 5a and 5b, 1041 2a and 2b), royalties (1065 box 7, 1120-S "
        "6), stgain and ltgain (1065 boxes 8 and 9a, 1120-S 7 and 8a, 1041 3 "
        "and 4a), which go to Schedules B, E line 4 and D lines 5 and 12, on "
        "top of the 1099s; prior is the K-1's prior-year unallowed passive loss "
        "(last year's Form 8582 Part VII column (c)), and active after passive "
        "or nonpassive marks a box 2 rental you actively participated in. "
        "Start with spouse when the "
        "self-employment earnings are your spouse's. Example: " + k1.EXAMPLE + ". "
        "Type none if there are none",
        "k1s",
        unlocks=("MAGI headroom", "Draft 1040", "State return draft"),
    ),
    Need(
        "farms",
        "Farming (Schedule F), or none",
        "Schedule 1 line 6 and Schedule SE line 1a: each farm's gross income "
        "less its expenses, with the passive-loss limit when you did not "
        "materially participate",
        "your farm books, sales receipts and expense records; Form 1099-PATR "
        "(cooperative distributions), 1099-G or CCC-1099-G (agricultural "
        "program payments, CCC loans), 1099-MISC (crop insurance); last "
        "year's Form 4562 for depreciation. One entry a farm, separated by "
        "semicolons: farm, then any of spouse (your spouse's farm), accrual "
        "(the accrual method, Part III), nonmaterial (you did not materially "
        "participate: line E No) and notatrisk (line 36b), then each line "
        "as a word and amount. Income, cash method: resale and basis (lines "
        "1a, 1b), raised (2), coop and cooptaxable (3a, 3b), program and "
        "programtaxable (4a, 4b), cccelected (5a), cccforfeited and "
        "ccctaxable (5b, 5c), cropins and cropinstaxable (6a, 6b), "
        "deferredin (6d, crop insurance deferred from last year), custom "
        "(7) and otherincome (8); accrual method: sales (37), coop, "
        "program, ccc words and cropins (38-41), custom, otherincome, and "
        "the inventory words begin, purchased and end (45, 46, 48). "
        "Expenses (lines 10-32): " + ", ".join(schf.EXPENSES) + "; "
        "conservationcarry is last year's conservation expense over the 25% "
        "limit; prior, with nonmaterial, is the farm's prior-year unallowed "
        "passive loss (last year's Form 8582 Part VII column (c)). A taxable "
        "amount not typed is all of it. Example: " + schf.EXAMPLE + ". "
        "Type none if there are none",
        "farms",
        unlocks=("MAGI headroom", "Draft 1040", "State return draft"),
    ),
    Need(
        "rental_active",
        "Did you actively participate in your rentals (yes or no)",
        "Form 8582 Part II's special allowance (up to $25,000, phased out "
        "between $100,000 and $150,000 of modified AGI) lets a rental loss "
        "offset other income only for rental real estate with active "
        "participation (Part IV)",
        "yes if you (and your spouse) own 10% or more of each rental and made "
        "the management decisions, such as approving tenants, setting rents "
        "and approving repairs, and none is held as a limited partner (Form "
        "8582 instructions, Special Allowance). Otherwise no",
        "enum",
        choices=("yes", "no"),
        asked=lambda s: _passive(s)[1] and _passive(s)[0] < 0,
        unlocks=("Draft 1040",),
    ),
    Need(
        "lived_apart",
        "Did you live apart from your spouse all year (yes or no)",
        "married filing separately, a passive rental loss gets Form 8582's "
        "special allowance ($12,500, phased out from $50,000 of modified AGI) "
        "only when you lived apart all year",
        "yes if you and your spouse did not live together at any time during "
        "the year; otherwise no",
        "enum",
        choices=("yes", "no"),
        asked=lambda s: (
            s.get("filing_status") == "married_separate" and _passive(s)[0] < 0
        ),
        unlocks=("Draft 1040",),
    ),
    Need(
        "tax_exempt_interest",
        "Tax-exempt interest",
        "ACA MAGI and Social Security taxation (Form 1040 line 2a)",
        "1099-INT box 8 and 1099-DIV box 12, exempt-interest dividends; type 0 if none",
        "money",
        boxes=(("1099-INT", "8"), ("1099-DIV", "12")),
        doc="vg_tax",
        unlocks=("MAGI headroom", "ACA credit", "Draft 1040"),
    ),
    Need(
        "ordinary_dividends",
        "Ordinary dividends",
        "ordinary income and MAGI",
        "1099-DIV box 1a",
        "money",
        boxes=(("1099-DIV", "1a"),),
        estimate=(("YTD", "dividends"),),
        doc="vg_tax",
        unlocks=("MAGI headroom", "Draft 1040", "Expected forms"),
    ),
    Need(
        "foreign_accounts",
        "A foreign financial account or foreign trust this year (yes or no)",
        "Schedule B Part III lines 7a and 8: asked whenever Schedule B is "
        "required, never assumed",
        "your own records: a bank, brokerage or pension account held outside "
        "the U.S., signature authority over one, or a foreign trust",
        "enum",
        choices=("yes", "no"),
        unlocks=("Draft 1040",),
        asked=lambda so_far: schedule_b_required(
            so_far.get("interest"), so_far.get("ordinary_dividends")
        ),
    ),
    Need(
        "qualified_dividends",
        "Qualified dividends",
        "0% / 15% rate stacking",
        "1099-DIV box 1b",
        "money",
        boxes=(("1099-DIV", "1b"),),
        doc="vg_tax",
        unlocks=("MAGI headroom", "Draft 1040"),
    ),
    Need(
        "short_term_gains",
        "Short-term gain or loss",
        "ordinary income",
        "the realized gains/losses CSV (or the 1099-B in the Vanguard tax "
        "forms); Schedule D line 7 once the year ends (planner gains)",
        "money",
        boxes=(("SCH-D", "7"),),
        estimate=(("YTD", "st_gain"),),
        doc="vg_realized",
        unlocks=("MAGI headroom", "Levers", "Draft 1040", "Schedule D"),
    ),
    Need(
        "long_term_gains",
        "Long-term gain or loss",
        "0% LTCG room",
        "the realized gains/losses CSV (or the 1099-B in the Vanguard tax "
        "forms), plus 1099-DIV box 2a; Schedule D line 15 once the year ends",
        "money",
        boxes=(("SCH-D", "15"),),
        estimate=(("YTD", "lt_gain"), ("YTD", "capital_gain_distributions")),
        doc="vg_realized",
        unlocks=("MAGI headroom", "Levers", "Draft 1040", "Schedule D"),
    ),
    Need(
        "st_loss_carryover",
        "Short-term capital loss carried in",
        "Schedule D line 6",
        "last year's Capital Loss Carryover Worksheet line 8 (Schedule D "
        "instructions), or last year's draft (planner draft); planner rollover "
        "carries it",
        "money",
        PRIOR,
        boxes=(("CARRY", "st"),),
        estimate=(("CARRY-EST", "st"),),
        doc="filed_return",
        unlocks=("Schedule D",),
    ),
    Need(
        "lt_loss_carryover",
        "Long-term capital loss carried in",
        "Schedule D line 14",
        "last year's Capital Loss Carryover Worksheet line 13, or last year's "
        "draft; planner rollover carries it",
        "money",
        PRIOR,
        boxes=(("CARRY", "lt"),),
        estimate=(("CARRY-EST", "lt"),),
        doc="filed_return",
        unlocks=("Schedule D",),
    ),
    Need(
        "ira_distributions",
        "Taxable IRA distributions",
        "ordinary income",
        "box 2a from each IRA custodian",
        "money",
        boxes=(("1099-R", "2a"),),
        doc="f1099r",
        unlocks=("MAGI headroom", "Levers", "Draft 1040"),
    ),
    Need(
        "social_security",
        "Social Security benefits",
        "up to 85% taxable; all of it counts in ACA MAGI",
        "box 5; type 0 before you claim",
        "money",
        boxes=(("SSA-1099", "5"),),
        doc="ssa_1099",
        unlocks=("MAGI headroom", "Draft 1040"),
    ),
    Need(
        "roth_conversion",
        "Roth conversion this year",
        "the conversion ledger",
        "box 3 (arrives in May) or your conversion confirmation",
        "money",
        boxes=(("5498", "3"),),
        doc="f5498",
        unlocks=("Roth conversion", "Levers", "Draft 1040"),
    ),
    Need(
        "traditional_ira_contribution",
        "Traditional IRA contribution",
        "the IRA deduction",
        "box 1 or the custodian's confirmation",
        "money",
        boxes=(("5498", "1"),),
        doc="f5498",
        unlocks=("Levers", "MAGI headroom"),
    ),
    Need(
        "roth_ira_contribution",
        "Roth IRA contributions for the year (and ABLE contributions as the beneficiary)",
        "Form 8880 line 1, the saver's credit, with the traditional IRA contribution",
        "box 10 or the custodian's confirmation, counting what goes in by the "
        "filing deadline for this year; not rollovers or conversions; add what you "
        "put in your own ABLE account; type 0 if none",
        "money",
        boxes=(("5498", "10"),),
        doc="f5498",
        unlocks=("Draft 1040",),
    ),
    Need(
        "elective_deferrals",
        "Elective deferrals and voluntary contributions to workplace plans",
        "Form 8880 line 2, the saver's credit",
        "W-2 box 12 codes D, E, F, G, H, S, AA, BB and EE (401(k), 403(b), "
        "governmental 457(b), SARSEP and SIMPLE deferrals, Roth ones included), "
        "plus voluntary after-tax contributions to a qualified plan; not "
        "414(h) contributions; type 0 if none",
        "money",
        boxes=tuple(
            ("W-2", f"12{c}") for c in ["D", "E", "F", "G", "H", "S", "AA", "BB", "EE"]
        ),
        doc="w2",
        unlocks=("Draft 1040",),
    ),
    Need(
        "savers_distributions",
        "Retirement distributions in the saver's credit testing period",
        "Form 8880 line 4: they come off the contributions before the credit",
        "every distribution from an IRA, Roth IRA, ABLE account or workplace plan "
        "in the two years before this one, this year, and next year up to the "
        "filing deadline; on a joint return both spouses' (a spouse's from a year "
        "you did not file jointly counts on their line only). Leave out rollovers, "
        "trustee-to-trustee transfers, conversions to a Roth IRA, plan loans, "
        "returned excess or same-year contributions, 404(k) dividends, military "
        "retirement and an inherited IRA's. Type 0 if none",
        "money",
        asked=lambda s: _saves(s, ""),
        unlocks=("Draft 1040",),
    ),
    Need(
        "savers_barred",
        "Were you claimed as a dependent on another return, or a student (yes or no)",
        "Form 8880: either one bars the saver's credit for that person's contributions",
        "yes if someone else claims you on their return, or you were a full-time "
        "student (or in a full-time on-farm training course) during some part of "
        "five calendar months of the year; online-only, correspondence and "
        "on-the-job courses do not count. Otherwise no",
        "enum",
        choices=("yes", "no"),
        asked=lambda s: _saves(s, ""),
        unlocks=("Draft 1040",),
    ),
    Need(
        "hsa_contribution",
        "HSA contribution you deduct (not through payroll)",
        "the HSA deduction",
        "Form 8889 line 13 once the year has ended (planner hsa); before then, "
        "what you have put in yourself so far",
        "money",
        boxes=(("8889", "13"), ("8889 (spouse)", "13")),
        unlocks=("Levers", "Draft 1040", "Year rollover"),
    ),
    Need(
        "hsa_contributions",
        "HSA contributions for the year, from every source",
        "Form 8889: what went in against the limit",
        "box 2 plus box 3 (box 2 also counts money put in this year for "
        "last year: take that part out)",
        "money",
        boxes=(("5498-SA", "2"), ("5498-SA", "3")),
        doc="f5498sa",
        unlocks=("Form 8889",),
    ),
    Need(
        "hsa_employer_contributions",
        "Employer and payroll HSA contributions",
        "Form 8889 line 9: already excluded from wages, never deducted again",
        "box 12 code W",
        "money",
        boxes=(("W-2", "12W"),),
        doc="w2",
        unlocks=("Form 8889", "Levers"),
    ),
    Need(
        "hsa_months",
        "Months with HSA-eligible coverage on the 1st (1-12)",
        "Form 8889 line 3: the limit is pro-rated by month",
        "your plan's start and end dates; 12 if covered all year or on December 1 "
        "(the last-month rule, with a 13-month testing period)",
        "int",
        unlocks=("Form 8889",),
    ),
    Need(
        "hsa_qualified_expenses",
        "Qualified medical expenses paid from the HSA",
        "Form 8889 line 15: HSA money not spent on medical care is taxed",
        "your HSA's claims history or receipts for each 1099-SA distribution",
        "money",
        unlocks=("Form 8889",),
    ),
    Need(
        "hsa_family_share",
        "Your share of the family HSA limit, in percent, when each spouse has an HSA",
        "Form 8889 line 6: spouses with their own HSAs split the family limit, "
        "equally unless they agree otherwise",
        "the split you and your spouse agree on; 50 when you have not chosen",
        "int",
        asked=lambda s: (
            s.get("filing_status") == "married_joint"
            and "family" in (s.get("hsa_coverage"), s.get("spouse_hsa_coverage"))
        ),
        unlocks=("Form 8889",),
    ),
    Need(
        "se_health_premiums",
        "Health premiums paid (self-employed), before any premium tax credit",
        "the SE health deduction and ACA reconciliation (the deduction is these "
        "premiums less the credit you are allowed, IRS Pub. 974)",
        "the marketplace or insurer billing statement, or 1095-A column A summed",
        "money",
        doc="insurer",
        unlocks=("Levers", "Draft 1040"),
    ),
    Need(
        "slcsp_monthly",
        "Benchmark silver (SLCSP) premium, monthly",
        "the ACA credit",
        "column B (January), or healthcare.gov's tax tool",
        "money",
        boxes=(("1095-A", "slcsp_01"),),
        doc="f1095a",
        unlocks=("ACA credit", "MAGI headroom"),
    ),
    Need(
        "aptc",
        "Advance premium tax credit paid for the year",
        "Form 8962: any of it above the credit you are allowed is repaid",
        "column C summed over the year (the monthly boxes are added), "
        "or your marketplace account's year-end statement",
        "money",
        boxes=tuple(("1095-A", f"aptc_{m:02d}") for m in range(1, 13)),
        doc="f1095a",
        unlocks=("ACA credit", "Draft 1040"),
    ),
)

SPOUSE = "spouse_"  # a joint spouse's own line: the head's key behind this
# Each spouse's own saver's credit lines (Form 8880 columns (a) and (b)).
PERSON_8880 = (*PERSON_SAVERS, "savers_barred")
SAVES = ("traditional_ira_contribution", "roth_ira_contribution", "elective_deferrals")


def _saves(s: dict[str, Any], prefix: str) -> bool:
    """The person made a contribution Form 8880 counts (lines 1 and 2)."""
    return any(float(s.get(prefix + k) or 0) > 0 for k in SAVES)


def _dependent_care(s: dict[str, Any]) -> bool:
    """Either spouse has W-2 box 10 benefits (Form 2441 lines 13-14 follow)."""
    return any(
        float(s.get(k) or 0) > 0
        for k in ("dependent_care_benefits", SPOUSE + "dependent_care_benefits")
    )


# Each spouse's own Form 8889 (unit 3a-7): typed and summed per person, like
# PERSON_INPUTS, though the engine takes only the household's deduction.
PERSON_HSA = (
    "hsa_coverage",
    "hsa_contributions",
    "hsa_employer_contributions",
    "hsa_months",
    "hsa_qualified_expenses",
)


def _spouse_asked(
    head: Callable[[dict[str, Any]], bool] | None, key: str
) -> Callable[[dict[str, Any]], bool]:
    def asked(s: dict[str, Any]) -> bool:
        if s.get("filing_status") != "married_joint" or not s.get("spouse_birth_date"):
            return False
        if key == "tipped_occupation_code":  # moot without the spouse's own tips
            return float(s.get(SPOUSE + "qualified_tips") or 0) > 0
        if key in ("savers_distributions", "savers_barred"):
            return _saves(s, SPOUSE)
        return head is None or head(s)

    return asked


def _with_spouse(needs: tuple[Need, ...]) -> tuple[Need, ...]:
    """Each per-person line (PERSON_INPUTS, PERSON_HSA, PERSON_8880) is the head's, summed from the
    head's documents, followed by a joint spouse's twin summed from theirs
    (``data/inbox/spouse/``, or ``planner owner``)."""
    out: list[Need] = []
    for n in needs:
        if n.key not in (*PERSON_INPUTS, *PERSON_HSA, *PERSON_8880):
            out.append(n)
            continue
        out.append(replace(n, owner="you"))
        out.append(
            replace(
                n,
                key=SPOUSE + n.key,
                label=f"Spouse's {n.label[0].lower()}{n.label[1:]}",
                source=f"the spouse's own: {n.source}",
                owner="spouse",
                asked=_spouse_asked(n.asked, n.key),
            )
        )
    return tuple(out)


NEEDS = _with_spouse(NEEDS)


@dataclass
class Group:
    """The items one document closes, with the outputs they unlock together.
    ``doc`` is None for the items no document supplies (typed answers)."""

    doc: Doc | None
    items: list[Status] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.doc.name if self.doc else TYPED

    @property
    def unlocks(self) -> tuple[str, ...]:
        """Every output the group's items feed, each once, in the order
        OUTPUTS lists them."""
        held = {u for st in self.items for u in st.need.unlocks}
        return tuple(o for o in OUTPUTS if o in held)


def group_by_document(items: Iterable[Status]) -> list[Group]:
    """One group per document, in the order each is first needed; the items
    no document supplies come last. Item order within a group is kept."""
    by_doc: dict[str, Group] = {}
    typed = Group(None)
    for st in items:
        if st.need.doc:
            by_doc.setdefault(st.need.doc, Group(DOCS[st.need.doc])).items.append(st)
        else:
            typed.items.append(st)
    return [*by_doc.values(), *([typed] if typed.items else [])]


def schedule_b_required(interest: Any, dividends: Any) -> bool:
    """Schedule B is filed when taxable interest or ordinary dividends are
    over $1,500 (Schedule B instructions); a missing figure counts as 0."""
    return any(float(x or 0) > SCHEDULE_B_OVER for x in (interest, dividends))


def manual_path(lay: Layout, year: int) -> Path:
    return lay.data / "manual" / f"{year}.yaml"


def profile_path(lay: Layout) -> Path:
    return lay.data / "profile" / "assumptions.yaml"


def dont_have_path(lay: Layout) -> Path:
    return lay.data / "profile" / "dont_have.yaml"


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")


def load_manual(lay: Layout, year: int) -> dict[str, Any]:
    data = _read(manual_path(lay, year))
    data.setdefault(MANUAL_VALUES, {})
    data.setdefault(MANUAL_DONT_HAVE, [])
    return data


def load_profile(lay: Layout) -> dict[str, Any]:
    path = profile_path(lay)
    if not path.exists():
        example = lay.config / "assumptions.example.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    return load_assumptions(path)


def account_need(number: str, death: bool = False) -> Need:
    """The two items an export cannot answer about an account it names: what
    kind of account it is, and for an inherited IRA, when the clock started."""
    if death:
        return Need(
            f"{ACCOUNT}{number}:death",
            f"Date of death for inherited IRA {number}",
            "the 10-year emptying deadline",
            "the inheritance paperwork or the custodian's beneficiary letter",
            "date",
            PROFILE,
            unlocks=("Withdrawal plan", "Glide path"),
        )
    return Need(
        f"{ACCOUNT}{number}",
        f"Account type for {number}",
        "which balances are spendable, locked or convertible",
        "the account's statement header: brokerage = taxable, traditional IRA, "
        "inherited IRA, Roth IRA, HSA, or a bank account = cash",
        "enum",
        PROFILE,
        choices=portfolio.TYPES,
        unlocks=("Withdrawal plan", "Roth conversion", "Glide path"),
    )


def need_for(key: str) -> Need:
    for n in NEEDS:
        if n.key == key:
            return n
    if key.startswith(ACCOUNT):
        number, _, tail = key[len(ACCOUNT) :].partition(":")
        if number and tail in ("", "death"):
            return account_need(number, death=tail == "death")
    raise KeyError(f"no such item: {key}")


# Bounds on typed answers: amounts that may be negative, whole-number ranges,
# and the cap past which a figure is a typo rather than a fact.
SIGNED = frozenset({"se_income", "short_term_gains", "long_term_gains"})
# The Treasury tipped-occupation list (IRS.gov/TippedOccupations) numbers its
# occupations with three-digit codes; 0 is "none".
DEPENDENTS_MAX = 20
INT_RANGE = {
    "spouse_premarriage_dependents": (0, DEPENDENTS_MAX),
    "ss_claim_age": (62, 70),
    "hsa_months": (0, 12),
    "hsa_family_share": (0, 100),
    "tipped_occupation_code": (0, 999),
    "nj_veterans": (0, 2),
}
MONEY_MAX = 100_000_000
TEXT_MAX = 200
DEPENDENT_MARKS = ("student", "disabled")  # full-time student; permanently disabled


def _number(need: Need, s: str, what: str) -> float:
    cleaned = s.replace("$", "").replace(",", "").replace(" ", "")
    neg = cleaned.startswith("(") and cleaned.endswith(")")
    try:
        v = float(cleaned.strip("()"))
    except ValueError:
        v = math.nan
    if not math.isfinite(v):
        raise ValueError(f"{need.key}: {what}, got {s!r}")
    return -v if neg else v


def _origin(f: db.FactRow) -> str:
    """Form, year and issuer, and the file and page a document fact came
    from; a fact the planner made itself (page 0) has no page to cite."""
    where = f"{f.form} {f.tax_year} {f.issuer}"
    return f"{where} ({f.file_name} p.{f.page})" if f.page else where


def _monthly(need: Need, s: str) -> dict[int, float]:
    """ "1:85, 2:60" -> {1: 85.0, 2: 60.0}: hours per calendar month."""
    out: dict[int, float] = {}
    for part in filter(None, (p.strip() for p in s.split(","))):
        month, sep, hours = part.partition(":")
        if not sep or not month.strip().isdigit() or not 1 <= int(month) <= 12:
            raise ValueError(f"{need.key}: month:hours with a month 1-12, got {part!r}")
        value = _number(need, hours.strip(), "hours")
        if not 0 <= value <= 744:  # 31 days x 24 hours
            raise ValueError(f"{need.key}: hours in a month 0-744, got {hours.strip()}")
        out[int(month)] = value
    if not out:
        raise ValueError(f"{need.key}: month:hours, ..., got {s!r}")
    return out


def _date(key: str, s: str) -> str:
    from datetime import date

    try:
        d = date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"{key}: a date as YYYY-MM-DD, got {s!r}") from None
    if not date(1900, 1, 1) <= d <= date.today():
        raise ValueError(f"{key}: between 1900-01-01 and today, got {s}")
    return d.isoformat()


def _dependents(key: str, s: str) -> list[dict[str, Any]]:
    """``2018-03-02, 2006-07-01 student; 1950-01-09 disabled`` or ``none``."""
    if s.lower() == "none":
        return []
    out = []
    for entry in (e.split() for e in re.split(r"[,;]", s) if e.strip()):
        marks = {m.lower() for m in entry[1:]}
        if unknown := sorted(marks - set(DEPENDENT_MARKS)):
            raise ValueError(
                f"{key}: unknown mark {unknown[0]!r} (after a birth date: "
                f"{' or '.join(DEPENDENT_MARKS)})"
            )
        out.append(
            {"birth_date": _date(key, entry[0])}
            | {m: m in marks for m in DEPENDENT_MARKS}
        )
    if len(out) > DEPENDENTS_MAX:
        raise ValueError(f"{key}: at most {DEPENDENTS_MAX} dependents")
    return out


_AMOUNT = r"\$?([\d,]+(?:\.\d+)?)"
_EDUCATION = re.compile(
    rf"(you|spouse|dependent\s+(\d+))\s+{_AMOUNT}(?:\s+aid\s+{_AMOUNT})?"
    r"\s+(aotc|llc)",
    re.IGNORECASE,
)


def _education(key: str, s: str) -> list[dict[str, Any]]:
    """``dependent 1 6500 aid 1500 aotc; you 3000 llc`` or ``none``: each
    student (you, spouse or dependent N), the expenses paid, the tax-free
    assistance that paid them, and the credit claimed."""
    if s.lower() == "none":
        return []
    out: list[dict[str, Any]] = []
    for entry in (e.strip() for e in s.split(";") if e.strip()):
        m = _EDUCATION.fullmatch(entry)
        if m is None:
            raise ValueError(
                f"{key}: {entry!r} is not who, paid, [aid amount,] aotc or llc "
                "(like dependent 1 6500 aid 1500 aotc; you 3000 llc), or none"
            )
        who = m.group(1).lower().split()
        student = " ".join(who) if who[0] != "dependent" else f"dependent {int(who[1])}"
        if student == "dependent 0":
            raise ValueError(f"{key}: dependents count from 1, in the order typed")
        if any(e["student"] == student for e in out):
            raise ValueError(f"{key}: {student} is named twice")
        paid, aid = (round(float((g or "0").replace(",", ""))) for g in m.group(3, 4))
        if max(paid, aid) > MONEY_MAX:
            raise ValueError(f"{key}: over {MONEY_MAX:,}; check the figure")
        out.append(
            {"student": student, "paid": paid, "aid": aid, "credit": m.group(5).lower()}
        )
    if len(out) > DEPENDENTS_MAX + 2:
        raise ValueError(f"{key}: at most {DEPENDENTS_MAX + 2} students")
    return out


def _known_state(so_far: dict[str, Any]) -> bool:
    """The state is typed as a state's code: where you lived and whether a
    locality taxes your income are asked in every state (unit 3d-5)."""
    code = so_far.get("state")
    return isinstance(code, str) and code in states.STATES


def _other_taxing_state(so_far: dict[str, Any]) -> bool:
    """A state other than NC that taxes income: its withholding and last
    year's tax are asked under their own keys (NC's are nc_withheld and
    prior_nc_tax, which its D-400 draft reads)."""
    code = so_far.get("state")
    return (
        isinstance(code, str)
        and code in states.STATES
        and code != "NC"
        and states.get(code).income_tax
    )


def parse_value(need: Need, text: str) -> Any:
    """Typed, validated; a bad answer is an error naming what is expected,
    never a guess."""
    s = text.strip()
    if need.kind == "date":
        return _date(need.key, s)
    if need.kind == "date_or_none":
        return "none" if s.lower() == "none" else _date(need.key, s)
    if need.kind == "dependents":
        return _dependents(need.key, s)
    if need.kind == "education":
        return _education(need.key, s)
    if need.kind == "rentals":
        return sche.parse(need.key, s)
    if need.kind == "k1s":
        return k1.parse(need.key, s)
    if need.kind == "farms":
        return schf.parse(need.key, s)
    if need.kind == "money":
        value = int(round(_number(need, s, "a dollar amount")))
        if value < 0 and need.key.removeprefix(SPOUSE) not in SIGNED:
            raise ValueError(f"{need.key}: not negative, got {s}")
        if abs(value) > MONEY_MAX:
            raise ValueError(f"{need.key}: over {MONEY_MAX:,}; check the figure")
        return value
    if need.kind == "int":
        v = _number(need, s, "a whole number")
        if v != int(v):
            raise ValueError(f"{need.key}: a whole number, got {s}")
        lo, hi = INT_RANGE.get(
            need.key, INT_RANGE.get(need.key.removeprefix(SPOUSE), (0, MONEY_MAX))
        )
        if not lo <= v <= hi:
            raise ValueError(f"{need.key}: between {lo} and {hi}, got {s}")
        return int(v)
    if need.kind == "fraction":
        expected = f"{need.key}: a fraction between 0 and 1 (or a percent)"
        try:
            v = float(s.rstrip("%"))
        except ValueError:
            raise ValueError(f"{expected}, got {s!r}") from None
        v = v / 100 if s.endswith("%") or v > 1 else v
        if not 0 <= v <= 1:  # also false for nan
            raise ValueError(f"{expected}, got {s}")
        return v
    if need.kind == "monthly":
        return _monthly(need, s)
    if need.kind == "enum":
        low = s.lower()
        if low not in need.choices:
            raise ValueError(f"{need.key}: one of {', '.join(need.choices)}")
        return low
    if need.choices:  # a code from a fixed list (the state)
        code = s.upper()
        if code not in need.choices:
            raise ValueError(
                f"{need.key}: a two-letter code ({', '.join(need.choices)})"
            )
        return code
    if len(s) > TEXT_MAX:
        raise ValueError(f"{need.key}: at most {TEXT_MAX} characters")
    if any(not c.isprintable() for c in s):
        raise ValueError(f"{need.key}: no control characters")
    return s


def enter(lay: Layout, year: int, key: str, text: str) -> Any:
    """Store one typed answer: profile keys go to the profile, others to the
    year's manual file. Returns the stored value."""
    need = need_for(key)
    value = parse_value(need, text)
    if key.startswith(ACCOUNT):
        number, _, tail = key[len(ACCOUNT) :].partition(":")
        portfolio.save_account(
            lay, number, **{"date_of_death" if tail else "type": value}
        )
    elif need.scope == PROFILE:
        path = profile_path(lay)
        load_profile(lay)
        data = _read(path)
        data[key] = value
        _write(path, {k: data.get(k) for k in ASSUMPTION_FIELDS})
        dh = _read(dont_have_path(lay))
        if key in dh.get(MANUAL_DONT_HAVE, []):
            dh[MANUAL_DONT_HAVE].remove(key)
            _write(dont_have_path(lay), dh)
    else:
        data = load_manual(lay, year)
        data[MANUAL_VALUES][key] = value
        if key in data[MANUAL_DONT_HAVE]:
            data[MANUAL_DONT_HAVE].remove(key)
        _write(manual_path(lay, year), data)
    return value


def dont_have(lay: Layout, year: int, key: str) -> None:
    need = need_for(key)
    path = dont_have_path(lay) if need.scope == PROFILE else manual_path(lay, year)
    data = _read(path) if need.scope == PROFILE else load_manual(lay, year)
    data.setdefault(MANUAL_DONT_HAVE, [])
    if key not in data[MANUAL_DONT_HAVE]:
        data[MANUAL_DONT_HAVE].append(key)
    _write(path, data)


def undo_dont_have(lay: Layout, year: int, key: str) -> bool:
    """Put an item marked don't-have back on the Needed list; False when it
    was not marked."""
    need = need_for(key)
    profile = need.scope == PROFILE
    path = dont_have_path(lay) if profile else manual_path(lay, year)
    data = _read(path) if profile else load_manual(lay, year)
    marked = data.get(MANUAL_DONT_HAVE, [])
    if key not in marked:
        return False
    marked.remove(key)
    _write(path, data)
    return True


def _text_box(
    conn: sqlite3.Connection,
    boxes: tuple[tuple[str, str], ...],
    year: int,
    choices: tuple[str, ...] = (),
) -> tuple[str, str] | None:
    """The words a text box holds on the latest return of ``year`` or earlier
    (a filing status or address is last year's until a newer return says
    otherwise); a value outside ``choices`` is not an answer."""
    best: tuple[int, str, str] | None = None
    for form, box in boxes:
        for f in db.facts_for(conn, None, form, text=True):
            if f.box != box or f.tax_year > year or f.text is None:
                continue
            if choices and f.text not in choices:
                continue
            if best is None or f.tax_year >= best[0]:
                where = _origin(f)
                best = (f.tax_year, f.text, where)
    return None if best is None else (best[1], best[2])


def _county_from_return(
    config: Path, conn: sqlite3.Connection, year: int, values: dict[str, Any]
) -> tuple[str | None, str] | None:
    """The county of the ZIP on the filed return, when that ZIP lies wholly in
    one county of the household's state; otherwise the reason it cannot say."""
    held = _text_box(conn, (("1040", "zip"),), year)
    if held is None:
        return None
    name, note = county_from_zip(config, held[0], values.get("state"))
    return name, f"{note} on {held[1]}" if name else note


def _refund_taxable(
    conn: sqlite3.Connection, year: int, values: dict[str, Any]
) -> tuple[float | None, str] | None:
    """The refund worksheet from last year's filed Form 1040 line 12 and this
    year's filing status and birth dates (planner.taxprep.refund); no lead
    without that return."""
    from planner.plan.inputs import FILING  # inputs imports this module

    held = _sum_boxes(conn, (("1040", "12"),), year - 1, None)
    status = FILING.get(str(values.get("filing_status")))
    if held is None or status is None:
        return None
    births = [values.get("birth_date")]
    if status in ("JOINT", "SEPARATE"):
        births.append(values.get("spouse_birth_date"))
    boxes = sum(refund.aged(b, year - 1) for b in births)
    value, why = refund.taxable_part(
        float(values.get("state_refund") or 0.0), held[0], status, year - 1, boxes
    )
    return value, f"{why} ({held[1]}; this year's filing status)"


def _mortgage_pi(conn: sqlite3.Connection, year: int) -> tuple[float, str] | None:
    """Monthly principal and interest from two consecutive years of Form 1098.

    Box 2 is the principal outstanding at the start of the year, so the
    principal paid in year Y is box 2 of Y less box 2 of Y+1, and the year's
    P&I is that plus box 1 (interest) of Y, over 12. 1098s dated after the
    plan ``year`` are ignored. Only a pair that ends in the latest 1098 year on
    file counts: a lender whose statements stop earlier (a loan paid off or
    refinanced away) is no longer the current mortgage. Each such lender is
    paired on its own and the pairs add up (a loan with several lenders
    servicing it in the same years). A pair whose balance rose (a refinance or
    a new loan) is skipped; nothing is inferred. Escrow is not on the 1098."""
    by: dict[tuple[str, int], dict[str, float]] = {}
    for f in db.facts_for(conn, None, "1098"):
        if f.tax_year <= year:
            by.setdefault((f.issuer, f.tax_year), {})[f.box] = f.value
    if not by:
        return None
    latest = max(y for _, y in by)
    pairs: list[tuple[str, int, float]] = []
    for issuer in sorted({i for i, y in by if y == latest}):
        first, later = by.get((issuer, latest - 1), {}), by[issuer, latest]
        if "1" not in first or "2" not in first or "2" not in later:
            continue
        paid = first["2"] - later["2"]
        if paid >= 0:
            pairs.append((issuer, latest - 1, (first["1"] + paid) / 12))
    if not pairs:
        return None
    where = "; ".join(f"1098 {y}-{y + 1} {i}" for i, y, _ in pairs)
    return round(
        sum(m for _, _, m in pairs), 2
    ), f"P&I from {where} (escrow not included)"


def _sum_boxes(
    conn: sqlite3.Connection,
    boxes: tuple[tuple[str, str], ...],
    year: int | None,
    owner: str | None = None,
) -> tuple[float, str] | None:
    total = 0.0
    origins: list[str] = []
    for form, box in boxes:
        name, sign = statereturn.signed(box)
        for f in db.facts_for(conn, year, form):
            if f.box == name and owner in (None, f.owner):
                total += sign * f.value
                origins.append(_origin(f))
    if not origins:
        return None
    return total, "; ".join(dict.fromkeys(origins))


def needed(lay: Layout, year: int) -> NeedsReport:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        return _needed(conn, lay, year)
    finally:
        conn.close()


def _so_far(report: NeedsReport) -> dict[str, Any]:
    got = {
        s.need.key: s.value for s in report.items if s.state in ("actual", "estimate")
    }
    return got | {"_year": report.year}


def _needed(conn: sqlite3.Connection, lay: Layout, year: int) -> NeedsReport:
    profile = load_profile(lay)
    manual = load_manual(lay, year)
    profile_dh = set(_read(dont_have_path(lay)).get(MANUAL_DONT_HAVE, []))
    report = NeedsReport(year)
    for need in NEEDS:
        if need.asked is not None and not need.asked(_so_far(report)):
            continue
        if need.scope == PROFILE and profile.get(need.key) is not None:
            report.items.append(Status(need, "actual", profile[need.key], "profile"))
            continue
        if need.key in manual[MANUAL_VALUES]:
            report.items.append(
                Status(need, "actual", manual[MANUAL_VALUES][need.key], "typed")
            )
            continue
        fact_year = (
            None if need.scope == PROFILE else year - 1 if need.scope == PRIOR else year
        )
        if need.kind in ("enum", "str") and need.boxes:
            word = _text_box(conn, need.boxes, year, need.choices)
            if word is not None:
                report.items.append(Status(need, "actual", word[0], word[1]))
                continue
        hit = (
            _sum_boxes(conn, need.boxes, fact_year, need.owner)
            if need.boxes and need.kind not in ("enum", "str")
            else None
        )
        if hit is not None:
            report.items.append(Status(need, "actual", hit[0], hit[1]))
            continue
        est = (
            _sum_boxes(conn, need.estimate, fact_year, need.owner)
            if need.estimate
            else None
        )
        if est is not None:
            report.items.append(Status(need, "estimate", est[0], est[1]))
            continue
        if need.derive is not None and (calc := need.derive(conn, year)) is not None:
            report.items.append(Status(need, "estimate", calc[0], calc[1]))
            continue
        note = ""
        if need.derive_text is not None:
            so_far = _so_far(report)
            if (said := need.derive_text(lay.config, conn, year, so_far)) is not None:
                if said[0] is not None:
                    report.items.append(Status(need, "actual", said[0], said[1]))
                    continue
                note = said[1]
        dh = profile_dh if need.scope == PROFILE else set(manual[MANUAL_DONT_HAVE])
        if need.key in dh:
            report.items.append(Status(need, "dont_have"))
        else:
            report.items.append(Status(need, "missing", None, note))
    accounts = portfolio.load_accounts(lay)
    for number in portfolio.seen_accounts(conn):
        entry = accounts.get(number, {})
        need = account_need(number)
        if entry.get("type"):
            report.items.append(Status(need, "actual", entry["type"], "accounts.yaml"))
        else:
            state = "dont_have" if need.key in profile_dh else "missing"
            report.items.append(Status(need, state))
        if entry.get("type") == "inherited_ira":
            need = account_need(number, death=True)
            death = entry.get("date_of_death")
            state = (
                "actual"
                if death
                else "dont_have"
                if need.key in profile_dh
                else "missing"
            )
            report.items.append(
                Status(need, state, death, "accounts.yaml" if death else "")
            )
    return report


def need_value(conn: sqlite3.Connection, lay: Layout, year: int, key: str) -> Any:
    """One item's value as the Needed panel sees it (typed, form or estimate),
    or None when it is missing or marked don't-have."""
    return need_values(conn, lay, year, (key,))[key]


def need_values(
    conn: sqlite3.Connection, lay: Layout, year: int, keys: tuple[str, ...]
) -> dict[str, Any]:
    """Several items at once, as ``need_value`` reads each."""
    out: dict[str, Any] = dict.fromkeys(keys)
    for st in _needed(conn, lay, year).items:
        if st.need.key in out and st.state in ("actual", "estimate"):
            out[st.need.key] = st.value
    return out
