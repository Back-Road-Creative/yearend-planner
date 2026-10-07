"""Unit 7c: a backup restored on a clean folder gives back the same figures.
Synthetic data (the tax-pack household of test_package and the cost-basis CSV
of test_csv)."""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner import health, rehearsal
from planner.backup import BackupError, backup
from planner.cli import app
from planner.ingest import ingest
from planner.paths import Layout
from tests.test_crash import lay  # noqa: F401  (the fixture)
from tests.test_csv import REALIZED, drop
from tests.test_package import lay as pack_lay  # noqa: F401  (the fixture)

runner = CliRunner()
AS_OF = date(2026, 2, 10)


def _data(folder: Layout) -> dict[str, str]:
    return {
        p.relative_to(folder.data).as_posix(): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in sorted(folder.data.rglob("*"))
        if p.is_file() and p != folder.lock_file
    }


@pytest.mark.engine
def test_backup_rehearse_restores_on_a_clean_folder_and_matches(
    pack_lay: Layout,  # noqa: F811
) -> None:
    before = _data(pack_lay)
    res = runner.invoke(app, ["backup", "--plain", "--rehearse"])
    assert res.exit_code == 0, res.output
    assert "rehearsal passed: " in res.output and " figures match (2025)" in res.output
    assert _data(pack_lay) == before  # the live data is only read
    (zipped,) = (pack_lay.out / "backups").glob("planner-backup-*.zip")
    kept = rehearsal.last(zipped)
    assert kept is not None and kept["ok"] is True
    assert int(str(kept["compared"])) > 50
    fact, problem = health._backup(pack_lay, date.today())
    assert f"rehearsed {date.today().isoformat()}: " in fact and problem == ""

    got = rehearsal.figures(pack_lay, [2025])
    assert any(k.startswith("draft 2025 1040 ") for k in got)
    assert any(k.startswith("needed 2025 ") for k in got)
    assert any(k.startswith("ledger rows 2025 ") for k in got)


def test_a_difference_is_named_and_kept(lay: Layout, tmp_path: Path) -> None:  # noqa: F811
    zipped = backup(lay, lay.out / "backups" / "planner-backup-1.zip")
    fact, _ = health._backup(lay, AS_OF)
    assert fact.endswith("never rehearsed (planner backup --rehearse)")
    drop(lay, "realized.csv", REALIZED)
    ingest(lay)  # live data moves on after the backup

    res = rehearsal.rehearse(lay, zipped, year_list=[], today=AS_OF)
    assert not res.ok and res.line.startswith("rehearsal FAILED: ")
    assert any(d.startswith("ledger documents all: live ") for d in res.differences)
    assert any("(none)" in d for d in res.differences)  # rows only the live has
    _, problem = health._backup(lay, AS_OF)
    assert problem.startswith("the last backup's rehearsal on 2026-02-10 found ")
    assert json.loads(rehearsal.record_path(zipped).read_text("utf-8"))["ok"] is False

    before = _data(lay)
    cli = runner.invoke(app, ["restore", str(zipped), "--rehearse"])
    assert cli.exit_code == 1 and "changes made since the backup" in cli.output
    assert "rehearsal FAILED: " in cli.output
    assert _data(lay) == before and not (lay.root / "data-previous").exists()


def test_a_damaged_zip_fails_the_rehearsal_as_it_would_the_restore(
    lay: Layout,  # noqa: F811
) -> None:
    zipped = backup(lay, lay.out / "backups" / "planner-backup-1.zip")
    bad = zipped.with_name("damaged.zip")
    with zipfile.ZipFile(zipped) as src, zipfile.ZipFile(bad, "w") as dst:
        for info in src.infolist():
            data = src.read(info)
            if info.filename.endswith(".db"):
                data = data[:-1] + bytes([data[-1] ^ 1])
            dst.writestr(info, data)
    with pytest.raises(BackupError, match="damaged"):
        rehearsal.rehearse(lay, bad, year_list=[])
    assert rehearsal.last(bad) is None
    cli = runner.invoke(app, ["restore", str(bad), "--rehearse"])
    assert cli.exit_code == 1 and "rehearsal refused: " in cli.output
