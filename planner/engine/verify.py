"""Independent check of a filed return, and the engine regression built on it.

A return file holds three keys: ``year``, ``household`` (the engine inputs, see
``Household``) and ``filed`` (the lines as filed). Nothing in this module knows
the user's figures; the private file lives in ``data/private/returns/`` and is
never committed.

``reference.yaml`` beside this module holds the shipped reference cases: the
same three keys under ``cases.<name>``, synthetic and hand-worked. They are the
engine regression:

* ``planner selfcheck --regression`` prints the engine's output on every case,
  one ``regression <case>.<field> <value>`` line per figure;
* the first ``planner run`` records that output for the pinned engine in
  ``data/engine-baseline.json`` (keyed by engine version) and prints its delta
  from the filed lines;
* ``planner update`` runs the same command inside the candidate and refuses a
  release whose output is more than :data:`BASELINE_TOLERANCE` dollars off the
  baseline on any figure.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from planner.engine.household import Household
from planner.engine.tax import compute, engine_version

REFERENCE = Path(__file__).with_name("reference.yaml")
BASELINE_FILE = "engine-baseline.json"
# Plan policy (Phase 1, "baseline-record"): a later engine must reproduce the
# pinned engine's output on every reference figure to within five dollars.
BASELINE_TOLERANCE = 5.0
REGRESSION_LABEL = "regression "
RETURNS = ("private", "returns")

LINES = {
    "fed_income_tax_after_credits": "Form 1040 line 22",
    "se_tax": "Schedule 2 line 4",
    "fed_total_tax": "Form 1040 line 24",
    "agi": "Form 1040 line 11",
    "taxable_income": "Form 1040 line 15",
    "qbi_deduction": "Form 1040 line 13",
    "state_tax": "NC D-400 line 15",
}


@dataclass(frozen=True)
class Line:
    name: str
    filed: float
    engine: float
    ok: bool


@dataclass(frozen=True)
class Report:
    year: int
    lines: list[Line]

    @property
    def n_ok(self) -> int:
        return sum(1 for line in self.lines if line.ok)

    @property
    def passed(self) -> bool:
        return bool(self.lines) and self.n_ok == len(self.lines)


def _mapping(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping")
    return data


def check_case(label: str, data: Mapping[str, Any], tolerance: float) -> Report:
    """The engine's figures for one case against its ``filed`` lines."""
    for key in ("year", "household", "filed"):
        if key not in data:
            raise ValueError(f"{label}: missing '{key}'")
    year = int(data["year"])
    result = asdict(compute(year, Household.from_mapping(data["household"])))
    filed = data["filed"]
    unknown = sorted(set(filed) - set(LINES))
    if unknown:
        raise ValueError(
            f"{label}: unknown filed lines {unknown}; known: {sorted(LINES)}"
        )
    lines = [
        Line(
            name,
            float(filed[name]),
            result[name],
            abs(float(filed[name]) - result[name]) <= tolerance,
        )
        for name in LINES
        if name in filed
    ]
    return Report(year=year, lines=lines)


def verify_return(path: Path, tolerance: float = 1.0) -> Report:
    return check_case(str(path), _mapping(path), tolerance)


def reference_cases(path: Path = REFERENCE) -> dict[str, dict[str, Any]]:
    """The shipped reference cases by name, in file order."""
    cases = _mapping(path).get("cases")
    if not isinstance(cases, dict) or not cases:
        raise ValueError(f"{path}: needs a 'cases' mapping")
    return {str(name): dict(case) for name, case in cases.items()}


def verify_file(path: Path, tolerance: float = 1.0) -> list[tuple[str, Report]]:
    """One report per case: a reference file gives one per named case, a single
    return file gives one named for the file."""
    if "cases" in _mapping(path):
        return [
            (name, check_case(f"{path}: {name}", case, tolerance))
            for name, case in reference_cases(path).items()
        ]
    return [(path.stem, verify_return(path, tolerance))]


def regression_values(path: Path = REFERENCE) -> dict[str, float]:
    """The engine's figure for every numeric output of every reference case,
    keyed ``<case>.<field>``."""
    out: dict[str, float] = {}
    for name, case in reference_cases(path).items():
        result = asdict(
            compute(int(case["year"]), Household.from_mapping(case["household"]))
        )
        for field, value in result.items():
            if isinstance(value, float):
                out[f"{name}.{field}"] = value
    return out


def format_regression(values: Mapping[str, float]) -> str:
    return "\n".join(f"{REGRESSION_LABEL}{k} {v:.2f}" for k, v in values.items())


def parse_regression(text: str) -> dict[str, float]:
    """The inverse of :func:`format_regression`; other output lines are ignored."""
    pattern = rf"^{re.escape(REGRESSION_LABEL)}(\S+) (-?\d+(?:\.\d+)?)$"
    return {k: float(v) for k, v in re.findall(pattern, text, re.MULTILINE)}


@dataclass(frozen=True)
class Drift:
    """A figure that moved more than the tolerance; ``candidate`` is None when
    the new engine printed no figure for a baseline key."""

    key: str
    baseline: float
    candidate: float | None


def drifts(
    baseline: Mapping[str, float],
    values: Mapping[str, float],
    tolerance: float = BASELINE_TOLERANCE,
) -> list[Drift]:
    """Baseline figures the new output lacks or moves by more than ``tolerance``
    dollars. Figures only the new output has (a case added later) are not drift."""
    out: list[Drift] = []
    for key, was in baseline.items():
        now = values.get(key)
        if now is None or round(abs(now - was), 2) > tolerance:
            out.append(Drift(key, was, now))
    return out


def describe_drift(d: Drift) -> str:
    if d.candidate is None:
        return f"{d.key}: baseline {d.baseline:,.2f}, not in the candidate's output"
    return (
        f"{d.key}: baseline {d.baseline:,.2f}, candidate {d.candidate:,.2f} "
        f"({d.candidate - d.baseline:+,.2f})"
    )


def baseline_path(root: Path) -> Path:
    return root / "data" / BASELINE_FILE


def load_baseline(root: Path) -> dict[str, Any] | None:
    """``{"pinned": version, "engines": {version: {"recorded", "values"}}}``;
    None before the first run. A damaged file is an error, never a fresh start:
    re-recording would let a drifted engine become its own baseline."""
    path = baseline_path(root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        engines = data["engines"]
        if data["pinned"] not in engines:
            raise KeyError(data["pinned"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(
            f"{path} is unreadable ({exc}); restore it from a backup or delete "
            "it to record a new baseline from the engine you run now"
        ) from exc
    record: dict[str, Any] = data
    return record


def _save(root: Path, record: Mapping[str, Any]) -> None:
    path = baseline_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def _record(root: Path, today: date | None) -> tuple[dict[str, Any], bool]:
    """The baseline with the running engine's output added under its version
    when it is not there yet; the second item says whether it was added."""
    record = load_baseline(root) or {"pinned": "", "engines": {}}
    version = engine_version()
    if version in record["engines"]:
        return record, False
    values = {k: round(v, 2) for k, v in regression_values().items()}
    record["engines"][version] = {
        "recorded": (today or date.today()).isoformat(),
        "values": values,
    }
    record["pinned"] = record["pinned"] or version
    _save(root, record)
    return record, True


def pinned_baseline(
    root: Path, today: date | None = None
) -> tuple[str, dict[str, float]]:
    """(engine version, figures) every later engine is held to. Recorded from the
    running engine when no run has done so yet."""
    record, _ = _record(root, today)
    pinned = str(record["pinned"])
    return pinned, dict(record["engines"][pinned]["values"])


def filed_deltas(root: Path, tolerance: float = 1.0) -> list[str]:
    """How far the engine is from the filed lines: the shipped reference cases,
    then each return the user filed under ``data/private/returns/``. Reported,
    never asserted: a gap is known variance, not a failure."""
    reports: list[tuple[str, Report]] = [
        (name, check_case(name, case, tolerance))
        for name, case in reference_cases().items()
    ]
    lines = [_delta_line("reference cases", reports, tolerance, prefix=True)]
    folder = root.joinpath("data", *RETURNS)
    for path in sorted(folder.glob("*.yaml")) if folder.is_dir() else []:
        if path.stem.endswith("-closed"):
            continue
        try:
            report = verify_return(path, tolerance)
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            lines.append(f"{path.stem}: unreadable ({str(exc).splitlines()[0]})")
            continue
        lines.append(_delta_line(path.stem, [(path.stem, report)], tolerance))
    return lines


def _delta_line(
    label: str,
    reports: list[tuple[str, Report]],
    tolerance: float,
    prefix: bool = False,
) -> str:
    total = sum(len(r.lines) for _, r in reports)
    good = sum(r.n_ok for _, r in reports)
    off = [
        f"{name} {line.name}" if prefix else line.name
        for name, r in reports
        for line in r.lines
        if not line.ok
    ]
    note = f" (off: {', '.join(off)})" if off else ""
    return f"{label}: {good}/{total} lines within ${tolerance:,.2f}{note}"


def ensure_baseline(root: Path, today: date | None = None) -> str:
    """Record the running engine's regression output if its version is new.

    The first engine recorded is the baseline; the message then gives its delta
    from the filed lines. A later version is recorded beside it and named, on
    every run, while it is off the baseline by more than
    :data:`BASELINE_TOLERANCE`. Returns "" when nothing needs saying."""
    record, added = _record(root, today)
    version, pinned = engine_version(), str(record["pinned"])
    values: dict[str, float] = record["engines"][version]["values"]
    if version != pinned:
        moved = drifts(record["engines"][pinned]["values"], values)
        if not moved:
            return ""
        shown = "; ".join(describe_drift(d) for d in moved[:3])
        return (
            f"policyengine-us {version} is off the engine baseline ({pinned}) by "
            f"more than ${BASELINE_TOLERANCE:,.2f} on {len(moved)} figures: {shown}"
        )
    if not added:
        return ""
    text = (
        f"engine baseline recorded for policyengine-us {version}: "
        f"{len(values)} values from {len(reference_cases())} reference cases "
        f"(data/{BASELINE_FILE}); a later engine must match within "
        f"${BASELINE_TOLERANCE:,.2f}"
    )
    delta = "\n".join(f"  {line}" for line in filed_deltas(root))
    return f"{text}\ndelta from the filed return:\n{delta}"
