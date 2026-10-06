"""The states the household can live in: 50 and DC. Which levy an income tax
the engine prices (every one does: ``state_income_tax``), whose return the
draft lays out, and each state's estimated-tax rules where the planner has
them from the state's own instructions. A state without its own rules is
priced on the federal installment rules and says so; it is an estimate, never
a silent copy of another state's."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class EstRules:
    """One agency's estimated-tax installments: the due dates as (years after
    the tax year, month, day), the share of the year's required payment due by
    each (cumulative), the de minimis (no installment due when the tax after
    withholding is under it), and the prior-year factor past the high-income
    AGI line."""

    due: tuple[tuple[int, int, int], ...]
    shares: tuple[float, ...]
    de_minimis: float
    high_income_factor: float
    source: str


FEDERAL = EstRules(
    ((0, 4, 15), (0, 6, 15), (0, 9, 15), (1, 1, 15)),
    (0.25, 0.50, 0.75, 1.00),
    1_000.0,
    1.10,
    "IRC 6654 (Form 1040-ES)",
)


@dataclass(frozen=True)
class State:
    code: str
    name: str
    income_tax: bool  # a tax on wages and other income the household files for
    est: EstRules | None = None  # None: not in the planner; FEDERAL stands in
    payee: str | None = None  # bank-row pattern for a payment to the state
    note: str = ""

    @property
    def agency(self) -> str:
        """The estimated-tax agency key: the lowercase code ("nc")."""
        return self.code.lower()


_NONE = {
    "AK": "Alaska",
    "FL": "Florida",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "WA": "Washington",
    "WY": "Wyoming",
}
_NOTES = {
    "NH": "the interest and dividends tax is repealed from 2025 (NH DRA TIR "
    "2025-001; RSA 77)",
    "WA": "no tax on wages; the capital gains excise (RCW 82.87) is the "
    "engine's, paid with its own return, no installments",
}
_TAXED = {
    "AL": "Alabama",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WV": "West Virginia",
    "WI": "Wisconsin",
}
# The states whose rules the planner has: NC's installments are the federal
# dates and shares, with no 110% step for a high prior-year AGI (Form NC-40).
_RULES = {
    "NC": EstRules(FEDERAL.due, FEDERAL.shares, 1_000.0, 1.00, "Form NC-40"),
}
_PAYEES = {
    "NC": r"NCDOR|NC ?DOR|N\.?C\.? DEPT\.? OF REV|NC DEPT REVENUE",
}

STATES: dict[str, State] = {
    **{c: State(c, n, False, note=_NOTES.get(c, "")) for c, n in _NONE.items()},
    **{c: State(c, n, True, _RULES.get(c), _PAYEES.get(c)) for c, n in _TAXED.items()},
}
DRAFTED = ("NC",)  # the states whose return the draft lays out


def get(code: str) -> State:
    """The state for a two-letter code (any case); ValueError for another."""
    try:
        return STATES[code.upper()]
    except KeyError:
        raise ValueError(f"not a state code: {code!r}") from None


def rules(code: str) -> tuple[EstRules, bool]:
    """The state's estimated-tax rules and whether they are its own (False:
    the federal rules stand in)."""
    st = get(code)
    return (st.est, True) if st.est else (FEDERAL, False)


def payee(code: str) -> re.Pattern[str] | None:
    pat = get(code).payee
    return re.compile(pat, re.I) if pat else None
