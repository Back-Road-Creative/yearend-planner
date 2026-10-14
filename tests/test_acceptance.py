"""Unit 7h3: ``planner acceptance``, a self-check a user runs on their own
documents. Each file is read the way ``planner ingest`` reads it, but nothing
moves and nothing reaches the ledger; the result names forms and years only,
never a figure, a payer or an account, and stays under data/private/."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from typer.testing import CliRunner

from planner import acceptance
from planner.cli import app
from planner.paths import Layout
from tests.pdfgen import make_pdf
from tests.test_csv import BANK

runner = CliRunner()
REAL = Path(__file__).resolve().parent / "fixtures" / "real"


def folder(tmp_path: Path) -> Path:
    """A real-layout 1099-INT, a bank CSV inside a ZIP inside a folder, a PDF no
    template knows and a file of no known type."""
    docs = tmp_path / "my docs"
    (docs / "bank").mkdir(parents=True)
    shutil.copy(REAL / "1099-int.pdf", docs / "interest.pdf")
    with zipfile.ZipFile(docs / "bank" / "statements.zip", "w") as zf:
        zf.writestr("2025/checking.csv", BANK)
    make_pdf(docs / "letter.pdf", [["Dear customer, nothing to report here."]])
    (docs / "notes.txt").write_text("call the bank", encoding="utf-8")
    (docs / ".hidden.csv").write_text("x", encoding="utf-8")
    return docs


def test_each_file_passes_or_fails_with_ingests_reason(tmp_path: Path) -> None:
    docs = folder(tmp_path)
    before = sorted(p.relative_to(docs).as_posix() for p in docs.rglob("*"))
    got = {c.name: c for c in acceptance.check([docs], ocr=None)}
    assert sorted(got) == [
        "bank/statements.zip!2025/checking.csv",
        "interest.pdf",
        "letter.pdf",
        "notes.txt",
    ]
    assert got["interest.pdf"].ok and got["interest.pdf"].detail == "1099-INT 2025"
    zipped = got["bank/statements.zip!2025/checking.csv"]
    assert zipped.ok and zipped.detail == "bank 2 rows"
    letter = got["letter.pdf"]
    assert not letter.ok and "no form template matched" in letter.detail
    assert got["notes.txt"].detail == "unknown file type '.txt'"
    # nothing moved, nothing unpacked beside the ZIP
    assert sorted(p.relative_to(docs).as_posix() for p in docs.rglob("*")) == before


def test_a_scan_without_the_engine_fails_and_says_so(tmp_path: Path) -> None:
    from PIL import Image

    Image.new("RGB", (40, 40), "white").save(tmp_path / "photo.png")
    (only,) = acceptance.check([tmp_path / "photo.png"], ocr=None)
    assert not only.ok and "not installed" in only.detail


def test_the_command_keeps_its_result_private_and_shows_no_figures(
    planner_home: Path,
) -> None:
    lay = Layout(planner_home)
    lay.ensure()
    docs = folder(planner_home.parent)
    res = runner.invoke(app, ["acceptance", str(docs)])
    assert res.exit_code == 1, res.output  # two files fail
    assert "PASS interest.pdf: 1099-INT 2025" in res.output
    assert "FAIL notes.txt: unknown file type '.txt'" in res.output
    assert "2 passed, 2 failed" in res.output
    (kept,) = (lay.data / "private" / "acceptance").glob("*.txt")
    assert kept.read_text(encoding="utf-8").splitlines()[-1] == "2 passed, 2 failed"
    for shown in (res.output, kept.read_text(encoding="utf-8")):
        # the payer, the amounts and the bank's descriptions stay out
        for secret in ("Example Bank", "1,235", "1235", "2,500", "ACME"):
            assert secret not in shown
    # nothing reached the ledger
    assert not (lay.data / "ledger" / "planner.db").exists()


def test_the_inbox_is_checked_by_default_and_a_clean_run_exits_zero(
    planner_home: Path,
) -> None:
    lay = Layout(planner_home)
    lay.ensure()
    empty = runner.invoke(app, ["acceptance"])
    assert empty.exit_code == 2 and "nothing to check" in empty.output
    shutil.copy(REAL / "1099-int.pdf", lay.data / "inbox" / "interest.pdf")
    res = runner.invoke(app, ["acceptance"])
    assert res.exit_code == 0, res.output
    assert "1 passed, 0 failed" in res.output
    assert (lay.data / "inbox" / "interest.pdf").exists()
    missing = runner.invoke(app, ["acceptance", str(planner_home / "nope")])
    assert missing.exit_code == 2 and "no such file or folder" in missing.output
