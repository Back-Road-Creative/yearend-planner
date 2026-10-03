"""The ledger as an engine household.

Every input comes from the Needed panel (a form, a YTD estimate or a typed
answer), so the household the planners price is the one the intake loop has
confirmed. Unknown is never zero: a missing item is left out and named in
``Inputs.unknown``; unknown qualified dividends are taxed as ordinary (the
worse case) and said so. Overrides are the few planning numbers no document
can supply: the Q4 dividend estimate, planned sales, a planned conversion and
a planned HSA contribution.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from planner.engine.household import Household, MissingInputError
from planner.ingest.needs import _needed
from planner.ledger import db
from planner.paths import Layout

FILING = {
    "single": "SINGLE",
    "married_joint": "JOINT",
    "married_separate": "SEPARATE",
    "head_of_household": "HEAD_OF_HOUSEHOLD",
}
REQUIRED = ("birth_date", "filing_status", "state")
# Needed-panel key -> Household field, dollars rounded to whole dollars.
MONEY = {
    "wages": "wages",
    "se_income": "se_income",
    "interest": "interest",
    "short_term_gains": "short_term_gains",
    "long_term_gains": "long_term_gains",
    "ira_distributions": "ira_distributions",
    "roth_conversion": "roth_conversion",
    "social_security": "social_security",
    "traditional_ira_contribution": "traditional_ira_contribution",
    "se_health_premiums": "se_health_premiums",
    "slcsp_monthly": "slcsp_monthly",
    "hsa_contribution": "hsa_contribution",
}
OVERRIDES = (
    "q4_dividend_estimate",
    "planned_st_sales",
    "planned_lt_sales",
    "planned_conversion",
    "planned_hsa",
)


@dataclass(frozen=True)
class Overrides:
    """Planning numbers added on top of the ledger (dollars). ``planned_hsa``
    replaces the year's HSA figure; the others add to it."""

    q4_dividend_estimate: float = 0.0
    planned_st_sales: float = 0.0
    planned_lt_sales: float = 0.0
    planned_conversion: float = 0.0
    planned_hsa: float | None = None

    def describe(self) -> list[str]:
        out = [
            f"{k} {getattr(self, k):,.0f}" for k in OVERRIDES[:4] if getattr(self, k)
        ]
        if self.planned_hsa is not None:
            out.append(f"planned_hsa {self.planned_hsa:,.0f}")
        return out


@dataclass
class Inputs:
    year: int
    household: Household
    origins: dict[str, str] = field(default_factory=dict)  # key -> where it came from
    estimates: list[str] = field(default_factory=list)  # keys standing in from YTD
    unknown: list[str] = field(default_factory=list)  # keys left out (not zero)
    notes: list[str] = field(default_factory=list)
    overrides: Overrides = field(default_factory=Overrides)


def age_at_year_end(birth: str, year: int) -> int:
    b = date.fromisoformat(birth)
    end = date(year, 12, 31)
    return end.year - b.year - ((end.month, end.day) < (b.month, b.day))


def _recorded_conversions(conn: sqlite3.Connection, year: int) -> float:
    return round(
        sum(c.amount for c in db.conversions(conn) if c.date.startswith(f"{year}-")),
        2,
    )


def build(
    lay: Layout, year: int, overrides: Overrides | None = None, *, with_db: bool = True
) -> Inputs:
    """The household the planners price, from the Needed panel plus overrides."""
    ov = overrides or Overrides()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
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
    missing = [k for k in REQUIRED if value.get(k) is None]
    if missing:
        raise MissingInputError(
            f"the plan needs {', '.join(missing)} (planner needed --year {year})"
        )
    fields: dict[str, Any] = {
        "age": age_at_year_end(str(value["birth_date"]), year),
        "filing_status": FILING[str(value["filing_status"])],
        "state": str(value["state"]),
        "county": value.get("county"),
    }
    for key, name in MONEY.items():
        if value.get(key) is not None:
            fields[name] = int(round(float(value[key])))
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
