"""On each launch: the limits for this year and next, from the installed engine.

``config/thresholds.yaml`` holds hand-entered limits, each with its IRS or
state source; those always win. This module writes
``config/thresholds.engine.yaml`` beside it for whatever that file lacks:

- a row the engine carries (CONFIG_PARAMS) for a year the hand file does not
  list, tagged published or projected (an indexed limit the IRS has not
  announced yet, inflated by the engine's own index)
- for a year with no hand rows at all, every other row carried from the latest
  hand year, marked "confirm"

Where both sides have a row and disagree, the hand row stays and the
disagreement is reported: the engine prices the tax with its own value until
policyengine-us is updated. Nothing here touches the network."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from planner.config import ENGINE_THRESHOLDS, load_thresholds
from planner.engine.tax import CONFIG_PARAMS, engine_value, engine_version
from planner.paths import Layout

HEADER = (
    "# Written by planner on each launch from the installed policyengine-us.\n"
    "# Do not edit: config/thresholds.yaml (hand-sourced) wins over every row "
    "here.\n"
)


@dataclass
class Limits:
    engine: str
    coverage: dict[int, str] = field(default_factory=dict)
    drift: dict[int, list[str]] = field(default_factory=dict)
    projected: dict[int, list[str]] = field(default_factory=dict)
    carried: dict[int, list[str]] = field(default_factory=dict)


def _num(v: float) -> float | int:
    return int(v) if float(v).is_integer() else v


def _fmt(v: Any) -> str:
    f = float(v)
    return f"{f:,.0f}" if f.is_integer() else f"{f:g}"


def refresh(lay: Layout, year: int, write: bool = True) -> Limits:
    """Limits for ``year`` and ``year + 1``; ``write`` (re)writes the engine
    file, else only reports what it would hold."""
    path = lay.config / "thresholds.yaml"
    hand = load_thresholds(path, merged=False)
    ver = engine_version()
    rep = Limits(ver)
    out: dict[int, dict[str, Any]] = {}
    for y in (year, year + 1):
        own = hand.get(y, {})
        rows: dict[str, Any] = {}
        for name, param in CONFIG_PARAMS.items():
            value, published = engine_value(name, y)
            if name in own:
                if float(own[name]["value"]) != value:
                    rep.drift.setdefault(y, []).append(
                        f"{name} {y}: {_fmt(own[name]['value'])} in "
                        f"config/thresholds.yaml ({own[name]['source']}); "
                        f"policyengine-us {ver} {'has' if published else 'projects'} "
                        f"{_fmt(value)} and prices the tax with that"
                    )
                continue
            tag = "" if published else " (projected by inflation; not yet published)"
            rows[name] = {
                "value": _num(value),
                "source": f"policyengine-us {ver} {param}{tag}",
            }
            if not published:
                rep.projected.setdefault(y, []).append(name)
        prior = [p for p in hand if p < y]
        if not own and prior:
            base = max(prior)
            for name, row in hand[base].items():
                if name not in rows and name not in CONFIG_PARAMS:
                    rows[name] = {
                        "value": row["value"],
                        "source": f"carried from {base}: {row['source']}; "
                        f"confirm for {y}",
                    }
                    rep.carried.setdefault(y, []).append(name)
        if rows:
            out[y] = rows
        parts = ["config"] if own else []
        if len(rows) > len(rep.carried.get(y, [])):
            parts.append("engine projection" if rep.projected.get(y) else "engine")
        if rep.carried.get(y):
            parts.append("carried")
        rep.coverage[y] = " + ".join(parts) or "none"
    if write:
        _write(lay.config / ENGINE_THRESHOLDS, out)
    return rep


def _write(target: Path, rows: dict[int, dict[str, Any]]) -> None:
    tmp = target.with_suffix(".tmp")
    tmp.write_text(
        HEADER + yaml.safe_dump(rows, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp.replace(target)


def summary(rep: Limits) -> str:
    return "; ".join(f"{y} {how}" for y, how in rep.coverage.items())
