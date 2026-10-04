"""``planner update``: swap in a newer release's folders, keeping the old ones.

The unit of update is a release zip from the project's GitHub Releases page, whose
sha256 is published beside it. The flow never touches the live folders until the
candidate has passed the same selfcheck the live install passes:

    python-candidate/   the zip extracted; selfcheck run with its own interpreter
    python/ planner/ config/ templates/ + planner.cmd LICENSE README.md VERSION
                        live, swapped only after the candidate passes
    python-previous/    the last live set, restored by ``planner update --rollback``

``data/`` and ``out/`` are never touched by an update or a rollback.

On Windows a running python.exe locks ``python/``, so the folders cannot move
while this process lives. There, :func:`stage` verifies and self-checks the
candidate, :func:`write_swap` writes ``data/update/swap.cmd``, and the process
exits with :data:`LAUNCHER_SWAP`; ``planner.cmd`` then calls that script, which
moves the folders (putting everything back if one is in use) and runs
``planner update --finish`` from the new interpreter. Elsewhere (a developer
clone) the swap happens in-process.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CANDIDATE = "python-candidate"
LIVE = "python"
PREVIOUS = "python-previous"
SWAPPED_DIRS = ("python", "planner", "config", "templates")
SWAPPED_FILES = ("planner.cmd", "LICENSE", "README.md", "VERSION")
READY = "READY.json"
LAUNCHER_SWAP = 75
SWAP_CMD = r"""@echo off
rem Written by "planner update" and rewritten on every update. planner.cmd
rem calls it on exit code 75, once no planner process runs from the folders.
rem R: planner folder, S: folder moving in, P: where the live set is parked.
setlocal
set "R=%~dp0..\..\"
set "S={src}"
set "P={park}"
set "I={items}"
rem a second for python.exe (and any virus scan) to let go of python\
ping -n 2 127.0.0.1 >nul
if exist "%R%%P%" rmdir /s /q "%R%%P%"
mkdir "%R%%P%" || goto :held
for %%f in (%I%) do if exist "%R%%%f" move "%R%%%f" "%R%%P%\" >nul || goto :undo
for %%f in (%I%) do if exist "%R%%S%\%%f" move "%R%%S%\%%f" "%R%" >nul || goto :undo
"%R%python\python.exe" -m planner update --finish
if errorlevel 1 exit /b %errorlevel%
{rerun}
exit /b %errorlevel%
:undo
for %%f in (%I%) do if exist "%R%%P%\%%f" (
  if exist "%R%%%f" move "%R%%%f" "%R%%S%\" >nul
  move "%R%%P%\%%f" "%R%" >nul
)
:held
echo update not applied: a planner file is in use. 1>&2
echo Close every other planner window and run the command again. 1>&2
exit /b 1
"""


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


def installed_version(root: Path) -> str:
    """The release's VERSION file; a developer clone has none."""
    from planner import __version__

    v = root / "VERSION"
    return v.read_text(encoding="utf-8").strip() if v.exists() else __version__


def version_key(version: str) -> tuple[int, ...]:
    """``v0.2.10`` -> (0, 2, 10); anything non-numeric sorts lowest."""
    try:
        return tuple(int(p) for p in version.strip().lstrip("v").split("."))
    except ValueError:
        return ()


def carry_thresholds(root: Path, cand: Path) -> list[str]:
    """Limits typed into the live config/thresholds.yaml that the new release
    lacks are copied into it, so an update never drops a hand-entered row.
    Rows both have keep the release's (maintained, sourced) value."""
    from planner.config import load_thresholds

    live, new = root / "config" / "thresholds.yaml", cand / "config" / "thresholds.yaml"
    if not live.exists() or not new.exists():
        return []
    mine = load_thresholds(live, merged=False)
    theirs = load_thresholds(new, merged=False)
    added: list[str] = []
    for year, rows in mine.items():
        for name, row in rows.items():
            if name not in theirs.get(year, {}):
                theirs.setdefault(year, {})[name] = row
                added.append(f"{year}.{name}")
    if added:
        new.write_text(
            yaml.safe_dump(dict(sorted(theirs.items())), sort_keys=False),
            encoding="utf-8",
        )
    return added


def stage(
    zip_path: Path, expected_sha256: str, root: Path, python_exe: str = "python.exe"
) -> UpdateResult:
    """Verify, extract and self-check the candidate, carry hand limits into it
    and mark it ready. Nothing live is touched."""
    verify_zip(zip_path, expected_sha256)
    cand = extract_candidate(zip_path, root)
    check = selfcheck_candidate(cand, python_exe)
    version = (cand / "VERSION").read_text(encoding="utf-8").strip()
    carried = carry_thresholds(root, cand)
    ready = {"version": version, "selfcheck": check, "carried": carried}
    (cand / READY).write_text(json.dumps(ready), encoding="utf-8")
    return UpdateResult(version=version, selfcheck=check)


def staged(root: Path) -> dict[str, Any] | None:
    ready = root / CANDIDATE / READY
    if not ready.exists():
        return None
    data: dict[str, Any] = json.loads(ready.read_text(encoding="utf-8"))
    return data


def launcher_swaps(root: Path) -> bool:
    """True when planner.cmd started this process from a release folder, so
    the folders must move after it exits."""
    return os.environ.get("PLANNER_LAUNCHER") == "cmd" and (root / LIVE).is_dir()


def write_swap(root: Path, mode: str, rerun: bool) -> Path:
    """``mode`` "in" (candidate -> live, live kept as previous) or "back"
    (previous -> live, live parked in the candidate folder and deleted).
    ``rerun`` runs the original command again on the new release."""
    src, park = (CANDIDATE, PREVIOUS) if mode == "in" else (PREVIOUS, CANDIDATE)
    folder = root / "data" / "update"
    folder.mkdir(parents=True, exist_ok=True)
    pending = {"mode": mode, "from": installed_version(root)}
    (folder / "pending.json").write_text(json.dumps(pending), encoding="utf-8")
    text = SWAP_CMD.format(
        src=src,
        park=park,
        items=" ".join(SWAPPED_DIRS + SWAPPED_FILES),
        rerun='"%R%python\\python.exe" -m planner %*' if rerun else "rem",
    )
    script = folder / "swap.cmd"
    script.write_bytes(text.replace("\n", "\r\n").encode("ascii"))
    return script


def finish(root: Path) -> str:
    """Run by swap.cmd from the new interpreter: report and tidy up."""
    folder = root / "data" / "update"
    pending_file = folder / "pending.json"
    if not pending_file.exists():
        raise UpdateError("no update is waiting to finish")
    pending = json.loads(pending_file.read_text(encoding="utf-8"))
    now = installed_version(root)
    if pending["mode"] == "in":
        # swap.cmd moved only the release items; READY.json stayed behind
        ready = json.loads((root / CANDIDATE / READY).read_text(encoding="utf-8"))
        shutil.rmtree(root / CANDIDATE, ignore_errors=True)
        msg = f"updated {pending['from']} -> {now}; candidate selfcheck: "
        msg += ready["selfcheck"]
        if ready.get("carried"):
            msg += f"; kept your limits: {', '.join(ready['carried'])}"
    else:
        shutil.rmtree(root / CANDIDATE, ignore_errors=True)
        shutil.rmtree(root / PREVIOUS, ignore_errors=True)
        msg = f"rolled back to {now}"
    pending_file.unlink()
    # swap.cmd stays: cmd is still running it (the rerun follows this call)
    # and reads each line from the file; write_swap rewrites it next time
    return msg


def apply_staged(root: Path) -> str:
    """In-process swap of a staged candidate (no launcher)."""
    ready = staged(root)
    if ready is None:
        raise UpdateError("no update is staged")
    before = installed_version(root)
    (root / CANDIDATE / READY).unlink()
    swap_in(root / CANDIDATE, root)
    v, check = ready["version"], ready["selfcheck"]
    return f"updated {before} -> {v}; candidate selfcheck: {check}"


def apply(
    zip_path: Path, expected_sha256: str, root: Path, python_exe: str = "python.exe"
) -> UpdateResult:
    result = stage(zip_path, expected_sha256, root, python_exe)
    (root / CANDIDATE / READY).unlink()
    swap_in(root / CANDIDATE, root)
    return result
