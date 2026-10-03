"""``planner update``: swap in a newer release's folders, keeping the old ones.

The unit of update is a release zip from the project's GitHub Releases page, whose
sha256 is published beside it. The flow never touches the live folders until the
candidate has passed the same selfcheck the live install passes:

    python-candidate/   the zip extracted; selfcheck run with its own interpreter
    python/ planner/ config/ templates/ + planner.cmd LICENSE README.md VERSION
                        live, swapped only after the candidate passes
    python-previous/    the last live set, restored by ``planner update --rollback``

``data/`` and ``out/`` are never touched by an update or a rollback.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

CANDIDATE = "python-candidate"
LIVE = "python"
PREVIOUS = "python-previous"
SWAPPED_DIRS = ("python", "planner", "config", "templates")
SWAPPED_FILES = ("planner.cmd", "LICENSE", "README.md", "VERSION")


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateResult:
    version: str
    selfcheck: str


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_zip(zip_path: Path, expected_sha256: str) -> None:
    actual = sha256(zip_path)
    if actual.lower() != expected_sha256.strip().lower():
        raise UpdateError(
            f"{zip_path.name}: sha256 {actual} != expected {expected_sha256}"
        )


def extract_candidate(zip_path: Path, root: Path) -> Path:
    cand = root / CANDIDATE
    if cand.exists():
        shutil.rmtree(cand)
    cand.mkdir()
    base = cand.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            target = (cand / member.filename).resolve()
            if target != base and base not in target.parents:
                raise UpdateError(f"zip entry escapes the folder: {member.filename}")
        zf.extractall(cand)
        # zipfile drops unix mode bits on extract; restore them (no-op on Windows)
        for member in zf.infolist():
            mode = (member.external_attr >> 16) & 0o777
            if mode and not member.is_dir():
                (cand / member.filename).chmod(mode)
    if not (cand / "VERSION").exists() or not (cand / LIVE).is_dir():
        raise UpdateError("zip is not a planner release (no VERSION or python/)")
    return cand


def selfcheck_candidate(cand: Path, python_exe: str = "python.exe") -> str:
    """Run the candidate's own interpreter against the candidate's own package."""
    exe = cand / LIVE / python_exe
    if not exe.exists():
        raise UpdateError(f"candidate interpreter missing: {exe}")
    proc = subprocess.run(  # noqa: S603
        [str(exe), "-m", "planner", "selfcheck"],
        cwd=cand,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        raise UpdateError(f"candidate selfcheck failed:\n{proc.stdout}{proc.stderr}")
    return proc.stdout.strip()


def _move(src: Path, dst: Path) -> None:
    if src.exists():
        shutil.move(str(src), str(dst))


def swap_in(cand: Path, root: Path) -> None:
    """candidate -> live, live -> previous. Data folders are never touched."""
    prev = root / PREVIOUS
    if prev.exists():
        shutil.rmtree(prev)
    prev.mkdir()
    for name in SWAPPED_DIRS + SWAPPED_FILES:
        _move(root / name, prev / name)
    for name in SWAPPED_DIRS + SWAPPED_FILES:
        _move(cand / name, root / name)
    shutil.rmtree(cand, ignore_errors=True)


def rollback(root: Path) -> str:
    prev = root / PREVIOUS
    if not (prev / "VERSION").exists():
        raise UpdateError("nothing to roll back to (no python-previous/VERSION)")
    broken = root / CANDIDATE
    if broken.exists():
        shutil.rmtree(broken)
    broken.mkdir()
    for name in SWAPPED_DIRS + SWAPPED_FILES:
        _move(root / name, broken / name)
    for name in SWAPPED_DIRS + SWAPPED_FILES:
        _move(prev / name, root / name)
    shutil.rmtree(prev, ignore_errors=True)
    shutil.rmtree(broken, ignore_errors=True)
    return (root / "VERSION").read_text(encoding="utf-8").strip()


def apply(
    zip_path: Path, expected_sha256: str, root: Path, python_exe: str = "python.exe"
) -> UpdateResult:
    verify_zip(zip_path, expected_sha256)
    cand = extract_candidate(zip_path, root)
    check = selfcheck_candidate(cand, python_exe)
    version = (cand / "VERSION").read_text(encoding="utf-8").strip()
    swap_in(cand, root)
    return UpdateResult(version=version, selfcheck=check)
