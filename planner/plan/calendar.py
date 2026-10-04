"""The year's deadline calendar. A date on a weekend or a federal holiday
moves to the next business day; the shift is computed from the holiday
rules, never typed in."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

AGENCY_NAMES = {"fed": "federal", "nc": "NC"}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """A fixed-date holiday on Saturday is observed Friday; on Sunday, Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def holidays(year: int) -> dict[date, str]:
    """Federal holidays as observed (5 U.S.C. 6103), plus DC Emancipation Day
    (April 16), which the IRS counts as a legal holiday for a due date."""
    fixed = {
        "New Year's Day": date(year, 1, 1),
        "Emancipation Day (DC)": date(year, 4, 16),
        "Juneteenth": date(year, 6, 19),
        "Independence Day": date(year, 7, 4),
        "Veterans Day": date(year, 11, 11),
        "Christmas Day": date(year, 12, 25),
    }
    out = {_observed(d): name for name, d in fixed.items()}
    out[_nth_weekday(year, 1, 0, 3)] = "Birthday of Martin Luther King, Jr."
    out[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    out[_last_weekday(year, 5, 0)] = "Memorial Day"
    out[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    out[_nth_weekday(year, 10, 0, 2)] = "Columbus Day"
    out[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    # New Year's Day of the next year observed on this December 31
    if date(year + 1, 1, 1).weekday() == 5:
        out[date(year, 12, 31)] = "New Year's Day (observed)"
    return out


def shift(d: date, step: int = 1) -> date:
    """The first business day on or after ``d`` (weekends and federal
    holidays skipped; the IRS and NCDOR both move a due date this way).
    ``step=-1`` walks back instead: a year-end cut-off such as a trade that
    must settle by December 31 lands on the last business day before it."""
    closed = holidays(d.year - 1) | holidays(d.year) | holidays(d.year + 1)
    while d.weekday() >= 5 or d in closed:
        d += timedelta(days=step)
    return d


NEXT, PRIOR, NONE = 1, -1, 0  # how a rule's date moves off a closed day


@dataclass(frozen=True)
class Deadline:
    date: str  # after the shift
    nominal: str  # the rule's own date
    item: str


def _rule(year: int, month: int, day: int, item: str, move: int = NEXT) -> Deadline:
    nominal = date(year, month, day)
    moved = shift(nominal, move) if move else nominal
    return Deadline(moved.isoformat(), nominal.isoformat(), item)


def _conditional(
    quarter: int, owing: Sequence[str] | None, why_none: str | None = None
) -> str:
    """A Q2 or Q3 line. ``owing`` is who must pay that installment, from the
    estimated-tax result; ``None`` when it is not known. ``why_none`` is the
    reason nobody owes, given by the caller that knows it."""
    item = f"Q{quarter} estimated payments (federal and NC) if required"
    if owing is None:
        return item
    if not owing:
        return f"{item}: not required" + (f" ({why_none})" if why_none else "")
    names = " and ".join(AGENCY_NAMES.get(a, a) for a in owing)
    return f"{item}: required for {names}"


def deadlines(
    year: int,
    owing: Mapping[int, Sequence[str]] | None = None,
    why_none: str | None = None,
) -> list[Deadline]:
    """The plan year's calendar from October through next September, in order.
    The June and September installments are marked "if required"; ``owing``
    maps installment 2 and 3 to the agencies whose estimated-tax result says a
    payment is required (empty: none), and names them on the line; when nobody
    owes, ``why_none`` is the reason the line gives."""
    owing = owing or {}
    items = [
        _rule(
            year, 10, 1, "loss harvest and gain pairing; set specific-ID first", NONE
        ),
        _rule(
            year, 11, 1, "ACA open enrollment opens; pick an HSA-eligible plan?", NONE
        ),
        _rule(
            year,
            11,
            15,
            "fund year-end distribution estimates; size the conversion",
            NONE,
        ),
        _rule(
            year,
            12,
            31,
            "Roth conversion settled; sales done; inherited-IRA withdrawal taken",
            PRIOR,
        ),
        _rule(year + 1, 1, 15, "Q4 estimated payments (federal and NC)"),
        _rule(year + 1, 1, 31, "planner rollover; engine update", NONE),
        _rule(
            year + 1,
            4,
            15,
            f"Q1 estimated payments; {year} Roth IRA and HSA contributions; filing",
        ),
        _rule(year + 1, 6, 15, _conditional(2, owing.get(2), why_none)),
        _rule(year + 1, 9, 15, _conditional(3, owing.get(3), why_none)),
    ]
    if year == 2026:
        items.append(
            _rule(
                2027,
                1,
                1,
                "NC Medicaid work requirement starts (80 hours/month, 6-month "
                "verification); keep an SE-hours log",
                NONE,
            )
        )
    return sorted(items, key=lambda d: d.date)
