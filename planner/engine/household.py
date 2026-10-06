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

FILING_STATUSES = (
    "SINGLE",
    "JOINT",
    "SEPARATE",
    "HEAD_OF_HOUSEHOLD",
    "SURVIVING_SPOUSE",  # joint rates, no spouse in the tax unit (unit 3b-1)
)
REQUIRED = ("age", "filing_status", "state")


class MissingInputError(ValueError):
    """A required household field is unknown."""


# Each person's own figures, as the engine names them (planner.engine.tax reads
# a person figure summed over the tax unit, so a joint return is the couple's).
PERSON_INPUTS = {
    "wages": "employment_income",
    "se_income": "self_employment_income",
    "ira_distributions": "taxable_ira_distributions",
    "roth_conversion": "taxable_roth_conversions",
    "social_security": "social_security",
    "traditional_ira_contribution": "traditional_ira_contributions",
    "qualified_tips": "tip_income",
    "tipped_occupation_code": "treasury_tipped_occupation_code",
    "qualified_overtime": "fsla_overtime_premium",
    # W-2 box 10 (Form 2441 line 12); planner.engine.tax.dependent_care works
    # the exclusion out and pins it (unit 3c-1)
    "dependent_care_benefits": "dependent_care_employer_benefits",
}


# Each person's own Form 8880 money lines (unit 3c-3), typed or summed per
# person like PERSON_INPUTS; the engine takes the line 5 they make, not these.
PERSON_SAVERS = ("roth_ira_contribution", "elective_deferrals", "savers_distributions")


@dataclass(frozen=True)
class Person:
    """The spouse on a joint return: their age and their own income. Investment
    income, deductions and the household's health plan stay on Household."""

    age: int
    wages: int = 0
    se_income: int = 0
    ira_distributions: int = 0
    roth_conversion: int = 0
    social_security: int = 0
    traditional_ira_contribution: int = 0
    qualified_tips: int = 0
    tipped_occupation_code: int = 0
    qualified_overtime: int = 0
    dependent_care_benefits: int = 0  # their W-2 box 10
    # Form 8880 (unit 3c-3): their Roth IRA and ABLE contributions (line 1 with the
    # traditional IRA), elective deferrals (line 2), the testing period's
    # distributions (line 4) and whether they can take the credit at all (None:
    # the engine's own age, student and dependent tests)
    roth_ira_contribution: int = 0
    elective_deferrals: int = 0
    savers_distributions: int = 0
    savers_eligible: bool | None = None
    # Their W-2 boxes 3 and 7, to the wage base (Schedule SE line 8a: unit 3a-5);
    # None = the engine takes their wages
    ss_wages: int | None = None


@dataclass(frozen=True)
class Dependent:
    """A dependent the return claims. The engine decides which credit each one
    earns (a child under 17, a student under 24 or a disabled person of any age
    is a qualifying child; others take the credit for other dependents)."""

    age: int
    full_time_student: bool = False
    disabled: bool = False
    wages: int = 0


# The engine's eligibility tests for each credit (policyengine-us takes them as
# inputs, all false by default): the student's answers that the typed credit
# claims are true (2025 Form 8863 Part III lines 23-26 and the 1098-T and EIN
# requirements; the lifetime learning credit needs only the school and form).
EDUCATION_CREDITS = {
    "aotc": (
        "is_pursuing_credential_for_american_opportunity_credit",
        "attends_eligible_educational_institution_for_american_opportunity_credit",
        "is_enrolled_at_least_half_time_for_american_opportunity_credit",
        "has_american_opportunity_credit_1098_t_or_exception",
        "has_american_opportunity_credit_institution_ein",
    ),
    "llc": (
        "attends_eligible_educational_institution_for_lifetime_learning_credit",
        "has_lifetime_learning_credit_1098_t_or_exception",
    ),
}


@dataclass(frozen=True)
class Student:
    """A student on the return (unit 3c-2): who ("p" the head, "s" the spouse,
    "d1"... a dependent in order), the adjusted qualified education expenses
    (paid less tax-free assistance: Form 8863's worksheet, before the $4,000
    or $10,000 cap) and the credit claimed for them, "aotc" (the American
    opportunity credit) or "llc" (the lifetime learning credit)."""

    who: str
    expenses: int
    credit: str


def _people(kind: str, cls: type, data: Any) -> Any:
    if not isinstance(data, Mapping):
        raise ValueError(f"{kind}: expected a mapping, not {data!r}")
    unknown = sorted(set(data) - {f.name for f in fields(cls)})
    if unknown:
        raise ValueError(f"unknown {kind} fields: {unknown}")
    return cls(**dict(data))


def savers_line5(x: Household | Person, floor: int) -> int:
    """Form 8880 line 5 for one person: lines 1 and 2 less line 4, not below
    zero. ``floor`` is the least line 4 can be (the year's IRA distributions)."""
    line3 = x.traditional_ira_contribution + x.roth_ira_contribution
    return max(line3 + x.elective_deferrals - max(x.savers_distributions, floor), 0)


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
    # Schedule 1 (unit 3e-1): unemployment compensation (line 7, 1099-G box 1),
    # the taxable part of a state or local income tax refund (line 1, the tax
    # benefit rule's worksheet) and canceled debt (line 8c, 1099-C box 2).
    unemployment: int = 0
    salt_refund: int = 0
    cancelled_debt: int = 0
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
    # Form 2441 (unit 3c-1): care paid for the year for a child under 13 (the
    # engine's qualifying persons), the head's W-2 box 10 benefits (a spouse's
    # are on Person), and the plan's grace-period carryover used (line 13) and
    # amount forfeited or carried forward (line 14), both the return's.
    care_expenses: int = 0
    dependent_care_benefits: int = 0
    dependent_care_grace: int = 0
    dependent_care_forfeited: int = 0
    # Form 8880 (unit 3c-3): the head's Roth IRA and ABLE contributions (line 1 with the
    # traditional IRA), elective deferrals (line 2), the testing period's
    # distributions (line 4) and whether the head can take the credit at all (None:
    # the engine's own age, student and dependent tests)
    roth_ira_contribution: int = 0
    elective_deferrals: int = 0
    savers_distributions: int = 0
    savers_eligible: bool | None = None
    other: dict[str, int] = field(default_factory=dict)
    # Tax-unit variables the engine takes as given instead of computing: the
    # draft return sets a Schedule 1-A deduction to the form's own figure (the
    # form rounds its phase-out; the engine does not) so the tax is priced on
    # the form's taxable income.
    tax_unit_inputs: dict[str, float] = field(default_factory=dict)
    spouse: Person | None = None  # a joint return's second person (unit 3a-1)
    dependents: tuple[Dependent, ...] = ()
    # Form 8863 (unit 3c-2): each student and their credit, and the line 7
    # box: a filer under 24 who meets its three conditions gets no refundable
    # American opportunity credit (all of it is nonrefundable).
    students: tuple[Student, ...] = ()
    aotc_refundable_barred: bool = False

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
        if self.spouse is not None and self.filing_status != "JOINT":
            raise ValueError(
                f"a spouse files JOINT here, not {self.filing_status}: a separate "
                "return's spouse is not in its tax unit"
            )
        people = {"p", *(("s",) if self.spouse is not None else ())}
        people |= {f"d{i}" for i in range(1, len(self.dependents) + 1)}
        seen: set[str] = set()
        for st in self.students:
            if st.who not in people or st.who in seen:
                raise ValueError(
                    f"student {st.who!r}: not a person on the return or named twice "
                    f"(one of {sorted(people)}, each once)"
                )
            if st.credit not in EDUCATION_CREDITS or st.expenses < 0:
                raise ValueError(
                    f"student {st.who!r}: credit one of {sorted(EDUCATION_CREDITS)} "
                    f"and expenses not negative, not {st.credit!r} {st.expenses}"
                )
            seen.add(st.who)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Household:
        """Build from a YAML mapping; unknown keys are an error, never dropped."""
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown household fields: {unknown}")
        out = dict(data)
        if out.get("spouse") is not None:
            out["spouse"] = _people("spouse", Person, out["spouse"])
        out["dependents"] = tuple(
            _people("dependent", Dependent, d) for d in out.get("dependents") or ()
        )
        out["students"] = tuple(
            _people("student", Student, st) for st in out.get("students") or ()
        )
        return cls(**out)

    def situation(self, year: int, *, omit: Iterable[str] = ()) -> dict[str, Any]:
        """The policyengine-us situation dict for one tax unit and one year: the
        first person ("p", the head), the spouse ("s") and dependents ("d1"...).

        ``omit`` drops the head's inputs (a swept axis must not also be a fixed
        input; an engine axis moves the first person only).
        """
        omit = tuple(omit)
        y = year
        person: dict[str, Any] = {
            "age": {y: self.age},
            "employment_income": {y: self.wages},
            "self_employment_income": {y: self.se_income},
            "taxable_interest_income": {y: self.interest},
            "tax_exempt_interest_income": {y: self.tax_exempt_interest},
            "unemployment_compensation": {y: self.unemployment},
            "salt_refund_income": {y: self.salt_refund},
            "debt_relief": {y: self.cancelled_debt},
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
        if self.care_expenses:  # the engine's own default is the SPM unit's
            person["care_expenses"] = {y: self.care_expenses}
        if self.dependent_care_benefits:
            person["dependent_care_employer_benefits"] = {
                y: self.dependent_care_benefits
            }
        for k, v in self.other.items():
            person[k] = {y: v}
        for name in omit:
            person.pop(name, None)
        people: dict[str, Any] = {"p": person}
        head = {"is_tax_unit_head": {y: True}}
        person.update(head)
        if self.spouse is not None:
            sp = self.spouse
            people["s"] = {
                "age": {y: sp.age},
                "is_tax_unit_spouse": {y: True},
                **{PERSON_INPUTS[k]: {y: getattr(sp, k)} for k in PERSON_INPUTS},
            }
            if sp.ss_wages is not None:
                people["s"]["taxable_earnings_for_social_security"] = {y: sp.ss_wages}
        for i, d in enumerate(self.dependents, 1):
            people[f"d{i}"] = {
                "age": {y: d.age},
                "is_tax_unit_dependent": {y: True},
                "is_full_time_student": {y: d.full_time_student},
                "is_disabled": {y: d.disabled},
                "is_permanently_and_totally_disabled": {y: d.disabled},
                "employment_income": {y: d.wages},
            }
        for st in self.students:
            people[st.who]["qualified_tuition_expenses"] = {y: st.expenses}
            for flag in EDUCATION_CREDITS[st.credit]:
                people[st.who][flag] = {y: True}
        savers = [("p", self), *((("s", self.spouse),) if self.spouse else ())]
        # Line 4 holds at least the year's own IRA distributions, both spouses'
        # on a joint return (Form 8880 line 4); conversions are not on it.
        floor = sum(x.ira_distributions for _, x in savers)
        for who, x in savers:
            if who == "p" and {
                "traditional_ira_contributions",
                "taxable_ira_distributions",
            } & set(omit):
                continue  # a sweep moves them: the engine's own line 5 follows
            people[who]["savers_credit_qualified_contributions"] = {
                y: savers_line5(x, floor)
            }
            if x.savers_eligible is not None:
                people[who]["savers_credit_eligible_person"] = {y: x.savers_eligible}
        members = list(people)
        couple = [m for m in ("p", "s") if m in people]
        tax_unit: dict[str, Any] = {
            "members": members,
            "filing_status": {y: self.filing_status},
            "health_savings_account_ald": {y: self.hsa_contribution},
        }
        if self.aotc_refundable_barred:
            tax_unit["refundable_american_opportunity_credit"] = {y: 0}
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
            "members": members,
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
        marital = {"mu": {"members": couple}} | {
            f"mu{m}": {"members": [m]} for m in members if m not in couple
        }
        return {
            "people": people,
            "tax_units": {"tu": tax_unit},
            "marital_units": marital,
            "families": {"fam": {"members": members}},
            "spm_units": {"spm": {"members": members}},
            "households": {"hh": household},
        }


def load_household(path: Path) -> Household:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping")
    return Household.from_mapping(data)
