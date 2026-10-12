"""The cash reserve (master plan unit 4b): the cash no move may spend.

A typed ``cash_target`` wins. Without one the reserve is the sum of four parts:

  spending     ``reserve_months`` of essential spending (``spending_floor`` / 12)
  deductibles  ``reserve_deductibles``: what a claim takes first (the health
               plan's deductible, the home and car deductibles)
  tax          the next estimated payment each agency still lacks (Form 1040-ES
               and the state's voucher: planner.plan.esttax)
  goals        goals dated inside 12 months (planner.goals.NEAR_MONTHS)

A part not entered is named and left out, never counted as 0, and the reserve
is then a floor. The feasibility check (planner.plan.feasible), the cash panel
(planner.plan.withdraw) and the monthly cash line (planner.plan.glidepath) all
hold it back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from planner import goals as goals_
from planner.engine.tax import r
from planner.paths import Layout
from planner.plan.inputs import Overrides

TYPE = "planner enter {key} <value>"


@dataclass(frozen=True)
class Part:
    name: str
    amount: float | None  # None: not entered, left out
    detail: str


@dataclass(frozen=True)
class Reserve:
    amount: float  # what the checks hold back
    typed: float | None  # cash_target
    basis: str  # typed | computed | none (neither typed nor the rule entered)
    parts: tuple[Part, ...] = ()
    notes: tuple[str, ...] = field(default=())

    def lines(self) -> list[str]:
        if self.basis == "typed":
            head = (
                f"reserve {self.amount:,.2f}: cash_target typed (it wins over the rule)"
            )
        elif self.basis == "computed":
            head = (
                f"reserve {self.amount:,.2f}: the rule (months of spending, "
                "deductibles, the next estimated payment, goals inside "
                f"{goals_.NEAR_MONTHS} months)"
            )
        else:
            head = (
                "no reserve: cash_target is not typed and the rule has nothing "
                "entered; every cash check holds back 0"
            )
        out = [head]
        for p in self.parts:
            if p.amount is None:
                out.append(f"  {p.name}: not entered ({p.detail})")
            else:
                out.append(f"  {p.name} {p.amount:,.2f} ({p.detail})")
        return out


def _spending(profile: dict[str, Any]) -> Part:
    months, floor = profile.get("reserve_months"), profile.get("spending_floor")
    missing = [
        k
        for k, v in (("reserve_months", months), ("spending_floor", floor))
        if v is None
    ]
    if months is None or floor is None:
        return Part("spending", None, "; ".join(TYPE.format(key=k) for k in missing))
    n, per_year = float(months), float(floor)
    return Part(
        "spending",
        r(n * per_year / 12),
        f"{n:g} months of the {per_year:,.0f} spending floor",
    )


def _deductibles(profile: dict[str, Any]) -> Part:
    v = profile.get("reserve_deductibles")
    if v is None:
        return Part("deductibles", None, TYPE.format(key="reserve_deductibles"))
    return Part("deductibles", r(float(v)), "typed: what a claim takes first")


def _tax(et: Any, why: str) -> Part:
    if et is None:
        return Part("tax", None, why or "the estimated-tax figure is not computed")
    due = [a for a in et.agencies if a.next_due and a.next_amount > 0]
    if not due:
        return Part("tax", 0.0, "no estimated payment left to make")
    return Part(
        "tax",
        r(sum(a.next_amount for a in due)),
        ", ".join(f"{a.name} {a.next_amount:,.0f} due {a.next_due}" for a in due),
    )


def _goals(gl: goals_.Goals | None, as_of: date, why: str) -> tuple[Part, list[str]]:
    if gl is None:
        return Part("goals", None, why or "goals not read"), []
    near = gl.near(as_of)
    counted = [g for g in near if g.amount is not None]
    notes = [
        f"goal {g.name} is dated {g.date} but has no amount: not counted as 0 "
        f'(planner goal "{g.name}" --amount ...)'
        for g in near
        if g.amount is None
    ]
    if not counted:
        return Part(
            "goals", 0.0, f"none dated inside {goals_.NEAR_MONTHS} months"
        ), notes
    detail = ", ".join(f"{g.name} {g.amount:,.0f} by {g.date}" for g in counted)
    return Part("goals", r(sum(g.amount or 0 for g in counted)), detail), notes


def compute(
    profile: dict[str, Any],
    gl: goals_.Goals | None,
    as_of: date,
    et: Any,
    why_no_tax: str = "",
    why_no_goals: str = "",
) -> Reserve:
    """The reserve from what is known: ``et`` is an esttax.EstTax (None when
    it could not be figured, ``why_no_tax`` saying why)."""
    typed = profile.get("cash_target")
    goal_part, notes = _goals(gl, as_of, why_no_goals)
    parts = (_spending(profile), _deductibles(profile), _tax(et, why_no_tax), goal_part)
    if typed is not None:
        return Reserve(r(float(typed)), r(float(typed)), "typed", parts, tuple(notes))
    # the rule is in force once one of its own inputs is entered or another
    # part holds money back; with neither there is no reserve, said so
    entered = parts[0].amount is not None or parts[1].amount is not None
    if not entered and not any(p.amount for p in parts):
        return Reserve(0.0, None, "none", parts, tuple(notes))
    amount = r(sum(p.amount for p in parts if p.amount is not None))
    missing = [p.name for p in parts if p.amount is None]
    if missing:
        notes.insert(
            0,
            f"the reserve is at least {amount:,.2f}: {', '.join(missing)} not "
            "entered, left out (not counted as 0)",
        )
    return Reserve(amount, None, "computed", parts, tuple(notes))


def load(
    lay: Layout,
    year: int,
    as_of: date,
    profile: dict[str, Any] | None = None,
    overrides: Overrides | None = None,
    pj: Any = None,
    et: Any = None,
) -> Reserve:
    """The household's reserve. The estimated-tax figure is computed only when
    the rule needs it (no typed cash_target) and ``et`` is not passed; ``pj``
    (a magi.Projection with the same overrides) saves the engine run."""
    from planner.engine.household import MissingInputError
    from planner.ingest.needs import load_profile
    from planner.plan import esttax
    from planner.plan.inputs import OverrideError

    profile = profile if profile is not None else load_profile(lay)
    try:
        gl, why_goals = goals_.load(lay), ""
    except goals_.GoalsError as exc:
        gl, why_goals = None, f"goals refused: {exc}"
    why_tax = ""
    if et is None and profile.get("cash_target") is None:
        try:
            et = esttax.estimate(lay, year, as_of, overrides, pj=pj)
        except (MissingInputError, OverrideError) as exc:
            why_tax = f"the estimated payment is not figured: {exc}"
    elif et is None:
        why_tax = "not figured: the typed cash_target wins"
    return compute(profile, gl, as_of, et, why_tax, why_goals)
