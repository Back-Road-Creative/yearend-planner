"""``planner taxpack``: everything a preparer asks for, in one folder.

``out/tax-<year>/`` opens with a cover sheet (unit 6a: scope, readiness for a
preparer, missing or waived documents, estimates, forms not handled, questions
for the preparer, versions) and holds the draft return (text, and HTML that
prints to PDF), Form 8949 as CSV, the Schedule C summary, the carryforward and
basis schedules, the estimated payments, the form inventory naming each source
file, and a ZIP of the archived originals. Each file is a view of what the
other commands already print; nothing in the folder is computed only here.
"""

from __future__ import annotations

import csv
import html
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from planner import NOTICE, __version__, coverage
from planner.dashboard.page import readiness_of
from planner.ingest.needs import needed
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.plan import esttax
from planner.taxprep import capgains, close, draft, expected, schedule_c

FILES = {
    "cover.txt": "the cover sheet: scope, readiness, documents, estimates, "
    "forms not handled, questions for the preparer, versions",
    "draft.txt": "the draft return, every line with its source",
    "draft.html": "the same draft, laid out to print (Print, then Save as PDF)",
    "form-8949.csv": "Form 8949 rows, columns (a)-(h) by box",
    "schedule-c.txt": "Schedule C summary and any uncategorised bank rows",
    "schedule-b.csv": "Schedule B rows by part, line and payer (when required)",
    "carryforward.csv": "what carries to next year: capital losses",
    "basis.csv": "cost basis of the open lots, and each Roth conversion",
    "estimated-payments.csv": "federal and state estimated payments, with origin",
    "forms.csv": "the expected forms, which arrived, and their source files",
    "coverage.csv": "each form verified, estimated or not handled, and each gap",
    "originals.zip": "the archived original documents for the year",
}
F8949 = (
    "box",
    "(a) description",
    "(b) date acquired",
    "(c) date sold",
    "(d) proceeds",
    "(e) cost basis",
    "(f) code",
    "(g) adjustment",
    "(h) gain or loss",
    "account",
)


@dataclass
class Pack:
    year: int
    folder: Path
    written: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ready: coverage.Answer | None = None  # ready for a preparer


def _bullets(items: list[str]) -> list[str]:
    return [f"  - {x}" for x in items] or ["  none"]


def cover(
    d: draft.Draft,
    ready: coverage.Answer,
    inv: expected.Inventory,
    today: date,
    closed: list[tuple[int, str]],
) -> str:
    """The cover sheet: each part is read from what the pack already holds."""
    forms = [f for f in draft.ORDER if any(ln.form == f for ln in d.lines)]
    drafted = ", ".join(f"{draft.HEADINGS.get(f, f)} ({d.tag(f)})" for f in forms)
    docs = [
        *(
            f"still to come: {e.form} from {e.issuer} (due {e.due})"
            for e in inv.outstanding
        ),
        *(f"waived: {e.form} from {e.issuer}" for e in inv.waived),
    ]
    estimated = [
        *(f"{k}: the year to date stands in for the annual form" for k in d.estimates),
        *(
            f"{draft.HEADINGS.get(f, f)}: estimated, no verified reference case"
            for f in forms
            if d.tag(f) == coverage.ESTIMATED
        ),
    ]
    gaps = [f"{g.reason} ({g.needed})" if g.needed else g.reason for g in d.coverage]
    questions = [
        *(n for n in d.notes if n.startswith("CHECK")),
        *(
            f"{k}: not known; left out of the draft, never counted as 0"
            for k in d.unknown
        ),
    ]
    answer = "yes" if ready.ready else "no"
    return (
        "\n".join(
            [
                f"Tax pack cover sheet for {d.year}",
                NOTICE,
                "",
                "Scope",
                f"  drafted here: {drafted}",
                "  every figure names its source in draft.txt; this folder is a "
                "preparer's working copy, not an import file for tax software",
                "",
                "Readiness",
                f"  {ready.question}: {answer}",
                *(f"  - {b}" for b in ready.blockers),
                "",
                "Documents",
                *(_bullets(docs) if docs else ["  every expected form is in"]),
                "",
                "Estimates",
                *_bullets(estimated),
                "",
                "Not handled",
                *_bullets(gaps),
                "",
                "Questions for the preparer",
                *_bullets(questions),
                "",
                "Versions",
                f"  planner {__version__}",
                f"  tax engine policyengine-us {d.engine_version}",
                f"  built {today.isoformat()}",
                *(
                    [f"  closed version {v} at {at}" for v, at in closed]
                    or [f"  not closed: no filed return recorded for {d.year}"]
                ),
            ]
        )
        + "\n"
    )


def _csv(path: Path, header: tuple[str, ...], rows: list[tuple[object, ...]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def _money(v: float) -> str:
    return f"{v:.2f}"


def html_page(d: draft.Draft) -> str:
    esc = html.escape
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        f"<title>Draft {d.year} return</title><style>",
        "body{font:11pt Georgia,serif;margin:2em;color:#111;background:#fff}",
        "table{border-collapse:collapse;width:100%;margin-bottom:1.5em}",
        "td{padding:2px 6px;border-bottom:1px solid #ddd;vertical-align:top}",
        "td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}",
        "td.s{color:#555;font-size:9pt}h2{break-after:avoid;margin-top:1.2em}",
        "@media print{body{margin:0}h2{break-before:auto}}</style></head><body>",
        f"<h1>Draft {d.year} return</h1>",
        f"<p>policyengine-us {esc(d.engine_version)}. A draft to check against the "
        "forms, not a filing.</p>",
        f"<p><strong>{esc(NOTICE)}</strong></p>",
    ]
    if d.not_ready:
        parts.append(f"<p><strong>{esc(d.not_ready)}</strong></p>")
    for form in draft.forms(d):
        lines = [ln for ln in d.lines if ln.form == form]
        parts.append(
            f"<h2>{esc(draft.heading(form))} <small>[{esc(d.tag(form))}]"
            "</small></h2><table>"
        )
        for ln in lines:
            places = 2 if round(ln.value, 2) == ln.value else 4
            parts.append(
                f"<tr><td>{esc(ln.line)}</td><td>{esc(ln.label)}</td>"
                f"<td class='n'>{ln.value:,.{places}f}</td>"
                f"<td class='s'>{esc(ln.source)}</td></tr>"
            )
        parts.append("</table>")
    extra = [
        *(f"estimate (YTD stands in): {k}" for k in d.estimates),
        *(f"unknown (left out, not zero): {k}" for k in d.unknown),
        *(f"form still to come: {m}" for m in d.missing),
        *d.notes,
    ]
    if extra:
        parts.append("<h2>Notes</h2><ul>")
        parts.extend(f"<li>{esc(n)}</li>" for n in extra)
        parts.append("</ul>")
    parts.append("</body></html>\n")
    return "\n".join(parts)


def _originals(lay: Layout, year: int, target: Path) -> int:
    folder = lay.data / "archive" / str(year)
    files = (
        sorted(p for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []
    )
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, p.relative_to(folder).as_posix())
    return len(files)


def _coverage(d: draft.Draft) -> list[tuple[object, ...]]:
    """A row per drafted form with its tag and why, then a row per gap."""
    touching = [g for g in d.coverage if "draft" in g.touches]
    rows: list[tuple[object, ...]] = []
    for form in draft.forms(d):
        row = draft.FORM_CAPABILITY.get(form, "draft_return")
        why = "; ".join(g.reason for g in touching) or (
            f"capability {row}: {d.statuses.get(row, 'no row')}"
        )
        needed = "; ".join(g.needed for g in touching)
        rows.append((draft.heading(form), d.tag(form), why, needed))
    rows.extend(
        (f"gap: {g.area}", coverage.NOT_HANDLED, g.reason, g.needed) for g in d.coverage
    )
    return rows


def build(lay: Layout, year: int, as_of: date | None = None) -> Pack:
    """Write ``out/tax-<year>/``; files from an earlier run are replaced."""
    d = draft.build(lay, year)  # raises MissingInputError before anything is written
    folder = lay.out / f"tax-{year}"
    folder.mkdir(parents=True, exist_ok=True)
    pack = Pack(year, folder)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        cg = capgains.build(conn, lay, year)
        sc = schedule_c.build(conn, lay, year)
        pays = esttax.payments(conn, lay, year)
        lots = portfolio.lots(conn)
        convs = db.conversions(conn)
    finally:
        conn.close()
    inv = expected.inventory(lay, year, as_of)
    today = as_of or date.today()
    rep = needed(lay, year)
    pack.ready = readiness_of(
        d.coverage,
        needed=rep.by_state("missing"),
        late=inv.late,
        loose=len(sc.uncategorised) if sc.business or sc.receipts_forms else 0,
        dont_have=rep.by_state("dont_have"),
        waived=inv.waived,
        estimates=rep.by_state("estimate"),
        year=year,
        as_of=today,
    )[2]
    (folder / "cover.txt").write_text(
        cover(d, pack.ready, inv, today, close.history(lay, year)), encoding="utf-8"
    )

    (folder / "draft.txt").write_text(draft.render(d), encoding="utf-8")
    (folder / "draft.html").write_text(html_page(d), encoding="utf-8")
    _csv(
        folder / "form-8949.csv",
        F8949,
        [
            (
                lt.box,
                lt.description,
                lt.acquired,
                lt.sold,
                _money(lt.proceeds),
                _money(lt.basis),
                lt.code,
                _money(lt.adjustment) if lt.adjustment else "",
                _money(lt.gain),
                lt.account,
            )
            for lt in cg.lots
        ],
    )
    (folder / "schedule-c.txt").write_text(schedule_c.render(sc), encoding="utf-8")
    sched_b = folder / "schedule-b.csv"
    rows_b = [ln for ln in d.lines if ln.form == "Sch B"]
    if rows_b:
        _csv(
            sched_b,
            ("part", "line", "payer", "amount", "source"),
            [
                (
                    "I" if ln.line <= "4" else "II" if ln.line <= "6" else "III",
                    ln.line,
                    ln.label,
                    _money(ln.value),
                    ln.source,
                )
                for ln in rows_b
            ],
        )
    else:
        sched_b.unlink(missing_ok=True)  # an earlier run's, no longer required
        pack.notes.extend(n for n in d.notes if n.startswith("Schedule B is not"))
    _csv(
        folder / "carryforward.csv",
        ("item", "amount", "source"),
        [
            (ln.label, _money(ln.value), ln.source)
            for ln in d.lines
            if ln.form == "Carryover"
        ],
    )
    _csv(
        folder / "basis.csv",
        ("kind", "account", "item", "date", "quantity", "basis", "value", "note"),
        [
            *(
                (
                    "open lot",
                    lt.account,
                    lt.symbol,
                    lt.acquired,
                    f"{lt.quantity:g}",
                    _money(lt.basis),
                    _money(lt.value),
                    lt.term,
                )
                for lt in lots
            ),
            *(
                (
                    "Roth conversion",
                    c.source_account,
                    "conversion",
                    c.date,
                    "",
                    _money(c.amount - c.taxable),
                    _money(c.amount),
                    f"taxable {_money(c.taxable)}; penalty-free from "
                    f"{c.accessible_date}",
                )
                for c in convs
            ),
        ],
    )
    _csv(
        folder / "estimated-payments.csv",
        ("agency", "date", "installment", "amount", "origin"),
        [(p.agency, p.date, p.installment, _money(p.amount), p.origin) for p in pays],
    )
    _csv(
        folder / "forms.csv",
        ("form", "issuer", "state", "due", "needed to file", "source files", "why"),
        [
            (
                e.form,
                e.issuer,
                "waived" if e.waived else "LATE" if e.late else e.state,
                e.due,
                "yes" if e.to_file else "no",
                "; ".join(e.documents),
                e.reason,
            )
            for e in inv.items
        ],
    )
    _csv(folder / "coverage.csv", ("section", "tag", "why", "needed"), _coverage(d))
    n = _originals(lay, year, folder / "originals.zip")
    pack.written = [name for name in FILES if (folder / name).exists()]
    if not n:
        pack.notes.append(f"no archived originals for {year} (originals.zip is empty)")
    if inv.outstanding:
        pack.notes.append(
            f"{len(inv.outstanding)} expected form(s) still to come; see forms.csv"
        )
    pack.notes[:0] = [x for x in d.notes if x.startswith("Not handled:")]
    if d.not_ready:
        pack.notes.insert(0, d.not_ready)
    checks = [x for x in d.notes if x.startswith("CHECK")]
    if checks:
        pack.notes.append(f"{len(checks)} CHECK line(s) in the draft to resolve first")
    return pack


def render(pack: Pack) -> str:
    out = [f"Tax pack for {pack.year}: {pack.folder}", f"  {NOTICE}"]
    if pack.ready:
        out.append(f"  {pack.ready.question}: {'yes' if pack.ready.ready else 'no'}")
    out.extend(f"  {name:24} {FILES[name]}" for name in pack.written)
    out.extend(f"note: {n}" for n in pack.notes)
    return "\n".join(out) + "\n"
