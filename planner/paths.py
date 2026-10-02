"""Folder layout and the rules that keep personal data in one place.

The planner lives in one folder: the one holding ``planner.cmd``. Everything
personal is under ``data/`` and ``out/`` inside it. Both are gitignored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

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


class CloudSyncedPathError(RuntimeError):
    """The planner folder sits inside a cloud-sync client's tree."""


def home() -> Path:
    """The planner folder: ``PLANNER_HOME`` if set, else the folder above this
    package."""
    env = os.environ.get("PLANNER_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


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

    def ensure(self) -> None:
        """Create ``data/`` and ``out/``; refuse a cloud-synced folder."""
        if is_cloud_synced(self.root):
            raise CloudSyncedPathError(
                f"{self.root} is inside a cloud-sync folder (OneDrive, Dropbox, ...). "
                "Move the planner folder somewhere local, e.g. C:\\Planner, "
                "and run again."
            )
        for sub in DATA_SUBDIRS:
            (self.data / sub).mkdir(parents=True, exist_ok=True)
        self.out.mkdir(exist_ok=True)


def layout() -> Layout:
    return Layout(home())
