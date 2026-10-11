"""Form 6251, Alternative Minimum Tax—Individuals, laid onto the draft from the
same engine run as the federal return (unit 3e-8).

Line numbers and rules follow the 2025 Form 6251 and its instructions; the
incentive stock option rule (line 2i) follows Pub. 525. Part I starts from
Form 1040 line 14 less Schedule 1-A line 37 (1a), takes it from line 11b (1b),
adds back the Schedule A taxes or the standard deduction (2a) and the spread
on incentive stock options exercised and kept (2i, the Needed panel's
``iso_amt_adjustment``: Form 3921 (box 4 - box 3) x box 5). Married filing
separately, line 4 over the separate limit grows by 25% of the excess, not more
than the exemption. Part II takes the exemption less 25% of line 4 over the
phase-out start (5) and taxes the rest at 26% and 28% (7); when Part III is
required (capital gain distributions, qualified dividends or gains on Schedule
D lines 15 and 16) line 7 is the engine's Part III figure. Line 8 is the
regular foreign tax credit (Schedule 3 line 1), which the instructions allow
when the AMT credit would come out the same; line 10 is 1040 line 16 plus
Schedule 2 line 1z less Schedule 3 line 1.

Not drafted, and named: lines 2b-2h and 2j-3, Part III's worksheet lines
12-40, the AMT Form 1116, Form 4972's tax on line 10 and the Form 8801 credit
the ISO adjustment earns in a later year.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from planner.engine import tax

FORM = "6251"
AMT = "gov.irs.income.amt"
ENGINE = (
    tax.ISO,
    "amt_income",
    "amt_part_iii_required",
    "amt_base_tax",
    "amt_tax_including_cg",
    "alternative_minimum_tax",
    "salt_deduction",
    "standard_deduction",
    "tax_unit_itemizes",
)

Add = Callable[..., float]


def required(v: Mapping[str, float]) -> bool:
    """File Form 6251: AMT is due or incentive stock options were exercised."""
    return v["alternative_minimum_tax"] > 0 or v[tax.ISO] > 0


def lay_lines(
    add: Add,
    notes: list[str],
    v: Mapping[str, float],
    year: int,
    status: str,
    agi: float,
    deductions: float,
    senior: float,
    ftc: float,
    line10: float,
) -> float:
    """Add Form 6251's lines; returns line 11 (Schedule 2 line 2). ``agi`` is
    1040 line 11b, ``deductions`` line 14, ``senior`` Schedule 1-A line 37,
    ``ftc`` Schedule 3 line 1 and ``line10`` 1040 line 16 plus Schedule 2 line
    1z."""
    l1a = add(
        FORM,
        "1a",
        "Form 1040 line 14 less Sch 1-A line 37",
        deductions - senior,
        "1040 line 14 - Sch 1-A line 37",
    )
    l1b = add(FORM, "1b", "Line 11b less line 1a", agi - l1a, "1040 line 11b - 1a")
    if v["tax_unit_itemizes"] > 0:
        l2a = add(
            FORM,
            "2a",
            "Taxes from Schedule A, line 7",
            v["salt_deduction"],
            "engine salt_deduction (Sch A line 7)",
        )
    else:
        l2a = add(
            FORM,
            "2a",
            "Standard deduction, Form 1040 line 12e",
            v["standard_deduction"],
            "1040 line 12e",
        )
    l2i = add(
        FORM,
        "2i",
        "Exercise of incentive stock options",
        v[tax.ISO],
        "Needed panel iso_amt_adjustment (Form 3921, Pub. 525)",
    )
    line4 = l1b + l2a + l2i
    limit = tax._param(f"{AMT}.exemption.separate_limit", year)
    amount = tax._param(f"{AMT}.exemption.amount.{status}", year)
    rate = tax._param(f"{AMT}.exemption.phase_out.rate", year)
    src = "1b + 2a + 2i (2b-2h, 2j-3 not drafted)"
    if status == "SEPARATE" and line4 > limit:
        line4 += min(rate * (line4 - limit), amount)
        src += (
            f", plus 25% of the excess over {limit:,.0f} (not more than {amount:,.0f})"
        )
    l4 = add(FORM, "4", "Alternative minimum taxable income", line4, src)
    start = tax._param(f"{AMT}.exemption.phase_out.start.{status}", year)
    l5 = add(
        FORM,
        "5",
        "Exemption",
        max(amount - rate * max(l4 - start, 0.0), 0.0),
        f"{amount:,.0f} less 25% of line 4 over {start:,.0f}",
    )
    l6 = add(FORM, "6", "Line 4 less line 5", max(l4 - l5, 0.0), "4 - 5, not below 0")
    if v["amt_part_iii_required"]:
        l7 = add(
            FORM,
            "7",
            "Tax (Part III)",
            min(v["amt_base_tax"], v["amt_tax_including_cg"]) if l6 else 0.0,
            "engine Part III line 40 (lines 12-40 not drafted)",
        )
    else:
        scale = tax.brackets(f"{AMT}.brackets", year)
        top = scale[-1][0] * tax._param(f"{AMT}.multiplier.{status}", year)
        low, high = scale[0][1], scale[-1][1]
        l7 = add(
            FORM,
            "7",
            "Tax",
            min(l6, top) * low + max(l6 - top, 0.0) * high,
            f"line 6 x {low:.0%} up to {top:,.0f}, {high:.0%} above",
        )
    l8 = add(
        FORM,
        "8",
        "Alternative minimum tax foreign tax credit",
        min(ftc, l7),
        "Sch 3 line 1 (the regular credit; AMT Form 1116 not drafted)",
    )
    l9 = add(FORM, "9", "Tentative minimum tax", l7 - l8, "7 - 8")
    l10 = add(
        FORM,
        "10",
        "Regular tax less the foreign tax credit",
        max(line10 - ftc, 0.0),
        "1040 line 16 + Sch 2 line 1z - Sch 3 line 1 (Form 4972 not drafted)",
    )
    l11 = add(
        FORM,
        "11",
        "Alternative minimum tax",
        max(l9 - l10, 0.0),
        "9 - 10, not below 0 (to Sch 2 line 2)",
    )

    # Lines 4 and 7 against the engine's own figures. Line 11 can differ from
    # the engine's AMT by line 10: 1040 line 16 follows the Tax Table.
    if abs(l4 - v["amt_income"]) > 1:
        notes.append(
            f"CHECK: Form 6251 line 4 against the engine's AMT income: "
            f"{l4:,.2f} vs {v['amt_income']:,.2f}"
        )
    want = v["amt_base_tax"]
    if v["amt_part_iii_required"]:
        want = min(want, v["amt_tax_including_cg"])
    if abs(l7 - want) > 1:
        notes.append(
            f"CHECK: Form 6251 line 7 against the engine's tax: "
            f"{l7:,.2f} vs {want:,.2f}"
        )
    if l2i:
        notes.append(
            "Form 6251 line 2i: keep the AMT basis of the incentive stock option "
            "shares (exercise price plus the line 2i spread); a later sale's gain "
            "for the AMT is figured on it (line 2k), and AMT paid on the spread "
            "can come back as the Form 8801 credit in a later year (Pub. 525)"
        )
    notes.append(
        "Form 6251 lines 2b-2h and 2j-3, Part III's lines 12-40, the AMT Form "
        "1116 and Form 8801 are not drafted"
    )
    return l11
