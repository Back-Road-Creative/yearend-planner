"""The states the household can live in: 50 and DC. Which levy an income tax
the engine prices (every one does: ``state_income_tax``), whose return the
draft lays out, and each state's estimated-tax rules where the planner has
them from the state's own instructions. A state without its own rules is
priced on the federal installment rules and says so; it is an estimate, never
a silent copy of another state's."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class PenaltyRule:
    """One state's charge for underpaid installments, from its underpayment
    form. ``method``: "interest" (each shortfall charged by the day until a
    later payment, applied to the earliest shortfall first, or the end date
    pays it), "window" (a payment counts only toward the installment whose
    window it lands in and an overpayment carries forward; a shortfall runs to
    the end date), "column" (each date's cumulative shortfall times the
    factor for the span to the next date, the last to the end date) or
    "tiered" (a flat share of each shortfall by how late the payment that
    clears it is, the earliest first). ``rates``: (first day, rate) as
    published, spread over ``basis`` days (None: the calendar year's 365 or
    366; 1.0: the form's printed daily factor); ``monthly``: months times the
    rate over 12 instead; ``places``: a column factor rounded as printed.
    ``through``: the last day the published rates cover (later days take the
    last rate, named). ``shifted``: the form counts from the business-day
    due dates. ``tiers``: (days late up to, share). ``addition``: (underpaid,
    nothing paid) share of each period's shortfall, a payment counted only in
    its own period (MI)."""

    form: str  # "Form D-422"
    word: str  # "interest" | "penalty"
    method: str
    how: str  # the method in a few words, on the note
    source: str
    rates: tuple[tuple[str, float], ...] = ()
    basis: float | None = None
    through: str = ""
    end: tuple[int, int] = (4, 15)  # month, day of the following year
    shifted: bool = False
    places: int | None = None
    monthly: bool = False
    tiers: tuple[tuple[int, float], ...] = ()
    addition: tuple[float, float] | None = None
    notes: tuple[str, ...] = ()


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
    filing separately. ``notes`` are the state's own caveats, on its line.
    ``penalty``: its underpayment charge (None: the federal Form 2210 method
    stands in)."""

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
    penalty: PenaltyRule | None = None

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
_FIFO = "each shortfall charged by the day until paid, the earliest first"
# Each state's 2025 underpayment form (the 2026 one is not out until the
# return season): its rates and how it applies a payment.
_PENALTIES = {
    # G.S. 105-241.21 rate set each half year (NCDOR memo, October 21, 2025)
    "NC": PenaltyRule(
        "Form D-422",
        "interest",
        "interest",
        _FIFO + ", over 365 days",
        "Form D-422 2025 lines 16-27; NCDOR interest memo January-June 2026",
        (("2025-01-01", 0.07),),
        365.0,
        "2026-06-30",
    ),
    "CA": PenaltyRule(
        "FTB 5805",
        "penalty",
        "interest",
        _FIFO + ", over 365 or 366 days",
        "FTB 5805 2025 Worksheet II; instructions K",
        (("2025-04-15", 0.08), ("2025-07-01", 0.07)),
        None,
        "2026-04-15",
    ),
    "NY": PenaltyRule(
        "Form IT-2105.9",
        "penalty",
        "interest",
        _FIFO + ", over 365 or 366 days",
        "Form IT-2105.9-I 2025 (rates, period of underpayment)",
        (("2025-04-15", 0.095),),
        None,
        "2026-04-15",
        notes=("New York City and Yonkers tax are not in the figure",),
    ),
    "PA": PenaltyRule(
        "REV-1630",
        "interest",
        "window",
        "a payment counts only in its own period, a shortfall runs to April 15 "
        "at 0.000192 a day",
        "REV-1630 2025 Sections I and III",
        (("2025-01-01", 0.000192),),
        1.0,
        "2026-04-15",
        shifted=True,
    ),
    "IL": PenaltyRule(
        "Form IL-2210",
        "penalty",
        "tiered",
        "2% of a shortfall paid within 30 days, 10% after or never, payments "
        "to the earliest first",
        "Form IL-2210 2025 Step 4 and instructions",
        tiers=((30, 0.02), (1_000_000, 0.10)),
        notes=(
            "no penalty when last year's 100% or this year's 90% was paid on "
            "time (IL-2210 Step 2), which the installment test already covers",
        ),
    ),
    "OH": PenaltyRule(
        "IT/SD 2210",
        "interest penalty",
        "column",
        "each date's cumulative shortfall times the rate for the days to the "
        "next date over 365.25",
        "Ohio IT/SD 2210 2025 lines 14-16",
        (("2025-01-01", 0.08), ("2026-01-01", 0.07)),
        365.25,
        "2026-12-31",
        shifted=True,
        places=6,
    ),
    "GA": PenaltyRule(
        "Form 500 UET",
        "penalty",
        "interest",
        _FIFO + ", 9% a year over 365 days",
        "Form 500 UET 2025 lines 17-21",
        (("2025-01-01", 0.09),),
        365.0,
        "2026-04-15",
    ),
    "MI": PenaltyRule(
        "MI-2210",
        "interest and penalty",
        "interest",
        _FIFO + " at the daily factor; plus 10% of each period's shortfall "
        "(25% when nothing was paid in it)",
        "MI-2210 2025 Parts 2 and 3",
        (
            ("2025-01-01", 0.0002595),
            ("2025-07-01", 0.0002373),
            ("2026-01-01", 0.0002324),
        ),
        1.0,
        "2026-06-30",
        shifted=True,
        addition=(0.10, 0.25),
        notes=(
            "no penalty, only the interest, when estimates were not required "
            "last year (MI-2210 line 3), which the planner does not know",
        ),
    ),
    "NJ": PenaltyRule(
        "NJ-2210",
        "interest",
        "column",
        "each date's cumulative shortfall times months to the next date times "
        "the rate over 12",
        "NJ-2210 2025 Part III Option 1",
        (("2025-04-16", 0.1075), ("2026-01-16", 0.10)),
        None,
        "2026-04-15",
        shifted=True,
        places=3,
        monthly=True,
        notes=(
            "Option 1, a shortfall carried to each next date; Option 2 (by the "
            "date each late payment was made) is not figured",
        ),
    ),
    "VA": PenaltyRule(
        "Form 760C",
        "penalty",
        "interest",
        _FIFO + " at 0.00025 a day, to May 1",
        "Form 760C 2025 lines 20-25",
        (("2025-01-01", 0.00025),),
        1.0,
        "2026-05-01",
        end=(5, 1),
    ),
}
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

_RULES = {c: replace(r, penalty=_PENALTIES.get(c)) for c, r in _RULES.items()}

STATES: dict[str, State] = {
    **{c: State(c, n, False, note=_NOTES.get(c, "")) for c, n in _NONE.items()},
    **{c: State(c, n, True, _RULES.get(c), _PAYEES.get(c)) for c, n in _TAXED.items()},
}


def label(code: str | None) -> str:
    """How an output line names the household's state tax: the code ("CA")
    for a state that taxes income, else "state"."""
    st = STATES.get((code or "").upper())
    return st.code if st is not None and st.income_tax else "state"


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
