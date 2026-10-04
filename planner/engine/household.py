"""The inputs one tax year of a household needs. Dollars as integers.

Unknown is ``None``, never 0: the engine refuses a household with a required
field missing rather than computing as if it were zero.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

FILING_STATUSES = ("SINGLE", "JOINT", "SEPARATE", "HEAD_OF_HOUSEHOLD")
REQUIRED = ("age", "filing_status", "state")


class MissingInputError(ValueError):
    """A required household field is unknown."""


@dataclass(frozen=True)
class Household:
    age: int
    filing_status: str
    state: str
    county: str | None = None
    wages: int = 0
    se_income: int = 0
    interest: int = 0
    tax_exempt_interest: int = 0  # 1040 line 2a: in ACA MAGI and Social Security
    non_qualified_dividends: int = 0
    qualified_dividends: int = 0
    short_term_gains: int = 0
    long_term_gains: int = 0
    ira_distributions: int = 0
    roth_conversion: int = 0
    social_security: int = 0
    traditional_ira_contribution: int = 0
    # Premiums for the year's self-employed health plan, before any premium tax
    # credit (1095-A column A summed). The engine settles the deduction itself
    # (planner.engine.tax, IRS Pub. 974): the credit comes off the premiums.
    se_health_premiums: int = 0
    hsa_contribution: int = 0  # health_savings_account_ald (tax-unit level)
    slcsp_monthly: int | None = None  # benchmark silver premium; None = engine estimate
    aptc: int = 0  # advance premium tax credit paid for the year (1095-A column C)
    # Schedule 1-A inputs (tax years 2025 to 2028): qualified tips (part of wages),
    # the overtime premium (part of wages) and qualified passenger vehicle loan
    # interest. The Needed panel asks for each (planner.plan.inputs reads them);
    # the default is none.
    qualified_tips: int = 0
    # Treasury tipped-occupation code (IRS.gov/TippedOccupations); the tips
    # deduction needs one, so tips without it carry no deduction.
    tipped_occupation_code: int = 0
    qualified_overtime: int = 0
    car_loan_interest: int = 0
    # Schedule A: gifts to charity (cash, and shares at market value) and the
    # two largest other itemized lines. The engine decides whether itemizing
    # beats the standard deduction.
    charitable_cash: int = 0
    charitable_shares: int = 0
    real_estate_taxes: int = 0
    mortgage_interest: int = 0
    other: dict[str, int] = field(default_factory=dict)
    # Tax-unit variables the engine takes as given instead of computing: the
    # draft return sets a Schedule 1-A deduction to the form's own figure (the
    # form rounds its phase-out; the engine does not) so the tax is priced on
    # the form's taxable income.
    tax_unit_inputs: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        missing = [
            f.name
            for f in fields(self)
            if f.name in ("age", "filing_status", "state")
            and getattr(self, f.name) is None
        ]
        if missing:
            raise MissingInputError(f"household needs {missing}")
        if self.filing_status not in FILING_STATUSES:
            raise ValueError(f"filing_status must be one of {FILING_STATUSES}")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Household:
        """Build from a YAML mapping; unknown keys are an error, never dropped."""
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown household fields: {unknown}")
        return cls(**dict(data))

    def situation(self, year: int, *, omit: Iterable[str] = ()) -> dict[str, Any]:
        """The policyengine-us situation dict for one person, one tax unit, one year.

        ``omit`` drops person inputs (a swept axis must not also be a fixed input).
        """
        y = year
        person: dict[str, Any] = {
            "age": {y: self.age},
            "employment_income": {y: self.wages},
            "self_employment_income": {y: self.se_income},
            "taxable_interest_income": {y: self.interest},
            "tax_exempt_interest_income": {y: self.tax_exempt_interest},
            "non_qualified_dividend_income": {y: self.non_qualified_dividends},
            "qualified_dividend_income": {y: self.qualified_dividends},
            "short_term_capital_gains": {y: self.short_term_gains},
            "long_term_capital_gains": {y: self.long_term_gains},
            "taxable_ira_distributions": {y: self.ira_distributions},
            "taxable_roth_conversions": {y: self.roth_conversion},
            "social_security": {y: self.social_security},
            "traditional_ira_contributions": {y: self.traditional_ira_contribution},
            "self_employed_health_insurance_premiums": {y: self.se_health_premiums},
            "tip_income": {y: self.qualified_tips},
            "treasury_tipped_occupation_code": {y: self.tipped_occupation_code},
            "fsla_overtime_premium": {y: self.qualified_overtime},
            "charitable_cash_donations": {y: self.charitable_cash},
            "charitable_non_cash_donations": {y: self.charitable_shares},
            "real_estate_taxes": {y: self.real_estate_taxes},
            "home_mortgage_interest": {y: self.mortgage_interest},
        }
        for k, v in self.other.items():
            person[k] = {y: v}
        for name in omit:
            person.pop(name, None)
        tax_unit: dict[str, Any] = {
            "members": ["p"],
            "filing_status": {y: self.filing_status},
            "health_savings_account_ald": {y: self.hsa_contribution},
        }
        for k, amount in self.tax_unit_inputs.items():
            tax_unit[k] = {y: amount}
        if self.slcsp_monthly is not None:
            tax_unit["slcsp"] = {
                f"{y}-{m:02d}": self.slcsp_monthly for m in range(1, 13)
            }
        # The prior year carries the state and county too: the ACA credit reads the
        # prior year's poverty guideline, which differs for Alaska and Hawaii.
        years = (y - 1, y)
        household: dict[str, Any] = {
            "members": ["p"],
            "state_name": {yr: self.state for yr in years},
            "qualified_passenger_vehicle_loan_interest": {y: self.car_loan_interest},
        }
        if self.county:
            if (
                self.county != self.county.upper()
                or " " in self.county
                or not self.county.endswith(f"_{self.state}")
            ):
                raise ValueError(
                    f"county {self.county!r} is not the engine's name for a county "
                    f"of {self.state}: the county name, its kind and the state in "
                    "capitals joined by underscores, like WAKE_COUNTY_NC "
                    "(planner.ingest.derive.resolve_county turns a typed name into it)"
                )
            household["county"] = {yr: self.county for yr in years}
        return {
            "people": {"p": person},
            "tax_units": {"tu": tax_unit},
            "households": {"hh": household},
        }


def load_household(path: Path) -> Household:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping")
    return Household.from_mapping(data)
