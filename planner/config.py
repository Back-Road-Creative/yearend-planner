"""Load the three config files. ``safe_load`` only; nothing executes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ASSUMPTION_FIELDS = (
    "birth_date",
    "filing_status",
    "state",
    "county",
    "spouse_birth_date",
    "dependents",
    "spending_floor",
    "spending_ceiling",
    "withdrawal_rate",
    "cash_target",
    "mortgage_monthly",
    "premium_monthly",
    "irregular",
    "inflation",
    "return_floor",
    "return_track",
    "ss_estimate_62",
    "ss_estimate_67",
    "ss_estimate_70",
    "ira_access_age",
    "ss_claim_age",
    "conversion_margin",
    "conversion_cap",
    "conversion_objective",
    "roth_basis_contributions",
    "hsa_coverage",
    "workplace_plan",
)


class ConfigError(ValueError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that refuses a mapping key given twice (PyYAML keeps
    the last one silently)."""

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        seen: set[Any] = set()
        for key_node, _value in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=deep)
            try:
                again = key in seen
            except TypeError:
                continue  # an unhashable key; the base class refuses it
            if again:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def safe_load(stream: Any) -> Any:
    """``yaml.safe_load`` that refuses a duplicate key; every planner YAML read
    goes through it."""
    loader = _UniqueKeyLoader(stream)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at top level")
    return data


def load_assumptions(path: Path) -> dict[str, Any]:
    """Profile assumptions. Missing fields stay ``None``: unknown is not zero."""
    data = load_yaml(path)
    unknown = sorted(set(data) - set(ASSUMPTION_FIELDS))
    if unknown:
        raise ConfigError(f"{path}: unknown fields {unknown}")
    return {k: data.get(k) for k in ASSUMPTION_FIELDS}


def missing_assumptions(assumptions: dict[str, Any]) -> list[str]:
    return [k for k in ASSUMPTION_FIELDS if assumptions.get(k) is None]


ENGINE_THRESHOLDS = "thresholds.engine.yaml"


def _threshold_rows(path: Path) -> dict[int, dict[str, Any]]:
    data = load_yaml(path)
    out: dict[int, dict[str, Any]] = {}
    for year, rows in data.items():
        if not isinstance(rows, dict):
            raise ConfigError(f"{path}: year {year} is not a mapping")
        for name, row in rows.items():
            if not isinstance(row, dict) or "value" not in row or "source" not in row:
                raise ConfigError(f"{path}: {year}.{name} needs value and source")
        out[int(year)] = rows
    return out


def load_thresholds(path: Path, merged: bool = True) -> dict[int, dict[str, Any]]:
    """``{year: {name: {value, source}}}``. Every threshold carries its source.

    The hand-sourced file wins; ``thresholds.engine.yaml`` beside it (written
    from the engine's parameters on each launch) fills the years and rows the
    hand file lacks. ``merged=False`` reads the hand file alone."""
    out = _threshold_rows(path)
    engine = path.with_name(ENGINE_THRESHOLDS)
    if merged and engine.exists():
        for year, rows in _threshold_rows(engine).items():
            out[year] = {**rows, **out.get(year, {})}
    return out


# A capability record (unit 2b, finding F13): the historical status kept, the
# evidence behind it named apart, never promoted by the status alone.
STATUSES = ("verified", "partial", "unsupported")
EVIDENCE = (
    "unreviewed",
    "implemented",
    "independently_validated",
    "end_to_end_accepted",
    "partial",
    "out_of_scope",
)
VALIDATED = ("independently_validated", "end_to_end_accepted")  # need `expected`
RECORD_FIELDS = ("status", "evidence", "tests", "note", "scope", "expected", "proven")
COMMIT = re.compile(r"[0-9a-f]{7,40}")


@dataclass(frozen=True)
class Capability:
    status: str  # verified | partial | unsupported (the historical claim)
    evidence: str  # one of EVIDENCE
    tests: tuple[str, ...]  # tests/<file>.py[::<name>] that prove it
    note: str = ""  # what the tests cover and leave out
    scope: str = ""  # who it holds for, when narrower than the planner's scope
    expected: str = ""  # the independent expected-value source
    proven: str = ""  # the commit a release run proved it at (shipped copy only)


def _record(name: str, raw: Any) -> tuple[Capability | None, list[str]]:
    if not isinstance(raw, dict):
        return None, [f"{name}: a record (status, evidence, tests), not {raw!r}"]
    errors = [f"{name}: unknown field {k}" for k in raw if k not in RECORD_FIELDS]
    errors += [f"{name}: needs {k}" for k in RECORD_FIELDS[:3] if k not in raw]
    status, evidence = raw.get("status"), raw.get("evidence")
    tests = raw.get("tests") or []
    text = {k: raw.get(k) or "" for k in RECORD_FIELDS[3:]}
    if status not in STATUSES:
        errors.append(f"{name}: status must be one of {list(STATUSES)}")
    if evidence not in EVIDENCE:
        errors.append(f"{name}: evidence must be one of {list(EVIDENCE)}")
    if not isinstance(tests, list) or not all(isinstance(t, str) for t in tests):
        errors.append(f"{name}: tests must be a list of tests/ paths")
    if not all(isinstance(v, str) for v in text.values()):
        errors.append(f"{name}: note, scope, expected and proven are text")
    elif text["proven"] and not COMMIT.fullmatch(text["proven"]):
        errors.append(f"{name}: proven must be a commit, not {text['proven']!r}")
    if evidence in VALIDATED and not text["expected"]:
        errors.append(f"{name}: {evidence} needs expected (its independent source)")
    if errors:
        return None, errors
    return Capability(str(status), str(evidence), tuple(tests), **text), []


def load_records(path: Path) -> dict[str, Capability]:
    """``{capability: Capability}`` from ``config/capabilities.yaml``."""
    out, errors = {}, []
    for name, raw in load_yaml(path).items():
        rec, bad = _record(str(name), raw)
        errors += bad
        if rec:
            out[str(name)] = rec
    if errors:
        raise ConfigError(f"{path}: " + "; ".join(errors))
    return out


def load_capabilities(path: Path) -> dict[str, str]:
    """``{capability: verified|partial|unsupported}``: each record's status."""
    return {name: rec.status for name, rec in load_records(path).items()}
