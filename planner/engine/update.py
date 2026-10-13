"""``planner update``: swap in a newer release's folders, keeping the old ones.

The unit of update is a release zip from the project's GitHub Releases page, whose
sha256 is published beside it. The flow never touches the live folders until the
candidate has passed the same selfcheck the live install passes, and its
regression over the shipped reference cases reproduces the baseline recorded
for the pinned engine (``data/engine-baseline.json``) to within $5:

    python-candidate/   the zip extracted; selfcheck run with its own interpreter
    python/ planner/ config/ templates/ + planner.cmd LICENSE README.md GUIDE.md VERSION
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
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import date
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as dist_version
from pathlib import Path
from typing import Any

import yaml

from planner.engine import verify
from planner.engine.selfcheck import format_years, years_in

CANDIDATE = "python-candidate"
LIVE = "python"
PREVIOUS = "python-previous"
SWAPPED_DIRS = ("python", "planner", "config", "templates")
SWAPPED_FILES = ("planner.cmd", "LICENSE", "README.md", "GUIDE.md", "VERSION")
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


ALLOW_MAJOR = "planner update --allow-major"


class UpdateError(RuntimeError):
    pass


class MajorUpdateHeld(UpdateError):
    """A candidate that jumps a major version waits for ``--allow-major``."""


def years_note(years: tuple[int, ...], missing_next: int | None) -> str:
    """What a release covers, for the user: its tax years and a missing next year."""
    if not years:
        return "this release does not report the tax years it covers"
    note = f"covers tax years {format_years(years)}"
    return note + (f"; {missing_next} is not modelled yet" if missing_next else "")


@dataclass(frozen=True)
class UpdateResult:
    version: str
    selfcheck: str
    years: tuple[int, ...] = ()
    missing_next: int | None = None  # next calendar year, when the engine lacks it

    @property
    def years_note(self) -> str:
        return years_note(self.years, self.missing_next)


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


def _run_candidate(cand: Path, python_exe: str, *args: str) -> str:
    exe = cand / LIVE / python_exe
    if not exe.exists():
        raise UpdateError(f"candidate interpreter missing: {exe}")
    proc = subprocess.run(  # noqa: S603
        [str(exe), "-m", "planner", *args],
        cwd=cand,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        raise UpdateError(
            f"candidate {' '.join(args)} failed:\n{proc.stdout}{proc.stderr}"
        )
    return proc.stdout.strip()


def regression_drift(
    printed: dict[str, float], baseline: dict[str, float], pinned: str
) -> None:
    """Raise :class:`UpdateError` when the candidate's regression output is
    missing, or differs from the pinned engine's baseline past
    :data:`verify.LIMITS` on any figure. The first line is
    the reason shown on the dashboard; the rest lists every figure."""
    if not printed:
        raise UpdateError(
            "candidate printed no regression values (its selfcheck --regression "
            "ran no reference cases), so it cannot be shown to match the "
            f"policyengine-us {pinned} baseline"
        )
    moved = verify.drifts(baseline, printed)
    if not moved:
        return
    head = (
        f"candidate regression is off the engine baseline (policyengine-us "
        f"{pinned}) past its limit ({verify.LIMITS}) on {len(moved)} of "
        f"{len(baseline)} figures; first {verify.describe_drift(moved[0])}"
    )
    rest = [f"  {verify.describe_drift(d)}" for d in moved[:20]]
    more = [f"  ... and {len(moved) - 20} more"] if len(moved) > 20 else []
    raise UpdateError("\n".join([head, *rest, *more]))


def selfcheck_candidate(
    cand: Path,
    python_exe: str = "python.exe",
    baseline: tuple[str, dict[str, float]] | None = None,
) -> str:
    """Run the candidate's own interpreter against the candidate's own package:
    its selfcheck, then, given the pinned ``baseline`` (engine version, figures),
    its regression over the reference cases, which must reproduce that baseline.
    Returns the selfcheck output."""
    check = _run_candidate(cand, python_exe, "selfcheck")
    if baseline is not None:
        out = _run_candidate(cand, python_exe, "selfcheck", "--regression")
        pinned, figures = baseline
        regression_drift(verify.parse_regression(out), figures, pinned)
    return check


def _move(src: Path, dst: Path) -> None:
    if src.exists():
        shutil.move(str(src), str(dst))


# VERSION parks first: once any live item is parked, python-previous/VERSION
# is there, so planner update --rollback can put back a swap cut off part way.
PARK_ORDER = ("VERSION", *SWAPPED_DIRS, *(f for f in SWAPPED_FILES if f != "VERSION"))


def cut_off(root: Path) -> bool:
    """A swap stopped part way: an item parked in python-previous/ has no live
    counterpart (a finished swap leaves a full set on each side)."""
    prev = root / PREVIOUS
    return any((prev / n).exists() and not (root / n).exists() for n in PARK_ORDER)


def swap_in(cand: Path, root: Path) -> None:
    """candidate -> live, live -> previous. Data folders are never touched. A
    move that fails (disk full, a file in use) puts back every move before it,
    as swap.cmd does; a swap killed part way is undone by :func:`rollback`."""
    prev = root / PREVIOUS
    if cut_off(root):
        raise UpdateError(
            "an earlier update was cut off part way: run planner update "
            "--rollback first (python-previous/ holds the release it replaced)"
        )
    if prev.exists():
        shutil.rmtree(prev)
    prev.mkdir()
    done: list[tuple[Path, Path]] = []
    try:
        for name in PARK_ORDER:
            if (root / name).exists():
                _move(root / name, prev / name)
                done.append((root / name, prev / name))
        for name in PARK_ORDER:
            if (cand / name).exists():
                _move(cand / name, root / name)
                done.append((cand / name, root / name))
    except BaseException:
        for src, dst in reversed(done):
            _move(dst, src)
        raise
    shutil.rmtree(cand, ignore_errors=True)


def rollback(root: Path) -> str:
    """previous -> live. Only the items parked in python-previous/ move back,
    so a swap cut off before it parked them all still comes back whole."""
    prev = root / PREVIOUS
    if not (prev / "VERSION").exists():
        raise UpdateError("nothing to roll back to (no python-previous/VERSION)")
    broken = root / CANDIDATE
    if broken.exists():
        shutil.rmtree(broken)
    broken.mkdir()
    parked = [n for n in PARK_ORDER if (prev / n).exists()]
    for name in parked:
        _move(root / name, broken / name)
    for name in parked:
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


def _major(version: str) -> int | None:
    m = re.match(r"v?(\d+)", version.strip())
    return int(m.group(1)) if m else None


def installed_engine() -> str | None:
    """The policyengine-us this process runs on; None when it is not installed."""
    try:
        return dist_version("policyengine-us")
    except PackageNotFoundError:
        return None


def candidate_engine(cand: Path) -> str | None:
    """policyengine-us as the candidate's own dist-info records it (Windows
    ``python/Lib/site-packages``, or ``python/lib/python3.x/site-packages``)."""
    base = cand / LIVE
    for site in [
        base / "Lib" / "site-packages",
        *base.glob("lib/python*/site-packages"),
    ]:
        for dist in sorted(site.glob("policyengine_us-*.dist-info")):
            meta = dist / "METADATA"
            if meta.exists():
                for line in meta.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines():
                    if line.startswith("Version:"):
                        return line.split(":", 1)[1].strip()
            return dist.name.removeprefix("policyengine_us-").removesuffix(".dist-info")
    return None


def hold_major(cand: Path, root: Path) -> None:
    """Raise :class:`MajorUpdateHeld` when the candidate's policyengine-us or
    planner is a new major version over the installed one."""
    jumps: list[str] = []
    new_engine, old_engine = candidate_engine(cand), installed_engine()
    if new_engine and old_engine:
        a, b = _major(new_engine), _major(old_engine)
        if a is not None and b is not None and a > b:
            jumps.append(
                f"policyengine-us {new_engine} is a new major version over {old_engine}"
            )
    new_planner = (cand / "VERSION").read_text(encoding="utf-8").strip()
    old_planner = installed_version(root)
    a, b = _major(new_planner), _major(old_planner)
    if a is not None and b is not None and a > b:
        jumps.append(f"planner {new_planner} is a new major version over {old_planner}")
    if jumps:
        raise MajorUpdateHeld(
            f"{'; '.join(jumps)}; results may change, run {ALLOW_MAJOR} to install it"
        )


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
    zip_path: Path,
    expected_sha256: str,
    root: Path,
    python_exe: str = "python.exe",
    allow_major: bool = False,
    today: date | None = None,
) -> UpdateResult:
    """Verify, extract and self-check the candidate, carry hand limits into it
    and mark it ready. Nothing live is touched. A candidate that jumps a major
    version of policyengine-us or the planner is held (before its selfcheck
    runs) unless ``allow_major``. The candidate then runs its selfcheck and its
    regression over the reference cases; a figure more than $5 off the pinned
    engine's baseline (``data/engine-baseline.json``, recorded here if no run
    has yet) refuses it, ``allow_major`` or not. The tax years its engine
    publishes are recorded, with next year flagged when absent."""
    verify_zip(zip_path, expected_sha256)
    cand = extract_candidate(zip_path, root)
    if not allow_major:
        try:
            hold_major(cand, root)
        except MajorUpdateHeld:
            shutil.rmtree(cand, ignore_errors=True)
            raise
    try:
        pinned = verify.pinned_baseline(root, today)
    except ValueError as exc:
        raise UpdateError(str(exc)) from exc
    check = selfcheck_candidate(cand, python_exe, pinned)
    version = (cand / "VERSION").read_text(encoding="utf-8").strip()
    years = years_in(check)
    next_year = (today or date.today()).year + 1
    missing = next_year if years and next_year not in years else None
    carried = carry_thresholds(root, cand)
    ready = {
        "version": version,
        "selfcheck": check,
        "carried": carried,
        "years": list(years),
        "missing_next": missing,
    }
    (cand / READY).write_text(json.dumps(ready), encoding="utf-8")
    return UpdateResult(version, check, years, missing)


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
        if "years" in ready:
            msg += f"; {_ready_note(ready)}"
    else:
        shutil.rmtree(root / CANDIDATE, ignore_errors=True)
        shutil.rmtree(root / PREVIOUS, ignore_errors=True)
        msg = f"rolled back to {now}"
    pending_file.unlink()
    # swap.cmd stays: cmd is still running it (the rerun follows this call)
    # and reads each line from the file; write_swap rewrites it next time
    return msg


def _ready_note(ready: dict[str, Any]) -> str:
    return years_note(tuple(ready["years"]), ready.get("missing_next"))


def apply_staged(root: Path) -> str:
    """In-process swap of a staged candidate (no launcher)."""
    ready = staged(root)
    if ready is None:
        raise UpdateError("no update is staged")
    before = installed_version(root)
    (root / CANDIDATE / READY).unlink()
    swap_in(root / CANDIDATE, root)
    v, check = ready["version"], ready["selfcheck"]
    msg = f"updated {before} -> {v}; candidate selfcheck: {check}"
    return f"{msg}; {_ready_note(ready)}" if "years" in ready else msg


def apply(
    zip_path: Path, expected_sha256: str, root: Path, python_exe: str = "python.exe"
) -> UpdateResult:
    result = stage(zip_path, expected_sha256, root, python_exe)
    (root / CANDIDATE / READY).unlink()
    swap_in(root / CANDIDATE, root)
    return result
