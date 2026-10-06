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
    withholding is under it, or not over it when ``over``; its own figure for
    married filing separately when ``separate_de_minimis``), the current-year
    percentage of the safe harbor, and the prior-year factor once last year's
    AGI tops ``high_income_agi``. ``current_only_agi``: this year's AGI at or
    over it leaves only the current-year leg. Both AGI lines halve for married
    filing separately. ``notes`` are the state's own caveats, on its line."""

    due: tuple[tuple[int, int, int], ...]
    shares: tuple[float, ...]
    de_minimis: float
    high_income_factor: float
    source: str
    current_factor: float = 0.90
    high_income_agi: float = 150_000.0
    over: bool = False
    separate_de_minimis: float | None = None
    current_only_agi: float | None = None
    notes: tuple[str, ...] = ()

    def installments(self) -> tuple[tuple[int, tuple[int, int, int], float], ...]:
        """(number, due, cumulative share) of each installment that asks for
        money; one whose share adds nothing (CA's September) is none."""
        out = []
        before = 0.0
        for n, (due, share) in enumerate(zip(self.due, self.shares, strict=True), 1):
            if share > before:
                out.append((n, due, share))
            before = share
        return tuple(out)

    def threshold(self, separate: bool = False) -> float:
        """The de minimis for the filing status."""
        if separate and self.separate_de_minimis is not None:
            return self.separate_de_minimis
        return self.de_minimis


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
# The states whose rules the planner has, each from its own 2026 estimated-tax
# instructions (and its underpayment form for the safe harbor). The federal
# dates and equal shares unless said; "over": required only when the tax after
# withholding is more than the de minimis, not "at least" it.
_FED_AGI = "taken as the federal AGI"
_RULES = {
    # no 110% step for a high prior-year AGI
    "NC": EstRules(FEDERAL.due, FEDERAL.shares, 1_000.0, 1.00, "Form NC-40"),
    # 30/40/0/30; at least 500 (250 separately); 110% over 150,000 of last
    # year's CA AGI; a CA AGI of 1,000,000 (500,000) this year: this year's tax
    "CA": EstRules(
        FEDERAL.due,
        (0.30, 0.70, 0.70, 1.00),
        500.0,
        1.10,
        "Form 540-ES 2026 instructions B-D",
        separate_de_minimis=250.0,
        current_only_agi=1_000_000.0,
        notes=(f"California AGI, last year's and this year's, is {_FED_AGI}",),
    ),
    # at least 300 (New York City and Yonkers tax included); 110% over 150,000
    # of last year's NYAGI
    "NY": EstRules(
        FEDERAL.due,
        FEDERAL.shares,
        300.0,
        1.10,
        "Form IT-2105-I 2026",
        notes=(
            "the 300 test and the payments include New York City and Yonkers "
            "tax, which the planner does not price; the test is per spouse "
            f"even on a joint return; New York AGI is {_FED_AGI}",
        ),
    ),
    # at least 430 (14,000 of income without withholding); the prior-year leg
    # is last year's PA taxable income at 3.07%
    "PA": EstRules(
        FEDERAL.due,
        FEDERAL.shares,
        430.0,
        1.00,
        "REV-413 (I) 2026",
        notes=(
            "the prior-year leg is last year's PA taxable income at 3.07% "
            "(last year's PA tax stands in), and is closed after a part-year "
            "return or none",
        ),
    ),
    # more than 1,000; IL-2210: the lesser of 90% and 100% of last year's
    "IL": EstRules(
        FEDERAL.due,
        FEDERAL.shares,
        1_000.0,
        1.00,
        "Form IL-1040-ES 2026; IL-2210 instructions",
        over=True,
    ),
    # more than 500; the lesser of 90% and 100% of last year's
    "OH": EstRules(
        FEDERAL.due, FEDERAL.shares, 500.0, 1.00, "Ohio IT 1040ES 2026", over=True
    ),
    # Form 500-UET: 70% of this year's or 100% of last year's; the 500-ES test
    # is income, not tax
    "GA": EstRules(
        FEDERAL.due,
        FEDERAL.shares,
        0.0,
        1.00,
        "Form 500-ES 2026; Form 500-UET",
        current_factor=0.70,
        over=True,
        notes=(
            "Form 500-ES asks for installments once income not subject to "
            "withholding passes 1,000 beyond the exemptions and standard "
            "deduction, a test of income the planner does not apply: any tax "
            "after withholding is treated as due",
        ),
    ),
    # more than 500; 110% over 150,000 (75,000 separately) of last year's AGI
    "MI": EstRules(
        FEDERAL.due,
        FEDERAL.shares,
        500.0,
        1.10,
        "Form MI-1040ES 2026",
        over=True,
        notes=(f"last year's AGI for the 110% step is {_FED_AGI}",),
    ),
    # more than 400; NJ-2210: 80% of this year's or last year's tax; interest,
    # not a penalty, on a shortfall
    "NJ": EstRules(
        FEDERAL.due,
        FEDERAL.shares,
        400.0,
        1.00,
        "Form NJ-1040-ES 2026; NJ-2210",
        current_factor=0.80,
        over=True,
    ),
    # May 1, June 15, September 15, January 15; more than 1,000
    "VA": EstRules(
        ((0, 5, 1), *FEDERAL.due[1:]),
        FEDERAL.shares,
        1_000.0,
        1.00,
        "Form 760ES 2026",
        over=True,
    ),
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
