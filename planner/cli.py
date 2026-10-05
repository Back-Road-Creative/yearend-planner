"""``planner <command>``. Every page action gets a CLI twin here."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from typer.core import TyperGroup

from planner.paths import CloudSyncedPathError, Layout, WriterBusyError, layout

if TYPE_CHECKING:
    from planner.plan.inputs import Overrides


class _Group(TyperGroup):
    """Notes whether the subcommand was asked for ``--help``: the group callback
    runs before the subcommand reads its arguments, and help takes no lock."""

    def resolve_command(self, ctx: Any, args: list[str]) -> Any:
        found = super().resolve_command(ctx, args)
        ctx.meta["planner.help"] = "--help" in found[2]
        return found


app = typer.Typer(add_completion=False, cls=_Group)

# Commands that never write under data/ (or only read it, as backup, facts and
# rows do: they open the ledger read-only), so they run beside a dashboard or
# another window. Every other command takes the
# single-writer lock in :func:`_single_writer`.
NO_LOCK = frozenset(
    {
        "backup",
        "check-config",
        "compute",
        "facts",
        "init",
        "paths",
        "rows",
        "selfcheck",
        "sweep",
        "thresholds",
        "verify",
        "version",
    }
)
BUSY_EXIT = 2


@app.callback()
def _single_writer(ctx: typer.Context) -> None:
    """Year-End Tax & Retirement Planner. One planner writes at a time."""
    if (
        ctx.resilient_parsing
        or ctx.invoked_subcommand is None
        or ctx.invoked_subcommand in NO_LOCK
        or ctx.meta.get("planner.help")
    ):
        return
    try:
        ctx.with_resource(layout().lock())
    except CloudSyncedPathError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except WriterBusyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=BUSY_EXIT) from exc


def _open_ledger_readonly() -> sqlite3.Connection | None:
    """The ledger opened read-only for the listing commands, which hold no lock;
    ``None`` when no ledger exists yet (nothing is created)."""
    from planner.ledger import db

    try:
        return db.connect_readonly(layout().data / "ledger" / "planner.db")
    except db.LedgerOutOfDate as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


def _ensure_or_refuse(lay: Layout) -> None:
    try:
        lay.ensure()
    except CloudSyncedPathError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@app.command()
def version() -> None:
    """Print the planner version (the release's VERSION file when there is one)."""
    from planner.engine.update import installed_version

    typer.echo(f"planner {installed_version(layout().root)}")


@app.command()
def init() -> None:
    """Set up this folder: create data/ and out/ with every subfolder. Refuses a
    folder inside OneDrive, Dropbox, iCloud Drive or Google Drive (exit 2),
    since a sync client would copy your ledger to the cloud. Safe to repeat."""
    lay = layout()
    _ensure_or_refuse(lay)
    typer.echo(f"ready: {lay.root}")
    typer.echo(f"drop your tax documents in {lay.data / 'inbox'}, then run planner")


@app.command()
def paths() -> None:
    """Show where this planner keeps its folders, creating data/ and out/."""
    lay = layout()
    _ensure_or_refuse(lay)
    typer.echo(f"root   {lay.root}")
    typer.echo(f"data   {lay.data}")
    typer.echo(f"out    {lay.out}")
    typer.echo(f"config {lay.config}")


@app.command()
def selfcheck(
    regression: bool = typer.Option(
        False,
        "--regression",
        help="run every shipped reference case and print the engine's figures "
        "(what planner update holds a candidate to)",
    ),
) -> None:
    """Run one real federal calculation through the tax engine and print it;
    --regression runs the shipped reference cases instead."""
    if regression:
        _regression()
        return
    from planner.engine.selfcheck import YEARS_LABEL, format_years, run

    result = run()
    typer.echo(
        f"policyengine-us {result.engine_version}: single, NC, "
        f"{result.year} wages ${result.wages:,} -> federal income tax "
        f"${result.income_tax:,.2f}"
    )
    typer.echo(f"{YEARS_LABEL}{format_years(result.years)}")
    if not result.income_tax > 0:
        typer.echo("selfcheck failed: tax is not positive", err=True)
        raise typer.Exit(code=1)


def _regression() -> None:
    from planner.engine import verify
    from planner.engine.tax import engine_version

    cases = verify.reference_cases()
    if not cases:
        typer.echo("selfcheck failed: no reference cases", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"policyengine-us {engine_version()}: {len(cases)} reference cases")
    values = verify.regression_values()
    typer.echo(verify.format_regression(values))
    for name, report in verify.verify_file(verify.REFERENCE):
        typer.echo(
            f"{name}: {report.n_ok}/{len(report.lines)} filed lines within $1.00"
        )


@app.command()
def check_config() -> None:
    """Load config/ and report; exit non-zero on a malformed file."""
    from planner.config import load_assumptions, load_capabilities, load_thresholds

    lay = layout()
    a = load_assumptions(lay.config / "assumptions.example.yaml")
    t = load_thresholds(lay.config / "thresholds.yaml")
    c = load_capabilities(lay.config / "capabilities.yaml")
    typer.echo(
        f"assumptions: {len(a)} fields; thresholds: years {sorted(t)}; "
        f"capabilities: {len(c)}"
    )


@app.command()
def compute(
    household: Path = typer.Argument(..., help="household YAML (see tests/fixtures)"),
    year: int = typer.Option(2026, help="tax year", min=1990, max=2100),
) -> None:
    """Every tax figure for one household-year, as JSON."""
    from dataclasses import asdict

    from planner.engine.household import load_household
    from planner.engine.tax import compute as _compute

    typer.echo(json.dumps(asdict(_compute(year, load_household(household))), indent=2))


@app.command()
def sweep(
    household: Path = typer.Argument(...),
    variable: str = typer.Option(
        "taxable_roth_conversions", help="engine input to sweep"
    ),
    lo: int = typer.Option(0, min=0),
    hi: int = typer.Option(100_000, min=0),
    step: int = typer.Option(5_000, min=1),
    year: int = typer.Option(2026, min=1990, max=2100),
) -> None:
    """Sweep one input across a range in a single engine run; one JSON row per step."""
    from planner.engine.household import load_household
    from planner.engine.tax import compute_sweep

    rows = compute_sweep(year, load_household(household), variable, lo, hi, step)
    typer.echo(json.dumps(rows, indent=2))


@app.command()
def verify(
    filed_return: Path | None = typer.Argument(
        None,
        help="data/private/returns/<year>.yaml; none: the shipped reference cases",
    ),
    tolerance: float = typer.Option(
        1.0, help="dollars of allowed difference per line", min=0
    ),
) -> None:
    """Recompute a filed year from its inputs; compare each line to what was filed."""
    from planner.engine import verify as verify_

    failed = False
    for name, report in verify_.verify_file(
        filed_return or verify_.REFERENCE, tolerance
    ):
        typer.echo(name)
        for line in report.lines:
            mark = "ok " if line.ok else "DIFF"
            typer.echo(
                f"{mark} {line.name:32} filed {line.filed:>12,.2f} "
                f"engine {line.engine:>12,.2f}"
            )
        typer.echo(
            f"{report.year}: {report.n_ok}/{len(report.lines)} lines "
            f"within ${tolerance:,.2f}"
        )
        failed = failed or not report.passed
    if failed:
        raise typer.Exit(code=1)


@app.command()
def update(
    release_zip: Path | None = typer.Argument(
        None, help="release zip from GitHub Releases"
    ),
    sha256: str | None = typer.Option(None, help="sha256 published beside the zip"),
    rollback: bool = typer.Option(False, "--rollback", help="restore python-previous/"),
    check: bool = typer.Option(False, "--check", help="look for a newer release now"),
    allow_major: bool = typer.Option(
        False,
        "--allow-major",
        help="install a release that jumps a major version of the planner or "
        "policyengine-us (alone: fetch the feed's release)",
    ),
    finish: bool = typer.Option(False, "--finish", hidden=True),
) -> None:
    """Swap in a newer release after its own selfcheck passes; --rollback undoes
    it; --check looks for one on the update feed now (the automatic check runs
    at most weekly). A release that jumps a major version waits for
    --allow-major. From planner.cmd the folders move after this process exits
    (it cannot move the interpreter it runs on)."""
    from planner.engine import feed
    from planner.engine import update as upd

    root = layout().root
    try:
        if finish:
            typer.echo(upd.finish(root))
            return
        if check:
            line = feed.check(layout(), force=True, allow_major=allow_major)
            typer.echo(line or "no newer release found")
            return
        if rollback:
            if not (root / upd.PREVIOUS / "VERSION").exists():
                raise upd.UpdateError(
                    "nothing to roll back to (no python-previous/VERSION)"
                )
            if upd.launcher_swaps(root):
                upd.write_swap(root, "back", rerun=False)
                raise typer.Exit(code=upd.LAUNCHER_SWAP)
            typer.echo(f"rolled back to {upd.rollback(root)}")
            return
        if release_zip is None and sha256 is None and allow_major:
            if feed.feed_url(layout()) is None:
                raise upd.UpdateError("the update feed is off (PLANNER_UPDATE_FEED)")
            line = feed.check(layout(), force=True, allow_major=True)
            typer.echo(line or "no newer release found")
            return
        if release_zip is None or sha256 is None:
            typer.echo(
                "update needs a release zip and --sha256 (or --rollback)", err=True
            )
            raise typer.Exit(code=2)
        exe = "python.exe" if sys.platform == "win32" else "python"
        result = upd.stage(
            release_zip, sha256, root, python_exe=exe, allow_major=allow_major
        )
        if upd.launcher_swaps(root):
            typer.echo(f"staged {result.version}; swapping it in; {result.years_note}")
            upd.write_swap(root, "in", rerun=False)
            raise typer.Exit(code=upd.LAUNCHER_SWAP)
        typer.echo(upd.apply_staged(root))
    except upd.UpdateError as exc:
        typer.echo(f"update refused: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def backup(
    dest: Path | None = typer.Argument(
        None, help="zip to write; default out/backups/planner-backup-<stamp>.zip"
    ),
    encrypt: bool = typer.Option(
        True, "--encrypt/--plain", help="ask for a password (AES-256)"
    ),
) -> None:
    """Zip data/ and config/ into one file you can copy to a USB drive. With a
    password (asked twice, never shown) it is AES-256; --plain skips it."""
    from planner import backup as bk

    password = ""
    if encrypt:
        password = typer.prompt(
            "Backup password (Enter for none)",
            default="",
            hide_input=True,
            confirmation_prompt=True,
            show_default=False,
        )
    lay = layout()
    lay.ensure()
    path = bk.backup(lay, dest, password)
    typer.echo(f"written {path}")
    typer.echo(
        "encrypted with AES-256: keep the password; it cannot be recovered"
        if password
        else "NOT encrypted: anyone with this file can read your figures"
    )


@app.command()
def restore(
    src: Path | None = typer.Argument(None, help="a zip made by planner backup"),
    undo: bool = typer.Option(
        False, "--undo", help="put data-previous/ back (before the next run)"
    ),
) -> None:
    """Check a backup (paths, size, every file against its manifest), then swap
    its data/ in. The current data/ is kept as data-previous/ until the next
    clean planner run; --undo puts it back."""
    from planner import backup as bk

    lay = layout()
    try:
        if undo:
            typer.echo(
                "data-previous/ is data/ again"
                if bk.undo(lay)
                else "nothing to undo (no data-previous/)"
            )
            return
        if src is None:
            typer.echo("restore needs a backup zip (or --undo)", err=True)
            raise typer.Exit(code=2)
        password = ""
        if bk.encrypted(src):
            password = typer.prompt("Backup password", hide_input=True)
        res = bk.restore(lay, src, password)
    except bk.BackupError as exc:
        typer.echo(f"restore refused: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"restored {res.files} files from {src.name}")
    if res.carried:
        typer.echo(
            f"limits carried into config/thresholds.yaml: {', '.join(res.carried)}"
        )
    if res.previous:
        typer.echo(
            f"the data it replaced is in {res.previous.name}/ until the next "
            "planner run; planner restore --undo puts it back"
        )


@app.command()
def ingest() -> None:
    """Read every file in data/inbox/ into the ledger; archive or mark UNMATCHED."""
    from planner.ingest import ingest as _ingest

    lay = layout()
    lay.ensure()
    report = _ingest(lay)
    for item in report.imported:
        typer.echo(f"imported  {item.file_name}: {', '.join(item.forms)}")
    for item in report.pending:
        typer.echo(
            f"pending   {item.file_name}: {', '.join(item.forms)} "
            "(OCR; planner confirm)"
        )
    for name in report.duplicates:
        typer.echo(f"duplicate {name} (already in the ledger; archived)")
    for name, reason in report.unmatched:
        typer.echo(f"UNMATCHED {name}: {reason}")
    for name, note in report.notes:
        typer.echo(f"note      {name}: {note}")
    for year, n in sorted(report.derived.items()):
        typer.echo(f"derived   {year}: {n} YTD facts from rows")
    typer.echo(
        f"batch {report.batch}: {len(report.imported)} imported, "
        f"{len(report.pending)} pending, {len(report.duplicates)} duplicate, "
        f"{len(report.unmatched)} unmatched"
    )


@app.command()
def derive(
    year: int = typer.Option(
        ..., help="tax year to recompute from the ledger rows", min=1990, max=2100
    ),
) -> None:
    """Recompute the YTD facts (realized gains, dividends, interest, bank flows)
    for one year from the imported rows; supersedes the previous run."""
    from planner.ingest.derive import derive as _derive
    from planner.ledger import db

    conn = db.connect(layout().data / "ledger" / "planner.db")
    n = _derive(conn, year)
    typer.echo(f"derived {year}: {n} YTD facts from rows")


@app.command()
def facts(
    year: int | None = typer.Option(None, help="tax year", min=1990, max=2100),
    form: str | None = typer.Option(None, help="form, e.g. 1099-DIV"),
) -> None:
    """List the accepted facts in the ledger, each with its source file and page."""
    from planner.ledger import db

    conn = _open_ledger_readonly()
    rows = db.facts_for(conn, year, form, text=None) if conn else []
    if conn:
        conn.close()
    for r in rows:
        shown = r.text if r.text is not None else f"{r.value:,.2f}"
        typer.echo(
            f"{r.tax_year} {r.form:9} {r.issuer[:24]:24} box {r.box:12} "
            f"{shown:>14}  {r.file_name} p{r.page}"
        )
    typer.echo(f"{len(rows)} facts")


@app.command()
def rows(
    year: int | None = typer.Option(None, help="tax year", min=1990, max=2100),
    source: str | None = typer.Option(None, help="e.g. vanguard_transactions, bank"),
    kind: str | None = typer.Option(
        None, help="holding, lot, transaction, realized, income or bank"
    ),
) -> None:
    """List imported CSV rows (holdings, lots, transactions, income, bank lines)."""
    from planner.ledger import db

    conn = _open_ledger_readonly()
    out = db.rows_for(conn, year, source, kind) if conn else []
    if conn:
        conn.close()
    for r in out:
        amount = f"{r.amount_cents / 100:>14,.2f}" if r.amount_cents is not None else ""
        typer.echo(
            f"{r.date or '':10} {r.source:22} {r.type[:14]:14} {r.symbol[:8]:8} "
            f"{amount:>14}  {r.file_name}:{r.line}"
        )
    typer.echo(f"{len(out)} rows")


@app.command()
def needed(
    year: int = typer.Option(..., help="plan year", min=1990, max=2100),
    all: bool = typer.Option(False, "--all", help="also list what is already covered"),
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; default today"),
) -> None:
    """What the plan still lacks for a year, grouped by the document that
    supplies it (one download closes several): its download path, the outputs
    the group unlocks, then each missing item and why, then every form past its
    due date that the return needs. Empty when the intake loop is done."""
    from datetime import date

    from planner.ingest.needs import group_by_document
    from planner.ingest.needs import needed as _needed
    from planner.ledger import db
    from planner.taxprep import schedule_c
    from planner.taxprep.expected import inventory

    rep = _needed(layout(), year)
    conn = db.connect(layout().data / "ledger" / "planner.db")
    try:
        sc = schedule_c.build(conn, layout(), year)
    finally:
        conn.close()
    loose = len(sc.uncategorised) if sc.business or sc.receipts_forms else 0
    late = inventory(layout(), year, date.fromisoformat(as_of) if as_of else None).late
    for g in group_by_document(rep.by_state("missing")):
        typer.echo(f"document  {g.title} ({len(g.items)} open)")
        if g.doc:
            typer.echo(f"          download: {g.doc.path}")
        typer.echo(f"          unlocks: {', '.join(g.unlocks)}")
        for st in g.items:
            typer.echo(f"needed    {st.need.key:28} {st.need.label}")
            typer.echo(f"          why: {st.need.why}")
            typer.echo(f"          {'look for' if g.doc else 'from'}: {st.need.source}")
            typer.echo(f"          unlocks: {', '.join(st.need.unlocks)}")
            if st.origin:
                typer.echo(f"          note: {st.origin}")
            typer.echo(
                f"          type: planner enter --year {year} {st.need.key} <value>"
            )
    for st in rep.items:
        if st.state == "estimate":
            typer.echo(f"estimate  {st.need.key:28} {st.value:,.2f}  ({st.origin})")
        elif all and st.state != "missing":
            tag = "dont-have" if st.state == "dont_have" else "actual   "
            val = "" if st.value is None else f" {st.value}"
            typer.echo(f"{tag} {st.need.key:28}{val}  ({st.origin})")
    for e in late:
        typer.echo(f"form      {e.form} from {e.issuer} (due {e.due})")
        typer.echo(f"          why: {e.reason}")
        typer.echo(f"          from: {e.where}; drop it in the inbox")
        typer.echo(
            f"          or, if it will not come: planner waive --year {year} "
            f'--form {e.form} --issuer "{e.issuer}"'
        )
    if loose:
        typer.echo(f"categorize {loose} bank row(s) with no Schedule C category")
        typer.echo("          why: Schedule C counts only categorised rows")
        typer.echo(f"          type: planner categorize --year {year}")
    n = len(rep.by_state("missing")) + len(late) + (1 if loose else 0)
    typer.echo("nothing needed" if n == 0 else f"{n} needed")


@app.command()
def gains(year: int = typer.Option(..., help="tax year", min=1990, max=2100)) -> None:
    """Form 8949 and Schedule D: each closed lot in a taxable account, wash sales
    across every account (code W), 1099-DIV capital gain distributions and the
    loss carried in. Once the year has ended its Schedule D feeds the Needed
    panel."""
    from planner.ledger import db
    from planner.taxprep import capgains

    lay = layout()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        cg = capgains.store(conn, lay, year)
    finally:
        conn.close()
    typer.echo(capgains.render(cg), nl=False)


@app.command()
def hsa(year: int = typer.Option(..., help="tax year", min=1990, max=2100)) -> None:
    """Form 8889: the HSA limit for your coverage and months, employer money
    against it, the deduction, any excess, and distributions not spent on
    medical care. Once the year has ended its deduction feeds the Needed
    panel."""
    from planner.ledger import db
    from planner.taxprep import hsa as form_8889

    lay = layout()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        h = form_8889.store(conn, lay, year)
    finally:
        conn.close()
    typer.echo(form_8889.render(h), nl=False)


@app.command()
def categorize(
    year: int = typer.Option(..., help="tax year", min=1990, max=2100),
    rule: str | None = typer.Option(None, help="description text to match"),
    row: str | None = typer.Option(None, help="one row's key, as listed"),
    as_: str | None = typer.Option(None, "--as", help="the category"),
) -> None:
    """Schedule C from the bank rows: categorise them by a rule (a piece of the
    description) or one row at a time, then see the lines and what is left.
    Nothing is categorised by guess."""
    from planner.ledger import db
    from planner.taxprep import schedule_c

    lay = layout()
    if (rule or row) and not as_:
        typer.echo("error: --rule and --row need --as CATEGORY")
        raise typer.Exit(2)
    try:
        if rule and as_:
            schedule_c.add_rule(lay, rule, as_)
        elif row and as_:
            schedule_c.assign(lay, row, as_)
    except ValueError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(2) from exc
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        sc = schedule_c.store(conn, lay, year)
    finally:
        conn.close()
    if rule and as_:
        caught = sum(
            1
            for r in sc.rows.get(as_, [])
            if rule.lower() in f"{r.type} {r.description}".lower()
        )
        typer.echo(f"rule {rule!r} -> {as_}: {caught} row(s) this year")
    typer.echo(schedule_c.render(sc), nl=False)


@app.command()
def forms(
    year: int = typer.Option(..., help="tax year", min=1990, max=2100),
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; default today"),
) -> None:
    """The forms the year should produce (from last year's issuers, the
    accounts and the Needed panel), which have arrived, and where to download
    the rest."""
    from datetime import date

    from planner.taxprep.expected import inventory, render

    inv = inventory(layout(), year, date.fromisoformat(as_of) if as_of else None)
    typer.echo(render(inv), nl=False)


@app.command()
def draft(
    year: int = typer.Option(..., help="tax year", min=1990, max=2100),
    as_json: bool = typer.Option(False, "--json", help="machine-readable"),
) -> None:
    """The draft return: Form 1040 with its schedules and forms, and the NC
    D-400 with Schedule S, every line priced by the engine and naming its source."""
    import json
    from dataclasses import asdict

    from planner.engine.household import MissingInputError
    from planner.taxprep.draft import build, render

    try:
        d = build(layout(), year)
    except MissingInputError as exc:
        typer.echo(f"blocked: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if as_json:
        typer.echo(json.dumps(asdict(d), indent=2))
    else:
        typer.echo(render(d), nl=False)


@app.command()
def taxpack(
    year: int = typer.Option(..., help="tax year", min=1990, max=2100),
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; default today"),
) -> None:
    """Everything a preparer asks for in out/tax-<year>/: the draft return
    (text and printable HTML), Form 8949 CSV, Schedule C, carryforward and
    basis, estimated payments, the form inventory, and the originals ZIP."""
    from datetime import date

    from planner.engine.household import MissingInputError
    from planner.taxprep import package

    try:
        pack = package.build(
            layout(), year, date.fromisoformat(as_of) if as_of else None
        )
    except MissingInputError as exc:
        typer.echo(f"blocked: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(package.render(pack), nl=False)


@app.command()
def close(year: int = typer.Option(..., help="tax year", min=1990, max=2100)) -> None:
    """Close the year from the filed return: every filed line beside the
    draft's, and the filed figures kept as the year's record. An amended return
    closes it again as a new version."""
    from planner.taxprep import close as closing

    try:
        c = closing.close(layout(), year)
    except closing.NotFiledError as exc:
        typer.echo(f"blocked: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(closing.render(c), nl=False)


@app.command()
def rollover(
    year: int | None = typer.Option(
        None, help="the year that ended; default last year", min=1990, max=2100
    ),
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; default today"),
    ask: bool | None = typer.Option(
        None,
        "--ask/--no-ask",
        help="prompt for next year's figures (default: when run in a console)",
    ),
) -> None:
    """Roll the year that ended into the next: carry AGI, total tax, NC tax and
    the capital loss carryforward (filed figures once closed, else the draft's),
    keep a snapshot of the ledger and the year's dashboard, make next year the
    active one, refresh its limits, report next year's spending band and glide
    path and print the checklist; --ask then prompts for next year's figures,
    last year's actual spending included. Running it again
    changes nothing unless a corrected form or the filed return changed what
    the year carries; then it records a new version."""
    import sys
    from datetime import date

    from planner.ingest import needs
    from planner.plan import rollover as roll

    lay = layout()
    lay.ensure()
    today = date.fromisoformat(as_of) if as_of else date.today()
    target = year or today.year - 1
    try:
        ro = roll.roll(lay, target, today)
    except roll.NotEndedError as exc:
        typer.echo(f"blocked: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(roll.render(ro), nl=False)
    if not (sys.stdin.isatty() if ask is None else ask):
        return
    nxt = target + 1
    typer.echo(f"next year's figures for {nxt} (Enter keeps the value shown):")
    report = {s.need.key: s for s in needs.needed(lay, nxt).items}
    for key in roll.ASK:
        st = report.get(key)
        if st is None:
            continue
        shown = "" if st.value is None else str(st.value)
        text = typer.prompt(
            f"  {st.need.label} [{st.need.where}]", default=shown, show_default=True
        ).strip()
        if text and text != shown:
            try:
                typer.echo(f"  saved {key} = {needs.enter(lay, nxt, key, text)}")
            except (KeyError, ValueError) as exc:
                typer.echo(f"  not saved: {exc}")


@app.command()
def enter(
    key: str = typer.Argument(..., help="item name from `planner needed`"),
    value: str = typer.Argument(..., help="the typed answer"),
    year: int = typer.Option(..., help="plan year", min=1990, max=2100),
) -> None:
    """Type one answer the documents did not supply; profile answers go to
    data/profile/assumptions.yaml, year answers to data/manual/<year>.yaml."""
    from planner.ingest.needs import enter as _enter

    try:
        stored = _enter(layout(), year, key, value)
    except (KeyError, ValueError) as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"entered {key} = {stored}")


@app.command()
def dont_have(
    key: str = typer.Argument(..., help="item name from `planner needed`"),
    year: int = typer.Option(..., help="plan year", min=1990, max=2100),
    undo: bool = typer.Option(False, "--undo", help="put it back on the list"),
) -> None:
    """Mark an item as not available; it leaves the Needed list and the plan
    shows it as unavailable instead of guessing. --undo puts it back."""
    from planner.ingest.needs import dont_have as _dont_have
    from planner.ingest.needs import undo_dont_have

    try:
        if undo:
            if not undo_dont_have(layout(), year, key):
                typer.echo(f"{key} was not marked don't have")
                raise typer.Exit(code=2)
            typer.echo(f"{key} is back on the Needed list")
            return
        _dont_have(layout(), year, key)
    except KeyError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"marked {key}: don't have")


@app.command()
def waive(
    year: int = typer.Option(..., help="tax year", min=1990, max=2100),
    form: str = typer.Option(..., help="form, as `planner forms` lists it"),
    issuer: str = typer.Option(..., help="issuer, as `planner forms` lists it"),
    undo: bool = typer.Option(False, "--undo", help="put it back on the list"),
) -> None:
    """Take a form that will not come (the issuer never sends one, or you do
    not have it) off the Needed list. It stays in `planner forms`, marked
    waived. --undo puts it back."""
    from planner.taxprep import expected

    lay = layout()
    if undo:
        if not expected.unwaive(lay, year, form, issuer):
            typer.echo(f"{form} from {issuer} was not waived")
            raise typer.Exit(code=2)
        typer.echo(f"{form} from {issuer} is back on the Needed list")
        return
    try:
        expected.waive(lay, year, form, issuer)
    except ValueError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"waived {form} from {issuer}: it will not come")


@app.command()
def confirm(
    doc: int | None = typer.Option(None, help="document id from the list"),
    accept: bool = typer.Option(
        False, "--accept", help="take the values into the ledger"
    ),
    reject: bool = typer.Option(
        False, "--reject", help="drop them; file goes to UNMATCHED"
    ),
    set_: list[str] = typer.Option(
        [], "--set", help="correct a box before accepting, e.g. --set 1a=1234.56"
    ),
) -> None:
    """Values read by OCR wait here. No options: list them. --doc N --accept
    (with any --set corrections) or --doc N --reject decides one document."""
    from planner.ingest.confirm import accept as _accept
    from planner.ingest.confirm import pending
    from planner.ingest.confirm import reject as _reject
    from planner.ledger import db

    lay = layout()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    if doc is None:
        last = None
        for f in pending(conn):
            if f.document_id != last:
                typer.echo(
                    f"doc {f.document_id}  {f.file_name}  "
                    f"{f.form} {f.tax_year} ({f.issuer})"
                )
                last = f.document_id
            shown = f.text if f.text is not None else f"{f.value:,.2f}"
            typer.echo(f"    page {f.page}  {f.box:10} {f.label[:40]:40} {shown:>14}")
        if last is None:
            typer.echo("nothing awaiting confirm")
        return
    if accept == reject:
        typer.echo("refused: give exactly one of --accept / --reject", err=True)
        raise typer.Exit(code=2)
    try:
        if reject:
            where = _reject(lay, conn, doc)
            typer.echo(f"rejected doc {doc}; file at {where}")
            return
        edits: dict[str, float | str] = {}
        for item in set_:
            box, _, raw = item.partition("=")
            edits[box.strip()] = raw.strip()
        facts = _accept(conn, doc, edits)
    except (KeyError, ValueError) as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"accepted doc {doc}: {len(facts)} facts")


@app.command()
def account(
    number: str | None = typer.Argument(
        None, help="account number as the export spells it"
    ),
    type: str | None = typer.Option(
        None, help="taxable, trad_ira, inherited_ira, roth, hsa or cash"
    ),
    name: str | None = typer.Option(None, help="a label for the status page"),
    date_of_death: str | None = typer.Option(None, help="inherited IRA: YYYY-MM-DD"),
    annual_rmd: bool | None = typer.Option(
        None,
        "--annual-rmd/--no-annual-rmd",
        help="inherited IRA: the owner had begun RMDs, so yearly RMDs apply",
    ),
    balance: str | None = typer.Option(
        None, help="typed balance for an account no export covers"
    ),
) -> None:
    """Describe one account (type, name, date of death, yearly RMDs, typed balance) in
    data/profile/accounts.yaml; with no arguments, list them."""
    from datetime import date

    from planner.ingest.needs import parse_value
    from planner.ledger import portfolio

    lay = layout()
    if number is None:
        for num, entry in sorted(portfolio.load_accounts(lay).items()):
            typer.echo(f"{num:14} {entry.get('type', '?'):14} {entry.get('name', '')}")
        return
    fields: dict[str, object] = {
        "type": type,
        "name": name,
        "date_of_death": date_of_death,
        "annual_rmd": annual_rmd,
    }
    if balance is not None:
        need = portfolio.account_balance_need(number)
        fields["balance"] = parse_value(need, balance)
        fields["balance_date"] = date.today().isoformat()
    try:
        entry = portfolio.save_account(lay, number, **fields)
    except ValueError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"account {number}: " + ", ".join(f"{k}={v}" for k, v in sorted(entry.items()))
    )


@app.command()
def forecast(
    stream: str | None = typer.Argument(
        None,
        help="wages, se_income, interest, tax_exempt_interest, "
        "ordinary_dividends, ira_distributions or social_security",
    ),
    year: int = typer.Option(..., help="plan year", min=1990, max=2100),
    cadence: str | None = typer.Option(
        None, help="weekly, biweekly, semimonthly, monthly, quarterly, annual or once"
    ),
    amount: str | None = typer.Option(None, help="each payment, with --cadence"),
    next: str | None = typer.Option(None, help="next payment date, YYYY-MM-DD"),
    remaining: str | None = typer.Option(None, help="the rest of the year, typed"),
    full_year: str | None = typer.Option(
        None, help="the whole year; replaces the forecast"
    ),
    ytd: str | None = typer.Option(None, help="a pay stub's year-to-date amount"),
    through: str | None = typer.Option(None, help="the date --ytd runs to"),
    low: str | None = typer.Option(None, help="low case (of --remaining or the year)"),
    high: str | None = typer.Option(None, help="high case, with --low"),
    owner: str = typer.Option("self", help="self or spouse (joint return)"),
    clear: bool = typer.Option(False, "--clear", help="drop this stream's forecast"),
) -> None:
    """Forecast one income stream to December 31: a pay schedule, the remaining
    amount or the full year (with a low and high when uncertain); with no
    stream, list every stream's actual so far, through date and full year."""
    from planner.engine.household import MissingInputError
    from planner.ingest.needs import need_for, parse_value
    from planner.plan import forecast as fc
    from planner.plan.inputs import OverrideError, build

    lay = layout()
    if stream is None:
        try:
            inp = build(lay, year)
        except (MissingInputError, OverrideError) as exc:
            typer.echo(f"refused: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(fc.render(inp.forecast, fc.load(lay, year), year), nl=False)
        for note in inp.notes:
            if note.split(" ", 1)[0].rstrip(":") in fc.STREAMS:
                typer.echo(f"note: {note}")
        return
    if clear:
        fc.save(lay, year, stream, None)
        typer.echo(f"{stream}: forecast cleared")
        return
    try:
        need = need_for(stream)

        def money(text: str | None) -> float | None:
            return None if text is None else float(parse_value(need, text))

        typed = fc.Typed(
            owner=owner,
            ytd=money(ytd),
            through=through,
            cadence=cadence,
            amount=money(amount),
            next=next,
            remaining=money(remaining),
            full_year=money(full_year),
            low=money(low),
            high=money(high),
        )
        fc.save(lay, year, stream, typed)
    except (KeyError, ValueError) as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"{stream}: forecast saved (planner forecast --year {year} lists it)")


@app.command()
def convert(
    date_: str = typer.Argument(..., metavar="DATE", help="YYYY-MM-DD"),
    amount: str = typer.Argument(..., help="dollars converted"),
    from_account: str = typer.Option(
        ..., "--from", help="the traditional IRA converted from"
    ),
    taxable: str | None = typer.Option(
        None, help="taxable part, if not the whole amount"
    ),
) -> None:
    """Record a Roth conversion; the planner dates when its principal becomes
    penalty-free. An inherited IRA is refused: it can never be converted."""
    from datetime import date

    from planner.ingest.needs import load_profile, need_for, parse_value
    from planner.ledger import db, portfolio

    lay = layout()
    entry = portfolio.load_accounts(lay).get(from_account, {})
    kind = entry.get("type")
    if kind == "inherited_ira":
        typer.echo(
            f"refused: {from_account} is an inherited IRA; it is never converted",
            err=True,
        )
        raise typer.Exit(code=2)
    if kind != "trad_ira":
        typer.echo(
            f"refused: {from_account} is {kind or 'untyped'}; only a traditional IRA "
            f"converts (planner account {from_account} --type trad_ira)",
            err=True,
        )
        raise typer.Exit(code=2)
    money = need_for("roth_conversion")
    try:
        when = date.fromisoformat(date_)
        cents = db.to_cents(parse_value(money, amount))
        taxable_cents = (
            cents if taxable is None else db.to_cents(parse_value(money, taxable))
        )
    except ValueError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    profile = load_profile(lay)
    free = portfolio.accessible_date(
        when, profile.get("birth_date"), float(profile.get("ira_access_age") or 59.5)
    )
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_conversion(
        conn,
        date=when.isoformat(),
        amount_cents=cents,
        taxable_cents=taxable_cents,
        source_account=from_account,
        accessible_date=free.isoformat(),
    )
    conn.close()
    typer.echo(
        f"recorded conversion {when.isoformat()} {cents / 100:,.2f} from "
        f"{from_account}; penalty-free {free.isoformat()}"
    )


@app.command()
def status(
    year: int = typer.Option(..., help="plan year", min=1990, max=2100),
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; default today"),
) -> None:
    """The portfolio today: every account, total, accessible and locked money,
    the Roth withdrawal order, the all-time peak, YTD income by type and its gap
    to the filed 1099s, unrealized gains and the carryforward."""
    from datetime import date

    from planner.ledger import portfolio

    st = portfolio.status(layout(), year, date.fromisoformat(as_of) if as_of else None)
    typer.echo(f"as of {st.as_of}  (plan year {st.year})")
    for p in st.positions:
        typer.echo(
            f"  {p.account:14} {p.name[:22]:22} {p.type:14} {p.value:>14,.2f}  "
            f"{p.as_of} {p.origin}"
        )
    typer.echo(f"total        {st.total:>14,.2f}")
    typer.echo(
        f"accessible   {st.accessible:>14,.2f}  taxable + cash + Roth basis "
        f"(contributions {st.roth_contributions or 0:,.2f}, seasoned conversions "
        f"{st.seasoned:,.2f})"
    )
    typer.echo(
        f"locked       {st.locked:>14,.2f}  IRAs, HSA, Roth earnings, unseasoned "
        f"conversions {st.unseasoned:,.2f}"
    )
    if st.untyped:
        typer.echo(f"untyped      {st.untyped:>14,.2f}")
    typer.echo(f"peak         {st.peak:>14,.2f}  ({st.peak_date})")
    for number, death, by in st.inherited:
        rmd = portfolio.rmd_text(st, number)
        rmd = f"; {rmd}" if rmd else ""
        typer.echo(
            f"inherited IRA {number}: death {death}; empty it by {by} "
            f"(10-year rule){rmd}"
        )
    for c in st.conversions:
        typer.echo(
            f"conversion   {c.date} {c.amount:>12,.2f} from {c.source_account}; "
            f"penalty-free {c.accessible_date}"
        )
    layers = portfolio.roth_layers(st)
    if layers:
        typer.echo("Roth withdrawal order")
        for lr in layers:
            typer.echo(f"  {lr.amount:>14,.2f}  {lr.label}")
    for g in st.gaps:
        if g.gap:
            typer.echo(
                f"{g.form} {g.reported:>14,.2f} vs YTD {g.box} {g.ytd:,.2f}: "
                f"{g.gap:+,.2f} (the form is the figure filed)"
            )
    if st.ytd_income:
        typer.echo(f"YTD income {st.year}")
        for box, value in st.ytd_income.items():
            typer.echo(f"  {box:26} {value:>14,.2f}")
    short, long = st.unrealized
    typer.echo(
        f"unrealized   {len(st.lots)} lots: short {short:,.2f}, long {long:,.2f}"
    )
    cf = (
        "unknown (planner needed)"
        if st.carryforward is None
        else f"{st.carryforward:,.2f}"
    )
    typer.echo(f"capital loss carryforward: {cf}")
    for note in st.notes:
        typer.echo(f"note: {note}")


def _overrides(
    q4_dividends: float,
    sales_st: float,
    sales_lt: float,
    conversion: float,
    hsa: float | None,
) -> Overrides:
    from planner.plan.inputs import Overrides

    return Overrides(q4_dividends, sales_st, sales_lt, conversion, hsa)


Q4 = typer.Option(0.0, help="Q4 dividend estimate to add ($)")
ST = typer.Option(0.0, help="planned short-term gain to add ($)")
LT = typer.Option(0.0, help="planned long-term gain to add ($)")
CONV = typer.Option(0.0, help="planned Roth conversion to add ($)")
HSA = typer.Option(None, help="planned HSA contribution ($), replaces the ledger's")


@app.command()
def magi(
    year: int = typer.Option(..., help="plan year", min=1990, max=2100),
    q4_dividends: float = Q4,
    sales_st: float = ST,
    sales_lt: float = LT,
    conversion: float = CONV,
    hsa: float | None = HSA,
) -> None:
    """Project the full year from the Needed panel plus overrides: every tax
    figure and the distance to each watched line. Unknown inputs are named,
    never treated as zero."""
    from planner.plan.magi import project

    pj = project(
        layout(), year, _overrides(q4_dividends, sales_st, sales_lt, conversion, hsa)
    )
    res = pj.result
    hh = pj.inputs.household
    typer.echo(
        f"{year} {hh.filing_status} {hh.state} age {hh.age}; "
        f"engine {res.engine_version}"
    )
    for k, v in sorted(pj.inputs.origins.items()):
        if k in pj.inputs.estimates:
            typer.echo(f"  estimate  {k:30} {v}")
    for d in pj.inputs.overrides.describe():
        typer.echo(f"  override  {d}")
    typer.echo(
        f"AGI {res.agi:,.2f}  taxable income {res.taxable_income:,.2f}  "
        f"ACA MAGI {res.aca_magi:,.2f} (add-backs {pj.aca_addbacks:,.2f}; "
        f"{res.aca_fpl_pct:.0f}% FPL)"
    )
    typer.echo(
        f"federal {res.fed_total_tax:,.2f} (SE {res.se_tax:,.2f}, on gains "
        f"{res.ltcg_tax:,.2f})  NC {res.state_tax:,.2f}  ACA credit {res.aca_ptc:,.2f}"
    )
    for ln in pj.lines:
        state = "OVER" if ln.over else "under"
        typer.echo(
            f"  {ln.name:40} {ln.limit:>12,.2f}  {ln.measure} {ln.value:>12,.2f}  "
            f"{state} by {abs(ln.room):,.2f}  ({ln.direction})"
        )
    if pj.inputs.unknown:
        typer.echo(
            "unknown (left out, not zero): "
            + ", ".join(pj.inputs.unknown)
            + f"  (planner needed --year {year})"
        )
    for note in pj.inputs.notes:
        typer.echo(f"note: {note}")


@app.command()
def conversions(
    year: int = typer.Option(..., help="plan year", min=1990, max=2100),
    q4_dividends: float = Q4,
    sales_st: float = ST,
    sales_lt: float = LT,
    hsa: float | None = HSA,
    step: int = typer.Option(500, help="sweep step ($)", min=1),
) -> None:
    """Size this year's Roth conversion: one engine sweep, a candidate per
    watched line, each with its federal and NC tax, ACA credit change, Medicaid
    effect and the cash needed from outside the IRA."""
    from planner.plan.conversion import size

    sz = size(
        layout(), year, _overrides(q4_dividends, sales_st, sales_lt, 0.0, hsa), step
    )
    bal = "unknown" if sz.trad_ira_balance is None else f"{sz.trad_ira_balance:,.2f}"
    typer.echo(
        f"{year}: already converted {sz.already:,.2f}; traditional IRA {bal}; "
        f"cap {sz.cap:,.2f}; margin {sz.margin:,.2f}"
    )
    for c in sz.candidates:
        mark = "*" if sz.recommendation is c else " "
        typer.echo(
            f"{mark} {c.name:15} {c.amount:>12,.2f}  federal +{c.fed_delta:,.2f}  "
            f"NC +{c.state_delta:,.2f}  ACA credit {c.ptc_delta:+,.2f}  "
            f"cash needed {c.cash_needed:,.2f}  taxable income {c.taxable_income:,.2f}"
            f"{'  Medicaid month OVER' if c.medicaid_month_over else ''}"
            f"{'  WARNING: qualified/LTCG into 15%' if c.qualified_spill else ''}"
            f"  ({c.note})"
        )
    rec = sz.recommendation
    typer.echo(
        f"recommendation: {rec.name} {rec.amount:,.2f} ({rec.note})"
        if rec
        else "recommendation: none"
    )
    for note in sz.notes:
        typer.echo(f"note: {note}")


AS_OF = typer.Option(None, help="YYYY-MM-DD; default today")
BALANCE = typer.Option(None, help="investable balance ($); default the ledger's")


@app.command()
def spend(
    year: int = typer.Option(..., help="plan year", min=1990, max=2100),
    as_of: str | None = AS_OF,
    balance: float | None = BALANCE,
    years: int = typer.Option(10, help="rows in the return-band table", min=1, max=60),
) -> None:
    """The spending band: rate x balance clamped to the floor and ceiling, the
    drawdown rule against the inflation-adjusted peak, and the return-band
    table under the floor and planning returns (real dollars)."""
    from datetime import date

    from planner.plan.spending import plan

    sp = plan(
        layout(), year, date.fromisoformat(as_of) if as_of else None, balance, years
    )
    typer.echo(
        f"{year} balance {sp.balance:,.2f} as of {sp.as_of}; peak {sp.peak:,.2f} "
        f"({sp.peak_date}) inflation-adjusted {sp.peak_adjusted:,.2f}"
    )
    typer.echo(
        f"rate {sp.rate:.2%} floor {sp.floor:,.2f} ceiling {sp.ceiling:,.2f} -> "
        f"spending {sp.spending:,.2f}" + ("  DRAWDOWN" if sp.in_drawdown else "")
    )
    typer.echo(
        f"{'year':>6} {'age':>4} {'floor balance':>15} {'spend':>10} "
        f"{'track balance':>15} {'spend':>10}"
    )
    for rw in sp.rows:
        typer.echo(
            f"{rw.year:>6} {rw.age:>4} {rw.balance_floor:>15,.2f} "
            f"{rw.spend_floor:>10,.2f} "
            f"{rw.balance_track:>15,.2f} {rw.spend_track:>10,.2f}"
        )
    for note in sp.notes:
        typer.echo(f"note: {note}")


@app.command()
def glide(
    year: int = typer.Option(..., help="plan year", min=1990, max=2100),
    as_of: str | None = AS_OF,
    balance: float | None = BALANCE,
    cash_in: list[str] = typer.Option(
        [], help="extra cash landing in a month, YYYY-MM:AMOUNT (sale proceeds, a gift)"
    ),
    q4_dividends: float = Q4,
    sales_st: float = ST,
    sales_lt: float = LT,
    conversion: float = CONV,
    hsa: float | None = HSA,
) -> None:
    """The age/year table to 95 under the planning return (the comfort-floor line,
    the same rule at the floor return, beside it), the accessible-bucket
    floor through the IRA access age, three stress rows, and the month-by-month
    cash line for this year and next. Estimated payments come from the
    ``esttax`` installments, and the planned sales (``--sales-st``,
    ``--sales-lt``) and conversion reach the line without ``--cash-in``."""
    from datetime import date

    from planner.plan.glidepath import HORIZON_AGE as HORIZON
    from planner.plan.glidepath import glide as run_glide

    extra: dict[str, float] = {}
    for item in cash_in:
        ym, _, amt = item.partition(":")
        extra[ym] = extra.get(ym, 0.0) + float(amt.replace(",", ""))
    g = run_glide(
        layout(),
        year,
        date.fromisoformat(as_of) if as_of else None,
        balance,
        extra,
        _overrides(q4_dividends, sales_st, sales_lt, conversion, hsa),
    )
    typer.echo(
        f"{year} age {g.age} balance {g.balance:,.2f} ({g.band}); "
        f"accessible {g.accessible:,.2f} "
        f"vs floor through {g.access_age:g} ({g.years_to_access} years) "
        f"{g.floor_needed:,.2f}: "
        + (f"SHORT by {g.floor_shortfall:,.2f}" if g.floor_shortfall else "covered")
    )
    typer.echo(
        f"{'year':>6} {'age':>4} {'real':>15} {'nominal':>15} {'SS':>10} "
        f"{'spend':>10} {'withdraw':>10} {'comfort floor':>15}"
    )
    for rw, fl in zip(g.rows, g.floor_rows, strict=True):
        typer.echo(
            f"{rw.year:>6} {rw.age:>4} {rw.balance_real:>15,.2f} "
            f"{rw.balance_nominal:>15,.2f} "
            f"{rw.ss:>10,.2f} {rw.spend:>10,.2f} {rw.withdrawal:>10,.2f} "
            f"{fl.balance_real:>15,.2f}"
        )
    for s in g.stresses:
        end = f"runs out at {s.runs_out_age}" if s.runs_out_age else "lasts"
        typer.echo(
            f"stress {s.name:22} {end}; at {HORIZON} {s.balance_at_horizon:,.2f}"
        )
    typer.echo(
        f"{'month':>8} {'SE':>10} {'other':>9} {'div':>9} {'in':>9} {'sales':>11} "
        f"{'living':>9} "
        f"{'mortg':>9} {'prem':>8} {'est tax':>9} {'tax due':>10} {'irreg':>9} "
        f"{'net':>10} {'cash':>12}"
    )
    for m in g.months:
        typer.echo(
            f"{m.year}-{m.month:02d}{'*' if m.actual else ' '} {m.se:>10,.2f} "
            f"{m.other_in:>9,.2f} {m.dividends:>9,.2f} {m.cash_in:>9,.2f} "
            f"{m.planned_in:>11,.2f} "
            f"{m.living:>9,.2f} {m.mortgage:>9,.2f} {m.premiums:>8,.2f} "
            f"{m.est_tax:>9,.2f} {m.balance_due:>10,.2f} {m.irregular:>9,.2f} "
            f"{m.net:>10,.2f} {m.cash:>12,.2f}"
        )
    typer.echo("* income columns from ledger rows; others at the run-rate")
    for note in g.notes:
        typer.echo(f"note: {note}")


@app.command()
def washsales(
    year: int | None = typer.Option(
        None, help="loss sales in this year; default all", min=1990, max=2100
    ),
    as_of: str | None = AS_OF,
) -> None:
    """Every loss sale with a buy of the same symbol within 30 days either side,
    across all accounts, and the symbols whose window is still open."""
    from datetime import date

    from planner.ledger import db
    from planner.plan import washsale

    lay = layout()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        flags = washsale.check(conn, year)
        windows = washsale.open_windows(
            conn, date.fromisoformat(as_of) if as_of else date.today()
        )
    finally:
        conn.close()
    for f in flags:
        typer.echo(
            f"WASH {f.sale.symbol:8} sold {f.sale.date} ({f.sale.account}) loss "
            f"{f.sale.loss:,.2f}; bought {f.buy.date} ({f.buy.account}, {f.buy.type}) "
            f"{f.days:+d} days"
        )
    if not flags:
        typer.echo("no wash sales flagged")
    for symbol, until, loss in windows:
        typer.echo(f"open window {symbol}: no buys before {until} (loss {loss:,.2f})")


LOT = typer.Option(None, help="sell this lot first: ACCOUNT:SYMBOL:YYYY-MM-DD (repeat)")


@app.command()
def withdraw(
    year: int = typer.Option(..., help="plan year", min=1990, max=2100),
    target: float | None = typer.Option(
        None, help="cash to hold; default cash_target", min=0
    ),
    budget: float | None = typer.Option(None, help="realized gain allowed ($)", min=0),
    as_of: str | None = AS_OF,
    lot: list[str] | None = LOT,
    q4_dividends: float = Q4,
    sales_st: float = ST,
    sales_lt: float = LT,
    conversion: float = CONV,
    hsa: float | None = HSA,
) -> None:
    """Raise the cash target: cash accounts first, then the taxable lots with
    the least gain per dollar (specific-ID lots first); the MAGI and tax
    effect is priced through the engine."""
    from datetime import date

    from planner.plan.withdraw import pick

    w = pick(
        layout(),
        year,
        target,
        budget,
        date.fromisoformat(as_of) if as_of else None,
        lot or [],
        _overrides(q4_dividends, sales_st, sales_lt, conversion, hsa),
    )
    typer.echo(
        f"{year} as of {w.as_of}: cash {w.cash_now:,.2f} of target {w.target:,.2f}; "
        f"need {w.need:,.2f}"
    )
    for s in w.sales:
        typer.echo(
            f"  sell {s.quantity:>12,.3f} {s.symbol:8} {s.account} acquired "
            f"{s.acquired} ({s.term}) proceeds {s.proceeds:>12,.2f} "
            f"basis {s.basis:>12,.2f} gain {s.gain:>12,.2f}"
        )
    typer.echo(
        f"proceeds {w.proceeds:,.2f}; gain short-term {w.gain_st:,.2f} "
        f"long-term {w.gain_lt:,.2f}; short {w.short:,.2f}"
    )
    typer.echo(
        f"ACA MAGI {w.magi_before:,.2f} -> {w.magi_after:,.2f}; "
        f"federal + NC tax {w.tax_before:,.2f} -> {w.tax_after:,.2f}"
    )
    for note in w.notes:
        typer.echo(f"note: {note}")


@app.command()
def esttax(
    year: int = typer.Option(..., help="plan year", min=1990, max=2100),
    as_of: str | None = AS_OF,
    q4_dividends: float = Q4,
    sales_st: float = ST,
    sales_lt: float = LT,
    conversion: float = CONV,
    hsa: float | None = HSA,
) -> None:
    """The safe harbor and the four installments, federal and NC: what was
    paid (bank rows to the IRS or NCDOR, plus `planner paid`), each due
    date's shortfall, and the next payment."""
    from datetime import date

    from planner.plan.esttax import estimate

    et = estimate(
        layout(),
        year,
        date.fromisoformat(as_of) if as_of else None,
        _overrides(q4_dividends, sales_st, sales_lt, conversion, hsa),
    )
    typer.echo(f"{year} as of {et.as_of}; projected AGI {et.agi:,.2f}")
    for ag in et.agencies:
        prior = f"{ag.prior_tax:,.2f}" if ag.prior_tax is not None else "unknown"
        typer.echo(
            f"{ag.name}: projected tax {ag.current_tax:,.2f}, prior {prior}; "
            f"safe harbor {ag.required:,.2f} ({ag.basis}; withheld {ag.withheld:,.2f})"
        )
        for i in ag.installments:
            state = "short" if i.shortfall else "met"
            typer.echo(
                f"  {i.n} due {i.due}  required {i.required:>10,.2f}  "
                f"paid {i.paid:>10,.2f}  {state} {i.shortfall:,.2f}"
            )
        for p in ag.payments:
            typer.echo(f"  paid {p.date} {p.amount:>10,.2f}  ({p.origin})")
        if ag.next_due:
            typer.echo(f"  next: {ag.next_amount:,.2f} by {ag.next_due}")
        for note in ag.notes:
            typer.echo(f"note: {note}")
    for note in et.notes:
        typer.echo(f"note: {note}")


@app.command()
def paid(
    year: int = typer.Option(
        ..., help="tax year the payment is for", min=1990, max=2100
    ),
    agency: str = typer.Option(..., help="fed or nc"),
    on: str = typer.Option(..., help="payment date YYYY-MM-DD"),
    amount: float = typer.Option(..., help="dollars", min=0),
) -> None:
    """Record an estimated payment the bank export does not show."""
    from planner.plan.esttax import record

    p = record(layout(), year, agency, on, amount)
    typer.echo(
        f"recorded {p.agency} {p.amount:,.2f} on {p.date} (installment {p.installment})"
    )


def _ask_amount(label: str, *, signed: bool = False) -> float | None:
    """One typed planning number, asked until it parses; Enter skips it."""
    import math

    from planner.ingest.needs import MONEY_MAX

    while True:
        text = typer.prompt(label, default="", show_default=False).strip()
        if not text:
            return None
        cleaned = text.replace("$", "").replace(",", "").replace(" ", "")
        neg = cleaned.startswith("(") and cleaned.endswith(")")
        try:
            v = float(cleaned.strip("()"))
        except ValueError:
            v = math.nan
        v = -v if neg else v
        if not math.isfinite(v):
            typer.echo(f"  not a number: {text!r}")
        elif (v < 0 and not signed) or abs(v) > MONEY_MAX:
            lo = "-" if signed else "0"
            typer.echo(f"  between {lo} and {MONEY_MAX:,} dollars, please: {text!r}")
        else:
            return v


@app.command()
def plan(
    year: int = typer.Option(..., help="plan year", min=1990, max=2100),
    as_of: str | None = AS_OF,
    total_income: float | None = typer.Option(
        None,
        help="the year's total income before the planned items ($); the line "
        "--total-income-line names becomes the total less the other income counted",
        min=0,
        max=100_000_000,
    ),
    total_income_line: str = typer.Option(
        "wages",
        help="where --total-income's residual goes: wages, se_income, interest, "
        "non_qualified_dividends or ira_distributions",
    ),
    q4_dividends: float | None = typer.Option(
        None, help="Q4 dividend estimate to add ($)"
    ),
    sales_st: float | None = typer.Option(
        None, help="planned short-term gain to add ($)"
    ),
    sales_lt: float | None = typer.Option(
        None, help="planned long-term gain to add ($)"
    ),
    conversion: float = CONV,
    conversion_target: str | None = typer.Option(
        None,
        help="manual (the --conversion amount; default) or auto (adopt the "
        "recommended conversion for the year)",
    ),
    hsa: float | None = HSA,
    ask: bool | None = typer.Option(
        None,
        "--ask/--no-ask",
        help="prompt for the typed fields not given as options; default when "
        "run from a terminal",
    ),
    write: bool = typer.Option(True, help="also write out/plan-<year>.md"),
) -> None:
    """The year-end plan on one page: the Needed panel, projected MAGI against
    every line, the conversion, the spending band, the glide path, cash to
    raise, estimated tax, wash sales and the deadline calendar. A planner
    still missing an input says so instead of stopping the page. From a
    terminal it first asks for the few typed fields (total income, Q4
    dividends, planned sales, conversion target); Enter skips one."""
    import sys
    from datetime import date

    from planner.plan import year as year_plan
    from planner.plan.inputs import CONVERSION_TARGETS, OverrideError, Overrides

    if conversion_target is not None and conversion_target not in CONVERSION_TARGETS:
        typer.echo(
            "refused: --conversion-target must be manual or auto, "
            f"got {conversion_target!r}",
            err=True,
        )
        raise typer.Exit(code=2)
    if sys.stdin.isatty() if ask is None else ask:
        if total_income is None:
            total_income = _ask_amount(
                "total income for the year, before the items below [Enter skips]"
            )
        if q4_dividends is None:
            q4_dividends = _ask_amount("Q4 dividend estimate [Enter skips]")
        if sales_st is None:
            sales_st = _ask_amount(
                "planned short-term gain from sales [Enter skips]", signed=True
            )
        if sales_lt is None:
            sales_lt = _ask_amount(
                "planned long-term gain from sales [Enter skips]", signed=True
            )
        while conversion_target is None and not conversion:
            text = typer.prompt(
                "conversion target: manual or auto (adopt the recommended conversion)",
                default="manual",
            ).strip()
            if text in CONVERSION_TARGETS:
                conversion_target = text
            else:
                typer.echo(f"  manual or auto, please: {text!r}")
    try:
        yp = year_plan.assemble(
            layout(),
            year,
            date.fromisoformat(as_of) if as_of else None,
            Overrides(
                q4_dividends or 0.0,
                sales_st or 0.0,
                sales_lt or 0.0,
                conversion,
                hsa,
                total_income,
                conversion_target or "manual",
                total_income_line,
            ),
        )
    except OverrideError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(year_plan.render(yp), nl=False)
    if write:
        typer.echo(f"written {year_plan.write(layout(), yp)}")


@app.command()
def dashboard(
    year: int | None = typer.Option(
        None, help="plan year; default this year", min=1990, max=2100
    ),
    as_of: str | None = AS_OF,
) -> None:
    """Write the dashboard as a static page, out/index.html, for printing and
    backup: the Needed panel, every planner, the draft return and the alerts,
    each panel tagged actual, estimate or unavailable."""
    from datetime import date

    from planner.dashboard import page, render
    from planner.engine import limits
    from planner.plan import rollover

    lay = layout()
    lay.ensure()
    today = date.fromisoformat(as_of) if as_of else date.today()
    active = year or rollover.active_year(lay, today)
    typer.echo(f"limits: {limits.summary(limits.refresh(lay, active))}")
    pg = page.gather(lay, active, today)
    typer.echo(f"written {render.write_static(lay, pg)}")
    typer.echo(
        f"{pg.needed_count} needed, {len(pg.alerts)} alert(s)"
        if pg.needed_count or pg.alerts
        else "nothing needed, no alerts"
    )


TASK_NAME = "Year-End Planner"


def on_windows() -> bool:
    return sys.platform == "win32"


def schtasks_argv(root: Path, remove: bool = False) -> list[str]:
    """Task Scheduler's command for the monthly quiet run (the 1st, 09:00), as
    an argument list; the launcher path is quoted inside /TR for spaces."""
    exe = shutil.which("schtasks") or "schtasks"
    if remove:
        return [exe, "/Delete", "/TN", TASK_NAME, "/F"]
    launcher = root / "planner.cmd"
    return [
        exe,
        "/Create",
        "/SC",
        "MONTHLY",
        "/D",
        "1",
        "/ST",
        "09:00",
        "/TN",
        TASK_NAME,
        "/TR",
        f'"{launcher}" run --quiet',
        "/F",
    ]


@app.command()
def schedule(
    monthly: bool = typer.Option(
        False, "--monthly", help="run `planner.cmd run --quiet` on the 1st at 09:00"
    ),
    remove: bool = typer.Option(False, "--remove", help="delete the scheduled run"),
) -> None:
    """Register a monthly quiet run with Windows Task Scheduler (the 1st of each
    month, 09:00, as you); --remove deletes it."""
    if monthly == remove:
        typer.echo("schedule needs --monthly or --remove", err=True)
        raise typer.Exit(code=2)
    argv = schtasks_argv(layout().root, remove=remove)
    if not on_windows():
        typer.echo("Task Scheduler is Windows only; the command would be:", err=True)
        typer.echo(subprocess.list2cmdline(argv))
        raise typer.Exit(code=2)
    done = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    if done.returncode != 0:
        typer.echo(
            f"schtasks refused: {(done.stderr or done.stdout).strip()}", err=True
        )
        raise typer.Exit(code=1)
    if remove:
        typer.echo(f"removed the scheduled task {TASK_NAME!r}")
    else:
        typer.echo(
            f"scheduled {TASK_NAME!r}: planner.cmd run --quiet, monthly on the "
            "1st at 09:00"
        )


@app.command()
def run(
    year: int | None = typer.Option(
        None, help="plan year; default this year", min=1990, max=2100
    ),
    as_of: str | None = AS_OF,
    quiet: bool = typer.Option(
        False, "--quiet", help="no browser, no server: refresh out/index.html"
    ),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="open the page"),
    port: int = typer.Option(
        0, help="local port; 0 lets the system pick", min=0, max=65535
    ),
    update_check: bool = typer.Option(
        True, "--update-check/--no-update-check", help="look for a newer release"
    ),
) -> None:
    """The one command: read the inbox, run every planner and the draft return,
    write out/index.html, then serve the page on this computer and open it.
    Drop files on the page, answer the Needed panel, confirm OCR values and
    build the tax package there. --quiet (Task Scheduler) stops after the
    static page."""
    from datetime import date

    from planner import backup as backup_
    from planner.dashboard import page, render, serve
    from planner.engine import feed, limits, verify
    from planner.engine import update as upd
    from planner.ingest import ingest as _ingest
    from planner.plan import rollover
    from planner.taxprep import close as close_

    lay = layout()
    lay.ensure()
    if upd.staged(lay.root):
        if upd.launcher_swaps(lay.root):
            typer.echo("a staged update is waiting; swapping it in first")
            upd.write_swap(lay.root, "in", rerun=True)
            raise typer.Exit(code=upd.LAUNCHER_SWAP)
        typer.echo(f"{upd.apply_staged(lay.root)}; restart to use it")
    # the first run records the pinned engine's output as the baseline every
    # later engine is held to; a new engine version is recorded the same way
    try:
        baseline_note = verify.ensure_baseline(lay.root)
    except ValueError as exc:
        baseline_note = f"engine baseline: {exc}"
    if baseline_note:
        typer.echo(baseline_note)
    # the update check comes first (at most weekly, so rarely a wait); its
    # outcome is on disk before the page is built, so the header shows it
    update_line = feed.check(lay) if update_check else ""
    if update_line:
        typer.echo(update_line)
    rep = _ingest(lay)
    typer.echo(
        f"inbox: {len(rep.imported)} imported, {len(rep.pending)} awaiting "
        f"confirm, {len(rep.duplicates)} duplicate, {len(rep.unmatched)} not read"
    )
    today = date.fromisoformat(as_of) if as_of else None
    for closing in close_.on_drop(lay, today or date.today()):
        typer.echo(close_.render(closing), nl=False)
    ro = rollover.refresh(lay, today)
    if ro is not None and ro.new:
        typer.echo(rollover.render(ro), nl=False)
    active = year or rollover.active_year(lay, today or date.today())
    typer.echo(f"limits: {limits.summary(limits.refresh(lay, active))}")
    pg = page.gather(lay, active, today)
    typer.echo(f"written {render.write_static(lay, pg)}")
    typer.echo(f"{pg.needed_count} needed, {len(pg.alerts)} alert(s)")
    if backup_.settle(lay):
        typer.echo("restore kept: data-previous/ removed")
    if quiet:
        return
    app_ = serve.App(lay, active, today)
    notes = [rollover.render(ro).splitlines()[0]] if ro is not None and ro.new else []
    app_.message = "\n".join([*notes, *([update_line] if update_line else [])])
    srv = serve.server(app_, port)
    address = serve.url(app_, srv)
    typer.echo(f"serving {address}  (Ctrl+C to stop)")
    if open_browser:
        import webbrowser

        webbrowser.open(address)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        srv.server_close()


def _lever_amounts(pairs: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for pair in pairs:
        key, sep, amount = pair.partition("=")
        if not sep:
            raise typer.BadParameter(f"--set {pair}: use lever=amount")
        out[key.strip()] = float(amount.replace(",", ""))
    return out


@app.command()
def levers(
    year: int = typer.Option(..., help="plan year", min=1990, max=2100),
    as_of: str | None = AS_OF,
    q4_dividends: float = Q4,
    sales_st: float = ST,
    sales_lt: float = LT,
    hsa: float | None = HSA,
) -> None:
    """Every move left this year that changes the tax bill or the ACA credit,
    sized from the ledger, priced through the engine and ranked: moves that
    get you under a line, and moves that use the room below the next one."""
    from datetime import date

    from planner.plan import levers as lv

    m = lv.menu(
        layout(),
        year,
        date.fromisoformat(as_of) if as_of else None,
        _overrides(q4_dividends, sales_st, sales_lt, 0.0, hsa),
    )
    typer.echo(lv.render_menu(m), nl=False)


@app.command()
def whatif(
    year: int = typer.Option(..., help="plan year", min=1990, max=2100),
    apply: str = typer.Option(..., help="lever keys, comma-separated"),
    set_: list[str] = typer.Option(  # noqa: B008
        [], "--set", help="lever=amount to size a lever yourself (repeatable)"
    ),
    as_of: str | None = AS_OF,
    q4_dividends: float = Q4,
    sales_st: float = ST,
    sales_lt: float = LT,
    hsa: float | None = HSA,
) -> None:
    """Recompute the full year with the chosen levers and show it before and
    after, side by side."""
    from datetime import date

    from planner.plan import levers as lv

    try:
        w = lv.whatif(
            layout(),
            year,
            [k.strip() for k in apply.split(",") if k.strip()],
            _lever_amounts(set_),
            date.fromisoformat(as_of) if as_of else None,
            _overrides(q4_dividends, sales_st, sales_lt, 0.0, hsa),
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(lv.render_whatif(w), nl=False)


@app.command()
def thresholds(
    year: int = typer.Option(..., help="tax year", min=1990, max=2100),
) -> None:
    """The sourced limits in config/thresholds.yaml for a year, checked against
    the engine's own parameters. A mismatch means one side is stale: the config
    row names its source; the engine updates with policyengine-us."""
    from planner.config import load_thresholds
    from planner.engine.tax import drift, engine_version

    rows = load_thresholds(layout().config / "thresholds.yaml").get(year)
    if rows is None:
        typer.echo(f"no {year} rows in config/thresholds.yaml", err=True)
        raise typer.Exit(1)
    engine = {name: e for name, _, e in drift(year, rows)}
    stale = 0
    for name, row in rows.items():
        mark = ""
        if name in engine:
            same = float(row["value"]) == engine[name]
            stale += not same
            mark = "  engine agrees" if same else f"  ENGINE HAS {engine[name]:,.0f}"
        typer.echo(f"{name:28} {row['value']!s:>12}  {row['source']}{mark}")
    if stale:
        typer.echo(
            f"{stale} row(s) differ from policyengine-us {engine_version()}: the "
            "engine prices tax with its own value until it is updated"
        )


def main(argv: list[str] | None = None) -> int:
    """Entry point. No arguments (a double-clicked planner.cmd) is planner run."""
    args = sys.argv[1:] if argv is None else argv
    app(args=args or ["run"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
