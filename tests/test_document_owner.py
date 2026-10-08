"""Whose each document is (phase 10, unit 3a-4): a joint return's spouse brings
their own W-2, 1099-R, SSA-1099 and 5498, and the per-person lines (wages, IRA,
Social Security) must land on that person. Synthetic figures only."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.engine.household import Person
from planner.ingest import ingest
from planner.ingest.needs import enter, needed
from planner.ledger import db
from planner.paths import Layout
from planner.plan import inputs
from tests.pdfgen import make_pdf

YEAR = 2025
EMPLOYER = "Example Employer Inc (synthetic)"


def _w2(wages: str) -> list[str]:
    return [
        "Form W-2 Wage and Tax Statement 2025",
        f"c Employer's name, address, and ZIP code: {EMPLOYER}",
        f"1 Wages, tips, other compensation $ {wages}",
        "2 Federal income tax withheld $ 2,400.00",
    ]


def _doc(conn: sqlite3.Connection, name: str, wages: float, owner: str = "you") -> int:
    return db.add_document(
        conn,
        fingerprint=name,
        file_name=name,
        kind="pdf",
        pages=1,
        batch="b1",
        owner=owner,
        facts=[db.Fact("W-2", YEAR, EMPLOYER, "1", "Wages", wages, 1)],
    )


def _wages(conn: sqlite3.Connection) -> dict[str, float]:
    return {f.file_name: f.value for f in db.facts_for(conn, YEAR, "W-2")}


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    return lay


@pytest.fixture
def conn(lay: Layout) -> Iterator[sqlite3.Connection]:
    c = db.connect(lay.data / "ledger" / "planner.db")
    yield c
    c.close()


def test_two_spouses_at_one_employer_keep_both_w2s(conn: sqlite3.Connection) -> None:
    _doc(conn, "mine.pdf", 50_000.0)
    _doc(conn, "theirs.pdf", 30_000.0, owner="spouse")
    assert _wages(conn) == {"mine.pdf": 50_000.0, "theirs.pdf": 30_000.0}
    owners = {f.file_name: f.owner for f in db.facts_for(conn, YEAR, "W-2")}
    assert owners == {"mine.pdf": "you", "theirs.pdf": "spouse"}


def test_a_corrected_w2_replaces_only_its_owners(conn: sqlite3.Connection) -> None:
    _doc(conn, "mine.pdf", 50_000.0)
    _doc(conn, "theirs.pdf", 30_000.0, owner="spouse")
    _doc(conn, "theirs-corrected.pdf", 31_000.0, owner="spouse")
    assert _wages(conn) == {"mine.pdf": 50_000.0, "theirs-corrected.pdf": 31_000.0}


def test_an_owner_outside_the_household_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="you or spouse"):
        _doc(conn, "x.pdf", 1.0, owner="dependent")


def test_reassigning_a_document_brings_back_what_it_hid(
    conn: sqlite3.Connection,
) -> None:
    """Both W-2s dropped as the head's: the second looked like a correction of
    the first. Marking the first the spouse's makes both count again."""
    _doc(conn, "theirs.pdf", 30_000.0)
    _doc(conn, "mine.pdf", 50_000.0)
    assert _wages(conn) == {"mine.pdf": 50_000.0}
    assert db.set_owner(conn, "theirs.pdf", "spouse") == 1
    assert _wages(conn) == {"mine.pdf": 50_000.0, "theirs.pdf": 30_000.0}
    assert db.set_owner(conn, "theirs.pdf", "you") == 1  # and back again
    assert _wages(conn) == {"mine.pdf": 50_000.0}


def test_set_owner_names_an_unknown_or_ambiguous_document(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(KeyError, match="no document"):
        db.set_owner(conn, "nothing.pdf", "spouse")
    _doc(conn, "w2.pdf", 1.0)
    db.add_document(
        conn,
        fingerprint="other",
        file_name="w2.pdf",
        kind="pdf",
        pages=1,
        batch="b2",
        facts=[db.Fact("W-2", YEAR, "Other Employer (synthetic)", "1", "W", 2.0, 1)],
    )
    with pytest.raises(KeyError, match="more than one"):
        db.set_owner(conn, "w2.pdf", "spouse")


def test_a_schema_4_ledger_gives_every_document_to_the_head(tmp_path: Path) -> None:
    ledger = tmp_path / "planner.db"
    c = db.connect(ledger)
    _doc(c, "old.pdf", 10.0)
    c.execute("ALTER TABLE documents DROP COLUMN owner")
    c.execute("UPDATE schema_version SET version = 4")
    c.commit()
    c.close()
    c = db.connect(ledger)
    assert [f.owner for f in db.facts_for(c, YEAR)] == ["you"]
    assert c.execute("SELECT version FROM schema_version").fetchone()[0] == 5
    c.close()
    assert (tmp_path / "planner.db.schema4.bak").is_file()


def test_the_spouse_folder_in_the_inbox_marks_their_documents(lay: Layout) -> None:
    inbox = lay.data / "inbox"
    (inbox / "Spouse").mkdir(parents=True)
    make_pdf(inbox / "w2.pdf", [_w2("50,000.00")])
    make_pdf(inbox / "Spouse" / "w2.pdf", [_w2("30,000.00")])
    ingest(lay)
    c = db.connect(lay.data / "ledger" / "planner.db")
    got = sorted(
        (f.owner, f.value) for f in db.facts_for(c, YEAR, "W-2") if f.box == "1"
    )
    c.close()
    assert got == [("spouse", 30_000.0), ("you", 50_000.0)]


def _joint(lay: Layout) -> None:
    for key, text in (
        ("birth_date", "1971-06-15"),
        ("filing_status", "married_joint"),
        ("spouse_birth_date", "1973-02-01"),
        ("state", "NC"),
    ):
        enter(lay, YEAR, key, text)


def test_the_spouse_is_asked_for_their_own_income(
    lay: Layout, conn: sqlite3.Connection
) -> None:
    _joint(lay)
    _doc(conn, "mine.pdf", 50_000.0)
    _doc(conn, "theirs.pdf", 30_000.0, owner="spouse")
    got = {s.need.key: (s.state, s.value) for s in needed(lay, YEAR).items}
    assert got["wages"] == ("actual", 50_000.0)
    assert got["spouse_wages"] == ("actual", 30_000.0)
    assert got["spouse_social_security"] == ("missing", None)
    assert "spouse_tipped_occupation_code" not in got  # moot without their tips


def test_a_single_filer_is_not_asked_for_spouse_income(lay: Layout) -> None:
    enter(lay, YEAR, "filing_status", "single")
    keys = {s.need.key for s in needed(lay, YEAR).items}
    assert not {k for k in keys if k.startswith("spouse_") and k != "spouse_birth_date"}


def test_the_household_carries_the_spouses_own_income(
    lay: Layout, conn: sqlite3.Connection
) -> None:
    _joint(lay)
    enter(lay, YEAR, "wages", "50,000")
    for key, text in (
        ("spouse_wages", "30,000"),
        ("spouse_social_security", "12,000"),
        ("spouse_traditional_ira_contribution", "7,000"),
    ):
        enter(lay, YEAR, key, text)
    hh = inputs.build(lay, YEAR).household
    assert hh.wages == 50_000
    assert hh.spouse == Person(
        age=52, wages=30_000, social_security=12_000, traditional_ira_contribution=7_000
    )


def test_planner_owner_moves_a_document(lay: Layout, conn: sqlite3.Connection) -> None:
    _doc(conn, "theirs.pdf", 30_000.0)
    r = CliRunner().invoke(app, ["owner", "theirs.pdf", "spouse"])
    assert r.exit_code == 0, r.output
    assert "theirs.pdf is now the spouse's" in r.output
    assert [f.owner for f in db.facts_for(conn, YEAR, "W-2")] == ["spouse"]
    r = CliRunner().invoke(app, ["owner", "nothing.pdf", "spouse"])
    assert r.exit_code == 2 and "no document" in r.output
    assert "Traceback" not in r.output


def test_a_joint_total_income_leaves_the_spouses_income_out_of_the_heads_wages(
    lay: Layout,
) -> None:
    _joint(lay)
    for key, text in (("wages", "50,000"), ("spouse_wages", "30,000")):
        enter(lay, YEAR, key, text)
    for key in ("se_income", "ira_distributions", "roth_conversion"):
        enter(lay, YEAR, key, "0")
    enter(lay, YEAR, "spouse_ira_distributions", "5,000")
    inp = inputs.build(lay, YEAR, inputs.Overrides(total_income=100_000.0))
    assert inp.household.wages == 65_000  # 100,000 less the spouse's 35,000
