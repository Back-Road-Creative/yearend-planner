"""Unit 4a: what the household is planning toward, typed once in
``data/profile/goals.yaml`` (``planner goal``, ``debt``, ``protect``, ``mix``).

Four parts, each optional:

* ``goals``: ranked goals and planned purchases, each with an amount, a date,
  an owner and how flexible it is. A goal without an amount or a date stays
  listed and says so; it is never priced as zero.
* ``debts``: balance, yearly rate and monthly payment; the payoff month is the
  standard amortization count, and a payment that does not cover the interest
  is named as never paying the debt off.
* ``protect``: the income lines (the MAGI panel's) the household wants kept,
  such as the ACA 400% cliff or the IRMAA first tier.
* ``target_mix``: the household's own split across asset classes. The planner
  never picks one; with none chosen it proposes no rebalancing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from planner.paths import Layout

KINDS = ("goal", "purchase")
OWNERS = ("self", "spouse", "joint")
FLEXIBILITY = ("fixed", "flexible", "optional")  # date and amount; date; either
ASSET_CLASSES = ("stocks", "bonds", "cash", "real_estate", "other")
# protect key -> the MAGI panel line it names (planner.plan.magi.lines)
PROTECT = {
    "medicaid": "Medicaid 138% FPL (monthly)",
    "aca_csr": "ACA CSR 250% FPL",
    "aca_cliff": "ACA 400% FPL cliff",
    "irmaa": "IRMAA first tier",
    "ltcg_zero": "0% LTCG / qualified-dividend ceiling",
    "bracket_12": "12% bracket top",
    "niit": "NIIT",
    "ss_half": "Social Security 50% taxable",
    "ss_most": "Social Security 85% taxable",
}
GOAL_FIELDS = ("name", "amount", "date", "rank", "owner", "flexibility", "kind")
DEBT_FIELDS = ("name", "balance", "rate", "payment", "owner")
TOP = ("goals", "debts", "protect", "target_mix")
MIX_TOTAL = 100.0
NEAR_MONTHS = 12  # goals dated inside a year (the reserve rule reads them)


class GoalsError(ValueError):
    pass


@dataclass(frozen=True)
class Goal:
    name: str
    rank: int
    amount: float | None = None
    date: str | None = None
    owner: str = "self"
    flexibility: str = "fixed"
    kind: str = "goal"


@dataclass(frozen=True)
class Debt:
    name: str
    balance: float
    rate: float  # yearly percent
    payment: float  # monthly
    owner: str = "self"

    def months(self) -> int | None:
        """Payments left (None: the payment never covers the interest)."""
        i = self.rate / 100 / 12
        if self.balance <= 0:
            return 0
        if i == 0:
            return math.ceil(self.balance / self.payment) if self.payment else None
        if self.payment <= self.balance * i:
            return None
        n = -math.log(1 - i * self.balance / self.payment) / math.log(1 + i)
        return math.ceil(round(n, 9))


@dataclass
class Goals:
    goals: list[Goal] = field(default_factory=list)
    debts: list[Debt] = field(default_factory=list)
    protect: list[str] = field(default_factory=list)
    target_mix: dict[str, float] | None = None
    notes: list[str] = field(default_factory=list)

    def ranked(self) -> list[Goal]:
        return sorted(self.goals, key=lambda g: (g.rank, g.name))

    def near(self, as_of: date, months: int = NEAR_MONTHS) -> list[Goal]:
        """Goals with a date inside ``months`` of ``as_of`` (passed ones too:
        the money is still owed)."""
        end = _add_months(as_of, months)
        return [
            g for g in self.ranked() if g.date and date.fromisoformat(g.date) <= end
        ]


def path(lay: Layout) -> Path:
    return lay.data / "profile" / "goals.yaml"


def _add_months(d: date, months: int) -> date:
    from planner.ledger.portfolio import add_months

    return add_months(d, months)


def _money(where: str, key: str, raw: Any, zero: bool = False) -> float:
    """A finite number above 0 (or 0 itself when ``zero``); typed text may
    carry a dollar sign, commas or a percent sign."""
    if isinstance(raw, str):
        raw = raw.replace(",", "").replace("$", "").rstrip("%").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise GoalsError(f"{where}: {key} must be a number") from exc
    if not (0 <= value < float("inf")) or (value == 0 and not zero):
        raise GoalsError(f"{where}: {key} must be {'0 or ' if zero else ''}more than 0")
    return round(value, 2)


def _choice(where: str, key: str, raw: Any, allowed: tuple[str, ...]) -> str:
    if raw not in allowed:
        raise GoalsError(f"{where}: {key} must be one of {', '.join(allowed)}")
    return str(raw)


def _goal(raw: Any, i: int) -> Goal:
    if not isinstance(raw, dict):
        raise GoalsError(f"goals[{i}]: expected a mapping")
    where = f"goal {raw.get('name') or i}"
    unknown = sorted(set(raw) - set(GOAL_FIELDS))
    if unknown:
        raise GoalsError(f"{where}: unknown field(s) {', '.join(unknown)}")
    if not str(raw.get("name") or "").strip():
        raise GoalsError(f"goals[{i}]: name is required")
    rank = raw.get("rank")
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise GoalsError(f"{where}: rank must be a whole number from 1")
    amount = raw.get("amount")
    when = raw.get("date")
    if when is not None:
        try:
            when = date.fromisoformat(str(when)).isoformat()
        except ValueError as exc:
            raise GoalsError(f"{where}: date must be YYYY-MM-DD") from exc
    return Goal(
        str(raw["name"]).strip(),
        rank,
        None if amount is None else _money(where, "amount", amount),
        when,
        _choice(where, "owner", raw.get("owner", "self"), OWNERS),
        _choice(where, "flexibility", raw.get("flexibility", "fixed"), FLEXIBILITY),
        _choice(where, "kind", raw.get("kind", "goal"), KINDS),
    )


def _debt(raw: Any, i: int) -> Debt:
    if not isinstance(raw, dict):
        raise GoalsError(f"debts[{i}]: expected a mapping")
    where = f"debt {raw.get('name') or i}"
    unknown = sorted(set(raw) - set(DEBT_FIELDS))
    if unknown:
        raise GoalsError(f"{where}: unknown field(s) {', '.join(unknown)}")
    if not str(raw.get("name") or "").strip():
        raise GoalsError(f"debts[{i}]: name is required")
    missing = [k for k in ("balance", "rate", "payment") if raw.get(k) is None]
    if missing:
        raise GoalsError(f"{where}: {', '.join(missing)} required")
    rate = _money(where, "rate", raw["rate"], zero=True)
    if rate > 100:
        raise GoalsError(f"{where}: rate is a yearly percent, 0 to 100")
    return Debt(
        str(raw["name"]).strip(),
        _money(where, "balance", raw["balance"]),
        rate,
        _money(where, "payment", raw["payment"], zero=True),
        _choice(where, "owner", raw.get("owner", "self"), OWNERS),
    )


def _mix(raw: Any) -> dict[str, float] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        raise GoalsError("target_mix: expected asset class: percent")
    unknown = sorted(set(raw) - set(ASSET_CLASSES))
    if unknown:
        raise GoalsError(
            f"target_mix: unknown class(es) {', '.join(map(str, unknown))} "
            f"(one of {', '.join(ASSET_CLASSES)})"
        )
    mix: dict[str, float] = {}
    for k, v in raw.items():
        try:
            pct = float(v)
        except (TypeError, ValueError) as exc:
            raise GoalsError(f"target_mix: {k} must be a percent") from exc
        if not 0 <= pct <= MIX_TOTAL:
            raise GoalsError(f"target_mix: {k} must be 0 to 100")
        mix[str(k)] = round(pct, 2)
    if abs(sum(mix.values()) - MIX_TOTAL) > 0.01:
        raise GoalsError(f"target_mix: adds to {sum(mix.values()):g}, not 100")
    return mix


def parse(data: dict[str, Any]) -> Goals:
    """Check a goals mapping; anything malformed is refused, never guessed."""
    unknown = sorted(set(data) - set(TOP))
    if unknown:
        raise GoalsError(f"unknown section(s) {', '.join(unknown)}")
    rows = {k: data.get(k) or [] for k in ("goals", "debts", "protect")}
    for k, v in rows.items():
        if not isinstance(v, list):
            raise GoalsError(f"{k}: expected a list")
    out = Goals(
        [_goal(g, i) for i, g in enumerate(rows["goals"])],
        [_debt(d, i) for i, d in enumerate(rows["debts"])],
        [str(p) for p in rows["protect"]],
        _mix(data.get("target_mix")),
    )
    for kind, names in (
        ("goal", [g.name for g in out.goals]),
        ("debt", [d.name for d in out.debts]),
    ):
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            raise GoalsError(f"{kind} named twice: {', '.join(twice)}")
    ranks = [g.rank for g in out.goals]
    shared = sorted({r for r in ranks if ranks.count(r) > 1})
    if shared:
        raise GoalsError(f"rank shared by two goals: {', '.join(map(str, shared))}")
    bad = sorted(set(out.protect) - set(PROTECT))
    if bad:
        raise GoalsError(
            f"protect: unknown line(s) {', '.join(bad)} (one of {', '.join(PROTECT)})"
        )
    return out


def load(lay: Layout) -> Goals:
    p = path(lay)
    if not p.exists():
        return Goals()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise GoalsError(f"{p}: expected a mapping at top level")
    try:
        return parse(data)
    except GoalsError as exc:
        raise GoalsError(f"{p}: {exc}") from exc


def _raw(lay: Layout) -> dict[str, Any]:
    p = path(lay)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _save(lay: Layout, data: dict[str, Any]) -> Goals:
    """Check the whole file before writing it: a refused change leaves the
    file as it was."""
    data = {k: v for k, v in data.items() if v not in (None, [], {})}
    goals = parse(data)
    p = path(lay)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return goals


def _upsert(rows: list[dict[str, Any]], name: str, fields: dict[str, Any]) -> None:
    entry = next((r for r in rows if r.get("name") == name), None)
    if entry is None:
        entry = {"name": name}
        rows.append(entry)
    entry.update({k: v for k, v in fields.items() if v is not None})


def save_goal(lay: Layout, name: str, **fields: Any) -> Goals:
    """Add a goal or change one (by name). A new goal with no rank goes last."""
    if fields.get("amount") is not None:
        fields["amount"] = _money(f"goal {name}", "amount", fields["amount"])
    if fields.get("date") is not None:
        try:
            fields["date"] = date.fromisoformat(str(fields["date"])).isoformat()
        except ValueError as exc:
            raise GoalsError(f"goal {name}: date must be YYYY-MM-DD") from exc
    data = _raw(lay)
    rows = list(data.get("goals") or [])
    if fields.get("rank") is None and not any(r.get("name") == name for r in rows):
        fields["rank"] = max((int(r.get("rank") or 0) for r in rows), default=0) + 1
    _upsert(rows, name, fields)
    data["goals"] = rows
    return _save(lay, data)


def save_debt(lay: Layout, name: str, **fields: Any) -> Goals:
    for key in ("balance", "rate", "payment"):
        if fields.get(key) is not None:
            fields[key] = _money(f"debt {name}", key, fields[key], key != "balance")
    data = _raw(lay)
    rows = list(data.get("debts") or [])
    _upsert(rows, name, fields)
    data["debts"] = rows
    return _save(lay, data)


def remove(lay: Layout, section: str, name: str) -> Goals:
    data = _raw(lay)
    rows = list(data.get(section) or [])
    left = [r for r in rows if r.get("name") != name]
    if len(left) == len(rows):
        raise GoalsError(f"no {section[:-1]} named {name}")
    data[section] = left
    return _save(lay, data)


def save_protect(lay: Layout, keys: list[str]) -> Goals:
    data = _raw(lay)
    data["protect"] = list(dict.fromkeys(keys))
    return _save(lay, data)


def save_mix(lay: Layout, mix: dict[str, float] | None) -> Goals:
    data = _raw(lay)
    data["target_mix"] = mix
    return _save(lay, data)


def parse_mix(pairs: list[str]) -> dict[str, float]:
    """``["stocks=60", "bonds=40"]`` -> ``{"stocks": 60.0, "bonds": 40.0}``."""
    mix: dict[str, float] = {}
    for pair in pairs:
        key, sep, pct = pair.partition("=")
        if not sep:
            raise GoalsError(f"{pair}: expected class=percent")
        try:
            mix[key.strip()] = float(pct.strip().rstrip("%"))
        except ValueError as exc:
            raise GoalsError(f"{pair}: {pct} is not a percent") from exc
    return mix


def _months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month


def summary(
    g: Goals,
    as_of: date,
    married: bool,
    lines: list[Any] | None = None,
    why_no_lines: str = "",
) -> tuple[list[str], list[str]]:
    """The goals panel: lines and notes. ``lines`` are the MAGI panel's lines
    (planner.plan.magi.Line), read for the protected ones' room."""
    out: list[str] = []
    notes = list(g.notes)
    for goal in g.ranked():
        amount = "amount not entered" if goal.amount is None else f"{goal.amount:,.2f}"
        when = goal.date or "date not entered"
        pace = ""
        if goal.amount is not None and goal.date:
            left = _months_between(as_of, date.fromisoformat(goal.date))
            n = max(left, 1)
            pace = (
                "  date passed"
                if left < 0
                else f"  {goal.amount / n:,.2f}/month for {n} months"
            )
        out.append(
            f"{goal.rank}. {goal.name} ({goal.kind}, {goal.owner}, {goal.flexibility})"
            f"  {amount} by {when}{pace}"
        )
        if goal.amount is None or goal.date is None:
            notes.append(
                f"goal {goal.name}: {'amount' if goal.amount is None else 'date'} not "
                f'entered; it is not counted as 0 (planner goal "{goal.name}" '
                f"--{'amount' if goal.amount is None else 'date'} ...)"
            )
        if goal.owner != "self" and not married:
            notes.append(
                f"goal {goal.name}: owner {goal.owner}, but the filing status is not "
                "married"
            )
    near = [x for x in g.near(as_of) if x.amount is not None]
    if near:
        total = sum(x.amount or 0 for x in near)
        out.append(
            f"dated inside {NEAR_MONTHS} months: {total:,.2f}"
            f" ({', '.join(x.name for x in near)})"
        )
    if not g.goals:
        notes.append('no goals entered (planner goal "NAME" --amount ... --date ...)')
    for d in g.debts:
        paid = d.months()
        if paid is None:
            payoff = "payment does not cover the interest: never paid off"
        else:
            month = _add_months(as_of, paid).strftime("%Y-%m")
            payoff = f"paid off {month} ({paid} payments)"
        out.append(
            f"debt {d.name} ({d.owner}) {d.balance:,.2f} at {d.rate:g}%, "
            f"{d.payment:,.2f}/month: {payoff}"
        )
    by_name = {ln.name: ln for ln in lines or []}
    for key in g.protect:
        label = PROTECT[key]
        hit = next((ln for n, ln in by_name.items() if n.startswith(label)), None)
        if hit is not None:
            state = "OVER" if hit.over else "under"
            out.append(f"protect {label}: {state} by {abs(hit.room):,.2f}")
        elif lines is None:
            out.append(f"protect {label}: room not computed ({why_no_lines})")
        else:
            out.append(f"protect {label}: does not apply this year")
    if g.target_mix is None:
        notes.append(
            "no target mix chosen; the planner proposes no rebalancing "
            "(planner mix stocks=60 bonds=40)"
        )
    else:
        out.append(
            "target mix: "
            + ", ".join(
                f"{k} {g.target_mix[k]:g}%"
                for k in ASSET_CLASSES
                if g.target_mix.get(k)
            )
        )
    return out, notes
