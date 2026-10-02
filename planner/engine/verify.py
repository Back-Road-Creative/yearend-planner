"""Independent check of a filed return: the engine recomputes it from its inputs.

The YAML holds two mappings: ``household`` (the engine inputs, see ``Household``)
and ``filed`` (the lines as filed). Nothing in this module knows the user's figures;
the private file lives in ``data/private/returns/`` and is never committed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from planner.engine.household import Household
from planner.engine.tax import compute

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


def verify_return(path: Path, tolerance: float = 1.0) -> Report:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    for key in ("year", "household", "filed"):
        if key not in data:
            raise ValueError(f"{path}: missing '{key}'")
    year = int(data["year"])
    result = asdict(compute(year, Household.from_mapping(data["household"])))
    filed = data["filed"]
    unknown = sorted(set(filed) - set(LINES))
    if unknown:
        raise ValueError(
            f"{path}: unknown filed lines {unknown}; known: {sorted(LINES)}"
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
