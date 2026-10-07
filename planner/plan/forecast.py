"""Each income stream forecast to December 31 (Phase 10, unit 2c; finding F03).

A stream is one income line of the return. The ledger gives the amount so
far; a statement's year-to-date figure (``YTD`` facts, and a Schedule C built
from bank rows) is actual only through its last row's date. The owner types
the rest of the year in ``data/manual/<year>.yaml`` under ``forecast``: a pay
schedule (cadence, amount per payment, next payment date), a remaining
amount, or a full-year figure, which replaces the forecast rather than adding
another year to it. A pay stub's year-to-date amount and date start a stream
the ledger has nothing for. ``low`` and ``high`` bound an uncertain stream
(the remaining amount, or the full year when that is what was typed). A
stream whose annual figure is in (a W-2, a 1099, a typed value) is final: its
forecast is not used, and said so. One owner per line until the spouse is a
full person (Stage 3a).
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import asdict, dataclass, fields
from datetime import date, timedelta
from typing import Any

from planner.ingest.derive import FORM as YTD
from planner.ingest.needs import NEEDS, _write, load_manual, manual_path
from planner.ledger import db
from planner.paths import Layout
from planner.taxprep import schedule_c

KEY = "forecast"
# Needed-panel key -> what kind of income it is on the return.
STREAMS = {
    "wages": "ordinary (W-2)",
    "se_income": "self-employment",
    "interest": "ordinary",
    "tax_exempt_interest": "tax-exempt",
    "ordinary_dividends": "dividends",
    "ira_distributions": "ordinary (IRA)",
    "social_security": "Social Security",
}
OWNERS = ("self", "spouse")
# cadence -> months between payments (0: by days, below)
MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}
DAYS = {"weekly": 7, "biweekly": 14}
CADENCES = (*DAYS, "semimonthly", *MONTHS, "once")


class ForecastError(ValueError):
    """A typed forecast that cannot be used, with what to change."""


def _plus_months(d: date, n: int, day: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def payments(first: date, cadence: str, year: int) -> list[date]:
    """The payment dates from ``first`` through December 31 of ``year``."""
    end = date(year, 12, 31)
    if cadence == "once":
        return [first] if first <= end else []
    if cadence in DAYS:
        step = timedelta(days=DAYS[cadence])
        out, d = [], first
        while d <= end:
            out.append(d)
            d += step
        return out
    if cadence == "semimonthly":  # two a month, half a month apart
        return sorted(
            payments(first, "monthly", year)
            + payments(first + timedelta(days=15), "monthly", year)
        )
    out, n = [], 0
    while (d := _plus_months(first, n, first.day)) <= end:
        out.append(d)
        n += MONTHS[cadence]
    return out


def _day(
    key: str, label: str, text: str | None, year: int | None = None
) -> date | None:
    if text is None:
        return None
    try:
        d = date.fromisoformat(str(text))
    except ValueError:
        raise ForecastError(f"{key}: {label} as YYYY-MM-DD, got {text!r}") from None
    if year is not None and d.year != year:
        raise ForecastError(f"{key}: {label} must be in {year}, got {d}")
    return d


@dataclass(frozen=True)
class Typed:
    """What the owner typed for one stream (dollars; dates as YYYY-MM-DD)."""

    owner: str = "self"
    ytd: float | None = None
    through: str | None = None
    cadence: str | None = None
    amount: float | None = None
    next: str | None = None
    remaining: float | None = None
    full_year: float | None = None
    low: float | None = None
    high: float | None = None

    def check(self, key: str, year: int) -> None:
        if key not in STREAMS:
            raise ForecastError(f"{key}: not an income stream ({', '.join(STREAMS)})")
        if self.owner not in OWNERS:
            raise ForecastError(
                f"{key}: owner is {' or '.join(OWNERS)}, got {self.owner!r}"
            )
        if self.full_year is not None and (
            self.cadence or self.remaining is not None or self.ytd is not None
        ):
            raise ForecastError(
                f"{key}: a full year replaces the forecast; type it alone "
                "(no --ytd, --cadence or --remaining)"
            )
        if self.cadence is not None:
            if self.cadence not in CADENCES:
                raise ForecastError(f"{key}: cadence is one of {', '.join(CADENCES)}")
            if self.remaining is not None:
                raise ForecastError(f"{key}: a schedule or --remaining, not both")
            if self.amount is None or self.next is None:
                raise ForecastError(f"{key}: a schedule needs --amount and --next")
        elif self.amount is not None or self.next is not None:
            raise ForecastError(f"{key}: --amount and --next need a --cadence")
        _day(key, "--next", self.next, year)
        if (self.ytd is None) != (self.through is None):
            raise ForecastError(f"{key}: --ytd and --through go together")
        _day(key, "--through", self.through, year)
        for name in ("ytd", "amount", "remaining", "full_year", "low", "high"):
            v = getattr(self, name)
            if v is not None and v < 0 and key != "se_income":
                raise ForecastError(f"{key}: {name} is not negative, got {v:,.0f}")
        if (self.low is None) != (self.high is None):
            raise ForecastError(f"{key}: --low and --high go together")
        base = self.full_year if self.full_year is not None else self.remaining
        if self.low is not None and self.high is not None:
            if not self.low <= self.high:
                raise ForecastError(f"{key}: low {self.low:,.0f} is above high")
            if base is not None and not self.low <= base <= self.high:
                raise ForecastError(
                    f"{key}: low {self.low:,.0f} and high {self.high:,.0f} must "
                    f"bracket {base:,.0f}"
                )

    def scheduled(self, year: int) -> tuple[float, str] | None:
        """The remaining amount and how it was reached, or None."""
        if self.remaining is not None:
            return self.remaining, f"{self.remaining:,.0f} remaining typed"
        if self.cadence and self.amount is not None and self.next:
            paid = payments(date.fromisoformat(self.next), self.cadence, year)
            return len(paid) * self.amount, (
                f"{len(paid)} {self.cadence} payments of {self.amount:,.0f} "
                f"from {self.next}"
            )
        return None


@dataclass(frozen=True)
class Stream:
    key: str
    owner: str
    tax_type: str
    actual: float | None
    through: str | None
    remaining: float | None
    full_year: float
    low: float | None = None
    high: float | None = None


def load(lay: Layout, year: int) -> dict[str, Typed]:
    raw = load_manual(lay, year).get(KEY) or {}
    names = {f.name for f in fields(Typed)}
    return {
        k: Typed(**{n: v for n, v in e.items() if n in names}) for k, e in raw.items()
    }


def save(lay: Layout, year: int, key: str, typed: Typed | None) -> None:
    """Store one stream's forecast (None clears it); refused if unusable."""
    if typed is not None:
        typed.check(key, year)
    data = load_manual(lay, year)
    entries = dict(data.get(KEY) or {})
    if typed is None:
        entries.pop(key, None)
    else:
        entries[key] = {k: v for k, v in asdict(typed).items() if v is not None}
    if entries:
        data[KEY] = entries
    else:
        data.pop(KEY, None)
    _write(manual_path(lay, year), data)


def ytd_through(conn: sqlite3.Connection, year: int) -> dict[str, str]:
    """Stream -> the last row date behind its year-to-date figure."""
    out: dict[str, str] = {}
    for need in NEEDS:
        if need.key not in STREAMS:
            continue
        boxes = {b for f, b in need.estimate if f == YTD}
        issuers = {f.issuer for f in db.facts_for(conn, year, YTD) if f.box in boxes}
        if issuers:
            sql = "SELECT MAX(date) FROM rows WHERE tax_year = ? AND source = ?"
            dates = [conn.execute(sql, (year, i)).fetchone()[0] for i in issuers]
            last = max((d for d in dates if d), default=None)
        elif need.key == "se_income" and db.facts_for(conn, year, schedule_c.FORM):
            last = conn.execute(
                "SELECT MAX(date) FROM rows WHERE tax_year = ? AND kind = 'bank'",
                (year,),
            ).fetchone()[0]
        else:
            continue
        if last:
            out[need.key] = str(last)
    return out


def partial(origin: str) -> bool:
    """True when an actual figure is the year so far (a Schedule C from bank
    rows), not the year's annual form."""
    return origin.startswith(f"{schedule_c.FORM} ")


def resolve(
    key: str,
    state: str | None,
    value: Any,
    origin: str,
    typed: Typed | None,
    through: str | None,
    year: int,
) -> tuple[Stream | None, str, str | None]:
    """The stream, its origin text and any note. ``state`` is the Needed
    panel's ("actual", "estimate", or None when there is no figure)."""
    note = None
    if state == "actual" and not partial(origin):
        if typed is not None:
            note = (
                f"{key}: the actual figure is in ({origin}); its forecast is not used"
            )
        return None, origin, note
    so_far = float(value) if state is not None else None
    if typed is None:
        if so_far is not None and through and through < f"{year}-12-31":
            note = (
                f"{key} {so_far:,.0f} is year-to-date through {through}; nothing "
                "is forecast for the rest of the year "
                f"(planner forecast {key} --year {year})"
            )
        return None, origin, note
    tax = STREAMS[key]
    if typed.full_year is not None:
        lo, hi = typed.low, typed.high
        s = Stream(
            key, typed.owner, tax, so_far, through, None, typed.full_year, lo, hi
        )
        return s, "typed full year", None
    if typed.ytd is not None:
        so_far, through, where = typed.ytd, typed.through, f"typed {typed.ytd:,.0f}"
    elif so_far is not None:
        where = f"YTD {so_far:,.0f} ({origin})"
    else:
        note = (
            f"{key}: the forecast needs the amount so far (planner forecast {key} "
            "--ytd N --through YYYY-MM-DD) or a --full-year figure; left unknown"
        )
        return None, origin, note
    rest = typed.scheduled(year) or (0.0, "nothing more forecast")
    full = so_far + rest[0]
    lo = so_far + typed.low if typed.low is not None else None
    hi = so_far + typed.high if typed.high is not None else None
    if lo is not None and hi is not None and not lo <= full <= hi:
        raise ForecastError(
            f"{key}: low {typed.low:,.0f} and high {typed.high:,.0f} must bracket the "
            f"{rest[0]:,.0f} forecast"
        )
    when = f" through {through}" if through else ""
    s = Stream(key, typed.owner, tax, so_far, through, rest[0], full, lo, hi)
    return s, f"{where}{when} + {rest[1]}", None


def render(streams: list[Stream], typed: dict[str, Typed], year: int) -> str:
    head = f"{'stream':20} {'owner':6} {'tax type':16} {'so far':>10} {'through':10} "
    out = [f"income forecast {year}", head + f"{'rest':>10} {'full year':>10}  range"]
    for s in streams:
        so_far = f"{s.actual:,.0f}" if s.actual is not None else "-"
        rest = f"{s.remaining:,.0f}" if s.remaining is not None else "-"
        rng = f"{s.low:,.0f} to {s.high:,.0f}" if s.low is not None else ""
        when = s.through or "-"
        out.append(
            f"{s.key:20} {s.owner:6} {s.tax_type:16} {so_far:>10} {when:10} "
            f"{rest:>10} {s.full_year:>10,.0f}  {rng}".rstrip()
        )
    unused = sorted(set(typed) - {s.key for s in streams})
    out.extend(f"{k}: typed but not used (see the plan's notes)" for k in unused)
    return "\n".join(out) + "\n"
