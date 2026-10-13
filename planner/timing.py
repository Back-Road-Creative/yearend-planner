"""Speed budgets (master plan unit 7e), measured on this computer with a
made-up household in a throwaway folder: nothing of yours is read, and the
only write is one row per measure in ``data/timing.csv``.

- first screen: from starting ``planner run`` until the browser gets a page;
  the server is up before any slow step (budget 3 s).
- refresh: reloading the running page once the refresh is done, with the
  engine loaded and the page gathered afresh (budget 15 s).
- cold run: ``planner run --quiet`` in a new process, so the tax engine loads
  from disk, in a folder whose engine baseline is already recorded (budget
  90 s). It prints a line per step as it goes.

The first run in a folder also records the engine baseline, a one-time cost
paid again only when the tax engine changes; it is shown without a budget."""

from __future__ import annotations

import csv
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from planner.paths import Layout

BUDGETS = {"first screen": 3.0, "refresh": 15.0, "cold run": 90.0}
FIRST_RUN = "first run (records the engine baseline)"
YEAR, AS_OF = "2025", "2026-02-10"
ANSWERS = (
    ("birth_date", "1971-06-15"),
    ("filing_status", "single"),
    ("state", "NC"),
    ("wages", "52,000"),
)
WAIT = 900.0  # seconds before a step that never answers counts as a failure
SERVING = re.compile(r"serving (http://\S+)")


class TimingError(RuntimeError):
    """A measured step failed or never answered."""


@dataclass(frozen=True)
class Result:
    name: str
    seconds: float
    budget: float | None

    @property
    def ok(self) -> bool:
        return self.budget is None or self.seconds <= self.budget

    def line(self) -> str:
        if self.budget is None:
            return f"{self.name}: {self.seconds:.1f} s"
        mark = "within" if self.ok else "OVER"
        return (
            f"{self.name}: {self.seconds:.1f} s ({mark} the {self.budget:g} s budget)"
        )


def machine() -> str:
    """This computer, as the measures are recorded against it."""
    cpu = platform.processor() or platform.machine() or "unknown processor"
    return (
        f"{platform.node() or 'this computer'}: {platform.system()} "
        f"{platform.release()}, {cpu}, {os.cpu_count() or '?'} threads"
    )


def planner_cmd(*args: str) -> list[str]:
    return [sys.executable, "-m", "planner", *args]


def _folder(release: Path, tmp: Path) -> dict[str, str]:
    """A planner folder with the release's config and four typed answers;
    the environment that points ``planner`` at it."""
    home = tmp / "home"
    shutil.copytree(release / "config", home / "config")
    env = {**os.environ, "PLANNER_HOME": str(home), "PYTHONUNBUFFERED": "1"}
    for key, value in ANSWERS:
        _call(env, release, "enter", "--year", YEAR, key, value)
    return env


def _call(env: dict[str, str], cwd: Path, *args: str) -> float:
    started = time.monotonic()
    done = subprocess.run(  # noqa: S603
        planner_cmd(*args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=WAIT,
        check=False,
    )
    if done.returncode != 0:
        raise TimingError(
            f"planner {' '.join(args)} failed:\n{done.stdout}{done.stderr}"
        )
    return time.monotonic() - started


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=WAIT) as r:  # noqa: S310 - loopback
        return str(r.read().decode("utf-8"))


def served(
    cmd: Sequence[str], env: dict[str, str], cwd: Path, log: Path
) -> tuple[float, float]:
    """(first screen, refresh) for the server ``cmd`` starts: seconds until
    its first page, then seconds for one reload once the page is live."""
    from planner.dashboard.serve import WARMING

    started = time.monotonic()
    with log.open("w", encoding="utf-8") as out:
        proc = subprocess.Popen(  # noqa: S603
            list(cmd), cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT
        )
    try:
        url = _until(lambda: _address(log), proc, started, log)
        _get(url)
        first = time.monotonic() - started
        _until(lambda: WARMING not in _get(url) or None, proc, started, log)
        t = time.monotonic()
        _get(url)
        return first, time.monotonic() - t
    finally:
        proc.terminate()
        proc.wait(timeout=60)


def _address(log: Path) -> str | None:
    found = SERVING.search(log.read_text(encoding="utf-8", errors="replace"))
    return found.group(1) if found else None


def _until(
    probe: Callable[[], object],
    proc: subprocess.Popen[bytes],
    started: float,
    log: Path,
) -> str:
    while time.monotonic() - started < WAIT:
        got = probe()
        if got:
            return str(got)
        if proc.poll() is not None:
            raise TimingError(
                f"planner run stopped:\n{log.read_text(errors='replace')}"
            )
        time.sleep(0.1)
    raise TimingError(f"planner run gave no page in {WAIT:g} s")


def measure(release: Path) -> list[Result]:
    """Every measure, in a throwaway folder, using the planner in ``release``."""
    run = ("run", "--year", YEAR, "--as-of", AS_OF, "--no-update-check")
    with tempfile.TemporaryDirectory(prefix="planner-timing-") as tmp:
        env = _folder(release, Path(tmp))
        first_run = _call(env, release, *run, "--quiet")
        cold = _call(env, release, *run, "--quiet")
        screen, refresh = served(
            planner_cmd(*run, "--no-open"), env, release, Path(tmp) / "run.log"
        )
    return [
        Result("first screen", screen, BUDGETS["first screen"]),
        Result("refresh", refresh, BUDGETS["refresh"]),
        Result("cold run", cold, BUDGETS["cold run"]),
        Result(FIRST_RUN, first_run, None),
    ]


def record(lay: Layout, results: list[Result], today: date | None = None) -> Path:
    """One row per measure in ``data/timing.csv``, against this computer."""
    path = lay.data / "timing.csv"
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "machine", "measure", "seconds", "budget"])
        for r in results:
            w.writerow(
                [
                    (today or date.today()).isoformat(),
                    machine(),
                    r.name,
                    f"{r.seconds:.1f}",
                    "" if r.budget is None else f"{r.budget:g}",
                ]
            )
    return path
