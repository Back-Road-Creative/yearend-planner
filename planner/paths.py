"""Folder layout and the rules that keep personal data in one place.

The planner lives in one folder: the one holding ``planner.cmd``. Everything
personal is under ``data/`` and ``out/`` inside it. Both are gitignored.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import IO

# Windows 11 moves Desktop/Documents into OneDrive by default (Known Folder
# Move). A planner folder under any sync client would copy the ledger to the
# cloud, so init refuses those paths. Matched case-insensitively on each part.
CLOUD_SYNC_PARTS = (
    "onedrive",
    "dropbox",
    "icloud drive",
    "iclouddrive",
    "google drive",
    "my drive",
)

DATA_SUBDIRS = (
    "profile",
    "inbox",
    "inbox/UNMATCHED",
    "ledger",
    "manual",
    "private",
    "private/returns",
    "archive",
)


# Windows refuses a path past 259 characters unless long paths are turned on
# (LongPathsEnabled). The release records its deepest file in python/LONGEST_PATH
# (scripts/build_release.py); in a folder so deep that file passes the limit,
# an import fails part way with a message that names neither cause nor cure,
# so every command but version refuses the folder first.
MAX_PATH = 259
LONGEST_FILE = "python/LONGEST_PATH"


class CloudSyncedPathError(RuntimeError):
    """The planner folder sits inside a cloud-sync client's tree."""


class PathTooLongError(RuntimeError):
    """The planner folder is too deep for Windows with long paths off."""


class WriterBusyError(RuntimeError):
    """Another planner process holds the single-writer lock."""


def _try_lock(fh: IO[bytes]) -> bool:
    """Take an exclusive, non-blocking OS lock on the file's first byte.

    The operating system drops the lock when the process ends, however it ends,
    so a lock file left behind by a dead process never blocks anyone.
    """
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fh: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# The locks this process holds, by lock-file path, so ``planner restore`` can
# let go of data/planner.lock before it renames data/ (Windows refuses to
# rename a folder that holds an open file).
_HELD: dict[Path, WriterLock] = {}


class WriterLock:
    """The single-writer lock: ``with layout().lock():`` or acquire/release."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[bytes] | None = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> None:
        """Take the lock or raise :class:`WriterBusyError`."""
        if self._fh is not None:
            return
        fh = self.path.open("a+b")
        try:
            got = _try_lock(fh)
        except BaseException:
            fh.close()
            raise
        if not got:
            fh.close()
            raise WriterBusyError(
                "another planner is running (the dashboard, a scheduled run or "
                "another window has data/ open). Close it or wait for it to "
                "finish, then run again."
            )
        self._fh = fh
        _HELD[self.path] = self

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if _HELD.get(self.path) is self:
            del _HELD[self.path]
        if fh is None:
            return
        try:
            _unlock(fh)
        finally:
            fh.close()

    def __enter__(self) -> WriterLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def home() -> Path:
    """The planner folder: ``PLANNER_HOME`` if set, else the folder above this
    package."""
    env = os.environ.get("PLANNER_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def long_paths_enabled() -> bool:
    """Windows' LongPathsEnabled is 1; always true off Windows."""
    if sys.platform != "win32":
        return True
    import winreg

    key = r"SYSTEM\CurrentControlSet\Control\FileSystem"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            return bool(winreg.QueryValueEx(k, "LongPathsEnabled")[0] == 1)
    except OSError:
        return False


def refuse_too_long(root: Path, enabled: bool | None = None) -> None:
    """Raise :class:`PathTooLongError` when the release's deepest file, under
    ``root``, passes MAX_PATH and long paths are off. A developer checkout has
    no python/LONGEST_PATH and is never refused."""
    try:
        rel = (root / LONGEST_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return
    full = len(str(root)) + 1 + len(rel)
    if not rel or full <= MAX_PATH:
        return
    if long_paths_enabled() if enabled is None else enabled:
        return
    raise PathTooLongError(
        f"{root} is too deep for Windows: the planner's deepest file would have "
        f"a {full}-character path, past the {MAX_PATH}-character limit. Move the "
        f"planner folder to a shorter path (at most {MAX_PATH - 1 - len(rel)} "
        "characters), e.g. C:\\Planner, or turn on Windows long paths, and run "
        "again."
    )


def is_cloud_synced(path: Path) -> bool:
    parts = [p.lower() for p in path.resolve().parts]
    return any(part in parts for part in (c.lower() for c in CLOUD_SYNC_PARTS))


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def out(self) -> Path:
        return self.root / "out"

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def lock_file(self) -> Path:
        return self.data / "planner.lock"

    def _refuse_cloud(self) -> None:
        if is_cloud_synced(self.root):
            raise CloudSyncedPathError(
                f"{self.root} is inside a cloud-sync folder (OneDrive, Dropbox, ...). "
                "Move the planner folder somewhere local, e.g. C:\\Planner, "
                "and run again."
            )

    def lock(self) -> WriterLock:
        """The single-writer lock on ``data/planner.lock`` (not yet taken: use
        it as ``with lay.lock():``). Makes ``data/`` but not in a cloud-synced
        folder, which is refused as :meth:`ensure` refuses it."""
        self._refuse_cloud()
        self.data.mkdir(parents=True, exist_ok=True)
        return WriterLock(self.lock_file)

    def release_lock(self) -> WriterLock | None:
        """Let go of the lock this process holds on this folder (the caller
        takes it back with ``acquire()``); None when it holds none."""
        held = _HELD.get(self.lock_file)
        if held is None:
            return None
        held.release()
        return held

    def ensure(self) -> None:
        """Create ``data/`` and ``out/``; refuse a cloud-synced folder."""
        self._refuse_cloud()
        for sub in DATA_SUBDIRS:
            (self.data / sub).mkdir(parents=True, exist_ok=True)
        self.out.mkdir(exist_ok=True)


def layout() -> Layout:
    return Layout(home())
