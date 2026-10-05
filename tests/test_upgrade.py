"""Phase 10, unit 2e: a data/ folder written by an earlier release opens in
this one. ``tests/fixtures/data-v0.1.0`` was made by the v0.1.0 tag itself
(``make.py`` beside it says how) from synthetic documents only; its ledger is
kept as SQL text and loaded back here."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ledger import db

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "data-v0.1.0"
RUN = ["run", "--quiet", "--no-update-check", "--year", "2025", "--as-of", "2026-02-10"]
runner = CliRunner()


def _restore(home: Path) -> Path:
    """The v0.1.0 data/ copied into ``home``; returns the ledger path."""
    data = home / "data"
    shutil.copytree(FIXTURE, data, ignore=shutil.ignore_patterns("make.py", "*.sql"))
    ledger = data / "ledger" / "planner.db"
    with sqlite3.connect(ledger) as conn:
        conn.executescript((FIXTURE / "ledger" / "planner.sql").read_text("utf-8"))
    conn.close()
    return ledger


COUNTS = {
    "documents": "SELECT COUNT(*) FROM documents",
    "facts": "SELECT COUNT(*) FROM facts",
    "rows": "SELECT COUNT(*) FROM rows",
    "conversions": "SELECT COUNT(*) FROM conversions",
}


def _count(ledger: Path, table: str) -> int:
    with sqlite3.connect(ledger) as conn:
        n = int(conn.execute(COUNTS[table]).fetchone()[0])
    conn.close()
    return n


@pytest.mark.engine
def test_a_v010_data_folder_runs_in_this_release(planner_home: Path) -> None:
    ledger = _restore(planner_home)
    before = {
        t: _count(ledger, t) for t in ("documents", "facts", "rows", "conversions")
    }
    r = runner.invoke(app, RUN)
    assert r.exit_code == 0, r.output
    page = (planner_home / "out" / "index.html").read_text(encoding="utf-8")
    assert "Example Bank (synthetic)" in page
    for table, n in before.items():
        assert _count(ledger, table) == n, table  # nothing lost or doubled
    r = runner.invoke(app, ["conversions", "--year", "2025"])
    assert r.exit_code == 0, r.output
    assert "5,000" in r.output


def _ledger_at(path: Path, version: int) -> None:
    db.connect(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version = ?", (version,))
    conn.close()


def test_a_ledger_from_a_newer_planner_is_refused_untouched(tmp_path: Path) -> None:
    ledger = tmp_path / "planner.db"
    _ledger_at(ledger, db.SCHEMA_VERSION + 1)
    before = ledger.read_bytes()
    with pytest.raises(db.LedgerTooNew, match="newer planner"):
        db.connect(ledger)
    with pytest.raises(db.LedgerTooNew, match="newer planner"):
        db.connect_readonly(ledger)
    assert ledger.read_bytes() == before  # not stamped back to this schema


def test_a_writing_command_refuses_a_newer_ledger(planner_home: Path) -> None:
    ledger = planner_home / "data" / "ledger" / "planner.db"
    _ledger_at(ledger, db.SCHEMA_VERSION + 1)
    r = runner.invoke(app, ["derive", "--year", "2025"])
    assert r.exit_code == 2
    assert "newer planner" in r.output
    assert "Traceback" not in r.output


def test_an_older_ledger_is_copied_aside_then_brought_up(tmp_path: Path) -> None:
    ledger = tmp_path / "planner.db"
    _ledger_at(ledger, 3)
    with sqlite3.connect(ledger) as conn:  # schema 3 had no value_text
        conn.execute("ALTER TABLE facts DROP COLUMN value_text")
    conn.close()
    db.connect(ledger).close()
    backup = tmp_path / "planner.db.schema3.bak"
    assert backup.is_file()
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 3
    conn.close()
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 4
        cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)")}
    conn.close()
    assert "value_text" in cols


def test_every_schema_step_since_the_first_release_is_registered() -> None:
    assert sorted(db.MIGRATIONS) == list(range(3, db.SCHEMA_VERSION))
