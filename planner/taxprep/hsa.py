"""Form 8889 (health savings accounts) from the HSA coverage in the profile, the
5498-SA and 1099-SA boxes, W-2 box 12 code W and a few typed answers.

Part I: the contribution limit for the coverage (self-only or family), pro-rated
by the months covered on the 1st, plus the 55-or-older catch-up; employer and
payroll money (code W) counts against it first, and the deduction is the
smaller of what you put in yourself and the room left. Money in over the limit
is named: it draws a 6% excise tax each year it stays (Form 5329) unless it is
taken out by the filing deadline. Part II: distributions not spent on qualified
medical expenses are income, plus a 20% additional tax before 65.

Not handled: Archer MSA contributions (line 4), a qualified HSA funding
distribution from an IRA (line 10), rollovers (line 14b), splitting a family
limit with a spouse's own HSA (line 6), and the testing-period income (Part
III); each is named when it could apply. The stored ``8889`` facts feed the
Needed panel's HSA deduction once the year has ended.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from planner.ingest.needs import load_profile, need_values
from planner.ledger import db
from planner.paths import Layout

FORM = "8889"
# Annual limits by coverage, IRC 223(b)(2) as indexed; the catch-up, IRC
# 223(b)(3)(B), is not indexed. A year missing here takes the latest earlier one
# and says so.
LIMITS = {
    2024: (4150.0, 8300.0, "Rev. Proc. 2023-23"),
    2025: (4300.0, 8550.0, "Rev. Proc. 2024-25"),
    2026: (4400.0, 8750.0, "Rev. Proc. 2025-19"),
}
CATCHUP = 1000.0
ADDITIONAL = 0.20
LABELS = {
    "2": "HSA contributions you made for the year",
    "3": "Contribution limit for your coverage",
    "6": "Your limit",
    "7": "Catch-up contribution (55 or older)",
    "8": "Lines 6 and 7",
    "9": "Employer contributions (W-2 box 12 code W)",
    "12": "Room left (line 8 less line 9)",
    "13": "HSA deduction",
    "14a": "Total distributions",
    "14c": "Distributions less rollovers",
    "15": "Qualified medical expenses paid from the HSA",
    "16": "Taxable HSA distributions",
    "17b": "Additional 20% tax",
}
KEYS = (
    "hsa_contributions",
    "hsa_employer_contributions",
    "hsa_months",
    "hsa_qualified_expenses",
    "filing_status",
)


@dataclass
class HSA:
    year: int
    lines: dict[str, float] = field(default_factory=dict)  # dollars, by line
    sources: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def limit(year: int, coverage: str) -> tuple[float, str]:
    """The full-year limit for ``self`` or ``family`` coverage, with its source."""
    have = sorted(y for y in LIMITS if y <= year)
    if not have:
        raise ValueError(f"no HSA limit on file for {year} or earlier")
    self_only, family, source = LIMITS[have[-1]]
    if have[-1] != year:
        source = f"{source} ({have[-1]}; no {year} figure on file)"
    return (family if coverage == "family" else self_only), source


def _r(x: float) -> float:
    return round(x, 2)


def build(conn: sqlite3.Connection, lay: Layout, year: int) -> HSA:
    """Form 8889 for a tax year; no lines when there is no HSA activity."""
    h = HSA(year)
    profile = load_profile(lay)
    v = need_values(conn, lay, year, KEYS)
    out = _r(sum(f.value for f in db.facts_for(conn, year, "1099-SA") if f.box == "1"))
    coverage = profile.get("hsa_coverage")
    total = float(v["hsa_contributions"] or 0)
    employer = float(v["hsa_employer_contributions"] or 0)
    if coverage not in ("self", "family") and not (total or employer or out):
        return h
    lines, src = h.lines, h.sources

    def put(line: str, value: float, source: str) -> float:
        lines[line], src[line] = _r(value), source
        return lines[line]

    born = profile.get("birth_date")
    age = year - date.fromisoformat(str(born)).year if born else None
    if total or employer or coverage in ("self", "family"):
        if coverage in ("self", "family"):
            months = int(v["hsa_months"] or 12)
            if v["hsa_months"] is None:
                h.notes.append(
                    "HSA coverage is taken as all 12 months; type hsa_months if it "
                    "started or ended during the year"
                )
            full, cite = limit(year, coverage)
            l3 = put(
                "3",
                full * months / 12,
                f"{coverage} limit {full:,.0f} ({cite}) x {months}/12",
            )
            catch = CATCHUP * months / 12 if age is not None and age >= 55 else 0.0
            if age is None:
                h.notes.append(
                    "no birth_date in the profile: the 55-or-older catch-up is left out"
                )
        else:
            l3, catch = put("3", 0.0, "no HSA-eligible coverage (hsa_coverage)"), 0.0
            h.notes.append(
                "HSA money went in without HSA-eligible coverage: all of it is an "
                "excess contribution"
            )
        l6 = put("6", l3, "line 3 (no Archer MSA on line 4)")
        l7 = put(
            "7",
            catch,
            "IRC 223(b)(3)(B), pro-rated by months"
            if catch
            else "under 55 at year end"
            if age is not None
            else "birth_date not given",
        )
        l8 = put("8", l6 + l7, "6 + 7")
        l9 = put("9", employer, "hsa_employer_contributions (W-2 box 12 code W)")
        l2 = put(
            "2",
            max(total - employer, 0.0),
            "hsa_contributions (5498-SA boxes 2 and 3) less line 9",
        )
        l12 = put("12", max(l8 - l9, 0.0), "8 - 9 (no funding distribution, line 10)")
        put("13", min(l2, l12), "smaller of 2 and 12")
        excess = _r(l2 + l9 - l8)
        if excess > 0:
            h.notes.append(
                f"{excess:,.2f} went into the HSA over the {l8:,.2f} limit: take it "
                "out with its earnings before the filing deadline, or a 6% excise "
                "tax applies for each year it stays (Form 5329 Part VII)"
                + ("; employer money over the limit is also wages" if l9 > l8 else "")
            )
        if coverage == "family" and str(v["filing_status"] or "").startswith("married"):
            h.notes.append(
                "line 6 takes the whole family limit: if your spouse has an HSA of "
                "their own, the limit is split between you"
            )
    if out:
        put("14a", out, "1099-SA box 1")
        l14c = put("14c", out, "14a (no rollover on line 14b)")
        spent = v["hsa_qualified_expenses"]
        if spent is None:
            h.notes.append(
                "hsa_qualified_expenses not given: the distributions are taken as "
                "spent on medical care (left out of income, not zeroed); type it "
                "from the HSA's claims history"
            )
            l15 = put("15", l14c, "hsa_qualified_expenses not given (taken as 14c)")
        else:
            l15 = put("15", float(spent), "hsa_qualified_expenses (typed)")
        l16 = put("16", max(l14c - l15, 0.0), "14c - 15")
        if l16:
            if age is not None and age >= 65:
                put("17b", 0.0, "65 or older: no additional tax (line 17a)")
            else:
                put("17b", l16 * ADDITIONAL, "16 x 20%")
                if age is None:
                    h.notes.append(
                        "no birth_date in the profile: the 20% tax is applied; at "
                        "65 or older, or if disabled, it is not"
                    )
    h.lines = {k: lines[k] for k in LABELS if k in lines}
    return h


def store(
    conn: sqlite3.Connection, lay: Layout, year: int, today: date | None = None
) -> HSA:
    """Rebuild the year; once it has ended, replace its 8889 facts when they
    changed (an open year keeps what you have typed)."""
    h = build(conn, lay, year)
    if (today or date.today()) <= date(year, 12, 31):
        if h.lines:
            h.notes.append(
                f"{year} is still open: the Needed panel keeps the typed HSA "
                "contribution"
            )
        return h
    db.replace_derived(conn, FORM, year, h.lines, LABELS, f"Form 8889 {year}")
    return h


def render(h: HSA) -> str:
    out = [f"Form 8889 (health savings accounts) for {h.year}"]
    if not h.lines:
        out.append("no HSA coverage, contributions or distributions on file")
    for line, value in h.lines.items():
        out.append(
            f"  line {line:4} {LABELS[line]:46} {value:>12,.2f}  {h.sources[line]}"
        )
    out.extend(f"note: {n}" for n in h.notes)
    return "\n".join(out) + "\n"
