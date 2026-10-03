"""``planner <command>``. Every page action gets a CLI twin here."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from planner import __version__
from planner.paths import CloudSyncedPathError, layout

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the planner version."""
    typer.echo(f"planner {__version__}")


@app.command()
def paths() -> None:
    """Show where this planner keeps its folders, creating data/ and out/."""
    lay = layout()
    try:
        lay.ensure()
    except CloudSyncedPathError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"root   {lay.root}")
    typer.echo(f"data   {lay.data}")
    typer.echo(f"out    {lay.out}")
    typer.echo(f"config {lay.config}")


@app.command()
def selfcheck() -> None:
    """Run one real federal calculation through the tax engine and print it."""
    from planner.engine.selfcheck import run

    result = run()
    typer.echo(
        f"policyengine-us {result.engine_version}: single, NC, "
        f"{result.year} wages ${result.wages:,} -> federal income tax "
        f"${result.income_tax:,.2f}"
    )
    if not result.income_tax > 0:
        typer.echo("selfcheck failed: tax is not positive", err=True)
        raise typer.Exit(code=1)


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
    year: int = typer.Option(2026, help="tax year"),
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
    lo: int = typer.Option(0),
    hi: int = typer.Option(100_000),
    step: int = typer.Option(5_000),
    year: int = typer.Option(2026),
) -> None:
    """Sweep one input across a range in a single engine run; one JSON row per step."""
    from planner.engine.household import load_household
    from planner.engine.tax import compute_sweep

    rows = compute_sweep(year, load_household(household), variable, lo, hi, step)
    typer.echo(json.dumps(rows, indent=2))


@app.command()
def verify(
    filed_return: Path = typer.Argument(..., help="data/private/returns/<year>.yaml"),
    tolerance: float = typer.Option(1.0, help="dollars of allowed difference per line"),
) -> None:
    """Recompute a filed year from its inputs; compare each line to what was filed."""
    from planner.engine.verify import verify_return

    report = verify_return(filed_return, tolerance)
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
    if not report.passed:
        raise typer.Exit(code=1)


@app.command()
def update(
    release_zip: Path | None = typer.Argument(
        None, help="release zip from GitHub Releases"
    ),
    sha256: str | None = typer.Option(None, help="sha256 published beside the zip"),
    rollback: bool = typer.Option(False, "--rollback", help="restore python-previous/"),
) -> None:
    """Swap in a newer release after its own selfcheck passes; --rollback undoes it."""
    from planner.engine import update as upd

    root = layout().root
    try:
        if rollback:
            typer.echo(f"rolled back to {upd.rollback(root)}")
            return
        if release_zip is None or sha256 is None:
            typer.echo(
                "update needs a release zip and --sha256 (or --rollback)", err=True
            )
            raise typer.Exit(code=2)
        exe = "python.exe" if sys.platform == "win32" else "python"
        result = upd.apply(release_zip, sha256, root, python_exe=exe)
    except upd.UpdateError as exc:
        typer.echo(f"update refused: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"updated to {result.version}; candidate selfcheck: {result.selfcheck}")


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
    for year, n in sorted(report.derived.items()):
        typer.echo(f"derived   {year}: {n} YTD facts from rows")
    typer.echo(
        f"batch {report.batch}: {len(report.imported)} imported, "
        f"{len(report.pending)} pending, {len(report.duplicates)} duplicate, "
        f"{len(report.unmatched)} unmatched"
    )


@app.command()
def derive(
    year: int = typer.Option(..., help="tax year to recompute from the ledger rows"),
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
    year: int | None = typer.Option(None, help="tax year"),
    form: str | None = typer.Option(None, help="form, e.g. 1099-DIV"),
) -> None:
    """List the accepted facts in the ledger, each with its source file and page."""
    from planner.ledger import db

    conn = db.connect(layout().data / "ledger" / "planner.db")
    rows = db.facts_for(conn, year, form)
    for r in rows:
        typer.echo(
            f"{r.tax_year} {r.form:9} {r.issuer[:24]:24} box {r.box:12} "
            f"{r.value:>14,.2f}  {r.file_name} p{r.page}"
        )
    typer.echo(f"{len(rows)} facts")


@app.command()
def rows(
    year: int | None = typer.Option(None, help="tax year"),
    source: str | None = typer.Option(None, help="e.g. vanguard_transactions, bank"),
    kind: str | None = typer.Option(
        None, help="holding, lot, transaction, realized, income or bank"
    ),
) -> None:
    """List imported CSV rows (holdings, lots, transactions, income, bank lines)."""
    from planner.ledger import db

    conn = db.connect(layout().data / "ledger" / "planner.db")
    out = db.rows_for(conn, year, source, kind)
    for r in out:
        amount = f"{r.amount_cents / 100:>14,.2f}" if r.amount_cents is not None else ""
        typer.echo(
            f"{r.date or '':10} {r.source:22} {r.type[:14]:14} {r.symbol[:8]:8} "
            f"{amount:>14}  {r.file_name}:{r.line}"
        )
    typer.echo(f"{len(out)} rows")


@app.command()
def needed(
    year: int = typer.Option(..., help="plan year"),
    all: bool = typer.Option(False, "--all", help="also list what is already covered"),
) -> None:
    """What the plan still lacks for a year: each missing item, why, and the
    document that supplies it. Empty when the intake loop is done."""
    from planner.ingest.needs import needed as _needed

    rep = _needed(layout(), year)
    for st in rep.items:
        if st.state == "missing":
            typer.echo(f"needed    {st.need.key:28} {st.need.label}")
            typer.echo(f"          why: {st.need.why}")
            typer.echo(f"          from: {st.need.source}")
            typer.echo(
                f"          type: planner enter --year {year} {st.need.key} <value>"
            )
        elif st.state == "estimate":
            typer.echo(f"estimate  {st.need.key:28} {st.value:,.2f}  ({st.origin})")
        elif all:
            tag = "dont-have" if st.state == "dont_have" else "actual   "
            val = "" if st.value is None else f" {st.value}"
            typer.echo(f"{tag} {st.need.key:28}{val}  ({st.origin})")
    n = len(rep.by_state("missing"))
    typer.echo("nothing needed" if n == 0 else f"{n} needed")


@app.command()
def enter(
    key: str = typer.Argument(..., help="item name from `planner needed`"),
    value: str = typer.Argument(..., help="the typed answer"),
    year: int = typer.Option(..., help="plan year"),
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
    year: int = typer.Option(..., help="plan year"),
) -> None:
    """Mark an item as not available; it leaves the Needed list and the plan
    shows it as unavailable instead of guessing."""
    from planner.ingest.needs import dont_have as _dont_have

    try:
        _dont_have(layout(), year, key)
    except KeyError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"marked {key}: don't have")


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
            typer.echo(
                f"    page {f.page}  {f.box:10} {f.label[:40]:40} {f.value:>14,.2f}"
            )
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
        edits: dict[str, float] = {}
        for item in set_:
            box, _, raw = item.partition("=")
            edits[box.strip()] = float(raw.replace(",", "").replace("$", ""))
        facts = _accept(conn, doc, edits)
    except (KeyError, ValueError) as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"accepted doc {doc}: {len(facts)} facts")


def main() -> int:
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
