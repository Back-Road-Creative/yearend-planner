"""Unit 7b: every step that writes survives being cut off. A kill is a child
Python that runs the real step and leaves with ``os._exit(137)`` at the worst
moment (no cleanup, no finally: as a power cut or Task Manager would); a full
disk is an ``OSError(ENOSPC)`` raised in process. Each test then runs the
planner again and checks nothing was lost or half-written. Synthetic data."""

from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner import backup as bk
from planner.cli import app
from planner.engine import update as upd
from planner.ingest import ingest
from planner.ledger import db
from planner.paths import Layout
from planner.plan import rollover
from planner.taxprep import package
from tests.test_capgains import lay as cg_lay  # noqa: F401  (the fixture)
from tests.test_csv import COST_BASIS, REALIZED, drop
from tests.test_package import lay as pack_lay  # noqa: F401  (the fixture)
from tests.test_rollover import JAN
from tests.test_rollover import lay as roll_lay  # noqa: F401  (the fixture)
from tests.test_upgrade import _restore

ROOT = Path(__file__).resolve().parent.parent
KILLED = 137
runner = CliRunner()


def killed(home: Path, script: str) -> None:
    """Run ``script`` in a child planner on ``home``; it must die by os._exit."""
    env = {**os.environ, "PLANNER_HOME": str(home), "PYTHONPATH": str(ROOT)}
    res = subprocess.run(  # noqa: S603  (the test's own script)
        [sys.executable, "-c", "import os, sys\n" + script],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert res.returncode == KILLED, res.stderr


def full_disk(*_: object, **__: object) -> None:
    raise OSError(errno.ENOSPC, "No space left on device")


def hashes(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    drop(lay, "basis.csv", COST_BASIS)
    ingest(lay)
    return lay


def test_restore_killed_between_its_two_moves_loses_nothing(
    lay: Layout, tmp_path: Path
) -> None:
    """The worst gap: data/ is already data-previous/ and the backup's data/
    is not yet in. Before 7b the next launch made an empty data/ and then
    deleted data-previous/ as a finished restore."""
    z = bk.backup(lay, tmp_path / "b.zip")
    (lay.data / "archive" / "since-backup.txt").write_text("newer", encoding="utf-8")
    killed(
        lay.root,
        "from pathlib import Path\n"
        "from planner import backup\n"
        "from planner.paths import layout\n"
        "real = Path.rename\n"
        "def rename(self, target):\n"
        "    if self.name == 'data' and self.parent.name == backup.STAGING:\n"
        "        os._exit(137)\n"
        "    return real(self, target)\n"
        "Path.rename = rename\n"
        f"backup.restore(layout(), Path({str(z)!r}))\n",
    )
    assert not lay.data.exists() and (lay.root / bk.JOURNAL).exists()
    assert not bk.settle(lay)  # a launch never deletes data-previous/ mid-restore

    res = runner.invoke(app, ["ingest"])
    assert res.exit_code == 0, res.output
    assert "an interrupted restore was finished" in res.output
    assert "data-previous/ until the next launch" in res.output
    assert not (lay.root / bk.JOURNAL).exists()
    assert not (lay.root / bk.STAGING).exists()
    assert (lay.data / "ledger" / "planner.db").exists()
    assert not (lay.data / "archive" / "since-backup.txt").exists()  # the backup's
    kept = lay.root / bk.PREVIOUS / "archive" / "since-backup.txt"
    assert kept.read_text(encoding="utf-8") == "newer"
    assert bk.undo(lay)  # and the restore can still be taken back
    assert (lay.data / "archive" / "since-backup.txt").exists()


def test_recover_reads_how_far_the_restore_got(lay: Layout, tmp_path: Path) -> None:
    staging, journal = lay.root / bk.STAGING, lay.root / bk.JOURNAL
    assert bk.recover(lay) == ""  # no restore was cut off

    # cut off before data/ moved: nothing changed
    (staging / "data").mkdir(parents=True)
    journal.write_text("{}", encoding="utf-8")
    before = hashes(lay.data)
    assert bk.recover(lay) == "an interrupted restore changed nothing; restore again"
    assert hashes(lay.data) == before and not staging.exists()

    # data/ moved, the unpacked copy lost: put the old data back
    lay.data.rename(lay.root / bk.PREVIOUS)
    journal.write_text("{}", encoding="utf-8")
    assert bk.recover(lay) == (
        "an interrupted restore was undone: your data is as it was"
    )
    assert hashes(lay.data) == before and not (lay.root / bk.PREVIOUS).exists()


def test_a_cut_off_migration_backup_is_not_kept(planner_home: Path) -> None:
    """The copy made before a schema upgrade: a kill mid-copy left a partial
    planner.db.schemaN.bak that every later run then took as made."""
    ledger = _restore(planner_home)
    killed(
        planner_home,
        "import sqlite3\n"
        "from pathlib import Path\n"
        "from planner.ledger import db\n"
        "real = sqlite3.connect\n"
        "def connect(path, *a, **k):\n"
        "    if '.schema' in Path(path).name:\n"
        "        part = real(path)\n"
        "        part.execute('CREATE TABLE half (x)')\n"
        "        part.commit()\n"
        "        os._exit(137)\n"
        "    return real(path, *a, **k)\n"
        "sqlite3.connect = connect\n"
        f"db.connect(Path({str(ledger)!r}))\n",
    )
    db.connect(ledger).close()
    (bak,) = ledger.parent.glob("planner.db.schema*.bak")
    with sqlite3.connect(bak) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    conn.close()
    assert "documents" in tables and "half" not in tables
    assert not list(ledger.parent.glob("*.part"))


def test_ingest_killed_after_its_commit_archives_on_the_next_run(
    lay: Layout,
) -> None:
    drop(lay, "realized.csv", REALIZED)
    killed(
        lay.root,
        "import planner.ingest as ing\n"
        "from planner.paths import layout\n"
        "ing.archive = lambda *a, **k: os._exit(137)\n"
        "ing.ingest(layout())\n",
    )
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        cut = conn.execute(
            "SELECT COUNT(*) FROM documents "
            "WHERE file_name = 'realized.csv' AND archived_as IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    assert cut == 1  # committed, never moved

    res = runner.invoke(app, ["ingest"])
    assert res.exit_code == 0, res.output
    assert (
        "recovered realized.csv" in res.output
        and "duplicate realized.csv" not in res.output
    )
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        got = dict(
            conn.execute(
                "SELECT file_name, archived_as FROM documents "
                "WHERE file_name IN ('basis.csv', 'realized.csv')"
            ).fetchall()
        )
    finally:
        conn.close()
    assert got["realized.csv"] == "archive/2025/realized.csv"
    assert all((lay.data / rel).exists() for rel in got.values())
    assert not (lay.data / "inbox" / "realized.csv").exists()


def _release(root: Path, version: str) -> None:
    for name in upd.SWAPPED_DIRS:
        (root / name).mkdir(parents=True)
        (root / name / "mark").write_text(version, encoding="utf-8")
    for name in upd.SWAPPED_FILES:
        (root / name).write_text(version + "\n", encoding="utf-8")


def _version(root: Path) -> set[str]:
    """The version every swapped item says it is (one, when the set is whole)."""
    seen = {(root / n / "mark").read_text("utf-8") for n in upd.SWAPPED_DIRS}
    return seen | {(root / n).read_text("utf-8").strip() for n in upd.SWAPPED_FILES}


def test_update_swap_on_a_full_disk_puts_the_live_release_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, cand = tmp_path / "root", tmp_path / "root" / upd.CANDIDATE
    _release(root, "0.1.0")
    _release(cand, "0.2.0")
    real, moves = upd._move, []

    def move(src: Path, dst: Path) -> None:
        moves.append(src)
        if len(moves) == len(upd.PARK_ORDER) + 3:  # part way into the candidate
            full_disk()
        real(src, dst)

    monkeypatch.setattr(upd, "_move", move)
    with pytest.raises(OSError, match="No space"):
        upd.swap_in(cand, root)
    assert _version(root) == {"0.1.0"} and _version(cand) == {"0.2.0"}
    assert not any((root / upd.PREVIOUS).iterdir())


def test_update_killed_mid_swap_rolls_back_whole(tmp_path: Path) -> None:
    root, cand = tmp_path / "root", tmp_path / "root" / upd.CANDIDATE
    _release(root, "0.1.0")
    _release(cand, "0.2.0")
    killed(
        root,
        "from pathlib import Path\n"
        "from planner.engine import update as upd\n"
        "real, n = upd._move, []\n"
        "def move(src, dst):\n"
        "    n.append(src)\n"
        "    if len(n) == 3:\n"
        "        os._exit(137)\n"
        "    real(src, dst)\n"
        "upd._move = move\n"
        f"root = Path({str(root)!r})\n"
        "upd.swap_in(root / upd.CANDIDATE, root)\n",
    )
    with pytest.raises(upd.UpdateError, match="cut off part way"):
        upd.swap_in(cand, root)  # never over a swap that is half done
    assert upd.rollback(root) == "0.1.0"
    assert _version(root) == {"0.1.0"} and not upd.cut_off(root)


@pytest.mark.engine
def test_taxpack_on_a_full_disk_keeps_the_last_pack_whole(
    pack_lay: Layout,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack = package.build(pack_lay, 2025, date(2026, 2, 10))
    before = hashes(pack.folder)
    monkeypatch.setattr(package, "html_page", full_disk)
    with pytest.raises(OSError, match="No space"):
        package.build(pack_lay, 2025, date(2026, 3, 1))
    assert hashes(pack.folder) == before
    monkeypatch.undo()
    again = package.build(pack_lay, 2025, date(2026, 3, 1))
    assert again.folder == pack.folder and set(again.written) == set(pack.written)
    assert not pack.folder.with_name("tax-2025.partial").exists()
    assert not pack.folder.with_name("tax-2025.old").exists()
    with zipfile.ZipFile(pack.folder / "originals.zip") as zf:
        assert zf.testzip() is None


def test_rollover_cut_off_after_its_snapshot_rolls_once(
    roll_lay: Layout,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rollover, "_checklist", full_disk)
    with pytest.raises(OSError, match="No space"):
        rollover.roll(roll_lay, 2025, JAN)
    monkeypatch.undo()
    ro = rollover.roll(roll_lay, 2025, JAN)
    assert (ro.version, ro.new) == (1, True)
    assert [v["version"] for v in rollover._state(roll_lay)["rolled"][2025]] == [1]
    snaps = list((roll_lay.data / "snapshots").rglob("*"))
    assert ro.snapshot in snaps
