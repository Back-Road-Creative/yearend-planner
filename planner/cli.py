"""``planner <command>``. Every page action gets a CLI twin here."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from planner import __version__
from planner.paths import CloudSyncedPathError, layout

if TYPE_CHECKING:
    from planner.plan.inputs import Overrides

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
    balance: str | None = typer.Option(
        None, help="typed balance for an account no export covers"
    ),
) -> None:
    """Describe one account (type, name, date of death, typed balance) in
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
    year: int = typer.Option(..., help="plan year"),
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; default today"),
) -> None:
    """The portfolio today: every account, total, accessible and locked money,
    the all-time peak, YTD income by type, unrealized gains and the carryforward."""
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
        typer.echo(
            f"inherited IRA {number}: death {death}; empty it by {by} (10-year rule)"
        )
    for c in st.conversions:
        typer.echo(
            f"conversion   {c.date} {c.amount:>12,.2f} from {c.source_account}; "
            f"penalty-free {c.accessible_date}"
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
    year: int = typer.Option(..., help="plan year"),
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
    year: int = typer.Option(..., help="plan year"),
    q4_dividends: float = Q4,
    sales_st: float = ST,
    sales_lt: float = LT,
    hsa: float | None = HSA,
    step: int = typer.Option(500, help="sweep step ($)"),
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
    year: int = typer.Option(..., help="plan year"),
    as_of: str | None = AS_OF,
    balance: float | None = BALANCE,
    years: int = typer.Option(10, help="rows in the return-band table"),
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
    year: int = typer.Option(..., help="plan year"),
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
    """The age/year table to 95 under the planning return, the accessible-bucket
    floor through the IRA access age, three stress rows, and the month-by-month
    cash line for this year and next."""
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
        f"{year} age {g.age} balance {g.balance:,.2f}; accessible {g.accessible:,.2f} "
        f"vs floor through {g.access_age:g} ({g.years_to_access} years) "
        f"{g.floor_needed:,.2f}: "
        + (f"SHORT by {g.floor_shortfall:,.2f}" if g.floor_shortfall else "covered")
    )
    typer.echo(
        f"{'year':>6} {'age':>4} {'real':>15} {'nominal':>15} {'SS':>10} "
        f"{'spend':>10} {'withdraw':>10}"
    )
    for rw in g.rows:
        typer.echo(
            f"{rw.year:>6} {rw.age:>4} {rw.balance_real:>15,.2f} "
            f"{rw.balance_nominal:>15,.2f} "
            f"{rw.ss:>10,.2f} {rw.spend:>10,.2f} {rw.withdrawal:>10,.2f}"
        )
    for s in g.stresses:
        end = f"runs out at {s.runs_out_age}" if s.runs_out_age else "lasts"
        typer.echo(
            f"stress {s.name:22} {end}; at {HORIZON} {s.balance_at_horizon:,.2f}"
        )
    typer.echo(
        f"{'month':>8} {'SE':>10} {'div':>9} {'in':>9} {'living':>9} {'mortg':>9} "
        f"{'prem':>8} {'est tax':>9} {'irreg':>9} {'net':>10} {'cash':>12}"
    )
    for m in g.months:
        typer.echo(
            f"{m.year}-{m.month:02d}{'*' if m.actual else ' '} {m.se:>10,.2f} "
            f"{m.dividends:>9,.2f} {m.cash_in:>9,.2f} {m.living:>9,.2f} "
            f"{m.mortgage:>9,.2f} {m.premiums:>8,.2f} {m.est_tax:>9,.2f} "
            f"{m.irregular:>9,.2f} {m.net:>10,.2f} {m.cash:>12,.2f}"
        )
    typer.echo("* income columns from ledger rows; others at the run-rate")
    for note in g.notes:
        typer.echo(f"note: {note}")


@app.command()
def washsales(
    year: int | None = typer.Option(None, help="loss sales in this year; default all"),
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
    year: int = typer.Option(..., help="plan year"),
    target: float | None = typer.Option(None, help="cash to hold; default cash_target"),
    budget: float | None = typer.Option(None, help="realized gain allowed ($)"),
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
    year: int = typer.Option(..., help="plan year"),
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
    year: int = typer.Option(..., help="tax year the payment is for"),
    agency: str = typer.Option(..., help="fed or nc"),
    on: str = typer.Option(..., help="payment date YYYY-MM-DD"),
    amount: float = typer.Option(..., help="dollars"),
) -> None:
    """Record an estimated payment the bank export does not show."""
    from planner.plan.esttax import record

    p = record(layout(), year, agency, on, amount)
    typer.echo(
        f"recorded {p.agency} {p.amount:,.2f} on {p.date} (installment {p.installment})"
    )


def main() -> int:
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
