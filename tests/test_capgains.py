"""Phase 4j-1: Form 8949 and Schedule D from synthetic lots: a long-term gain,
a short-term loss washed 40% by an IRA buy, a short-term loss with no term
column, a sale inside an IRA (not reported), 1099-DIV capital gain
distributions and a typed long-term loss carried in."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest import ingest
from planner.ingest.needs import enter, needed
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.taxprep import capgains, draft
from tests.test_csv import TXN_HEADER, drop

runner = CliRunner()
TAXABLE, IRA = "11112222", "33334444"
LOTS = "\n".join(
    [
        "Account Number,Investment Name,Symbol,Date Acquired,Date Sold,Shares,"
        "Proceeds,Cost Basis,Term",
        f"{TAXABLE},Total Stock,VTSAX,03/02/2020,02/10/2025,100,12000,8000,Long-term",
        f"{TAXABLE},Intl Stock,VXUS,01/06/2025,05/01/2025,100,5000,6000,Short-term",
        f"{TAXABLE},Total Bond,BND,11/01/2024,09/15/2025,50,3500,3700,",
        f"{IRA},Total Stock,VTSAX,01/02/2015,03/03/2025,10,1500,900,Long-term",
        "",
    ]
)
BUYS = "\n".join(
    [
        TXN_HEADER,
        f"{IRA},05/20/2025,05/21/2025,Buy,Buy,Intl,VXUS,40,60,2400,0,-2400,0,IRA,",
        "",
    ]
)


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    portfolio.save_account(lay, TAXABLE, type="taxable")
    portfolio.save_account(lay, IRA, type="trad_ira")
    drop(lay, "lots.csv", LOTS)
    drop(lay, "buys.csv", BUYS)
    ingest(lay)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-forms",
        file_name="forms.pdf",
        kind="pdf",
        pages=1,
        batch="b1",
        facts=[
            db.Fact(
                "1099-DIV", 2025, "Fund Co (synthetic)", "2a", "Cap gain", 300.0, 1
            ),
            db.Fact("1099-B", 2025, "Broker (synthetic)", "st_proceeds", "", 8500.0, 1),
            db.Fact(
                "1099-B", 2025, "Broker (synthetic)", "lt_proceeds", "", 12000.0, 1
            ),
            db.Fact("1099-B", 2025, "Broker (synthetic)", "wash_sale", "", 0.0, 1),
        ],
    )
    conn.close()
    return lay


def _store(lay: Layout, today: date) -> capgains.CapGains:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        return capgains.store(conn, lay, 2025, today)
    finally:
        conn.close()


def _documents(lay: Layout) -> int:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE file_name = 'Schedule D 2025'"
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def test_8949_wash_sale_and_schedule_d(lay: Layout) -> None:
    enter(lay, 2025, "lt_loss_carryover", "1,000")
    cg = _store(lay, date(2026, 2, 1))
    by = {(lt.box, lt.description): lt for lt in cg.lots}
    assert len(cg.lots) == 3  # the IRA's own sale is not reported
    vxus = by[("A", "100 sh VXUS")]
    assert (vxus.code, vxus.adjustment, vxus.gain) == ("W", 400.0, -600.0)
    assert by[("A", "50 sh BND")].gain == -200.0  # term from the dates
    assert by[("D", "100 sh VTSAX")].gain == 4000.0
    assert cg.lines == {
        "1b": -800.0,
        "7": -800.0,
        "8b": 4000.0,
        "13": 300.0,
        "14": -1000.0,
        "15": 3300.0,
        "16": 2500.0,
    }
    notes = " ".join(cg.notes)
    assert "trad_ira account; that loss is lost for good" in notes
    assert "400.00 here vs 0.00 on the 1099-B" in notes
    assert "vs 1099-B proceeds" not in notes  # 20,500 both ways
    by_need = {s.need.key: s for s in needed(lay, 2025).items}
    assert (by_need["long_term_gains"].state, by_need["long_term_gains"].value) == (
        "actual",
        3300.0,
    )
    assert by_need["short_term_gains"].value == -800.0
    before = _documents(lay)
    _store(lay, date(2026, 2, 2))
    assert _documents(lay) == before  # unchanged lines are not stored again


def test_open_year_keeps_the_estimate(lay: Layout) -> None:
    enter(lay, 2025, "st_loss_carryover", "500")  # a change an open year won't store
    before = _documents(lay)
    cg = _store(lay, date(2025, 12, 1))
    assert "2025 is still open" in " ".join(cg.notes)
    assert _documents(lay) == before and cg.lines["6"] == -500.0


def test_1099b_summary_without_lots(planner_home: Path) -> None:
    lay = Layout(planner_home)
    lay.ensure()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        db.add_document(
            conn,
            fingerprint="b-only",
            file_name="b.pdf",
            kind="pdf",
            pages=1,
            batch="b1",
            facts=[
                db.Fact("1099-B", 2025, "Broker", "st_proceeds", "", 1000.0, 1),
                db.Fact("1099-B", 2025, "Broker", "st_basis", "", 1200.0, 1),
                db.Fact("1099-B", 2025, "Broker", "lt_proceeds", "", 5000.0, 1),
                db.Fact("1099-B", 2025, "Broker", "lt_basis", "", 2000.0, 1),
                db.Fact("1099-B", 2025, "Broker", "wash_sale", "", 50.0, 1),
            ],
        )
        cg = capgains.build(conn, lay, 2025)
    finally:
        conn.close()
    assert not cg.lots
    assert (cg.lines["1a"], cg.lines["8a"], cg.lines["16"]) == (-200.0, 3000.0, 2800.0)
    assert "import the realized-lots CSV" in " ".join(cg.notes)


def test_carryover_worksheet() -> None:
    assert capgains.carryover(-10000, 2000, -8000, -3000, 20000) == (5000.0, 0.0)
    assert capgains.carryover(1000, -9000, -8000, -3000, -500) == (0.0, 5500.0)
    assert capgains.carryover(-800, -5700, -6500, -3000, 40000) == (0.0, 3500.0)
    assert capgains.carryover(500, 100, 600, 600, 1000) == (0.0, 0.0)


def test_draft_carries_schedule_d_and_the_loss_forward(lay: Layout) -> None:
    for key, text in (
        ("birth_date", "1971-06-15"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("wages", "60,000"),
        ("lt_loss_carryover", "10,000"),
    ):
        enter(lay, 2025, key, text)
    d = draft.build(lay, 2025)  # inputs re-store Schedule D with the typed carryover
    assert d.get("Sch D", "15") == -5700.0 and d.get("Sch D", "16") == -6500.0
    assert d.get("Sch D", "21") == -3000.0 and d.get("1040", "7a") == -3000.0
    assert d.get("Carryover", "8") == 0.0 and d.get("Carryover", "13") == 3500.0
    assert d.get("8949", "A1") == -600.0 and d.get("8949", "D1") == 4000.0
    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes
    r = runner.invoke(app, ["gains", "--year", "2025"])
    assert r.exit_code == 0, r.output
    assert "Form 8949 box A" in r.output and "W" in r.output
    assert "line 16" in r.output and "-6,500.00" in r.output
    text = draft.render(d)
    assert "Capital loss carryover to next year" in text and "Form 8949" in text
