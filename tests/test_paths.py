from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner import cli, paths
from planner.ledger import db
from planner.paths import (
    DATA_SUBDIRS,
    CloudSyncedPathError,
    Layout,
    WriterBusyError,
    is_cloud_synced,
    layout,
)

runner = CliRunner()


def test_layout_creates_every_data_subdir(planner_home: Path) -> None:
    lay = layout()
    assert lay.root == planner_home.resolve()
    lay.ensure()
    for sub in DATA_SUBDIRS:
        assert (lay.data / sub).is_dir()
    assert lay.out.is_dir()
    assert lay.lock_file.parent == lay.data


@pytest.mark.parametrize(
    "parts",
    [
        ("Users", "jp", "OneDrive", "Planner"),
        ("Dropbox", "x"),
        ("Google Drive", "My Drive"),
    ],
)
def test_cloud_sync_paths_are_detected(tmp_path: Path, parts: tuple[str, ...]) -> None:
    p = tmp_path.joinpath(*parts)
    assert is_cloud_synced(p)


def test_local_path_is_not_cloud_synced(tmp_path: Path) -> None:
    assert not is_cloud_synced(tmp_path / "Planner")


def test_ensure_refuses_cloud_synced_root(tmp_path: Path) -> None:
    root = tmp_path / "OneDrive" / "Planner"
    root.mkdir(parents=True)
    with pytest.raises(CloudSyncedPathError):
        Layout(root).ensure()
    assert not (root / "data").exists()


def test_second_writer_exits_with_message(planner_home: Path) -> None:
    lay = layout()
    with lay.lock():
        r = runner.invoke(cli.app, ["ingest"])
        assert r.exit_code == 2
        assert "another planner is running" in r.output
        # a command that never touches data/ is not held up
        assert runner.invoke(cli.app, ["version"]).exit_code == 0
        # a backup only reads data/, so it runs beside the dashboard
        assert runner.invoke(cli.app, ["backup", "--plain"]).exit_code == 0
    # released: the next writer gets in
    r = runner.invoke(cli.app, ["ingest"])
    assert r.exit_code == 0, r.output
    assert "another planner is running" not in r.output


def test_writer_lock_is_released_when_a_command_ends_or_fails(
    planner_home: Path,
) -> None:
    lay = layout()
    assert runner.invoke(cli.app, ["needed", "--year", "1800"]).exit_code != 0
    with lay.lock() as lock:  # the failed command let go
        assert lock.held
    assert not lock.held


def test_lock_is_exclusive_and_reusable(planner_home: Path) -> None:
    lay = layout()
    first = lay.lock()
    first.acquire()
    first.acquire()  # holding it twice is not a second lock
    with pytest.raises(WriterBusyError, match="another planner is running"):
        lay.lock().acquire()
    first.release()
    first.release()  # releasing twice is harmless
    with lay.lock() as again:
        assert again.held


def test_a_lock_file_left_by_a_dead_process_does_not_block(
    planner_home: Path,
) -> None:
    lay = layout()
    code = (
        "import os, sys\n"
        "from planner.paths import layout\n"
        "layout().lock().acquire()\n"
        "print('held', flush=True)\n"
        "os._exit(0)\n"  # no cleanup: the file stays behind
    )
    env = {**os.environ, "PLANNER_HOME": str(planner_home)}
    out = subprocess.run(  # noqa: S603  (fixed argv)
        [sys.executable, "-c", code], env=env, capture_output=True, text=True
    )
    assert out.stdout.strip() == "held", out.stderr
    assert lay.lock_file.exists()  # the stale file is still there ...
    with lay.lock() as lock:  # ... and does not stop the next planner
        assert lock.held


def test_a_live_process_blocks_a_second_one(planner_home: Path) -> None:
    lay = layout()
    code = (
        "import sys\n"
        "from planner.paths import layout\n"
        "layout().lock().acquire()\n"
        "print('held', flush=True)\n"
        "sys.stdin.readline()\n"
    )
    env = {**os.environ, "PLANNER_HOME": str(planner_home)}
    proc = subprocess.Popen(  # noqa: S603  (fixed argv)
        [sys.executable, "-c", code],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "held"
        r = runner.invoke(cli.app, ["ingest"])
        assert r.exit_code == 2 and "another planner is running" in r.output
    finally:
        proc.communicate("\n", timeout=30)
    with lay.lock():
        pass


def test_lock_refuses_a_cloud_synced_folder_without_making_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "OneDrive" / "Planner"
    home.mkdir(parents=True)
    monkeypatch.setenv("PLANNER_HOME", str(home))
    with pytest.raises(CloudSyncedPathError):
        layout().lock()
    r = runner.invoke(cli.app, ["ingest"])
    assert r.exit_code == 2 and "refused" in r.output
    assert not (home / "data").exists()


def test_every_unlocked_command_exists() -> None:
    names = {
        c.name or (c.callback.__name__ if c.callback else "").replace("_", "-")
        for c in cli.app.registered_commands
    }
    assert cli.NO_LOCK <= names
    assert "init" in names


def test_help_for_a_writing_command_works_while_it_is_locked(
    planner_home: Path,
) -> None:
    with layout().lock():
        r = runner.invoke(cli.app, ["ingest", "--help"])
    assert r.exit_code == 0 and "Usage" in r.output


@pytest.mark.parametrize("command", ["facts", "rows"])
def test_listing_commands_read_a_busy_ledger_without_writing(
    planner_home: Path, command: str
) -> None:
    """``facts`` and ``rows`` take no lock, so they must never write the ledger:
    a writer in mid-transaction (a scheduled ``planner run``) does not make
    them fail, and they change nothing on disk."""
    db_path = layout().data / "ledger" / "planner.db"
    db.connect(db_path).close()
    before = db_path.read_bytes()
    writer = sqlite3.connect(db_path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        r = runner.invoke(cli.app, [command])
        assert r.exit_code == 0, r.output
        assert time.monotonic() - started < 2
        assert r.output.strip().endswith(f"0 {command}")
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert db_path.read_bytes() == before


@pytest.mark.parametrize("command", ["facts", "rows"])
def test_listing_commands_do_not_create_a_missing_ledger(
    planner_home: Path, command: str
) -> None:
    r = runner.invoke(cli.app, [command])
    assert r.exit_code == 0, r.output
    assert r.output.strip().endswith(f"0 {command}")
    assert not (layout().data / "ledger").exists()


@pytest.mark.parametrize("command", ["facts", "rows"])
def test_listing_commands_refuse_an_out_of_date_ledger(
    planner_home: Path, command: str
) -> None:
    db_path = layout().data / "ledger" / "planner.db"
    conn = db.connect(db_path)
    conn.execute("UPDATE schema_version SET version = version - 1")
    conn.commit()
    conn.close()
    r = runner.invoke(cli.app, [command])
    assert r.exit_code == 1
    assert "older planner" in r.output


def test_windows_branch_locks_the_first_byte(
    planner_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """msvcrt.locking is Windows-only; a stand-in shows the calls made there."""
    calls: list[tuple[int, int]] = []
    busy = False

    def locking(fd: int, mode: int, nbytes: int) -> None:
        calls.append((mode, nbytes))
        if busy and mode == 2:
            raise OSError(36, "Resource deadlock avoided")

    fake = types.SimpleNamespace(LK_UNLCK=0, LK_NBLCK=2, locking=locking)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    lay = layout()
    with lay.lock():
        pass
    assert calls == [(2, 1), (0, 1)]
    busy = True
    with pytest.raises(WriterBusyError):
        lay.lock().acquire()
    assert paths._HELD == {}


def _deep_release(root: Path, depth: int) -> str:
    """A release-like folder whose recorded deepest file is ``depth`` characters."""
    rel = "python\\Lib\\" + "x" * (depth - len("python\\Lib\\"))
    (root / "python").mkdir(parents=True, exist_ok=True)
    (root / paths.LONGEST_FILE).write_text(rel + "\n", encoding="utf-8")
    return rel


def test_too_deep_for_windows_is_refused_only_with_long_paths_off(
    tmp_path: Path,
) -> None:
    """Phase 10, unit 7f: past 259 characters with long paths off (the Windows
    default) a file cannot be opened, so the planner names the limit and a
    shorter folder before any step can fail half way."""
    paths.refuse_too_long(tmp_path, enabled=False)  # a checkout records nothing
    room = paths.MAX_PATH - 1 - len(str(tmp_path))
    _deep_release(tmp_path, room)
    paths.refuse_too_long(tmp_path, enabled=False)  # exactly at the limit
    _deep_release(tmp_path, room + 1)
    with pytest.raises(paths.PathTooLongError) as exc:
        paths.refuse_too_long(tmp_path, enabled=False)
    msg = str(exc.value)
    assert "too deep for Windows" in msg and f"{paths.MAX_PATH + 1}-character" in msg
    assert f"at most {len(str(tmp_path)) - 1} characters" in msg
    paths.refuse_too_long(tmp_path, enabled=True)
    if sys.platform != "win32":
        assert paths.long_paths_enabled()
        paths.refuse_too_long(tmp_path)


def test_long_paths_setting_is_read_from_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class Key:
        def __enter__(self) -> Key:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def open_key(*args: object) -> Key:
        calls.append(args)
        return Key()

    value = {"v": 1}
    reg = types.SimpleNamespace(
        HKEY_LOCAL_MACHINE="HKLM",
        OpenKey=open_key,
        QueryValueEx=lambda key, name: (value["v"], 4),
    )
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", reg)
    assert paths.long_paths_enabled()
    assert calls == [("HKLM", r"SYSTEM\CurrentControlSet\Control\FileSystem")]
    value["v"] = 0
    assert not paths.long_paths_enabled()

    def missing(*args: object) -> Key:
        raise FileNotFoundError(args)

    monkeypatch.setattr(reg, "OpenKey", missing)
    assert not paths.long_paths_enabled()


def test_cli_refuses_a_too_deep_folder_but_still_says_its_version(
    planner_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = layout().root
    _deep_release(root, paths.MAX_PATH - len(str(root)))
    monkeypatch.setattr(paths, "long_paths_enabled", lambda: False)
    for args in (["selfcheck"], ["run", "--quiet"], ["check-config"]):
        res = runner.invoke(cli.app, args)
        assert res.exit_code == 2, (args, res.output)
        assert "refused:" in res.output and "too deep for Windows" in res.output
    assert runner.invoke(cli.app, ["version"]).exit_code == 0
    monkeypatch.setattr(paths, "long_paths_enabled", lambda: True)
    assert runner.invoke(cli.app, ["check-config"]).exit_code == 0
