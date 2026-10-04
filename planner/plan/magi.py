"""Full-year MAGI from YTD actuals plus overrides, and the distance to every
line the plan watches. ACA MAGI is the engine's: AGI plus tax-exempt interest
and untaxed Social Security (the add-backs are reported separately)."""

from __future__ import annotations

from dataclasses import dataclass, field

from planner.engine.tax import (
    TaxResult,
    compute,
    engine_value,
    irmaa_first_tier,
    r,
    ss_taxation_thresholds,
    thresholds,
)
from planner.paths import Layout
from planner.plan.inputs import Inputs, Overrides, build

MEDICAID_FPL = 1.38
CSR_FPL = 2.50
CLIFF_FPL = 4.00
# IRC 6654(d)(1)(C): above this AGI, next year's safe harbor is 110% of this
# year's tax (half for married filing separately)
HIGH_INCOME_AGI = 150_000.0
IRMAA_AGE = 63  # Medicare at 65 prices premiums on income two years earlier
WORK_HOURS = 80.0  # the Medicaid work requirement, per month


@dataclass(frozen=True)
class Line:
    name: str
    limit: float
    measure: str  # which figure is tested
    value: float
    direction: str  # use room | get under | both | watch

    @property
    def room(self) -> float:
        return r(self.limit - self.value)

    @property
    def over(self) -> bool:
        return self.value > self.limit


@dataclass(frozen=True)
class Watch:
    """What the lines beyond the engine's result read: the household's age,
    exempt interest and Social Security, the NC rate step, and the Medicaid
    work requirement with the SE-hours log."""

    age: int = 0
    tax_exempt_interest: float = 0.0
    social_security: float = 0.0
    nc_rate: float | None = None
    nc_next_rate: float | None = None  # next year's scheduled rate
    work_requirement_from: int | None = None
    medicaid_target: bool = False  # conversion_objective medicaid_under
    se_hours: dict[int, float] = field(default_factory=dict)  # month -> hours


@dataclass
class Projection:
    inputs: Inputs
    result: TaxResult
    lines: list[Line] = field(default_factory=list)

    @property
    def aca_addbacks(self) -> float:
        return r(self.result.aca_magi - self.result.agi)


def lines(res: TaxResult, filing_status: str, watch: Watch | None = None) -> list[Line]:
    th = thresholds(res.year, filing_status)
    fpg, aca_fpg = (
        res.fpg,
        res.aca_fpg,
    )  # Medicaid: this year's; the credit: last year's
    return [
        Line("standard deduction", th["std_deduction"], "AGI", res.agi, "use room"),
        Line(
            "0% LTCG / qualified-dividend ceiling",
            th["ltcg_0pct_top"],
            "taxable income",
            res.taxable_income,
            "both",
        ),
        Line(
            "12% bracket top",
            th["bracket_12pct_top"],
            "taxable income",
            res.taxable_income,
            "both",
        ),
        Line(
            "Medicaid 138% FPL (monthly)",
            r(fpg * MEDICAID_FPL / 12),
            "ACA MAGI / 12",
            res.medicaid_magi_monthly,
            "get under",
        ),
        Line(
            "ACA CSR 250% FPL",
            r(aca_fpg * CSR_FPL),
            "ACA MAGI",
            res.aca_magi,
            "get under",
        ),
        Line(
            "ACA 400% FPL cliff",
            r(aca_fpg * CLIFF_FPL),
            "ACA MAGI",
            res.aca_magi,
            "get under",
        ),
        Line("NIIT", th["niit_threshold"], "AGI", res.agi, "watch"),
        _safe_harbor(res, filing_status),
    ] + (_watched(res, filing_status, watch) if watch else [])


def _safe_harbor(res: TaxResult, filing_status: str) -> Line:
    top = HIGH_INCOME_AGI / (2 if filing_status == "SEPARATE" else 1)
    return Line(
        f"safe harbor 110% for {res.year + 1} (AGI over {top:,.0f})",
        top,
        "AGI",
        res.agi,
        "watch",
    )


def _watched(res: TaxResult, filing_status: str, w: Watch) -> list[Line]:
    out = []
    now, nxt = w.nc_rate, w.nc_next_rate
    if now and nxt is not None and nxt < now:
        out.append(
            Line(
                f"NC rate {now:.2%}, {nxt:.2%} scheduled {res.year + 1}",
                r(res.state_tax * nxt / now),
                "NC tax at next year's rate",
                res.state_tax,
                "watch",
            )
        )
    if w.age >= IRMAA_AGE:
        out.append(
            Line(
                f"IRMAA first tier (Medicare premiums in {res.year + 2})",
                irmaa_first_tier(res.year, filing_status),
                "AGI + tax-exempt interest",
                r(res.agi + w.tax_exempt_interest),
                "get under",
            )
        )
    if w.social_security > 0:
        # the engine's ACA MAGI adds exempt interest and untaxed benefits to AGI
        untaxed = res.aca_magi - res.agi - w.tax_exempt_interest
        taxed = w.social_security - untaxed
        provisional = r(res.agi - taxed + w.tax_exempt_interest + w.social_security / 2)
        half, most = ss_taxation_thresholds(res.year, filing_status)
        out += [
            Line(
                "Social Security 50% taxable",
                half,
                "provisional income",
                provisional,
                "watch",
            ),
            Line(
                "Social Security 85% taxable",
                most,
                "provisional income",
                provisional,
                "watch",
            ),
        ]
    if (
        w.medicaid_target
        and w.work_requirement_from is not None
        and res.year >= w.work_requirement_from
    ):
        logged = [w.se_hours.get(m, 0.0) for m in range(1, 13) if m in w.se_hours]
        out.append(
            Line(
                f"Medicaid work requirement ({WORK_HOURS:g} hours a month)",
                WORK_HOURS,
                "fewest SE hours logged in a month",
                min(logged, default=0.0),
                "watch",
            )
        )
    return out


def watch_for(lay: Layout, year: int, inputs: Inputs) -> Watch:
    """The household and config facts the extra lines read."""
    from planner.config import load_thresholds
    from planner.ingest.needs import MANUAL_VALUES, load_manual, load_profile

    rows = load_thresholds(lay.config / "thresholds.yaml")
    row = rows.get(year, {})
    start = next(
        (
            int(str(rows[y]["medicaid_work_requirement_start"]["value"])[:4])
            for y in sorted(rows)
            if "medicaid_work_requirement_start" in rows[y]
        ),
        None,
    )
    nxt = row.get("nc_income_tax_rate_next")
    hours = load_manual(lay, year)[MANUAL_VALUES].get("se_hours") or {}
    hh = inputs.household
    return Watch(
        age=hh.age,
        tax_exempt_interest=float(hh.tax_exempt_interest),
        social_security=float(hh.social_security),
        nc_rate=engine_value("nc_income_tax_rate", year)[0]
        if hh.state == "NC"
        else None,
        nc_next_rate=float(nxt["value"]) if nxt and hh.state == "NC" else None,
        work_requirement_from=start,
        medicaid_target=load_profile(lay).get("conversion_objective")
        == "medicaid_under",
        se_hours={int(k): float(v) for k, v in hours.items()},
    )


def project(lay: Layout, year: int, overrides: Overrides | None = None) -> Projection:
    inputs = build(lay, year, overrides)
    res = compute(year, inputs.household)
    watched = lines(res, inputs.household.filing_status, watch_for(lay, year, inputs))
    return Projection(inputs, res, watched)
