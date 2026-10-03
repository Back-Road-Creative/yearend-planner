"""Phase 8a: backup and restore of a synthetic planner folder: AES and plain
zips, wrong passwords, every hostile or damaged archive refused before data/
moves, data-previous/ kept until the next clean run (or put back by --undo),
and hand limits carried into the live config."""

from __future__ import annotations

import json
import shutil
import stat
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner import backup as bk
from planner import paths
from planner.cli import app
from planner.ingest.needs import enter
from planner.ledger import db
from planner.paths import Layout

runner = CliRunner()
PASSWORD = "correct horse"  # noqa: S105  (a test password)
HAND_ROW = '\n2031:\n  my_typed_limit:\n    value: 1234\n    source: "typed by me"\n'


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-w2",
        file_name="w2.pdf",
        kind="pdf",
        pages=1,
        batch="b1",
        facts=[db.Fact("W-2", 2025, "Employer (synthetic)", "1", "Wages", 41000.0, 1)],
    )
    conn.close()  # a connection left open below mimics a running dashboard
    enter(lay, 2025, "filing_status", "single")
    (lay.data / "inbox" / "note.txt").write_text("synthetic", encoding="utf-8")
    return lay


def _wages(lay: Layout) -> list[float]:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        sql = "SELECT value_cents FROM facts WHERE form = 'W-2'"
        rows = conn.execute(sql).fetchall()
        return [r[0] / 100 for r in rows]
    finally:
        conn.close()


def _wreck(lay: Layout) -> None:
    (lay.data / "ledger" / "planner.db").unlink()
    (lay.data / "inbox" / "note.txt").write_text("changed", encoding="utf-8")


def test_encrypted_round_trip_keeps_previous_until_the_next_run(
    lay: Layout, tmp_path: Path
) -> None:
    live = db.connect(lay.data / "ledger" / "planner.db")  # the dashboard
    z = bk.backup(lay, tmp_path / "b.zip", password=PASSWORD)
    live.close()
    assert bk.encrypted(z)
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
        assert all(i.flag_bits & 0x1 for i in zf.infolist())
        with pytest.raises((RuntimeError, NotImplementedError)):
            zf.read("data/inbox/note.txt")  # not readable without the password
    assert "backup.json" in names and "config/thresholds.yaml" in names
    assert "data/ledger/planner.db" in names
    assert not [n for n in names if n.startswith("data/update")]

    _wreck(lay)
    for pw in ("", "wrong"):
        with pytest.raises(bk.BackupError, match="password"):
            bk.restore(lay, z, pw)
        assert not (lay.root / bk.STAGING).exists()
        assert not (lay.data / "ledger" / "planner.db").exists()  # untouched

    res = bk.restore(lay, z, PASSWORD)
    assert res.files == len(names) - 1 and res.previous == lay.root / "data-previous"
    assert _wages(lay) == [41000.0]
    assert (lay.data / "inbox" / "note.txt").read_text(encoding="utf-8") == "synthetic"
    assert (res.previous / "inbox" / "note.txt").read_text(
        encoding="utf-8"
    ) == "changed"
    with pytest.raises(bk.BackupError, match="still there"):
        bk.restore(lay, z, PASSWORD)
    assert bk.settle(lay) and not res.previous.exists()
    assert not bk.settle(lay)


def test_plain_backup_and_undo(lay: Layout, tmp_path: Path) -> None:
    z = bk.backup(lay, tmp_path / "plain.zip")
    assert not bk.encrypted(z)
    _wreck(lay)
    bk.restore(lay, z)
    assert _wages(lay) == [41000.0]
    assert bk.undo(lay)
    assert not (lay.data / "ledger" / "planner.db").exists()  # the wrecked data
    assert not (lay.root / "data-previous").exists() and not bk.undo(lay)


def _zip(path: Path, entries: dict[str, bytes], manifest: bool = True) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        if manifest:
            files = {n: bk._sha(d) for n, d in entries.items()}
            zf.writestr("backup.json", json.dumps({"files": files}))
    return path


@pytest.mark.parametrize(
    ("entries", "manifest", "match"),
    [
        ({"../evil.txt": b"x"}, True, "leaves the folder"),
        ({"/etc/evil": b"x"}, True, "leaves the folder"),
        ({"C:/evil": b"x"}, True, "drive or backslash"),
        # Windows' zipfile turns the backslashes into slashes on read
        ({"data\\..\\evil": b"x"}, True, "drive or backslash|leaves the folder"),
        ({"python/planner.cmd": b"x"}, True, "unexpected entry"),
        ({"data/a.txt": b"x"}, False, "no backup.json"),
        ({"data/ledger/planner.db": b"not sqlite"}, True, "planner.db"),
        ({"config/thresholds.yaml": b"2026: {}\n"}, True, "no data/ folder"),
    ],
)
def test_hostile_or_damaged_archives_are_refused(
    lay: Layout,
    tmp_path: Path,
    entries: dict[str, bytes],
    manifest: bool,
    match: str,
) -> None:
    z = _zip(tmp_path / "x.zip", entries, manifest)
    with pytest.raises(bk.BackupError, match=match):
        bk.restore(lay, z)
    assert _wages(lay) == [41000.0]
    assert not (lay.root / bk.STAGING).exists()
    assert not (lay.root / "data-previous").exists()
    assert not (tmp_path / "evil.txt").exists()


def test_symlinks_tampering_and_size_are_refused(
    lay: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    z = tmp_path / "link.zip"
    with zipfile.ZipFile(z, "w") as zf:
        info = zipfile.ZipInfo("data/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
        zf.writestr("backup.json", "{}")
    with pytest.raises(bk.BackupError, match="symlink"):
        bk.restore(lay, z)

    good = bk.backup(lay, tmp_path / "good.zip")
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(tampered, "w") as dst:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "data/inbox/note.txt":
                data = b"tampered"
            dst.writestr(info.filename, data)
    with pytest.raises(bk.BackupError, match="damaged"):
        bk.restore(lay, tampered)
    with zipfile.ZipFile(tampered, "a") as zf:
        zf.writestr("data/extra.txt", b"x")
    with pytest.raises(bk.BackupError, match="manifest"):
        bk.restore(lay, tampered)

    monkeypatch.setattr(bk, "MAX_BYTES", 10)
    with pytest.raises(bk.BackupError, match="unpacks to more than"):
        bk.restore(lay, good)
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    with pytest.raises(bk.BackupError, match="not a zip"):
        bk.restore(lay, tmp_path / "junk.zip")
    assert _wages(lay) == [41000.0]


def test_hand_limits_in_the_backup_reach_the_live_config(
    lay: Layout, tmp_path: Path
) -> None:
    thresholds = lay.config / "thresholds.yaml"
    shipped = thresholds.read_text(encoding="utf-8")
    thresholds.write_text(shipped + HAND_ROW, encoding="utf-8")
    z = bk.backup(lay, tmp_path / "b.zip")
    thresholds.write_text(shipped, encoding="utf-8")  # a new release's config
    res = bk.restore(lay, z)
    assert res.carried == ["2031.my_typed_limit"]
    assert "my_typed_limit" in thresholds.read_text(encoding="utf-8")


def test_cli_backup_restore_and_run_keeps_the_restore(lay: Layout) -> None:
    r = runner.invoke(app, ["backup", "--plain"])
    assert r.exit_code == 0, r.output
    assert "NOT encrypted" in r.output
    plain = next((lay.out / "backups").glob("planner-backup-*.zip"))
    r = runner.invoke(app, ["backup", str(lay.out / "s.zip")], input="pw1\npw1\n")
    assert r.exit_code == 0 and "AES-256" in r.output, r.output

    _wreck(lay)
    r = runner.invoke(app, ["restore", str(lay.out / "s.zip")], input="nope\n")
    assert r.exit_code == 1 and "restore refused: wrong or missing password" in r.output
    r = runner.invoke(app, ["restore", str(lay.out / "s.zip")], input="pw1\n")
    assert r.exit_code == 0, r.output
    assert "restored" in r.output and "restore --undo" in r.output
    assert _wages(lay) == [41000.0]
    r = runner.invoke(app, ["restore", str(plain)])
    assert r.exit_code == 1 and "still there" in r.output

    r = runner.invoke(app, ["run", "--quiet", "--no-update-check", "--year", "2025"])
    assert r.exit_code == 0, r.output
    assert "restore kept: data-previous/ removed" in r.output
    r = runner.invoke(app, ["restore", "--undo"])
    assert r.exit_code == 0 and "nothing to undo" in r.output


def test_restore_and_undo_let_go_of_the_lock_before_data_moves(
    lay: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows cannot rename a folder that holds an open file: planner.lock is
    released around the swap, then taken again for the rest of the command."""
    z = bk.backup(lay, tmp_path / "b.zip")
    seen: list[bool] = []
    real = Path.rename

    def spy(self: Path, target: str | Path) -> Path:
        if self == lay.data:
            seen.append(lock.held)
        return real(self, target)

    monkeypatch.setattr(Path, "rename", spy)
    with lay.lock() as lock:
        bk.restore(lay, z)
        assert lock.held and lay.lock_file.exists()
        assert bk.undo(lay)
        assert lock.held
        with pytest.raises(paths.WriterBusyError):
            lay.lock().acquire()
    assert seen == [False, False]


def test_restore_into_a_folder_with_no_data_keeps_no_previous(
    planner_home: Path, lay: Layout, tmp_path: Path
) -> None:
    z = bk.backup(lay, tmp_path / "b.zip")
    shutil.rmtree(lay.data)
    r = runner.invoke(app, ["restore", str(z)])
    assert r.exit_code == 0, r.output
    assert "data-previous" not in r.output
    assert _wages(lay) == [41000.0]
