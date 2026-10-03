"""Phase 4j-2: Form 8889 from synthetic 5498-SA, 1099-SA and W-2 boxes: payroll
money against the limit, the 55-or-older catch-up, an excess contribution, a
distribution partly not spent on medical care, and months of coverage."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.config import load_thresholds
from planner.ingest.needs import enter, needed
from planner.ledger import db
from planner.paths import Layout
from planner.taxprep import draft, hsa

runner = CliRunner()


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-hsa",
        file_name="hsa.pdf",
        kind="pdf",
        pages=1,
        batch="b1",
        facts=[
            db.Fact("5498-SA", 2025, "HSA Bank (synthetic)", "2", "", 5200.0, 1),
            db.Fact("5498-SA", 2025, "HSA Bank (synthetic)", "3", "", 300.0, 1),
            db.Fact("1099-SA", 2025, "HSA Bank (synthetic)", "1", "", 900.0, 1),
            db.Fact("W-2", 2025, "Employer (synthetic)", "1", "", 50000.0, 1),
            db.Fact("W-2", 2025, "Employer (synthetic)", "2", "", 4000.0, 1),
            db.Fact("W-2", 2025, "Employer (synthetic)", "12W", "", 1200.0, 1),
        ],
    )
    conn.close()
    enter(lay, 2025, "hsa_coverage", "self")
    return lay


def _store(lay: Layout, today: date) -> hsa.HSA:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        return hsa.store(conn, lay, 2025, today)
    finally:
        conn.close()


def _documents(lay: Layout) -> int:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE file_name = 'Form 8889 2025'"
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def test_limit_catch_up_excess_and_taxable_distribution(lay: Layout) -> None:
    enter(lay, 2025, "birth_date", "1968-03-01")  # 57 at the end of 2025
    enter(lay, 2025, "hsa_qualified_expenses", "600")
    h = _store(lay, date(2026, 2, 1))
    assert h.lines == {
        "2": 4300.0,  # 5,200 + 300 in, less 1,200 through payroll
        "3": 4300.0,
        "6": 4300.0,
        "7": 1000.0,
        "8": 5300.0,
        "9": 1200.0,
        "12": 4100.0,
        "13": 4100.0,
        "14a": 900.0,
        "14c": 900.0,
        "15": 600.0,
        "16": 300.0,
        "17b": 60.0,
    }
    notes = " ".join(h.notes)
    assert "200.00 went into the HSA over the 5,300.00 limit" in notes
    assert "all 12 months" in notes
    hsa_need = {s.need.key: s for s in needed(lay, 2025).items}["hsa_contribution"]
    assert (hsa_need.state, hsa_need.value, hsa_need.origin) == (
        "actual",
        4100.0,
        "8889 2025 planner",
    )
    before = _documents(lay)
    _store(lay, date(2026, 2, 2))
    assert before == 1 and _documents(lay) == 1  # unchanged lines: not stored again


def test_months_family_and_no_birth_date(lay: Layout) -> None:
    enter(lay, 2025, "hsa_coverage", "family")
    enter(lay, 2025, "hsa_months", "6")
    h = _store(lay, date(2025, 12, 1))  # still open: nothing stored
    assert (h.lines["3"], h.lines["7"], h.lines["13"]) == (4275.0, 0.0, 3075.0)
    assert h.sources["7"] == "birth_date not given"
    notes = " ".join(h.notes)
    assert "55-or-older catch-up is left out" in notes
    assert "hsa_qualified_expenses not given" in notes
    assert h.lines["16"] == 0.0 and "17b" not in h.lines
    assert "2025 is still open" in notes and _documents(lay) == 0


def test_limits_match_the_thresholds_file(planner_home: Path) -> None:
    rows = load_thresholds(Layout(planner_home).config / "thresholds.yaml")
    for year, (self_only, family, _) in hsa.LIMITS.items():
        if year in rows:
            assert rows[year]["hsa_limit_self"]["value"] == self_only
            assert rows[year]["hsa_limit_family"]["value"] == family
    assert hsa.limit(2027, "self") == (
        4400.0,
        "Rev. Proc. 2025-19 (2026; no 2027 figure on file)",
    )
    with pytest.raises(ValueError, match="2020"):
        hsa.limit(2020, "self")


def test_draft_carries_form_8889(lay: Layout) -> None:
    for key, text in (
        ("birth_date", "1968-03-01"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("hsa_qualified_expenses", "600"),
    ):
        enter(lay, 2025, key, text)
    d = draft.build(lay, 2025)
    assert d.get("8889", "13") == 4100.0 and d.get("Sch 1", "13") == 4100.0
    assert d.get("Sch 1", "8f") == 300.0 and d.get("Sch 1", "10") == 300.0
    assert d.get("Sch 2", "17c") == 60.0 and d.get("Sch 2", "18") == 60.0
    assert d.get("1040", "1z") == 50000.0
    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes
    assert "Form 8889 (health savings accounts)" in draft.render(d)
    r = runner.invoke(app, ["hsa", "--year", "2025"])
    assert r.exit_code == 0, r.output
    assert "line 13" in r.output and "4,100.00" in r.output


def test_no_hsa_no_form(planner_home: Path) -> None:
    lay = Layout(planner_home)
    lay.ensure()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        h = hsa.build(conn, lay, 2025)
    finally:
        conn.close()
    assert not h.lines and "no HSA coverage" in hsa.render(h)
