"""The ledger as an engine household.

Every input comes from the Needed panel (a form, a YTD estimate or a typed
answer), so the household the planners price is the one the intake loop has
confirmed. Unknown is never zero: a missing item is left out and named in
``Inputs.unknown``; unknown qualified dividends are taxed as ordinary (the
worse case) and said so. Overrides are the few planning numbers no document
can supply: the Q4 dividend estimate, planned sales, a planned conversion (typed,
or ``conversion_target="auto"`` to adopt the recommended one), a planned HSA
contribution and a typed total income for the year.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from planner.engine.household import Household, MissingInputError
from planner.ingest.derive import CountyError, resolve_county
from planner.ingest.needs import _needed
from planner.ledger import db
from planner.paths import Layout
from planner.taxprep import capgains, hsa

FILING = {
    "single": "SINGLE",
    "married_joint": "JOINT",
    "married_separate": "SEPARATE",
    "head_of_household": "HEAD_OF_HOUSEHOLD",
}
REQUIRED = ("birth_date", "filing_status", "state")
# The planner models one person and no dependents. A status whose answer turns
# on a second person is priced as that one person and tagged, never passed off
# as a full plan (master plan unit 0b; lifted when unit 3a models the household).
NOT_HANDLED = {
    "JOINT": (
        "Not handled: married filing jointly is priced for one person; the "
        "spouse's income, age, deductions and credits are left out, so every "
        "figure here is this person's share only, not the joint return"
    ),
    "SEPARATE": (
        "Not handled: married filing separately is priced from this person's "
        "figures alone; the spouse's choice to itemize (which binds this "
        "return), a community-property split and the spouse's figures are left out"
    ),
    "HEAD_OF_HOUSEHOLD": (
        "Not handled: head of household is priced with no qualifying person; "
        "dependents' credits (child tax credit, earned income credit with "
        "children, dependent care) and the larger household for the ACA credit "
        "and benefits are left out"
    ),
}


def scope_gaps(filing_status: str) -> list[str]:
    """The Not handled lines for an engine filing status (empty for SINGLE)."""
    gap = NOT_HANDLED.get(filing_status)
    return [gap] if gap else []


# Needed-panel key -> Household field, dollars rounded to whole dollars.
MONEY = {
    "wages": "wages",
    "se_income": "se_income",
    "interest": "interest",
    "tax_exempt_interest": "tax_exempt_interest",
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
TAX_KEYS = (*MONEY, *CODES, "ordinary_dividends", "qualified_dividends")
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
    become the total less the other income lines the ledger counts.
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

    def __post_init__(self) -> None:
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
    notes: list[str] = field(default_factory=list)
    overrides: Overrides = field(default_factory=Overrides)
    scope: list[str] = field(default_factory=list)  # Not handled lines (also notes)

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


def age_at_year_end(birth: str, year: int) -> int:
    b = date.fromisoformat(birth)
    end = date(year, 12, 31)
    return end.year - b.year - ((end.month, end.day) < (b.month, b.day))


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


def build(
    lay: Layout, year: int, overrides: Overrides | None = None, *, with_db: bool = True
) -> Inputs:
    """The household the planners price, from the Needed panel plus overrides."""
    ov = overrides or Overrides()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        capgains.store(conn, lay, year)  # typed carryovers reach Schedule D
        hsa.store(conn, lay, year)  # and typed HSA answers reach Form 8889
        report = _needed(conn, lay, year)
        recorded = _recorded_conversions(conn, year)
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
    out.scope = scope_gaps(fields["filing_status"])
    out.notes.extend(out.scope)
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
    if recorded > fields.get("roth_conversion", 0):
        fields["roth_conversion"] = int(round(recorded))
        out.origins["roth_conversion"] = "conversions recorded this year"
    if ov.total_income is not None:
        net = int(fields.get("short_term_gains", 0)) + int(
            fields.get("long_term_gains", 0)
        )
        limit = (
            CAPITAL_LOSS_LIMIT_MFS
            if fields["filing_status"] == "SEPARATE"
            else CAPITAL_LOSS_LIMIT
        )
        others = sum(int(fields.get(k, 0)) for k in TOTAL_INCOME_LINES) + max(
            net, -limit
        )
        wages = int(round(ov.total_income)) - others
        if wages < 0:
            raise OverrideError(
                f"total_income {ov.total_income:,.0f} is below the other income "
                f"already counted ({others:,.0f}); type a total of at least that"
            )
        fields["wages"] = wages
        out.origins["wages"] = "total_income override (total less the other income)"
        if "wages" in out.unknown:
            out.unknown.remove("wages")
        if "wages" in out.estimates:
            out.estimates.remove("wages")
        out.notes.append(
            f"total_income {ov.total_income:,.0f} typed: wages {wages:,} = the total "
            f"less {others:,} of other income counted (Social Security excluded); "
            "planned items are added on top"
        )
    # overrides
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
    out.household = Household(**fields)
    return out
