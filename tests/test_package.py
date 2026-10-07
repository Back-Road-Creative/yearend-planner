"""Phase 4l: ``planner taxpack`` writes out/tax-<year>/ from synthetic lots, a
typed carryover, a Roth conversion, an estimated payment and the archived
original, and every file agrees with the command that already prints it.
Unit 6a: the cover sheet (scope, readiness, documents, estimates, forms not
handled, questions for the preparer, versions)."""

from __future__ import annotations

import csv
import re
import zipfile
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner import NOTICE, __version__
from planner.cli import app
from planner.dashboard import page as dash
from planner.engine.household import MissingInputError
from planner.ingest import ingest
from planner.ingest.needs import enter
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.plan import esttax
from planner.taxprep import draft, expected, package
from tests.test_capgains import IRA, LOTS, TAXABLE
from tests.test_csv import drop

runner = CliRunner()


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    portfolio.save_account(lay, TAXABLE, type="taxable")
    portfolio.save_account(lay, IRA, type="trad_ira")
    drop(lay, "lots.csv", LOTS)
    ingest(lay)
    for key, text in (
        ("birth_date", "1971-06-15"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("wages", "60,000"),
        ("lt_loss_carryover", "10,000"),
    ):
        enter(lay, 2025, key, text)
    esttax.record(lay, 2025, "fed", "2025-09-15", 400.0)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_conversion(
        conn,
        date="2025-11-03",
        amount_cents=1_000_000,
        taxable_cents=900_000,
        source_account=IRA,
        accessible_date="2030-01-01",
    )
    conn.close()
    return lay


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_taxpack_writes_every_file(lay: Layout) -> None:
    pack = package.build(lay, 2025)
    folder = lay.out / "tax-2025"
    assert pack.folder == folder
    # Schedule B is written only when required; this household's is not
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        set(package.FILES) - {"schedule-b.csv"}
    )
    d = draft.build(lay, 2025)
    assert (folder / "draft.txt").read_text(encoding="utf-8") == draft.render(d)
    page = (folder / "draft.html").read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>") and "NC Form D-400" in page
    assert "<script" not in page
    # the limits notice heads the draft, its printable page and the pack summary
    assert NOTICE in draft.render(d) and NOTICE in page
    assert package.render(pack).splitlines()[1] == f"  {NOTICE}"

    lots = _rows(folder / "form-8949.csv")
    assert len(lots) == 3  # the IRA's own sale is not reported
    assert {r["box"] for r in lots} == {"A", "D"}
    vtsax = next(r for r in lots if "VTSAX" in r["(a) description"])
    assert (vtsax["(d) proceeds"], vtsax["(e) cost basis"]) == ("12000.00", "8000.00")
    assert vtsax["(h) gain or loss"] == "4000.00"

    carry = _rows(folder / "carryforward.csv")
    assert carry and all(r["source"].startswith("Capital Loss") for r in carry)
    assert sum(float(r["amount"]) for r in carry) == pytest.approx(
        sum(ln.value for ln in d.lines if ln.form == "Carryover")
    )
    roth = [r for r in _rows(folder / "basis.csv") if r["kind"] == "Roth conversion"]
    assert roth == [
        {
            "kind": "Roth conversion",
            "account": IRA,
            "item": "conversion",
            "date": "2025-11-03",
            "quantity": "",
            "basis": "1000.00",
            "value": "10000.00",
            "note": "taxable 9000.00; penalty-free from 2030-01-01",
        }
    ]
    pays = _rows(folder / "estimated-payments.csv")
    assert pays == [
        {
            "agency": "fed",
            "date": "2025-09-15",
            "installment": "3",
            "amount": "400.00",
            "origin": "typed",
        }
    ]
    assert "Schedule C for 2025" in (folder / "schedule-c.txt").read_text("utf-8")
    assert _rows(folder / "forms.csv")[0].keys() >= {"form", "source files"}
    with zipfile.ZipFile(folder / "originals.zip") as zf:
        assert "lots.csv" in zf.namelist()


def test_taxpack_forms_csv_tags_a_waived_late_form_waived(lay: Layout) -> None:
    as_of = date(2026, 12, 1)  # every form for 2025 is past its due date
    inv = expected.inventory(lay, 2025, as_of)
    assert inv.late, "the fixture should leave at least one form late"
    target = inv.late[0]
    pack = package.build(lay, 2025, as_of)
    rows = {(r["form"], r["issuer"]): r for r in _rows(pack.folder / "forms.csv")}
    assert rows[(target.form, target.issuer)]["state"] == "LATE"
    count = len(inv.outstanding)
    assert f"{count} expected form(s) still to come" in " ".join(pack.notes)

    expected.waive(lay, 2025, target.form, target.issuer, as_of)
    pack = package.build(lay, 2025, as_of)
    rows = {(r["form"], r["issuer"]): r for r in _rows(pack.folder / "forms.csv")}
    assert rows[(target.form, target.issuer)]["state"] == "waived"
    assert not any(
        r["state"] == "LATE"
        and (r["form"], r["issuer"]) == (target.form, target.issuer)
        for r in rows.values()
    )
    assert f"{count - 1} expected form(s) still to come" in " ".join(pack.notes)


def test_taxpack_rerun_replaces_and_cli_lists(lay: Layout) -> None:
    package.build(lay, 2025)
    esttax.record(lay, 2025, "nc", "2025-12-15", 150.0)
    res = runner.invoke(app, ["taxpack", "--year", "2025"])
    assert res.exit_code == 0, res.output
    assert "draft.html" in res.output and "originals.zip" in res.output
    pays = _rows(lay.out / "tax-2025" / "estimated-payments.csv")
    assert [p["agency"] for p in pays] == ["fed", "nc"]


def test_taxpack_notes_a_joint_return_it_does_not_model(lay: Layout) -> None:
    enter(lay, 2025, "filing_status", "married_joint")
    pack = package.build(lay, 2025)
    (gap,) = [n for n in pack.notes if n.startswith("Not handled:")]
    assert "spouse" in gap and f"note: {gap}" in package.render(pack)


def test_taxpack_blocked_writes_nothing(planner_home: Path) -> None:
    lay = Layout(planner_home)
    lay.ensure()
    with pytest.raises(MissingInputError):
        package.build(lay, 2025)
    assert not (lay.out / "tax-2025").exists()
    res = runner.invoke(app, ["taxpack", "--year", "2025"])
    assert res.exit_code == 2 and "blocked" in res.output


def test_html_escapes_sources() -> None:
    d = draft.Draft(year=2025, engine_version="x")
    d.lines.append(draft.Line("1040", "1z", "Wages", 1.0, "<b>Acme & Co</b>"))
    d.notes.append("note <i>")
    page = package.html_page(d)
    assert "&lt;b&gt;Acme &amp; Co&lt;/b&gt;" in page and "note &lt;i&gt;" in page


def test_taxpack_writes_schedule_b_only_when_required(lay: Layout) -> None:
    pack = package.build(lay, 2025)
    assert d_has_no_sched_b(pack)
    assert any("Schedule B is not required" in n for n in pack.notes), pack.notes
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-int",
        file_name="synthetic-int.pdf",
        kind="pdf",
        pages=1,
        batch="b9",
        facts=[
            db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "1", "x", 2500.0, 1)
        ],
    )
    conn.close()
    pack = package.build(lay, 2025)
    rows = _rows(pack.folder / "schedule-b.csv")
    assert ("I", "1", "Example Bank (synthetic)", "2500.00") in [
        (r["part"], r["line"], r["payer"], r["amount"]) for r in rows
    ]
    assert "schedule-b.csv" in pack.written
    assert "Schedule B" in (pack.folder / "draft.html").read_text(encoding="utf-8")


def d_has_no_sched_b(pack: package.Pack) -> bool:
    return (
        not (pack.folder / "schedule-b.csv").exists()
        and "schedule-b.csv" not in pack.written
    )


COVER = (
    "Scope",
    "Readiness",
    "Documents",
    "Estimates",
    "Not handled",
    "Questions for the preparer",
    "Versions",
)


def _section(cover: str, name: str) -> list[str]:
    lines = cover.splitlines()
    start = lines.index(name) + 1
    end = next((i for i in range(start, len(lines)) if lines[i] in COVER), len(lines))
    return [ln.strip() for ln in lines[start:end] if ln.strip()]


def test_cover_sheet_heads_the_pack_with_the_dashboards_readiness(
    lay: Layout,
) -> None:
    as_of = date(2026, 3, 1)
    pack = package.build(lay, 2025, as_of)
    assert pack.written[0] == "cover.txt"
    cover = (pack.folder / "cover.txt").read_text(encoding="utf-8")
    lines = cover.splitlines()
    assert lines[:2] == ["Tax pack cover sheet for 2025", NOTICE]
    assert [ln for ln in lines if ln in COVER] == list(COVER)
    # the same three-way answer the dashboard gives, every reason listed
    answer = dash.gather(lay, 2025, as_of).readiness[2]
    ready = _section(cover, "Readiness")
    assert ready[0] == f"{answer.question}: {'yes' if answer.ready else 'no'}"
    assert ready[1:] == [f"- {b}" for b in answer.blockers]
    assert package.render(pack).splitlines()[2] == f"  {ready[0]}"
    d = draft.build(lay, 2025)
    versions = _section(cover, "Versions")
    assert versions[:3] == [
        f"planner {__version__}",
        f"tax engine policyengine-us {d.engine_version}",
        "built 2026-03-01",
    ]
    assert "not closed: no filed return recorded for 2025" in versions
    scope = _section(cover, "Scope")
    assert scope[0].startswith("drafted here: Form 1040")
    assert "NC Form D-400" in scope[0]
    assert any("not an import file for tax software" in ln for ln in scope)
    assert _section(cover, "Not handled") == ["none"]
    questions = _section(cover, "Questions for the preparer")
    asks = [n for n in d.notes if n.startswith("CHECK")]
    asks += [
        f"{k}: not known; left out of the draft, never counted as 0" for k in d.unknown
    ]
    assert questions == ([f"- {c}" for c in asks] or ["none"])


def test_cover_sheet_lists_waived_and_outstanding_forms_and_gaps(
    lay: Layout,
) -> None:
    as_of = date(2026, 12, 1)
    inv = expected.inventory(lay, 2025, as_of)
    target = inv.late[0]
    expected.waive(lay, 2025, target.form, target.issuer, as_of)
    enter(lay, 2025, "filing_status", "married_joint")
    pack = package.build(lay, 2025, as_of)
    cover = (pack.folder / "cover.txt").read_text(encoding="utf-8")
    docs = _section(cover, "Documents")
    assert f"- waived: {target.form} from {target.issuer}" in docs
    left = expected.inventory(lay, 2025, as_of).outstanding
    for e in left:
        assert f"- still to come: {e.form} from {e.issuer} (due {e.due})" in docs
    gaps = _section(cover, "Not handled")
    assert any("spouse" in g for g in gaps)
    assert _section(cover, "Readiness")[0] == "Ready for a preparer: no"


def test_no_claim_of_an_import_into_preparer_software() -> None:
    """No preparer-software import claim until it is checked in that software
    (master plan stage 6): the docs and the code never promise one."""
    brands = r"TurboTax|H&R Block|TaxAct|Drake|Lacerte|ProSeries|UltraTax|TaxSlayer"
    claim = re.compile(rf"import\w*\b[^.\n]{{0,40}}\b(?:{brands})", re.I)
    root = Path(__file__).resolve().parents[1]
    files = [root / "README.md", root / "GUIDE.md", *root.glob("planner/**/*.py")]
    hits = [
        f"{p.name}: {m.group(0)}"
        for p in files
        for m in claim.finditer(p.read_text("utf-8"))
    ]
    assert not hits


def test_four_snapshots_kept_apart_and_compared(lay: Layout) -> None:
    """Unit 6b: forecast while the year is open, provisional with forms to come,
    reconciled once each is in or waived, filed once closed; none replaces
    another and a repeat with the same figures adds nothing."""
    from planner.taxprep import close, snapshots
    from tests.test_close import _file, _filed

    got = snapshots.take(lay, 2025, date(2025, 7, 1))
    assert got.new and got.snapshot and got.snapshot.kind == snapshots.FORECAST
    assert set(got.snapshot.figures) == set(snapshots.FIGURES)
    assert not snapshots.take(lay, 2025, date(2025, 8, 1)).new

    as_of = date(2026, 3, 1)
    inv = expected.inventory(lay, 2025, as_of)
    assert inv.outstanding, "the fixture should leave a form to come"
    assert snapshots.kind_now(lay, 2025, as_of) == snapshots.PROVISIONAL
    prov = snapshots.take(lay, 2025, as_of).snapshot
    assert prov and prov.source == "the draft return"
    for e in inv.outstanding:
        expected.waive(lay, 2025, e.form, e.issuer, as_of)
    assert snapshots.kind_now(lay, 2025, as_of) == snapshots.RECONCILED
    # the same figures as the provisional one, yet a kind of its own
    assert snapshots.take(lay, 2025, as_of).new
    d = draft.build(lay, 2025)
    tax, agi = d.get("1040", "24"), d.get("1040", "11a")
    assert tax is not None and agi is not None
    _file(lay, "filed-1040.pdf", _filed(d, total_tax_bump=25.0))
    close.close(lay, 2025)
    assert snapshots.kind_now(lay, 2025, as_of) == snapshots.FILED
    filed = snapshots.take(lay, 2025, as_of).snapshot
    assert filed and filed.source == "the filed return, version 1"
    assert filed.figures["federal_tax"] == round(tax + 25.0, 2)
    assert filed.figures["agi"] == round(agi, 2)
    assert filed.figures["state_tax"] == prov.figures["state_tax"]

    kinds = [s.kind for s in snapshots.load(lay, 2025)]
    assert kinds == list(snapshots.KINDS)
    text = snapshots.lines(lay, 2025)
    assert text[0] == "Snapshots for 2025"
    assert any(
        ln.strip().startswith("filed against reconciled actual: AGI ") for ln in text
    )
    res = runner.invoke(app, ["snapshots", "--year", "2025"])
    assert res.exit_code == 0 and "2025-07-01  forecast: AGI " in res.output
    # the figures are the user's own: under data/private, never in the repo
    assert snapshots.path(lay, 2025).is_relative_to(lay.data / "private")


def test_run_takes_the_years_snapshots(lay: Layout) -> None:
    from planner.taxprep import snapshots

    got = snapshots.refresh(lay, 2026, date(2026, 3, 1))
    assert [g.year for g in got] == [2025, 2026]
    assert got[1].snapshot and got[1].snapshot.kind == snapshots.FORECAST
    assert got[0].snapshot and got[0].snapshot.kind == snapshots.PROVISIONAL
    assert not any(g.new for g in snapshots.refresh(lay, 2026, date(2026, 3, 2)))
