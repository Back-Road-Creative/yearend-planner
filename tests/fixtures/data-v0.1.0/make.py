"""Build the synthetic v0.1.0 data/ folder that tests/test_upgrade.py opens.

Run from a checkout of the v0.1.0 tag (its planner and its tests helpers):

    git worktree add --detach /tmp/v010 v0.1.0
    cd /tmp/v010 && PLANNER_UPDATE_FEED=off python <this file> <out-dir>

Every figure is synthetic: the tests' example bank, broker and household.
The ledger is written out as SQL text (``ledger/planner.sql``) so a reviewer
can read it; the test loads it back into a database.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from typer.testing import CliRunner

ROOT = Path.cwd()


def main(out: Path) -> None:
    home = Path(tempfile.mkdtemp()) / "home"
    home.mkdir()
    shutil.copytree(ROOT / "config", home / "config")
    os.environ["PLANNER_HOME"] = str(home)

    from planner.cli import app
    from tests.pdfgen import make_pdf
    from tests.test_csv import BANK, VANGUARD_DOWNLOAD
    from tests.test_ingest import DIV_2025, INT_2025

    runner = CliRunner()

    def ok(*args: str) -> None:
        r = runner.invoke(app, list(args))
        if r.exit_code != 0:
            raise SystemExit(f"planner {' '.join(args)} failed:\n{r.output}")

    ok("init")
    inbox = home / "data" / "inbox"
    make_pdf(inbox / "int.pdf", [INT_2025])
    make_pdf(inbox / "div.pdf", [DIV_2025])
    (inbox / "ofxdownload.csv").write_text(VANGUARD_DOWNLOAD, encoding="utf-8")
    (inbox / "bank.csv").write_text(BANK, encoding="utf-8")
    ok("ingest")
    for key, value in (
        ("birth_date", "1971-06-15"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("wages", "52,000"),
    ):
        ok("enter", "--year", "2025", key, value)
    ok("account", "IRA-1", "--type", "trad_ira", "--balance", "80,000")
    ok("account", "CASH-1", "--type", "cash", "--balance", "15,000")
    ok("convert", "2025-11-03", "5,000", "--from", "IRA-1")
    ok("derive", "--year", "2025")
    ok("run", "--quiet", "--no-update-check", "--year", "2025", "--as-of", "2026-02-10")

    data = home / "data"
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(
        data, out, ignore=shutil.ignore_patterns("planner.lock", "*.db", "crops")
    )
    with sqlite3.connect(data / "ledger" / "planner.db") as conn:
        dump = "\n".join(conn.iterdump()) + "\n"
    (out / "ledger" / "planner.sql").write_text(dump, encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
