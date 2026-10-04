"""Phase 4l: ``planner taxpack`` writes out/tax-<year>/ from synthetic lots, a
typed carryover, a Roth conversion, an estimated payment and the archived
original, and every file agrees with the command that already prints it."""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.engine.household import MissingInputError
from planner.ingest import ingest
from planner.ingest.needs import enter
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.plan import esttax
from planner.taxprep import draft, package
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
    assert sorted(p.name for p in folder.iterdir()) == sorted(package.FILES)
    d = draft.build(lay, 2025)
    assert (folder / "draft.txt").read_text(encoding="utf-8") == draft.render(d)
    page = (folder / "draft.html").read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>") and "NC Form D-400" in page
    assert "<script" not in page

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


def test_taxpack_rerun_replaces_and_cli_lists(lay: Layout) -> None:
    package.build(lay, 2025)
    esttax.record(lay, 2025, "nc", "2025-12-15", 150.0)
    res = runner.invoke(app, ["taxpack", "--year", "2025"])
    assert res.exit_code == 0, res.output
    assert "draft.html" in res.output and "originals.zip" in res.output
    pays = _rows(lay.out / "tax-2025" / "estimated-payments.csv")
    assert [p["agency"] for p in pays] == ["fed", "nc"]


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
