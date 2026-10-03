"""Load the three config files. ``safe_load`` only; nothing executes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ASSUMPTION_FIELDS = (
    "birth_date",
    "filing_status",
    "state",
    "county",
    "spending_floor",
    "spending_ceiling",
    "withdrawal_rate",
    "cash_target",
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
)


class ConfigError(ValueError):
    pass


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
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


def load_thresholds(path: Path) -> dict[int, dict[str, Any]]:
    """``{year: {name: {value, source}}}``. Every threshold carries its source."""
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


def load_capabilities(path: Path) -> dict[str, str]:
    """``{feature: verified|partial|unsupported}``."""
    data = load_yaml(path)
    allowed = {"verified", "partial", "unsupported"}
    bad = {k: v for k, v in data.items() if v not in allowed}
    if bad:
        raise ConfigError(f"{path}: statuses must be one of {sorted(allowed)}: {bad}")
    return {str(k): str(v) for k, v in data.items()}
