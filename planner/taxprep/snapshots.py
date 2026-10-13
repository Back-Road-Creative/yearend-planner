"""Four snapshots of a tax year, kept apart (master plan unit 6b).

- forecast: the year plan's full-year projection while the year is open;
- provisional actual: the draft once the year ends, forms still to come;
- reconciled actual: the draft once every expected form is in or waived;
- filed: the return ``planner close`` recorded.

Each records the same headline figures (AGI, taxable income, the federal and
state tax) with the date taken and where they came from. A snapshot of one
kind never replaces another kind; taking the same kind again adds a record only
when a figure moved. ``planner run`` takes them, and ``planner snapshots``
lists them with the change from each kind to the next.

The records live in ``data/private/snapshots/<year>.yaml``: the user's own
figures, never committed or shared.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from planner.config import safe_load
from planner.engine.household import MissingInputError
from planner.paths import Layout

FORECAST = "forecast"
PROVISIONAL = "provisional actual"
RECONCILED = "reconciled actual"
FILED = "filed"
KINDS = (FORECAST, PROVISIONAL, RECONCILED, FILED)
FIGURES = {
    "agi": "AGI",
    "taxable_income": "taxable income",
    "federal_tax": "federal tax",
    "state_tax": "state tax",
}


@dataclass(frozen=True)
class Snapshot:
    kind: str
    taken: str  # ISO date
    figures: dict[str, float]
    source: str

    def line(self) -> str:
        shown = "  ".join(
            f"{FIGURES[k]} {v:,.2f}" for k, v in self.figures.items() if k in FIGURES
        )
        return f"{self.taken}  {self.kind}: {shown}  ({self.source})"


@dataclass
class Taken:
    year: int
    snapshot: Snapshot | None
    new: bool = False
    notes: list[str] = field(default_factory=list)


def path(lay: Layout, year: int) -> Path:
    return lay.data / "private" / "snapshots" / f"{year}.yaml"


def load(lay: Layout, year: int) -> list[Snapshot]:
    p = path(lay, year)
    if not p.exists():
        return []
    data = safe_load(p.read_text(encoding="utf-8")) or {}
    rows = data.get("snapshots", []) if isinstance(data, dict) else []
    return [
        Snapshot(
            str(r["kind"]),
            str(r["taken"]),
            {str(k): float(v) for k, v in (r.get("figures") or {}).items()},
            str(r.get("source", "")),
        )
        for r in rows
        if isinstance(r, dict) and r.get("kind") in KINDS
    ]


def _save(lay: Layout, year: int, snaps: list[Snapshot]) -> None:
    p = path(lay, year)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = [
        {"kind": s.kind, "taken": s.taken, "figures": s.figures, "source": s.source}
        for s in snaps
    ]
    p.write_text(
        yaml.safe_dump({"year": year, "snapshots": rows}, sort_keys=False),
        encoding="utf-8",
    )


def _headline(get: Callable[[str, str], float | None]) -> dict[str, float]:
    """AGI, taxable income and the federal and state tax from a return's lines
    (``get`` is Draft.get, or the filed return's lines read the same way)."""
    from planner.taxprep import statereturn

    out: dict[str, float] = {}
    for key, (form, line) in (
        ("agi", ("1040", "11a")),
        ("taxable_income", ("1040", "15")),
        ("federal_tax", ("1040", "24")),
    ):
        value = get(form, line)
        if value is not None:
            out[key] = round(value, 2)
    states = [statereturn.carried(get, r) for r in statereturn.RETURNS.values()]
    if any(s is not None for s in states):
        out["state_tax"] = round(sum(s for s in states if s is not None), 2)
    return out


def kind_now(lay: Layout, year: int, as_of: date) -> str:
    """Which snapshot ``year`` is ready for on ``as_of``."""
    from planner.taxprep import close, expected

    if as_of <= date(year, 12, 31):
        return FORECAST
    if close.closed(lay, year) is not None:
        return FILED
    return (
        PROVISIONAL if expected.inventory(lay, year, as_of).outstanding else RECONCILED
    )


def _figures(lay: Layout, year: int, kind: str) -> tuple[dict[str, float], str]:
    from planner.plan import magi
    from planner.taxprep import close, draft

    if kind == FORECAST:
        res = magi.project(lay, year).result
        figures = {
            "agi": res.agi,
            "taxable_income": res.taxable_income,
            "federal_tax": res.fed_total_tax,
            "state_tax": res.state_tax,
        }
        return {k: round(v, 2) for k, v in figures.items()}, "the year plan"
    if kind == FILED:
        filed = close.closed(lay, year) or {}
        back = {v: k for k, v in close.MAP.items()}

        def get(form: str, line: str) -> float | None:
            key = back.get((form, line))
            return filed.get(" ".join(key)) if key else None

        version = close.history(lay, year)[-1][0]
        return _headline(get), f"the filed return, version {version}"
    return _headline(draft.build(lay, year).get), "the draft return"


def take(lay: Layout, year: int, as_of: date) -> Taken:
    """Record the snapshot ``year`` is ready for, unless the last one of that
    kind already holds the same figures."""
    kind = kind_now(lay, year, as_of)
    try:
        figures, source = _figures(lay, year, kind)
    except MissingInputError as exc:
        return Taken(year, None, notes=[f"no {kind} snapshot for {year}: {exc}"])
    snaps = load(lay, year)
    same = [s for s in snaps if s.kind == kind]
    if same and same[-1].figures == figures:
        return Taken(year, same[-1])
    snap = Snapshot(kind, as_of.isoformat(), figures, source)
    _save(lay, year, [*snaps, snap])
    return Taken(year, snap, new=True)


def refresh(lay: Layout, active: int, as_of: date) -> list[Taken]:
    """``planner run``: the active year and the one before it."""
    return [take(lay, y, as_of) for y in (active - 1, active)]


def _change(a: Snapshot, b: Snapshot) -> str:
    moved = [
        f"{FIGURES[k]} {b.figures[k] - a.figures[k]:+,.2f}"
        for k in FIGURES
        if k in a.figures and k in b.figures
    ]
    return f"{b.kind} against {a.kind}: " + ("  ".join(moved) or "nothing to compare")


def lines(lay: Layout, year: int) -> list[str]:
    snaps = load(lay, year)
    if not snaps:
        return [f"no snapshots for {year} yet (planner run takes them)"]
    out = [f"Snapshots for {year}"]
    for kind in KINDS:
        out += [f"  {s.line()}" for s in snaps if s.kind == kind]
    last = [
        next(s for s in reversed(snaps) if s.kind == k)
        for k in KINDS
        if any(s.kind == k for s in snaps)
    ]
    if len(last) > 1:
        out.append("Change between kinds (the latest of each)")
        out += [f"  {_change(a, b)}" for a, b in zip(last, last[1:], strict=False)]
    return out
