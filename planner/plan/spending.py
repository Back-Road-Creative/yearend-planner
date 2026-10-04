"""The spending band: withdrawal rate × the balance, clamped to the profile's
floor and ceiling. A drawdown (balance below 90% of the inflation-adjusted
all-time peak, which a rollover never resets) holds spending at the floor until
the balance recovers. The return-band table runs the next years under the
floor and the planning real return, in today's dollars."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from planner.engine.household import MissingInputError
from planner.engine.tax import r
from planner.ingest.needs import load_profile
from planner.ledger import portfolio
from planner.paths import Layout
from planner.plan.inputs import age_at_year_end

DRAWDOWN = 0.90
BAND_YEARS = 10  # rows in the return-band table (planner spend, the plan page)
REQUIRED = (
    "withdrawal_rate",
    "spending_floor",
    "spending_ceiling",
    "inflation",
    "return_floor",
    "return_track",
    "birth_date",
)


@dataclass(frozen=True)
class BandRow:
    year: int
    age: int
    balance_floor: float
    spend_floor: float
    balance_track: float
    spend_track: float


@dataclass
class Spending:
    year: int
    as_of: str
    balance: float
    peak: float
    peak_date: str
    peak_adjusted: float
    in_drawdown: bool
    rate: float
    floor: float
    ceiling: float
    spending: float
    rows: list[BandRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def require(profile: dict[str, Any], keys: tuple[str, ...], year: int) -> None:
    missing = [k for k in keys if profile.get(k) is None]
    if missing:
        raise MissingInputError(
            f"the plan needs {', '.join(missing)} (planner needed --year {year})"
        )


def clamp(rate: float, balance: float, floor: float, ceiling: float) -> float:
    return r(min(max(rate * balance, floor), ceiling))


def adjusted_peak(peak: float, peak_date: str, as_of: date, inflation: float) -> float:
    years = (as_of - date.fromisoformat(peak_date)).days / 365.25
    return r(peak * (1 + inflation) ** max(years, 0.0))


def band(
    year: int,
    age: int,
    balance: float,
    rate: float,
    floor: float,
    ceiling: float,
    peak: float,
    ret_floor: float,
    ret_track: float,
    years: int,
) -> list[BandRow]:
    """Real-dollar projection: spend at the start of the year, grow the rest."""
    rows: list[BandRow] = []
    b_floor = b_track = balance
    p_floor = p_track = peak
    for i in range(years):
        s_floor = (
            floor
            if b_floor < DRAWDOWN * p_floor
            else clamp(rate, b_floor, floor, ceiling)
        )
        s_track = (
            floor
            if b_track < DRAWDOWN * p_track
            else clamp(rate, b_track, floor, ceiling)
        )
        rows.append(
            BandRow(year + i, age + i, r(b_floor), s_floor, r(b_track), s_track)
        )
        b_floor = max((b_floor - s_floor) * (1 + ret_floor), 0.0)
        b_track = max((b_track - s_track) * (1 + ret_track), 0.0)
        p_floor, p_track = max(p_floor, b_floor), max(p_track, b_track)
    return rows


def plan(
    lay: Layout,
    year: int,
    as_of: date | None = None,
    balance: float | None = None,
    years: int = 10,
) -> Spending:
    profile = load_profile(lay)
    require(profile, REQUIRED, year)
    today = as_of or date.today()
    st = portfolio.status(lay, year, today)
    bal = r(balance if balance is not None else st.total)
    inflation = float(profile["inflation"])
    # a January balance above the ledger's peak is the new peak
    peak_adj = max(
        adjusted_peak(st.peak, st.peak_date, today, inflation) if st.peak else bal,
        bal,
    )
    rate = float(profile["withdrawal_rate"])
    floor = float(profile["spending_floor"])
    ceiling = float(profile["spending_ceiling"])
    drawdown = bal < DRAWDOWN * peak_adj
    sp = Spending(
        year,
        today.isoformat(),
        bal,
        st.peak,
        st.peak_date,
        peak_adj,
        drawdown,
        rate,
        floor,
        ceiling,
        floor if drawdown else clamp(rate, bal, floor, ceiling),
    )
    age = age_at_year_end(str(profile["birth_date"]), year)
    sp.rows = band(
        year,
        age,
        bal,
        rate,
        floor,
        ceiling,
        peak_adj,
        float(profile["return_floor"]),
        float(profile["return_track"]),
        years,
    )
    if balance is None:
        sp.notes.append(
            f"balance is the ledger's latest snapshot as of {st.as_of}; pass "
            "--balance for the January 1 figure"
        )
    if drawdown:
        sp.notes.append(
            f"drawdown: {bal:,.2f} is below {DRAWDOWN:.0%} of the inflation-adjusted "
            f"peak {peak_adj:,.2f} ({st.peak_date}); spending holds at the floor"
        )
    if st.untyped:
        sp.notes.append(f"{st.untyped:,.2f} sits in untyped accounts")
    return sp
