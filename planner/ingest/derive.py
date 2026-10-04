"""``planner derive``: year-to-date facts computed from the imported CSV rows.

The broker's 1099 arrives in February; until then the only picture of the year is
the rows already in the ledger. This module folds them into facts under the form
``YTD`` (issuer = the CSV source that produced them) so the engine and the Needed
panel read one table whether a figure came from a form or from rows. Every run
recomputes the year and supersedes the previous run's facts, so a later export
corrects an earlier estimate. Nothing is inferred: a figure with no rows behind it
is simply absent.
"""

from __future__ import annotations

import csv
import gzip
import io
import re
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

from planner.ledger import db

FORM = "YTD"

# (box, label): the realized boxes mirror the 1099-B summary template so the two
# can be compared line by line once the form arrives.
LABELS = {
    "st_proceeds": "Short-term proceeds (YTD)",
    "st_basis": "Short-term cost basis (YTD)",
    "st_gain": "Short-term gain or loss (YTD)",
    "lt_proceeds": "Long-term proceeds (YTD)",
    "lt_basis": "Long-term cost basis (YTD)",
    "lt_gain": "Long-term gain or loss (YTD)",
    "dividends": "Dividends (YTD)",
    "interest": "Interest (YTD)",
    "capital_gain_distributions": "Capital gain distributions (YTD)",
    "deposits": "Bank deposits (YTD)",
    "withdrawals": "Bank withdrawals (YTD)",
}


# YTD box -> the form box that reports the same figure once the year's forms
# arrive. The form is then the authority (the needs lookup reads it first); the
# YTD figure stays only to show how far the projection was off.
FORM_BOXES = {
    "st_proceeds": ("1099-B", "st_proceeds"),
    "st_basis": ("1099-B", "st_basis"),
    "lt_proceeds": ("1099-B", "lt_proceeds"),
    "lt_basis": ("1099-B", "lt_basis"),
    "dividends": ("1099-DIV", "1a"),
    "capital_gain_distributions": ("1099-DIV", "2a"),
    "interest": ("1099-INT", "1"),
}


@dataclass(frozen=True)
class Gap:
    box: str  # the YTD box
    form: str  # "1099-DIV 1a"
    ytd: float
    reported: float  # every payer's form for the year, summed

    @property
    def gap(self) -> float:
        return round(self.reported - self.ytd, 2)


def gaps(conn: sqlite3.Connection, year: int) -> list[Gap]:
    """Each YTD figure a filed form also reports, beside the form's figure:
    the gap is reported, the two are never added. Boxes with no YTD figure or
    no form yet are left out."""
    ytd: dict[str, float] = defaultdict(float)
    for f in db.facts_for(conn, year, FORM):
        ytd[f.box] += f.value
    reported: dict[tuple[str, str], float] = defaultdict(float)
    forms = {form for form, _ in FORM_BOXES.values()}
    for f in db.facts_for(conn, year):
        if f.form in forms:
            reported[(f.form, f.box)] += f.value
    return [
        Gap(box, f"{form} {fbox}", round(ytd[box], 2), round(reported[key], 2))
        for box, key in FORM_BOXES.items()
        for form, fbox in [key]
        if box in ytd and key in reported
    ]


def _classify_income(kind: str, typ: str) -> str | None:
    low = typ.lower()
    if "capital gain" in low:
        return "capital_gain_distributions"
    if "dividend" in low and "reinvest" not in low:
        return "dividends"
    if "interest" in low:
        return "interest"
    return None


def derive_year(rows: list[db.LedgerRow], year: int) -> list[db.Fact]:
    """Fold one year's rows into YTD facts, one set per CSV source.

    Realized sales come from ``realized`` rows (term short/long). Dividends and
    interest come from ``income`` rows when any income export covers the year,
    else from ``transaction`` rows typed Dividend/Interest, so a download and an
    income export for the same year never double-count. Bank rows give deposits
    (positive) and withdrawals (negative, reported as a positive figure).
    """
    cents: dict[tuple[str, str], int] = defaultdict(int)
    has_income = any(r.kind == "income" and r.tax_year == year for r in rows)
    for r in rows:
        if r.tax_year != year or r.amount_cents is None:
            continue
        if r.kind == "realized" and r.term in ("short", "long"):
            pre = "st" if r.term == "short" else "lt"
            cents[(r.source, f"{pre}_proceeds")] += r.amount_cents
            if r.basis_cents is not None:
                cents[(r.source, f"{pre}_basis")] += r.basis_cents
        elif r.kind == "income" or (r.kind == "transaction" and not has_income):
            box = _classify_income(r.kind, r.type)
            if box is not None:
                cents[(r.source, box)] += r.amount_cents
        elif r.kind == "bank":
            box = "deposits" if r.amount_cents > 0 else "withdrawals"
            cents[(r.source, box)] += abs(r.amount_cents)
    for source in {s for s, _ in cents}:
        for pre in ("st", "lt"):
            if (source, f"{pre}_proceeds") in cents and (
                source,
                f"{pre}_basis",
            ) in cents:
                cents[(source, f"{pre}_gain")] = (
                    cents[(source, f"{pre}_proceeds")] - cents[(source, f"{pre}_basis")]
                )
    return [
        db.Fact(
            form=FORM,
            tax_year=year,
            issuer=source,
            box=box,
            label=LABELS[box],
            value=db.from_cents(value),
            page=0,
        )
        for (source, box), value in sorted(cents.items())
    ]


def derive(conn: sqlite3.Connection, year: int, batch: str | None = None) -> int:
    """Recompute and store the YTD facts for ``year``; returns how many were written.

    The facts hang off a synthetic ``derived`` document so the ledger's one
    source-per-fact rule holds; each run is a new document and supersedes the
    last run's accepted YTD facts for the same year and source.
    """
    facts = derive_year(db.rows_for(conn, year), year)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    with conn:
        conn.execute(
            "UPDATE facts SET status = 'superseded' WHERE form = ? AND tax_year = ? "
            "AND status = 'accepted'",
            (FORM, year),
        )
    if facts:
        db.add_document(
            conn,
            fingerprint=f"derived:{year}:{stamp}",
            file_name=f"derived {year}",
            kind="derived",
            pages=0,
            batch=batch or stamp,
            facts=facts,
        )
    return len(facts)


def row_years(conn: sqlite3.Connection) -> list[int]:
    return [
        int(r["tax_year"])
        for r in conn.execute(
            "SELECT DISTINCT tax_year FROM rows WHERE tax_year IS NOT NULL "
            "ORDER BY tax_year"
        )
    ]


# --- county from ZIP -------------------------------------------------------
#
# ``config/zip_county.csv.gz`` is the Census Bureau's 2020 ZCTA-to-county
# relationship file reduced by ``scripts/refresh_zip_county.py`` (source, URL and
# download date in ``config/zip_county.SOURCE.md``). A ZIP in one county names it;
# a ZIP that straddles counties is a question for the household, never a guess.

CROSSWALK = "zip_county.csv.gz"
# Words a county's own name ends in; a typed "Macon" may leave one off.
DESIGNATORS = (
    "county",
    "parish",
    "borough",
    "census area",
    "municipality",
    "municipio",
    "city and borough",
    "city",
)
_ZIP5 = re.compile(r"\d{5}")


class CountyError(ValueError):
    """A typed county that names no county of the state, or more than one."""


@dataclass(frozen=True)
class County:
    fips: str
    name: str  # as the Census Bureau names it: "Macon County", "Baltimore city"
    state: str
    share: float  # of the ZIP's land area, 0 < share <= 1; 1.0 outside a ZIP lookup

    @property
    def key(self) -> str:
        """policyengine-us's name for the county: "MACON_COUNTY_NC"."""
        return re.sub(r"[ \-']", "_", self.name.replace(".", "").upper()) + (
            f"_{self.state}"
        )


@cache
def _crosswalk(path: str) -> tuple[dict[str, tuple[County, ...]], dict[str, County]]:
    """(counties by ZIP, largest first; every county by FIPS) from the csv.gz."""
    text = gzip.decompress(Path(path).read_bytes()).decode("utf-8")
    by_zip: dict[str, list[County]] = defaultdict(list)
    every: dict[str, County] = {}
    for row in csv.DictReader(io.StringIO(text)):
        county = County(
            row["county_fips"],
            row["county_name"],
            row["state"],
            float(row["land_area_share"]),
        )
        by_zip[row["zcta"]].append(county)
        every[county.fips] = County(county.fips, county.name, county.state, 1.0)
    return (
        {
            z: tuple(sorted(cs, key=lambda c: (-c.share, c.name)))
            for z, cs in by_zip.items()
        },
        every,
    )


def counties_for_zip(config: Path, zip5: str) -> tuple[County, ...]:
    """Every county with land in the ZIP, the largest share first; empty when the
    text is not a five-digit ZIP or the Census file has no area for it."""
    if not _ZIP5.fullmatch(zip5):
        return ()
    return _crosswalk(str(config / CROSSWALK))[0].get(zip5, ())


def counties_of_state(config: Path, state: str) -> tuple[County, ...]:
    """All counties of a state (two-letter code), by name."""
    every = _crosswalk(str(config / CROSSWALK))[1].values()
    return tuple(sorted((c for c in every if c.state == state), key=lambda c: c.name))


def _plain(text: str) -> str:
    """Case, accents, periods, hyphens and "Saint" spelled out do not matter."""
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c)).casefold()
    folded = re.sub(r"[.']", "", folded).replace("-", " ")
    folded = re.sub(r"\bsaint\b", "st", folded)
    return " ".join(folded.split())


def resolve_county(config: Path, text: str, state: str) -> str:
    """The engine's county name ("MACON_COUNTY_NC") for what a person typed.

    Accepts the Census name ("Macon County"), the short name ("Macon"), a name
    with the state ("Macon County, NC") or the engine's own name. A name that
    fits two counties ("Baltimore" in MD) or none is a ``CountyError`` that says
    which, never a pick."""
    counties = counties_of_state(config, state)
    typed = text.strip()
    if re.fullmatch(r"[A-Z0-9_\u00c0-\u00dd]+_[A-Z]{2}", typed):  # the engine's name
        for c in counties:
            if c.key == typed:
                return c.key
        raise CountyError(f"{typed}: not a county of {state}")
    name, _, tail = typed.rpartition(",")
    if name and re.fullmatch(r"\s*[A-Za-z]{2}\s*", tail):
        if tail.strip().upper() != state:
            raise CountyError(f"{typed}: not a county of {state}")
        typed = name
    want = _plain(typed)
    exact = [c for c in counties if _plain(c.name) == want]
    if not exact:
        exact = [
            c
            for c in counties
            if any(_plain(c.name) == f"{want} {d}" for d in DESIGNATORS)
        ]
    if len(exact) == 1:
        return exact[0].key
    if exact:
        raise CountyError(
            f"{text.strip()!r} fits {', '.join(c.name for c in exact)} in {state}; "
            "type the full name"
        )
    raise CountyError(f"{text.strip()!r}: not a county of {state}")


def county_from_zip(
    config: Path, zip5: str, state: str | None
) -> tuple[str | None, str]:
    """(county name, note) for a return's ZIP.

    The name is set only when the ZIP lies wholly in one county of ``state``. The
    note says what was found: the source of the answer, or why there is none (a
    ZIP the Census file lacks, in another state, or split across counties, with
    the candidates, largest first)."""
    found = counties_for_zip(config, zip5)
    if not found:
        return None, f"ZIP {zip5} is not in the Census ZIP-to-county file"
    states = sorted({c.state for c in found})
    if state is None or states != [state]:
        where = "/".join(states)
        return None, (
            f"ZIP {zip5} is in {where}, not {state}"
            if state
            else f"ZIP {zip5}: no state"
        )
    if len(found) > 1:
        names = ", ".join(f"{c.name} ({c.share:.0%})" for c in found)
        return None, f"ZIP {zip5} spans {names}; type yours"
    return found[0].name, f"ZIP {zip5}"
