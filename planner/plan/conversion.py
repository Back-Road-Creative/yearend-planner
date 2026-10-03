"""Roth conversion candidates, each priced by the engine in one sweep.

A candidate is the largest (or, for landing over the Medicaid line, the
smallest) conversion that keeps a watched line, less the profile's margin:
the 0% LTCG ceiling, the 12% bracket top, the ACA 400% cliff, under or just
over the Medicaid line, and the hard cap. Every candidate shows the federal
and NC tax it adds, the ACA credit it costs, what happens to Medicaid in the
month it lands, and the cash needed from outside the IRA to pay for it. The
recommendation is the candidate matching the profile's objective; the rest
stay on the page. The source is a traditional IRA only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from planner.engine.tax import compute, compute_sweep, r, thresholds
from planner.ingest.needs import load_profile
from planner.ledger import portfolio
from planner.paths import Layout
from planner.plan.inputs import Overrides
from planner.plan.magi import CLIFF_FPL, MEDICAID_FPL, Projection, project

VARIABLE = "taxable_roth_conversions"
OBJECTIVES = ("ltcg_0pct", "bracket_12", "aca_400", "medicaid_under", "medicaid_over")
LABEL = {
    "ltcg_0pct": "fill to the 0% LTCG line",
    "bracket_12": "fill to the 12% bracket top",
    "aca_400": "stay under the ACA 400% cliff",
    "medicaid_under": "stay under the Medicaid line in the month it lands",
    "medicaid_over": "land just over the Medicaid line",
    "cap": "the hard cap",
}


@dataclass(frozen=True)
class Candidate:
    name: str
    amount: float
    taxable_income: float
    aca_magi: float
    fed_tax: float
    state_tax: float
    aca_ptc: float
    fed_delta: float
    state_delta: float
    ptc_delta: float  # negative = credit lost
    medicaid_month_over: bool
    qualified_spill: bool  # qualified dividends / LTCG pushed into 15%
    note: str = ""

    @property
    def cash_needed(self) -> float:
        return r(self.fed_delta + self.state_delta - self.ptc_delta)


@dataclass
class Sizing:
    base: Projection
    already: float  # conversions already in the year
    cap: float
    margin: float
    objective: str | None
    trad_ira_balance: float | None
    candidates: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def recommendation(self) -> Candidate | None:
        for c in self.candidates:
            if c.name == self.objective:
                return c
        return None


def _largest(rows: list[dict[str, float]], ok: Any) -> dict[str, float] | None:
    best = None
    for row in rows:
        if ok(row):
            best = row
    return best


def _smallest(rows: list[dict[str, float]], ok: Any) -> dict[str, float] | None:
    for row in rows:
        if ok(row):
            return row
    return None


def size(
    lay: Layout,
    year: int,
    overrides: Overrides | None = None,
    step: int = 500,
) -> Sizing:
    base = project(lay, year, overrides)
    hh = base.inputs.household
    profile = load_profile(lay)
    st = portfolio.status(lay, year)
    trad = [p.value for p in st.positions if p.type == "trad_ira"]
    balance = r(sum(trad)) if trad else None
    margin = float(profile.get("conversion_margin") or 0)
    cap_profile = profile.get("conversion_cap")
    already = float(hh.roth_conversion)
    limits = [float(cap_profile)] if cap_profile is not None else []
    if balance is not None:
        limits.append(already + balance)
    sizing = Sizing(
        base, already, 0.0, margin, profile.get("conversion_objective"), balance
    )
    if balance is None:
        sizing.notes.append(
            "no account typed trad_ira: candidates are capped by conversion_cap only "
            f"(planner needed --year {year})"
        )
    if cap_profile is None:
        sizing.notes.append("conversion_cap not set: the hard cap is the IRA balance")
    if not limits:
        sizing.notes.append("no cap and no traditional IRA balance: nothing to size")
        return sizing
    sizing.cap = r(min(limits))
    hi = int(sizing.cap)
    if hi <= already:
        sizing.notes.append("the year's conversions already reach the cap")
        return sizing
    rows = compute_sweep(year, replace(hh, roth_conversion=0), VARIABLE, 0, hi, step)
    base_row = _smallest(rows, lambda rw: rw[VARIABLE] >= already) or rows[0]
    th = thresholds(year, hh.filing_status)
    fpg = base.result.fpg
    cliff = fpg * CLIFF_FPL - margin
    medicaid_month = fpg * MEDICAID_FPL / 12
    # Medicaid counts a conversion only in the month it lands (42 CFR 435.603(e)(1)),
    # so the test is recurring monthly income (without this year's conversions)
    # plus the increment being sized.
    recurring_monthly = (base_row["aca_magi"] - already) / 12
    picks: dict[str, dict[str, float] | None] = {
        "ltcg_0pct": _largest(
            rows, lambda rw: rw["taxable_income"] <= th["ltcg_0pct_top"] - margin
        ),
        "bracket_12": _largest(
            rows, lambda rw: rw["taxable_income"] <= th["bracket_12pct_top"] - margin
        ),
        "aca_400": _largest(rows, lambda rw: rw["aca_magi"] <= cliff),
        "medicaid_under": _largest(
            rows,
            lambda rw: (
                recurring_monthly + (rw[VARIABLE] - already) <= medicaid_month - margin
            ),
        ),
        "medicaid_over": _smallest(
            rows,
            lambda rw: (
                recurring_monthly + (rw[VARIABLE] - already) > medicaid_month + margin
            ),
        ),
        "cap": rows[-1],
    }
    for name, row in picks.items():
        if row is None or row[VARIABLE] < already:
            sizing.notes.append(f"{name}: no amount in range meets it")
            continue
        amount = row[VARIABLE]
        full = compute(year, replace(hh, roth_conversion=int(amount)))
        sizing.candidates.append(
            Candidate(
                name,
                amount=r(amount),
                taxable_income=row["taxable_income"],
                aca_magi=row["aca_magi"],
                fed_tax=row["income_tax"],
                state_tax=row["state_income_tax"],
                aca_ptc=row["aca_ptc"],
                fed_delta=r(row["income_tax"] - base_row["income_tax"]),
                state_delta=r(row["state_income_tax"] - base_row["state_income_tax"]),
                ptc_delta=r(row["aca_ptc"] - base_row["aca_ptc"]),
                medicaid_month_over=recurring_monthly + (amount - already)
                > medicaid_month,
                qualified_spill=full.ltcg_tax > 0,
                note=LABEL[name],
            )
        )
    if sizing.objective is None:
        sizing.notes.append(
            "conversion_objective not set: no recommendation "
            f"(planner enter conversion_objective <{'|'.join(OBJECTIVES)}>)"
        )
    elif sizing.recommendation is None:
        sizing.notes.append(f"objective {sizing.objective}: no candidate meets it")
    return sizing
