"""Everything the dashboard shows, gathered in one pass: the year plan's
sections, the Needed panel (missing items grouped by the document that supplies
them, with its download path and the outputs each group unlocks), forms still to
come, the draft return, values awaiting confirm and the alerts. Every panel
carries a tag saying whether its figures are actual, an estimate or unavailable;
nothing is filled in to look complete."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

from planner.engine import feed
from planner.engine import limits as limits_
from planner.engine.household import MissingInputError
from planner.engine.selfcheck import format_years
from planner.engine.tax import engine_version, published_years
from planner.ingest import confirm, ocr
from planner.ingest.derive import gaps as ytd_gaps
from planner.ingest.needs import Group, Status, group_by_document, needed
from planner.ledger import db
from planner.paths import Layout
from planner.plan import rollover
from planner.plan import year as year_plan
from planner.plan.inputs import FILING, Overrides, scope_gaps
from planner.taxprep import close, draft, expected, schedule_c

ACTUAL, ESTIMATE, UNAVAILABLE = "actual", "estimate", "unavailable"
LOOSE_SHOWN = 40  # bank rows listed with their own form; a rule catches the rest
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
    kind: str  # scope | unmatched | wash | pending | stale | gap | thresholds | blocked
    text: str


@dataclass(frozen=True)
class Pending:
    """One document's OCR values awaiting confirm."""

    document_id: int
    file_name: str
    form: str
    values: list[tuple[str, str, float | str]]  # (box, label, value or words)
    crops: dict[int, int] = field(default_factory=dict)  # row of values -> fact id
    # boxes read for more than one form, payer or year in this scan: a
    # correction cannot say which one it means, so the page shows them without
    # an edit field
    shared: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Panel:
    name: str
    title: str
    tag: str
    lines: list[str]
    notes: list[str]
    tables: list[year_plan.Table] = field(default_factory=list)


@dataclass
class Page:
    year: int
    as_of: str
    engine: str
    needed: list[Status] = field(default_factory=list)
    estimates: list[Status] = field(default_factory=list)
    dont_have: list[Status] = field(default_factory=list)
    late_forms: list[expected.Expected] = field(default_factory=list)
    waived_forms: list[expected.Expected] = field(default_factory=list)
    loose: list[db.LedgerRow] = field(default_factory=list)
    panels: list[Panel] = field(default_factory=list)
    inventory: expected.Inventory | None = None
    draft: draft.Draft | None = None
    draft_blocked: str = ""
    pending: list[Pending] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)  # Not handled: the household
    alerts: list[Alert] = field(default_factory=list)
    limits: limits_.Limits | None = None
    stale_days: int | None = None  # set once STALE_DAYS have passed
    years: list[int] = field(default_factory=list)  # tax years the engine publishes
    update_check: str = ""  # the header's words for the last update check
    closings: list[dict[str, Any]] = field(default_factory=list)  # closed years

    @property
    def years_text(self) -> str:
        return format_years(self.years)

    @property
    def next_missing(self) -> int | None:
        """The year after the plan year, when the engine does not publish it."""
        nxt = self.year + 1
        return nxt if self.years and nxt not in self.years else None

    @property
    def needed_groups(self) -> list[Group]:
        """The missing items folded by the document that supplies them."""
        return group_by_document(self.needed)

    @property
    def loose_rows(self) -> int:
        """Bank rows with no Schedule C category, once the year has any
        business income to build the schedule from."""
        return len(self.loose)

    @property
    def shown_loose(self) -> list[db.LedgerRow]:
        return self.loose[:LOOSE_SHOWN]

    @property
    def categories(self) -> list[tuple[str, str]]:
        """(key, label) of every category a bank row can be given."""
        return [
            *(
                (k, f"{label} (line {line})")
                for k, (line, label) in schedule_c.CATEGORIES.items()
            ),
            *((k, f"{k} ({why})") for k, why in schedule_c.EXCLUDED.items()),
        ]

    @property
    def needed_count(self) -> int:
        return len(self.needed) + len(self.late_forms) + (1 if self.loose_rows else 0)

    @property
    def set_aside(self) -> int:
        """Inputs marked don't have and late forms waived: off the list, not on hand."""
        return len(self.dont_have) + len(self.waived_forms)

    def panel(self, name: str) -> Panel:
        return next(p for p in self.panels if p.name == name)


def _tag(section: year_plan.Section, projected: bool) -> str:
    if not section.ok:
        return UNAVAILABLE
    if section.rests_on:
        return ESTIMATE
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
        p.values.append((f.box, f.label, f.value if f.text is None else f.text))
        if ocr.crop_path(lay, f.document_id, f.id).is_file():
            p.crops[len(p.values) - 1] = f.id
    return [
        replace(
            p,
            shared=confirm.shared_boxes(f for f in rows if f.document_id == doc),
        )
        for doc, p in docs.items()
    ]


def last_import(lay: Layout) -> str | None:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        row = conn.execute("SELECT MAX(imported_at) FROM documents").fetchone()
    finally:
        conn.close()
    return str(row[0]) if row and row[0] else None


def _alerts(lay: Layout, page: Page, today: date) -> list[Alert]:
    out = [Alert("scope", text) for text in page.scope]  # first: it frames the rest
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
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        for y in (page.year - 1, page.year):
            for g in ytd_gaps(conn, y):
                if g.gap:
                    out.append(
                        Alert(
                            "gap",
                            f"{y} {g.form} reports {g.reported:,.2f}; the YTD "
                            f"{g.box} was {g.ytd:,.2f} ({g.gap:+,.2f}): the form "
                            "is the figure filed",
                        )
                    )
    finally:
        conn.close()
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
    page.dont_have = rep.by_state("dont_have")
    page.estimates = rep.by_state("estimate")
    projected = today <= date(year, 12, 31) or bool(page.estimates)
    by_name = {s.name: s for s in plan.sections}
    page.panels = [
        Panel(n, TITLES[n], _tag(s, projected), s.lines, s.notes, s.tables)
        for n, s in ((n, by_name[n]) for n in ORDER)
    ]
    page.inventory = expected.inventory(lay, year, today)
    page.late_forms = page.inventory.late
    page.waived_forms = page.inventory.waived
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        sc = schedule_c.build(conn, lay, year)
    finally:
        conn.close()
    page.loose = sc.uncategorised if sc.business or sc.receipts_forms else []
    try:
        page.draft = draft.build(lay, year)
    except MissingInputError as exc:
        page.draft_blocked = str(exc)
    page.pending = _pending(lay)
    status = next((s for s in rep.items if s.need.key == "filing_status"), None)
    if status is not None and status.value in FILING:
        page.scope = scope_gaps(FILING[str(status.value)])
    page.closings = [
        c for c in (close.latest(lay, y) for y in (year - 1, year)) if c is not None
    ]
    page.limits = limits_.refresh(lay, year, write=False)
    page.years = list(published_years())
    page.update_check = feed.status_line(lay)
    page.alerts = _alerts(lay, page, today)
    page.stale_days = _stale_days(lay, today)  # also a banner atop every page
    return page
