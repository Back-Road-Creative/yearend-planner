"""Phase 4h: Schedule C from categorised bank rows (synthetic payees)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest import ingest
from planner.ingest.needs import needed
from planner.ledger import db
from planner.paths import Layout
from planner.taxprep import schedule_c

runner = CliRunner()
BANK = """\
Transaction ID,Date,Description,Amount,Account
T-1,01/05/2026,CLIENT PAYMENT ACME,"4,000.00",Checking
T-2,03/06/2026,CLIENT PAYMENT ACME,"8,000.00",Checking
T-3,03/07/2026,CARD PURCHASE OFFICE DEPOT,(45.10),Checking
T-4,03/09/2026,GROCERY MART,(120.00),Checking
"""


def _nec(lay: Layout, amount: float) -> None:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        db.add_document(
            conn,
            fingerprint=f"nec-{amount}",
            file_name="nec.pdf",
            kind="pdf",
            pages=1,
            batch="test",
            facts=[
                db.Fact(
                    "1099-NEC", 2026, "Acme Widgets (synthetic)", "1", "c", amount, 1
                )
            ],
        )
    finally:
        conn.close()


def _build(lay: Layout) -> schedule_c.ScheduleC:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        return schedule_c.store(conn, lay, 2026)
    finally:
        conn.close()


def _se(lay: Layout) -> tuple[str, object, str]:
    st = next(s for s in needed(lay, 2026).items if s.need.key == "se_income")
    return st.state, st.value, st.origin


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    (lay.data / "inbox" / "bank.csv").write_text(BANK, encoding="utf-8")
    ingest(lay)
    return lay


def test_nothing_is_guessed_and_the_forms_stand_in(lay: Layout) -> None:
    _nec(lay, 10_000.0)
    sc = _build(lay)
    assert sc.lines == {} and len(sc.uncategorised) == 4
    assert sc.receipts_forms == 10_000.0
    assert _se(lay) == (
        "estimate",
        10_000.0,
        "1099-NEC 2026 Acme Widgets (synthetic) (nec.pdf p.1)",
    )


def test_rules_and_rows_build_the_lines(lay: Layout) -> None:
    _nec(lay, 10_000.0)
    schedule_c.add_rule(lay, "client payment", "receipts")
    schedule_c.add_rule(lay, "grocery", "personal")
    schedule_c.add_rule(lay, "office depot", "office")
    schedule_c.assign(lay, "bank:T-3", "supplies")  # the row beats the rule
    sc = _build(lay)
    assert sc.uncategorised == [] and sc.categorised == 4
    assert sc.lines == {
        "1": 12_000.0,
        "2": 0.0,
        "7": 12_000.0,
        "22": 45.10,
        "28": 45.10,
        "31": 11_954.90,
    }
    assert "include the 10,000.00 on 1099-NEC/K" in sc.notes[0]
    assert _se(lay) == ("actual", 11_954.90, "SCH-C 2026 categorised bank rows")
    text = schedule_c.render(sc)
    assert "line 31   Net profit or loss" in text
    assert "personal: 1 row(s) left out (not business)" in text
    # the same text again replaces the rule, it does not add a second
    schedule_c.add_rule(lay, "CLIENT PAYMENT", "receipts")
    rules, rows = schedule_c.load_rules(lay)
    assert len(rules) == 3 and rows == {"bank:T-3": "supplies"}


def test_forms_over_rows_meals_half_and_uncategorised(lay: Layout) -> None:
    _nec(lay, 15_000.0)
    schedule_c.add_rule(lay, "client payment", "receipts")
    schedule_c.assign(lay, "bank:T-3", "meals")
    sc = _build(lay)
    assert sc.lines["1"] == 15_000.0 and sc.lines["24b"] == 22.55
    assert sc.lines["31"] == round(15_000 - 22.55, 2)
    assert sc.notes[0].startswith("1099-NEC/K total 15,000.00 exceeds")
    assert sc.notes[-1] == (
        "1 bank row(s) uncategorised and left out (planner categorize --year 2026)"
    )
    assert [r.row_key for r in sc.uncategorised] == ["bank:T-4"]


def test_bad_category_and_ingest_rebuilds(lay: Layout) -> None:
    with pytest.raises(ValueError, match="unknown category food; one of receipts"):
        schedule_c.add_rule(lay, "grocery", "food")
    with pytest.raises(ValueError, match="needs some description"):
        schedule_c.add_rule(lay, "  ", "personal")
    schedule_c.add_rule(lay, "client payment", "receipts")
    more = "Transaction ID,Date,Description,Amount,Account\n"
    more += 'T-9,04/01/2026,CLIENT PAYMENT ZED,"1,000.00",Checking\n'
    (lay.data / "inbox" / "bank-april.csv").write_text(more, encoding="utf-8")
    ingest(lay)  # a new export recomputes the stored Schedule C
    assert _se(lay) == ("actual", 13_000.0, "SCH-C 2026 categorised bank rows")


def test_cli_categorize_and_needed(lay: Layout) -> None:
    _nec(lay, 10_000.0)
    r = runner.invoke(app, ["needed", "--year", "2026", "--as-of", "2026-12-01"])
    assert "categorize 4 bank row(s) with no Schedule C category" in r.output
    r = runner.invoke(app, ["categorize", "--year", "2026"])
    assert r.exit_code == 0, r.output
    assert "uncategorised 4:" in r.output and "[bank:T-4]" in r.output
    r = runner.invoke(
        app,
        [
            "categorize",
            "--year",
            "2026",
            "--rule",
            "client payment",
            "--as",
            "receipts",
        ],
    )
    assert "rule 'client payment' -> receipts: 2 row(s) this year" in r.output
    r = runner.invoke(
        app, ["categorize", "--year", "2026", "--rule", ".*", "--as", "personal"]
    )
    assert r.exit_code == 0  # text, not a pattern: catches nothing
    for key, cat in (("bank:T-3", "supplies"), ("bank:T-4", "personal")):
        r = runner.invoke(
            app, ["categorize", "--year", "2026", "--row", key, "--as", cat]
        )
        assert r.exit_code == 0, r.output
    assert "line 31   Net profit or loss" in r.output and "11,954.90" in r.output
    r = runner.invoke(app, ["needed", "--year", "2026", "--as-of", "2026-12-01"])
    assert "categorize" not in r.output
    r = runner.invoke(app, ["categorize", "--year", "2026", "--row", "bank:T-4"])
    assert r.exit_code == 2 and "need --as" in r.output
    r = runner.invoke(
        app, ["categorize", "--year", "2026", "--row", "x", "--as", "food"]
    )
    assert r.exit_code == 2 and "unknown category food" in r.output
