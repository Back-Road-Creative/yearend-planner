"""Everything the dashboard shows, gathered in one pass: the year plan's
sections, the Needed panel (each missing item with the document that supplies
it), forms still to come, the draft return, values awaiting confirm and the
alerts. Every panel carries a tag saying whether its figures are actual, an
estimate or unavailable; nothing is filled in to look complete."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from planner.engine import feed
from planner.engine import limits as limits_
from planner.engine.household import MissingInputError
from planner.engine.tax import engine_version
from planner.ingest import confirm
from planner.ingest.needs import Status, needed
from planner.ledger import db
from planner.paths import Layout
from planner.plan import rollover
from planner.plan import year as year_plan
from planner.plan.inputs import Overrides
from planner.taxprep import draft, expected, schedule_c

ACTUAL, ESTIMATE, UNAVAILABLE = "actual", "estimate", "unavailable"
STALE_DAYS = 90  # no document imported for this long: every page says so
# Panels whose figures project the year forward from what has happened so far.
PROJECTIONS = ("magi", "conversion", "levers", "spending", "glide", "cash", "esttax")
TITLES = {
    "needed": "Needed",
    "glide": "Status vs. glide path",
    "spending": "Spending band",
    "magi": "MAGI headroom",
    "levers": "Levers",
    "conversion": "Roth conversion",
    "forms": "Tax prep: forms",
    "esttax": "Estimated tax",
    "cash": "Cash buffer",
    "washsales": "Wash sales",
    "calendar": "Deadlines",
}
# Top to bottom, as the plan lays the page out.
ORDER = (
    "glide",
    "spending",
    "magi",
    "levers",
    "conversion",
    "forms",
    "esttax",
    "cash",
    "washsales",
    "calendar",
)


@dataclass(frozen=True)
class Alert:
    kind: str  # unmatched | wash | pending | stale | thresholds | blocked
    text: str


@dataclass(frozen=True)
class Pending:
    """One document's OCR values awaiting confirm."""

    document_id: int
    file_name: str
    form: str
    values: list[tuple[str, str, float]]  # (box, label, value)


@dataclass(frozen=True)
class Panel:
    name: str
    title: str
    tag: str
    lines: list[str]
    notes: list[str]


@dataclass
class Page:
    year: int
    as_of: str
    engine: str
    needed: list[Status] = field(default_factory=list)
    estimates: list[Status] = field(default_factory=list)
    late_forms: list[expected.Expected] = field(default_factory=list)
    loose_rows: int = 0
    panels: list[Panel] = field(default_factory=list)
    inventory: expected.Inventory | None = None
    draft: draft.Draft | None = None
    draft_blocked: str = ""
    pending: list[Pending] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    limits: limits_.Limits | None = None
    stale_days: int | None = None  # set once STALE_DAYS have passed

    @property
    def needed_count(self) -> int:
        return len(self.needed) + len(self.late_forms) + (1 if self.loose_rows else 0)

    def panel(self, name: str) -> Panel:
        return next(p for p in self.panels if p.name == name)


def _tag(section: year_plan.Section, projected: bool) -> str:
    if not section.ok:
        return UNAVAILABLE
    return ESTIMATE if projected and section.name in PROJECTIONS else ACTUAL


def _pending(lay: Layout) -> list[Pending]:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        rows = confirm.pending(conn)
    finally:
        conn.close()
    docs: dict[int, Pending] = {}
    for f in rows:
        p = docs.setdefault(
            f.document_id, Pending(f.document_id, f.file_name, f.form, [])
        )
        p.values.append((f.box, f.label, f.value))
    return list(docs.values())


def last_import(lay: Layout) -> str | None:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        row = conn.execute("SELECT MAX(imported_at) FROM documents").fetchone()
    finally:
        conn.close()
    return str(row[0]) if row and row[0] else None


def _alerts(lay: Layout, page: Page, today: date) -> list[Alert]:
    out: list[Alert] = []
    due = rollover.due(lay, today)
    if due is not None:
        out.append(
            Alert(
                "rollover",
                f"{due} has ended: roll it over to plan {due + 1} (the button "
                f"below, or planner rollover --year {due})",
            )
        )
    hold = feed.held(lay)
    if hold:
        out.append(
            Alert(
                "update",
                f"engine update {hold['version']} held on {hold['date']}: "
                f"{hold['reason']}; still on {hold['installed']}",
            )
        )
    unmatched = lay.data / "inbox" / "UNMATCHED"
    for path in sorted(unmatched.glob("*")) if unmatched.is_dir() else []:
        if path.name.endswith(".reason.txt"):
            continue
        reason = path.with_name(path.name + ".reason.txt")
        why = reason.read_text(encoding="utf-8").strip() if reason.exists() else ""
        text = f"{path.name} was not read" + (f": {why}" if why else "")
        out.append(Alert("unmatched", text))
    wash = [ln for ln in page.panel("washsales").lines if ln.startswith("WASH")]
    out += [Alert("wash", ln) for ln in wash]
    for doc in page.pending:
        out.append(
            Alert(
                "pending",
                f"{doc.file_name} ({doc.form}): {len(doc.values)} OCR value(s) "
                "to confirm",
            )
        )
    last = last_import(lay)
    if last is None:
        out.append(
            Alert("stale", "no document imported yet: drop your documents on this page")
        )
    elif (age := _stale_days(lay, today)) is not None:
        out.append(
            Alert(
                "stale",
                f"last document imported {age} days ago ({last[:10]}): "
                "figures may be stale",
            )
        )
    lim = page.limits
    if lim is not None:
        if lim.coverage.get(page.year) == "none":
            text = f"config/thresholds.yaml has no {page.year} rows"
            out.append(Alert("thresholds", text))
        years = [page.year] + ([page.year + 1] if today.month >= 10 else [])
        for y in years:
            soft = lim.projected.get(y, []) + lim.carried.get(y, [])
            if soft:
                out.append(
                    Alert(
                        "thresholds",
                        f"{y} limits not yet confirmed: {', '.join(soft)} "
                        "(projected by the engine or carried from the year "
                        "before); enter the published figures in "
                        "config/thresholds.yaml",
                    )
                )
        for line in lim.drift.get(page.year, []):
            out.append(Alert("thresholds", line))
    for p in page.panels:
        if p.tag == UNAVAILABLE:
            out.append(Alert("blocked", f"{p.title}: {'; '.join(p.lines)}"))
    return out


def _stale_days(lay: Layout, today: date) -> int | None:
    """Days since the newest import when past STALE_DAYS; the plan still runs."""
    last = last_import(lay)
    if last is None:
        return None
    age = (today - datetime.fromisoformat(last).date()).days
    return age if age > STALE_DAYS else None


def gather(
    lay: Layout,
    year: int,
    as_of: date | None = None,
    overrides: Overrides | None = None,
) -> Page:
    today = as_of or date.today()
    plan = year_plan.assemble(lay, year, today, overrides)
    rep = needed(lay, year)
    page = Page(year, today.isoformat(), engine_version())
    page.needed = rep.by_state("missing")
    page.estimates = rep.by_state("estimate")
    projected = today <= date(year, 12, 31) or bool(page.estimates)
    by_name = {s.name: s for s in plan.sections}
    page.panels = [
        Panel(n, TITLES[n], _tag(s, projected), s.lines, s.notes)
        for n, s in ((n, by_name[n]) for n in ORDER)
    ]
    page.inventory = expected.inventory(lay, year, today)
    page.late_forms = page.inventory.late
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        sc = schedule_c.build(conn, lay, year)
    finally:
        conn.close()
    page.loose_rows = len(sc.uncategorised) if sc.business or sc.receipts_forms else 0
    try:
        page.draft = draft.build(lay, year)
    except MissingInputError as exc:
        page.draft_blocked = str(exc)
    page.pending = _pending(lay)
    page.limits = limits_.refresh(lay, year, write=False)
    page.alerts = _alerts(lay, page, today)
    page.stale_days = _stale_days(lay, today)  # also a banner atop every page
    return page
