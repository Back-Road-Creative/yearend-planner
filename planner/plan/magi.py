"""Full-year MAGI from YTD actuals plus overrides, and the distance to every
line the plan watches. ACA MAGI is the engine's: AGI plus tax-exempt interest
and untaxed Social Security (the add-backs are reported separately)."""

from __future__ import annotations

from dataclasses import dataclass, field

from planner.engine.tax import TaxResult, compute, r, thresholds
from planner.paths import Layout
from planner.plan.inputs import Inputs, Overrides, build

MEDICAID_FPL = 1.38
CSR_FPL = 2.50
CLIFF_FPL = 4.00


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


@dataclass
class Projection:
    inputs: Inputs
    result: TaxResult
    lines: list[Line] = field(default_factory=list)

    @property
    def aca_addbacks(self) -> float:
        return r(self.result.aca_magi - self.result.agi)


def lines(res: TaxResult, filing_status: str) -> list[Line]:
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
    ]


def project(lay: Layout, year: int, overrides: Overrides | None = None) -> Projection:
    inputs = build(lay, year, overrides)
    res = compute(year, inputs.household)
    return Projection(inputs, res, lines(res, inputs.household.filing_status))
