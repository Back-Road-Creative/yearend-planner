"""``planner backup`` / ``planner restore``: one zip of ``data/`` and ``config/``.

A backup holds every file under ``data/`` (the ledger copied through sqlite's
backup API, so a live dashboard cannot tear it) and ``config/``, plus
``backup.json`` listing each file's sha256. With a password the zip is AES-256
(pyzipper, WinZip AES); without one it is plain and the command says so.

A restore trusts nothing in the archive until it has checked it: every entry
must sit under ``data/`` or ``config/`` (no absolute paths, drive letters,
``..`` or symlinks), the total stays under :data:`MAX_BYTES`, and every file
must match the manifest after it is unpacked to ``restore-staging/``. Only then
is the live ``data/`` parked as ``data-previous/`` and the staged one moved in.
``config/`` belongs to the release, so only limits typed into the backed-up
``thresholds.yaml`` that the live one lacks are carried over. The next clean
``planner run`` deletes ``data-previous/``; ``planner restore --undo`` puts it
back until then.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import stat
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

import pyzipper

from planner.paths import Layout, WriterBusyError

MANIFEST = "backup.json"
STAGING = "restore-staging"
PREVIOUS = "data-previous"
TOPS = ("data", "config")
MAX_BYTES = 4 << 30  # 4 GiB unpacked
MAX_FILES = 200_000
# rebuilt on the next run or never needed again
SKIP = ("data/update", "data/planner.lock")


class BackupError(RuntimeError):
    pass


@dataclass
class Restored:
    files: int
    carried: list[str] = field(default_factory=list)
    previous: Path | None = None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _skipped(rel: str) -> bool:
    return any(rel == s or rel.startswith(s + "/") for s in SKIP) or rel.endswith(
        ("-journal", "-wal", "-shm")
    )


def _read(path: Path) -> bytes:
    """A file's bytes; a sqlite database through the backup API."""
    if path.suffix == ".db":
        src = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            mem = sqlite3.connect(":memory:")
            src.backup(mem)
            return mem.serialize()
        finally:
            src.close()
    return path.read_bytes()


def backup(lay: Layout, dest: Path | None = None, password: str = "") -> Path:
    """Write the backup zip and return its path (default
    ``out/backups/planner-backup-<stamp>.zip``)."""
    if dest is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = lay.out / "backups" / f"planner-backup-{stamp}.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    files: dict[str, str] = {}
    with pyzipper.AESZipFile(
        tmp,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES if password else None,
    ) as zf:
        if password:
            zf.setpassword(password.encode("utf-8"))
        for top in TOPS:
            base = lay.root / top
            for path in sorted(p for p in base.rglob("*") if p.is_file()):
                rel = path.relative_to(lay.root).as_posix()
                if _skipped(rel) or path.is_symlink():
                    continue
                data = _read(path)
                files[rel] = _sha(data)
                zf.writestr(rel, data)
        meta = {"created": datetime.now().isoformat(timespec="seconds"), "files": files}
        zf.writestr(MANIFEST, json.dumps(meta, indent=1))
    tmp.replace(dest)
    return dest


def encrypted(src: Path) -> bool:
    with zipfile.ZipFile(src) as zf:
        return any(i.flag_bits & 0x1 for i in zf.infolist())


def _check_entry(info: zipfile.ZipInfo) -> None:
    name = info.filename
    if "\\" in name or ":" in name:
        raise BackupError(
            f"not a planner backup: entry {name!r} has a drive or backslash"
        )
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts:
        raise BackupError(f"not a planner backup: entry {name!r} leaves the folder")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise BackupError(f"not a planner backup: entry {name!r} is a symlink")
    if name != MANIFEST and p.parts[0] not in TOPS:
        raise BackupError(f"not a planner backup: unexpected entry {name!r}")


def validate(src: Path) -> list[zipfile.ZipInfo]:
    """The archive's file entries, or BackupError before anything is unpacked."""
    try:
        with zipfile.ZipFile(src) as zf:
            infos = zf.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupError(f"{src.name}: not a zip file ({exc})") from exc
    if len(infos) > MAX_FILES:
        raise BackupError(f"{src.name}: {len(infos)} entries, more than {MAX_FILES}")
    for info in infos:
        _check_entry(info)
    if sum(i.file_size for i in infos) > MAX_BYTES:
        raise BackupError(f"{src.name}: unpacks to more than {MAX_BYTES >> 30} GiB")
    if MANIFEST not in {i.filename for i in infos}:
        raise BackupError(f"{src.name}: no {MANIFEST}; not made by planner backup")
    return [i for i in infos if not i.is_dir()]


def _unpack(src: Path, password: str, staging: Path) -> dict[str, str]:
    infos = validate(src)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    try:
        with pyzipper.AESZipFile(src) as zf:
            if password:
                zf.setpassword(password.encode("utf-8"))
            meta = json.loads(zf.read(MANIFEST))
            listed: dict[str, str] = meta["files"]
            names = {i.filename for i in infos} - {MANIFEST}
            if names != set(listed):
                extra = sorted(names ^ set(listed))[:3]
                raise BackupError(f"backup does not match its manifest: {extra}")
            for name in sorted(names):
                data = zf.read(name)
                if _sha(data) != listed[name]:
                    raise BackupError(
                        f"{name}: damaged (sha256 differs from the manifest)"
                    )
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
    except RuntimeError as exc:  # pyzipper: bad or missing password
        if isinstance(exc, BackupError):
            raise
        raise BackupError("wrong or missing password") from exc
    except (KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise BackupError(f"{src.name}: unreadable ({exc})") from exc
    for db in (staging / "data").rglob("*.db"):
        conn = sqlite3.connect(db)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            ok = str(exc)
        finally:
            conn.close()
        if ok != "ok":
            raise BackupError(f"{db.relative_to(staging).as_posix()}: {ok}")
    return listed


@contextmanager
def _lock_released(lay: Layout) -> Iterator[None]:
    """Let go of data/planner.lock while data/ is renamed: Windows will not
    rename a folder that holds an open file. The lock is taken again on the way
    out, in whichever data/ is there then; if another planner took it in the
    gap, it keeps it and this command finishes without."""
    held = lay.release_lock()
    if held is not None:
        # a data/ that holds only our lock file is not data worth keeping
        if lay.data.is_dir() and [p.name for p in lay.data.iterdir()] == [
            lay.lock_file.name
        ]:
            lay.lock_file.unlink()
            lay.data.rmdir()
    try:
        yield
    finally:
        if held is not None:
            with suppress(WriterBusyError):
                lay.data.mkdir(parents=True, exist_ok=True)
                held.acquire()


def restore(lay: Layout, src: Path, password: str = "") -> Restored:
    """Check and unpack ``src``, then swap its ``data/`` in. The live data
    stays as ``data-previous/``."""
    from planner.engine.update import carry_thresholds

    staging, previous = lay.root / STAGING, lay.root / PREVIOUS
    if previous.exists():
        raise BackupError(
            f"{PREVIOUS}/ from the last restore is still there: run planner once "
            "(which keeps the restore) or planner restore --undo, then restore again"
        )
    try:
        listed = _unpack(src, password, staging)
        if not (staging / "data").is_dir():
            raise BackupError(f"{src.name}: holds no data/ folder")
        with _lock_released(lay):
            if lay.data.exists():
                try:
                    lay.data.rename(previous)
                except OSError as exc:
                    raise BackupError(
                        "data/ is in use: close the dashboard and other planner "
                        f"windows, then restore again ({exc})"
                    ) from exc
            try:
                (staging / "data").rename(lay.data)
            except OSError:
                if previous.exists():
                    previous.rename(lay.data)
                raise
        carried = carry_thresholds(staging, lay.root)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return Restored(
        files=len(listed),
        carried=carried,
        previous=previous if previous.exists() else None,
    )


def undo(lay: Layout) -> bool:
    """Put ``data-previous/`` back as ``data/``; False when there is none."""
    previous = lay.root / PREVIOUS
    if not previous.is_dir():
        return False
    parked = lay.root / "data-restored"
    if parked.exists():
        shutil.rmtree(parked)
    with _lock_released(lay):
        if lay.data.exists():
            lay.data.rename(parked)
        previous.rename(lay.data)
    shutil.rmtree(parked, ignore_errors=True)
    return True


def settle(lay: Layout) -> bool:
    """After a clean launch: delete ``data-previous/``. True if it existed."""
    previous = lay.root / PREVIOUS
    if not previous.is_dir():
        return False
    shutil.rmtree(previous, ignore_errors=True)
    return True
