"""``planner close``: the filed return closes the year.

Drop the filed 1040 (and its schedules) and the NC D-400 into the inbox; the
form templates read the filed figures (issuer ``self`` and ``NC``). The delta
report sets each filed line beside the draft's, and the year closes with the
filed figures as the record that next year's rollover reads. An amended return
supersedes the original's facts, so closing again records a new version.

The record lives in ``data/private/returns/<year>-closed.yaml``: the user's own
figures, never committed or shared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from planner.engine.household import MissingInputError
from planner.ledger import db
from planner.paths import Layout
from planner.taxprep import draft, statereturn

TOLERANCE = 1.0
FILERS = ("self", *statereturn.RETURNS)  # a state template's issuer is its code
# (filed form, template box) -> (draft form, draft line). Template boxes follow
# the 2024 forms; the draft follows 2025's, so a few numbers differ.
MAP: dict[tuple[str, str], tuple[str, str]] = {
    **{
        ("1040", b): ("1040", b)
        for b in (
            "1z",
            "2a",
            "2b",
            "3a",
            "3b",
            "4b",
            "6b",
            "8",
            "9",
            "10",
            "15",
            "16",
            "22",
            "23",
            "24",
            "25d",
            "26",
            "33",
            "34",
            "37",
        )
    },
    ("1040", "7"): ("1040", "7a"),
    ("1040", "11"): ("1040", "11a"),
    ("1040", "12"): ("1040", "12e"),
    ("1040", "13"): ("1040", "13a"),
    **{("1040-SCH1", b): ("Sch 1", b) for b in ("3", "10", "15", "16", "17", "20")},
    ("1040-SCH1", "26"): ("Sch 1", "26"),
    ("1040-SCH2", "2"): ("Sch 2", "1a"),  # excess APTC repayment
    ("1040-SCH2", "4"): ("Sch 2", "4"),
    ("1040-SCH2", "21"): ("Sch 2", "21"),
    ("1040-SCH3", "8"): ("Sch 3", "8"),
    ("1040-SCH3", "9"): ("Sch 3", "9"),
    **{("1040-SCHC", b): ("Sch C", b) for b in ("1", "7", "28", "31")},
    ("1040-SCHSE", "12"): ("Sch SE", "12"),
    ("1040-SCHSE", "13"): ("Sch SE", "13"),
    **{("1040-SCHD", b): ("Sch D", b) for b in ("7", "15", "16")},
    **{
        (r.template, b): (r.form, b)
        for r in statereturn.RETURNS.values()
        for b in r.boxes
    },
}
REQUIRED = {
    "federal": ("1040", "24"),
    **{c: (r.template, r.tax_line) for c, r in statereturn.RETURNS.items()},
}


class NotFiledError(RuntimeError):
    """The filed return (or the part the household owes) is not on file."""


@dataclass(frozen=True)
class Delta:
    key: str  # "1040 24"
    label: str
    filed: float
    drafted: float | None  # None: the draft has no such line

    @property
    def gap(self) -> float | None:
        return None if self.drafted is None else round(self.filed - self.drafted, 2)


@dataclass
class Closing:
    year: int
    version: int
    new: bool  # a version was written by this run
    filed: dict[str, float] = field(default_factory=dict)
    documents: list[str] = field(default_factory=list)
    deltas: list[Delta] = field(default_factory=list)  # lines that differ
    filed_only: list[Delta] = field(default_factory=list)  # no draft line to match
    matched: int = 0  # lines where filed and draft agree
    notes: list[str] = field(default_factory=list)


def record_path(lay: Layout, year: int) -> Path:
    return lay.data / "private" / "returns" / f"{year}-closed.yaml"


def _versions(lay: Layout, year: int) -> list[dict[str, Any]]:
    path = record_path(lay, year)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    versions = data.get("versions", []) if isinstance(data, dict) else []
    return [v for v in versions if isinstance(v, dict)]


def closed(lay: Layout, year: int) -> dict[str, float] | None:
    """The latest closed version's filed figures ("1040 24": dollars), or None
    while the year is open."""
    versions = _versions(lay, year)
    if not versions:
        return None
    return {str(k): float(v) for k, v in versions[-1]["filed"].items()}


def history(lay: Layout, year: int) -> list[tuple[int, str]]:
    """Each closed version of ``year`` and when it closed, oldest first."""
    return [(int(v["version"]), str(v["closed_at"])) for v in _versions(lay, year)]


def latest(lay: Layout, year: int) -> dict[str, Any] | None:
    """The latest closed version of ``year`` as written (its filed figures,
    documents, and the lines that differ from the draft), or None while open."""
    versions = _versions(lay, year)
    if not versions:
        return None
    out = {"year": year, "matched": 0, "lines": [], **versions[-1]}
    out["documents"] = list(out.get("documents") or [])
    return out


def on_drop(lay: Layout, today: date) -> list[Closing]:
    """Close every ended year whose filed 1040 (line 24) is in the ledger and
    differs from its last closed record. Dropping the filed return on the page,
    or in the inbox before ``planner run``, closes the year: no terminal."""
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        years = sorted(
            {
                f.tax_year
                for f in db.facts_for(conn, None, "1040")
                if f.box == "24" and f.issuer in FILERS and f.tax_year < today.year
            }
        )
        filed = {
            y: {f"{f.form} {f.box}": f.value for f in filed_facts(conn, y)[0]}
            for y in years
        }
    finally:
        conn.close()
    return [close(lay, y) for y in years if filed[y] != closed(lay, y)]


def filed_facts(conn: Any, year: int) -> tuple[list[db.FactRow], int]:
    """(accepted filed-return facts, count still pending confirmation)."""
    forms = {f for f, _ in MAP}
    keep = [
        f
        for f in db.facts_for(conn, tax_year=year)
        if f.form in forms and f.issuer in FILERS
    ]
    pending = [
        f
        for f in db.facts_for(conn, tax_year=year, status="pending")
        if f.form in forms and f.issuer in FILERS
    ]
    return keep, len(pending)


def close(lay: Layout, year: int, now: datetime | None = None) -> Closing:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        facts, pending = filed_facts(conn, year)
    finally:
        conn.close()
    filed = {f"{f.form} {f.box}": f.value for f in facts}
    labels = {f"{f.form} {f.box}": f.label for f in facts}
    if " ".join(REQUIRED["federal"]) not in filed:
        raise NotFiledError(
            f"no filed {year} Form 1040 with line 24 on file: drop the filed "
            "return (PDF) in the inbox and run ingest"
            + (f" ({pending} filed-return value(s) await confirm)" if pending else "")
        )
    out = Closing(year, 0, False, filed, sorted({f.file_name for f in facts}))
    if pending:
        out.notes.append(
            f"{pending} filed-return value(s) read by OCR await `planner confirm`; "
            "they are not in this record"
        )
    try:
        d: draft.Draft | None = draft.build(lay, year)
    except MissingInputError as exc:
        d = None
        out.notes.append(f"no draft to compare against: {exc}")
    if d is not None:
        for code, ret in statereturn.RETURNS.items():
            has_form = d.get(ret.form, ret.tax_line) is not None
            if has_form and " ".join(REQUIRED[code]) not in filed:
                out.notes.append(
                    f"the draft has a {ret.form} but no filed {year} {ret.form} "
                    "is on file; drop it and close again to record it"
                )
        for key, value in sorted(filed.items()):
            form, box = key.split(" ", 1)
            target = MAP.get((form, box))
            drafted = d.get(*target) if target else None
            if drafted is None:
                out.filed_only.append(Delta(key, labels[key], value, None))
            elif abs(value - drafted) <= TOLERANCE:
                out.matched += 1
            else:
                out.deltas.append(Delta(key, labels[key], value, drafted))

    versions = _versions(lay, year)
    if versions and {str(k): float(v) for k, v in versions[-1]["filed"].items()} == (
        filed
    ):
        out.version = int(versions[-1]["version"])
        out.notes.append(f"{year} is already closed as version {out.version}")
        return out
    out.version = len(versions) + 1
    out.new = True
    if versions:
        out.notes.append(
            f"the filed figures changed (an amended return): {year} re-opened and "
            f"closed again as version {out.version}"
        )
    versions.append(
        {
            "version": out.version,
            "closed_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
            "documents": out.documents,
            "filed": dict(sorted(filed.items())),
            "deltas": len(out.deltas),
            "matched": out.matched,
            "lines": [
                {"key": x.key, "label": x.label, "filed": x.filed, "drafted": x.drafted}
                for x in (*out.deltas, *out.filed_only)
            ],
        }
    )
    path = record_path(lay, year)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"year": year, "versions": versions}, sort_keys=False),
        encoding="utf-8",
    )
    return out


def render(c: Closing) -> str:
    verb = "closed" if c.new else "unchanged"
    out = [f"{c.year} {verb}: version {c.version}, from {', '.join(c.documents)}"]
    if c.deltas:
        out.append(
            f"{len(c.deltas)} line(s) differ from the draft ({c.matched} agree):"
        )
        for x in c.deltas:
            out.append(
                f"  {x.key:16} {x.label:36.36} filed {x.filed:>12,.2f}  "
                f"draft {x.drafted or 0.0:>12,.2f}  ({x.gap or 0.0:+,.2f})"
            )
    elif c.matched:
        out.append(f"every filed line agrees with the draft ({c.matched} lines)")
    if c.filed_only:
        out.append(f"{len(c.filed_only)} filed line(s) the draft has no line for:")
        out.extend(
            f"  {x.key:16} {x.label:36.36} filed {x.filed:>12,.2f}"
            for x in c.filed_only
        )
    out.extend(f"note: {n}" for n in c.notes)
    return "\n".join(out) + "\n"
