"""How much of a state or local income tax refund is taxable: the State and
Local Income Tax Refund Worksheet (Form 1040 instructions, Schedule 1 line 1).

A refund is income only to the extent the deduction it reverses lowered last
year's tax (the tax benefit rule, IRC 111). The worksheet compares last year's
itemized deductions (line 4) with the standard deduction you could have taken
(lines 5-7): when they were not more, none of the refund is taxable (line 8
"STOP"). The planner reads line 4 from last year's filed Form 1040 line 12
(Schedule A line 17 when you itemized, the standard deduction otherwise). When
the itemized deductions were larger, the taxable part also turns on last year's
Schedule A lines 5d and 5e (worksheet lines 1-3), which the planner does not
read, so that answer is yours to type, with the most it can be.
"""

from __future__ import annotations

from datetime import date

from planner.engine import tax as engine

STANDARD = "gov.irs.deductions.standard.amount"
EXTRA = "gov.irs.deductions.standard.aged_or_blind.amount"
WORKSHEET = "the Schedule 1 line 1 worksheet in the Form 1040 instructions"


def aged(birth: str | None, prior: int) -> bool:
    """Worksheet line 6's box: born before January 2 of the year 65 years
    before ``prior`` (65 by the end of ``prior``)."""
    return birth is not None and date.fromisoformat(birth) < date(prior - 64, 1, 2)


def taxable_part(
    refund: float, deduction: float, status: str, prior: int, boxes: int
) -> tuple[float | None, str]:
    """(taxable part or None, why) for a ``refund`` received this year, given
    last year's (``prior``) Form 1040 line 12 ``deduction``, the engine filing
    ``status`` and the number of line 6 boxes checked (``boxes``, 65 or older;
    blindness is not asked, so a blind filer's boxes are undercounted and the
    answer errs toward asking). None means the planner cannot finish it."""
    if status == "SEPARATE":
        return None, (
            f"married filing separately: if your spouse itemized in {prior}, "
            f"lines 5-7 are skipped; use {WORKSHEET}"
        )
    line7 = engine._param(f"{STANDARD}.{status}", prior) + boxes * engine._param(
        f"{EXTRA}.{status}", prior
    )
    if deduction <= line7:
        return 0.0, (
            f"{prior} Form 1040 line 12 {deduction:,.0f} is not more than the "
            f"{line7:,.0f} standard deduction (worksheet lines 4-8): none of the "
            f"refund is taxable"
        )
    most = min(refund, deduction - line7)
    return None, (
        f"you itemized in {prior} ({deduction:,.0f} on line 12, {line7:,.0f} "
        f"standard): up to {most:,.0f} is taxable; finish {WORKSHEET} with your "
        f"{prior} Schedule A lines 5d and 5e and type line 9"
    )
