"""Phase 4m: ``planner close`` from a synthetic filed 1040 and D-400: the delta
against the draft, the closed record, an unchanged re-run and an amended return
closing as version 2."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest.needs import enter
from planner.ledger import db
from planner.paths import Layout
from planner.taxprep import close, draft
from tests.test_d400 import _lay

runner = CliRunner()
NOW = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)


def _file(lay: Layout, name: str, facts: list[db.Fact]) -> None:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint=f"synthetic-{name}",
        file_name=name,
        kind="pdf",
        pages=2,
        batch=name,
        facts=facts,
    )
    conn.close()


def _filed(d: draft.Draft, total_tax_bump: float = 0.0) -> list[db.Fact]:
    def get(form: str, line: str) -> float:
        value = d.get(form, line)
        assert value is not None, (form, line)
        return value

    return [
        db.Fact("1040", 2025, "self", "11", "AGI", get("1040", "11a"), 1),
        db.Fact("1040", 2025, "self", "15", "Taxable", get("1040", "15"), 1),
        db.Fact(
            "1040",
            2025,
            "self",
            "24",
            "Total tax",
            get("1040", "24") + total_tax_bump,
            2,
        ),
        db.Fact("1040", 2025, "self", "5b", "Pensions, taxable", 0.0, 1),
        db.Fact("NC-D400", 2025, "NC", "15", "NC tax", get("D-400", "15"), 1),
    ]


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    return _lay(planner_home, 2000.0)


def test_close_matches_the_draft_and_writes_version_1(lay: Layout) -> None:
    d = draft.build(lay, 2025)
    _file(lay, "filed-2025.pdf", _filed(d))
    c = close.close(lay, 2025, NOW)
    assert (c.version, c.new, c.matched) == (1, True, 4)
    assert c.deltas == []
    assert [x.key for x in c.filed_only] == ["1040 5b"]  # the draft has no 5b
    assert c.documents == ["filed-2025.pdf"]
    rec = close.closed(lay, 2025)
    assert rec is not None and rec["1040 24"] == d.get("1040", "24")
    assert close.record_path(lay, 2025).parent == lay.data / "private" / "returns"
    out = close.render(c)
    assert "every filed line agrees with the draft (4 lines)" in out

    again = close.close(lay, 2025, NOW)
    assert (again.version, again.new) == (1, False)
    assert any("already closed as version 1" in n for n in again.notes)


def test_amended_return_closes_as_version_2_with_the_delta(lay: Layout) -> None:
    d = draft.build(lay, 2025)
    _file(lay, "filed-2025.pdf", _filed(d))
    close.close(lay, 2025, NOW)
    _file(lay, "amended-2025.pdf", _filed(d, total_tax_bump=250.0))
    c = close.close(lay, 2025, NOW)
    assert (c.version, c.new) == (2, True)
    assert [(x.key, x.gap) for x in c.deltas] == [("1040 24", 250.0)]
    assert any("amended return" in n for n in c.notes)
    rec = close.closed(lay, 2025)
    assert rec is not None and rec["1040 24"] == pytest.approx(
        (d.get("1040", "24") or 0.0) + 250.0
    )
    assert "1 line(s) differ from the draft" in close.render(c)


def test_close_needs_the_filed_1040(lay: Layout) -> None:
    with pytest.raises(close.NotFiledError):
        close.close(lay, 2025)
    assert close.closed(lay, 2025) is None
    res = runner.invoke(app, ["close", "--year", "2025"])
    assert res.exit_code == 2 and "blocked" in res.output


def test_missing_d400_is_named_and_cli_closes(lay: Layout) -> None:
    d = draft.build(lay, 2025)
    _file(lay, "filed-2025.pdf", [f for f in _filed(d) if f.form == "1040"])
    res = runner.invoke(app, ["close", "--year", "2025"])
    assert res.exit_code == 0, res.output
    assert "closed: version 1" in res.output
    assert "no filed 2025 D-400" in res.output


def test_close_without_a_draft_still_records(planner_home: Path) -> None:
    lay = Layout(planner_home)
    lay.ensure()
    _file(
        lay,
        "filed-2025.pdf",
        [db.Fact("1040", 2025, "self", "24", "Total tax", 1234.0, 2)],
    )
    c = close.close(lay, 2025, NOW)
    assert c.version == 1 and any("no draft to compare" in n for n in c.notes)
    assert close.closed(lay, 2025) == {"1040 24": 1234.0}
    enter(lay, 2025, "state", "NC")  # the record survives later edits
    assert close.closed(lay, 2025) == {"1040 24": 1234.0}


def test_filed_schedule_c_lines_pair_with_the_drafts_sheet() -> None:
    """Schedule C is part of the draft now, so the filed lines compare to it."""
    for box in ("1", "7", "28", "31"):
        assert close.MAP[("1040-SCHC", box)] == ("Sch C", box)
    assert not hasattr(close, "FILED_ONLY_FORMS")
