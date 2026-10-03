"""Phase 8c, the done test: documents dropped in the inbox reach the
dashboard, and a fresh copy of the release plus only the data/ folder (a new
computer, or a restored backup) renders the identical page."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from tests.pdfgen import make_pdf
from tests.test_ingest import DIV_2025, INT_2025

ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()
RUN = ["run", "--quiet", "--no-update-check", "--year", "2025", "--as-of", "2026-02-10"]


def _answer(home: Path) -> None:
    for key, value in (
        ("birth_date", "1971-06-15"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("wages", "52,000"),
    ):
        r = runner.invoke(app, ["enter", "--year", "2025", key, value])
        assert r.exit_code == 0, r.output


@pytest.mark.engine
def test_inbox_to_dashboard_and_a_fresh_copy_renders_the_same(
    planner_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = planner_home / "data" / "inbox"
    inbox.mkdir(parents=True)
    make_pdf(inbox / "int.pdf", [INT_2025])
    make_pdf(inbox / "div.pdf", [DIV_2025])
    _answer(planner_home)
    r = runner.invoke(app, RUN)
    assert r.exit_code == 0, r.output
    first = (planner_home / "out" / "index.html").read_text(encoding="utf-8")
    assert not [p for p in inbox.iterdir() if p.is_file()]  # every file taken
    # 1099-INT 1,234.56 reaches the draft as 1040 line 2b, in whole dollars
    for shown in ("Example Bank (synthetic)", "1,235.00", "9,800.00"):
        assert shown in first, shown
    assert str(planner_home) not in first  # nothing tied to this folder

    fresh = tmp_path / "another computer"
    fresh.mkdir()
    shutil.copytree(ROOT / "config", fresh / "config")  # the release's config
    shutil.copytree(planner_home / "data", fresh / "data")
    monkeypatch.setenv("PLANNER_HOME", str(fresh))
    r = runner.invoke(app, RUN)
    assert r.exit_code == 0, r.output
    assert (fresh / "out" / "index.html").read_text(encoding="utf-8") == first
