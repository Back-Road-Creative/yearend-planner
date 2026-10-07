"""``planner rollover``: the year that ended becomes last year.

Rolling a year carries what next year's plan needs from it: AGI, total tax and
the NC tax (the safe-harbor inputs; another state's prior tax is asked) and the
capital loss carried forward. They
come from the filed return once ``planner close`` has recorded it, else from
the draft, marked as estimates. Each rollover also keeps a snapshot of the
ledger and of the year's dashboard, bumps the active year, refreshes next
year's limits and writes a checklist.

A corrected form (or an amended return) for a rolled year changes what it
carries: rolling it again, or the next ``planner run``, records a new version
with a fresh snapshot and next year's plan reads the new figures. The older
snapshots stay. Nothing changed means nothing written: rollover is idempotent.

State lives in ``data/profile/years.yaml``; snapshots in
``data/snapshots/<year>-v<n>/``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from planner.config import safe_load
from planner.engine.household import MissingInputError
from planner.ledger import db
from planner.paths import Layout
from planner.taxprep import carries

FILED = "CARRY"  # derived facts from the filed return (the Needed panel's actual)
DRAFTED = "CARRY-EST"  # derived facts from the draft (the Needed panel's estimate)
LABELS = {
    "agi": "AGI carried from the draft (1040 line 11)",
    "total_tax": "Total tax carried from the draft (1040 line 24)",
    "nc_tax": "NC tax carried from the draft (D-400 line 15)",
    "state_tax": "State tax carried from the draft (the safe harbor's lines)",
    "st": "Short-term capital loss carried forward",
    "lt": "Long-term capital loss carried forward",
}
# next year's Needed items a rollover asks about (blank keeps the value shown)
ASK = (
    "prior_agi",
    "prior_total_tax",
    "prior_nc_tax",
    "prior_state_tax",
    "prior_capital_loss_carryforward",
    "ss_estimate_62",
    "ss_estimate_67",
    "ss_estimate_70",
    "spouse_ss_estimate_62",
    "spouse_ss_estimate_67",
    "spouse_ss_estimate_70",
    "premium_monthly",
    "roth_basis_contributions",
    "hsa_contribution",
    "spending_actual",
)
CHECKLIST = (
    "Update the engine: planner update --check (planner run also checks)",
    "Confirm {next} limits in config/thresholds.yaml ({limits})",
    "Re-check the PDF form templates against the {year} forms before the "
    "filed return goes in the inbox",
    "Re-enter the {next} ACA plan and premium (premium_monthly)",
    "Set dividend reinvestment off in every taxable account",
    "Confirm the cost-basis method is specific-ID at every broker",
)


class NotEndedError(ValueError):
    """The year asked for has not ended yet."""


@dataclass
class Carry:
    year: int
    basis: str  # "filed", "draft" or "none"
    values: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    forward: list[carries.Carry] = field(default_factory=list)  # the draft's


@dataclass
class Rollover:
    year: int
    version: int
    new: bool  # a version was written by this run
    carry: Carry
    changed: list[str] = field(default_factory=list)  # keys a re-open changed
    snapshot: Path | None = None
    checklist: Path | None = None
    accessible: list[str] = field(default_factory=list)
    plan: list[str] = field(default_factory=list)  # next year's band and glide path
    notes: list[str] = field(default_factory=list)


def state_path(lay: Layout) -> Path:
    return lay.data / "profile" / "years.yaml"


def _state(lay: Layout) -> dict[str, Any]:
    path = state_path(lay)
    if not path.exists():
        return {"active_year": None, "rolled": {}}
    data = safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("active_year", None)
    data["rolled"] = {int(k): v for k, v in (data.get("rolled") or {}).items()}
    return data


def _save(lay: Layout, state: dict[str, Any]) -> None:
    path = state_path(lay)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(state, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def active_year(lay: Layout, today: date) -> int:
    """The year the dashboard plans: set by the last rollover, else today's."""
    return int(_state(lay)["active_year"] or today.year)


def versions(lay: Layout, year: int) -> list[dict[str, Any]]:
    return list(_state(lay)["rolled"].get(year, []))


def due(lay: Layout, today: date) -> int | None:
    """The year that has ended but not been rolled, while the planner still
    plans it (or has never rolled and holds documents for it)."""
    state = _state(lay)
    last = today.year - 1
    if last in state["rolled"]:
        return None
    if state["active_year"] is not None:
        return last if int(state["active_year"]) <= last else None
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        held = db.facts_for(conn, tax_year=last) or db.rows_for(conn, tax_year=last)
    finally:
        conn.close()
    return last if held else None


def _filing_status(lay: Layout, year: int) -> str:
    """The year's filing status as the Needed panel has it (typed, else read
    from the filed 1040); single when neither is known."""
    from planner.ingest.needs import needed

    for st in needed(lay, year).items:
        if st.need.key == "filing_status" and st.state in ("actual", "estimate"):
            return str(st.value)
    return "single"


def _filed_carryover(
    lay: Layout, year: int, filed: dict[str, float]
) -> tuple[float, float] | None:
    """(short, long) loss carried forward from the filed Schedule D, or None
    when the filed return has no Schedule D line 16."""
    from planner.taxprep import capgains

    l16 = filed.get("1040-SCHD 16")
    if l16 is None:
        return None
    limit = 1500.0 if _filing_status(lay, year) == "married_separate" else 3000.0
    l21 = filed.get("1040 7", l16 if l16 >= 0 else max(l16, -limit))
    return capgains.carryover(
        filed.get("1040-SCHD 7", 0.0),
        filed.get("1040-SCHD 15", 0.0),
        l16,
        l21,
        filed.get("1040 15", 0.0),
    )


def carry(lay: Layout, year: int) -> Carry:
    """What ``year`` carries into the next: the filed figures once closed,
    else the draft's."""
    from planner.taxprep import close, draft, statereturn

    why = ""
    try:
        d: draft.Draft | None = draft.build(lay, year)
    except MissingInputError as exc:
        d = None
        why = str(exc)
    drafted = (
        (d.get("Carryover", "8") or 0.0, d.get("Carryover", "13") or 0.0) if d else None
    )
    filed = close.closed(lay, year)
    if filed is not None:
        out = Carry(year, "filed")
        loss = _filed_carryover(lay, year, filed)
        if loss is None and drafted and any(drafted):
            loss = drafted
            out.notes.append(
                f"the filed {year} return has no Schedule D line 16; the loss "
                "carried forward is the draft's"
            )
        out.values = {"st": (loss or (0.0, 0.0))[0], "lt": (loss or (0.0, 0.0))[1]}
        out.forward = d.carries if d else []
        return out
    if d is None or drafted is None:
        return Carry(year, "none", notes=[f"no {year} draft to carry from: {why}"])
    out = Carry(year, "draft", {"st": drafted[0], "lt": drafted[1]}, forward=d.carries)
    for key, (form, line) in (
        ("agi", ("1040", "11a")),
        ("total_tax", ("1040", "24")),
    ):
        value = d.get(form, line)
        if value is not None:
            out.values[key] = value
    for r in statereturn.RETURNS.values():
        tax = statereturn.carried(d.get, r)
        if tax is not None:
            out.values[r.carry] = tax
    out.notes.append(
        f"{year} is not closed: next year reads the draft's figures as estimates "
        f"until the filed return is in (planner close --year {year})"
    )
    return out


def _store(conn: sqlite3.Connection, c: Carry) -> None:
    filed = c.values if c.basis == "filed" else {}
    drafted = c.values if c.basis == "draft" else {}
    db.replace_derived(conn, FILED, c.year, filed, LABELS, f"Carried from {c.year}")
    db.replace_derived(
        conn, DRAFTED, c.year, drafted, LABELS, f"Carried from the {c.year} draft"
    )


def _snapshot(lay: Layout, year: int, version: int, today: date) -> Path:
    """The ledger as it stood, and the year's dashboard, kept for good."""
    from planner.dashboard import page, render

    folder = lay.data / "snapshots" / f"{year}-v{version}"
    folder.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(lay.data / "ledger" / "planner.db")
    dst = sqlite3.connect(folder / "planner.db")
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    pg = page.gather(lay, year, min(today, date(year, 12, 31)))
    (folder / "dashboard.html").write_text(render.html(pg), encoding="utf-8")
    return folder


def _accessible(lay: Layout, year: int) -> list[str]:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        conv = db.conversions(conn)
    finally:
        conn.close()
    return [
        f"Roth conversion of {c.amount:,.2f} on {c.date} becomes penalty-free on "
        f"{c.accessible_date}"
        for c in conv
        if c.accessible_date.startswith(str(year))
    ]


def _plan(lay: Layout, year: int, today: date) -> list[str]:
    """Next year's spending band and glide path, recomputed from the balance
    the ledger holds now, with last year's actual spending beside the band."""
    from planner.engine.household import MissingInputError
    from planner.plan import glidepath, spending

    try:
        g = glidepath.glide(lay, year, today)
    except MissingInputError as exc:
        return [f"glide path and spending band for {year} wait on: {exc}"]
    out = [
        f"spending band for {year}: {g.spending:,.2f} ({g.band}) on a balance "
        f"of {g.balance:,.2f}"
    ]
    ends = glidepath.runs_out(g.rows)
    path = (
        f"glide path for {year}: {g.accessible:,.2f} reachable before age "
        f"{g.access_age:g}, the floor to then needs {g.floor_needed:,.2f}"
    )
    if g.floor_shortfall:
        path += f" (short {g.floor_shortfall:,.2f})"
    if ends:
        path += f"; runs out at age {ends}"
    elif g.rows:
        path += f"; lasts through age {g.rows[-1].age}"
    out.append(path)
    said = spending.against_actual(lay, year, g.spending)
    if said:
        out.append(said)
    return out


def _checklist(lay: Layout, ro: Rollover, limits_line: str, missing: list[str]) -> Path:
    nxt = ro.year + 1
    lines = [f"Rollover {ro.year} -> {nxt} (version {ro.version})", ""]
    items = [t.format(year=ro.year, next=nxt, limits=limits_line) for t in CHECKLIST]
    items += [
        f"Carry {'by hand ' if c.hand else ''}{c.item} {c.amount:,.2f} ({c.line}) "
        f"into {nxt}: {c.next}"
        for c in ro.carry.forward
        if not c.line.startswith("Carryover")
    ]
    items += ro.accessible
    items += [f"Answer in the Needed panel: {m}" for m in missing]
    lines += [f"[ ] {t}" for t in items]
    path = lay.out / f"rollover-{nxt}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def roll(lay: Layout, year: int, today: date | None = None) -> Rollover:
    """Roll ``year`` into ``year + 1``; again later, record what changed."""
    from planner.engine import limits
    from planner.ingest.needs import needed

    today = today or date.today()
    if today <= date(year, 12, 31):
        raise NotEndedError(f"{year} has not ended: roll it over from January")
    c = carry(lay, year)
    state = _state(lay)
    past = state["rolled"].get(year, [])
    record = {"basis": c.basis, "carry": dict(sorted(c.values.items()))}
    if past and {k: past[-1][k] for k in record} == record:
        ro = Rollover(year, int(past[-1]["version"]), False, c)
        ro.notes.append(f"{year} is already rolled as version {ro.version}")
        ro.plan = _plan(lay, year + 1, today)
        return ro
    ro = Rollover(year, len(past) + 1, True, c)
    if past:
        before = past[-1]["carry"]
        ro.changed = sorted(
            k for k in set(before) | set(c.values) if before.get(k) != c.values.get(k)
        )
        if past[-1]["basis"] != c.basis:
            ro.changed.insert(0, f"basis {past[-1]['basis']} -> {c.basis}")
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        _store(conn, c)
    finally:
        conn.close()
    ro.snapshot = _snapshot(lay, year, ro.version, today)
    carries.record(lay, year, c.forward, today)
    ro.accessible = _accessible(lay, year + 1)
    lim = limits.refresh(lay, year + 1, write=True)
    rep = needed(lay, year + 1)
    missing = [s.need.key for s in rep.by_state("missing") if s.need.key in ASK]
    ro.checklist = _checklist(lay, ro, limits.summary(lim), missing)
    ro.plan = _plan(lay, year + 1, today)
    past.append(
        {
            "version": ro.version,
            "rolled_at": datetime.now(UTC).isoformat(timespec="seconds"),
            **record,
            "snapshot": ro.snapshot.relative_to(lay.data).as_posix(),
        }
    )
    state["rolled"][year] = past
    state["active_year"] = max(int(state["active_year"] or 0), year + 1)
    _save(lay, state)
    return ro


def refresh(lay: Layout, today: date | None = None) -> Rollover | None:
    """Re-roll the latest rolled year when what it carries has changed (a
    corrected form, the filed return closed); None when nothing is rolled."""
    rolled = _state(lay)["rolled"]
    if not rolled:
        return None
    return roll(lay, max(rolled), today)


def render(ro: Rollover) -> str:
    nxt = ro.year + 1
    if not ro.new:
        out = [f"{ro.year} unchanged: rolled as version {ro.version}"]
    elif ro.version == 1:
        out = [f"rolled {ro.year} -> {nxt}: {nxt} is now the active year"]
    else:
        out = [
            f"{ro.year} re-opened ({', '.join(ro.changed)}): version {ro.version}; "
            f"the {nxt} plan reads the new figures"
        ]
    c = ro.carry
    if c.values:
        shown = ", ".join(f"{k} {v:,.2f}" for k, v in sorted(c.values.items()))
        out.append(f"carried from the {c.basis} {ro.year} return: {shown}")
    if ro.snapshot:
        out.append(f"snapshot: {ro.snapshot}")
    out.extend(ro.accessible)
    out.extend(ro.plan)
    if ro.checklist:
        out.append(f"checklist: {ro.checklist}")
        out.extend(
            "  " + ln
            for ln in ro.checklist.read_text(encoding="utf-8").splitlines()[2:]
        )
    out.extend(f"note: {n}" for n in c.notes + ro.notes)
    return "\n".join(out) + "\n"
