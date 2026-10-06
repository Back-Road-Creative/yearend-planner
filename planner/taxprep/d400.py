"""NC Form D-400 and its Schedule S, laid onto the draft from the same engine
run as the federal return, for a full-year North Carolina resident.

Line numbers, the rate, the standard deduction and the child deduction table
follow the 2025 D-400 instructions (D-401, pp. 12-18). The engine prices the
deductions; this module adds what it does not see: Schedule S items you type,
the use tax you type, NC withholding from the W-2 and 1099-R boxes and the NC
estimated payments recorded for the year. Not handled, and named: part-year and
nonresident returns (line 13, Schedule PN), D-400TC credits, penalties and
interest (lines 26b-26e, Form D-422), and amended-return lines 22 and 24.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from planner.ledger import db

FORM = "D-400"
SCHED = "Sch S"
# Line 15: NC taxable income times a flat rate, G.S. 105-153.7.
RATE = {
    2024: (0.045, "2024 D-401 p. 7"),
    2025: (0.0425, "2025 D-401 p. 7"),
    2026: (0.0399, "G.S. 105-153.7 as amended by S.L. 2023-134"),
}
ENGINE = (
    "us_govt_interest",
    "taxable_social_security",
    "ctc_qualifying_children",
    "nc_child_deduction",
    "nc_standard_deduction",
    "nc_itemized_deductions",
    "nc_standard_or_itemized_deductions",
    "nc_taxable_income",
    "nc_non_refundable_credits",
    "nc_use_tax",
)
KEYS = ("nc_additions", "nc_other_deductions", "nc_use_tax", "nc_withheld")
US_INTEREST = (("1099-INT", "3"),)
NC_WITHHELD = (("W-2", "17"), ("1099-R", "14"), ("1099-G", "11"))
TOLERANCE = 1.0

Add = Callable[..., float]


def rate(year: int) -> tuple[float, str]:
    """The year's rate; a year not listed uses the latest earlier one, and says so."""
    if year in RATE:
        return RATE[year]
    earlier = [y for y in RATE if y < year]
    if not earlier:
        raise ValueError(f"no NC income tax rate on file for {year}")
    r, why = RATE[max(earlier)]
    return r, f"{why} ({year} not on file; {max(earlier)} rate)"


def _given(typed: Mapping[str, Any], key: str) -> tuple[float, str]:
    value = typed.get(key)
    if value is None:
        return 0.0, f"{key}: not given (left out, not zero)"
    return float(value), "typed"


def lay_lines(
    add: Add,
    notes: list[str],
    v: Mapping[str, float],
    facts: list[db.FactRow],
    paid: list[tuple[str, float, str]],
    year: int,
    agi: float,
    typed: Mapping[str, Any],
    status: str,
) -> None:
    """Add the Schedule S and D-400 lines. ``paid`` is (date, amount, origin) for
    each NC estimated payment; ``agi`` is 1040 line 11a; ``status`` is the
    filing status (Household.filing_status)."""
    married = status == "JOINT"
    # Schedule S
    additions, add_src = _given(typed, "nc_additions")
    s16 = add(SCHED, "16", "Total additions (Part A)", additions, add_src)
    us = round(sum(f.value for f in facts if (f.form, f.box) in US_INTEREST), 2)
    cited = sorted(
        {
            f"1099-INT box 3 ({f.issuer})"
            for f in facts
            if f.box == "3" and f.form == "1099-INT"
        }
    )
    s18 = add(
        SCHED,
        "18",
        "Interest on US obligations",
        us,
        ", ".join(cited) or "no form shows any",
    )
    s19 = add(
        SCHED,
        "19",
        "Taxable Social Security and Railroad Retirement",
        v["taxable_social_security"],
        "1040 line 6b",
    )
    other, other_src = _given(typed, "nc_other_deductions")
    s_other = add(
        SCHED,
        "20-40",
        "Other deductions (Bailey, uniformed services, ...)",
        other,
        other_src,
    )
    s41 = add(
        SCHED, "41", "Total deductions (Part B)", s18 + s19 + s_other, "18 + 19 + 20-40"
    )

    # D-400 income
    l6 = add(FORM, "6", "Federal adjusted gross income", agi, "1040 line 11a")
    l7 = add(FORM, "7", "Additions to federal AGI", s16, "Sch S line 16")
    l8 = add(FORM, "8", "Lines 6 and 7", l6 + l7, "6 + 7")
    l9 = add(FORM, "9", "Deductions from federal AGI", s41, "Sch S line 41")
    add(
        FORM,
        "10a",
        "Qualifying children",
        v["ctc_qualifying_children"],
        "engine ctc_qualifying_children",
        0,
    )
    l10b = add(
        FORM,
        "10b",
        "Child deduction",
        v["nc_child_deduction"],
        "engine nc_child_deduction (D-401 child deduction table)",
    )
    itemized = v["nc_itemized_deductions"] > v["nc_standard_deduction"]
    l11 = add(
        FORM,
        "11",
        "NC itemized deductions" if itemized else "NC standard deduction",
        v["nc_standard_or_itemized_deductions"],
        "engine nc_itemized_deductions (Schedule A)"
        if itemized
        else "engine nc_standard_deduction",
    )
    l12a = add(FORM, "12a", "Lines 9, 10b and 11", l9 + l10b + l11, "9 + 10b + 11")
    l12b = add(FORM, "12b", "Line 8 less line 12a", l8 - l12a, "8 - 12a")
    l14 = add(FORM, "14", "NC taxable income", l12b, "line 12b (full-year resident)")

    # D-400 tax
    r, r_src = rate(year)
    l15 = add(FORM, "15", "NC income tax", max(l14, 0.0) * r, f"14 x {r:.2%} ({r_src})")
    l16 = add(
        FORM,
        "16",
        "Tax credits",
        v["nc_non_refundable_credits"],
        "engine nc_non_refundable_credits (D-400TC credits are not drafted)",
    )
    l17 = add(FORM, "17", "Line 15 less line 16", max(l15 - l16, 0.0), "15 - 16")
    use = typed.get("nc_use_tax")
    l18 = add(
        FORM,
        "18",
        "Consumer use tax",
        float(use) if use is not None else v["nc_use_tax"],
        "typed"
        if use is not None
        else "engine nc_use_tax (the D-401 use tax table by income)",
    )
    l19 = add(FORM, "19", "Lines 17 and 18", l17 + l18, "17 + 18")

    # D-400 payments
    # Married filing jointly: yours on 20a, the spouse's on 20b (2025 D-401
    # p. 15), read from whose documents show it (unit 3a-4).
    withheld = typed.get("nc_withheld")
    total = float(withheld or 0.0)
    l20b = 0.0
    if married:
        l20b = add(
            FORM,
            "20b",
            "Spouse's NC income tax withheld",
            min(
                sum(
                    f.value
                    for f in facts
                    if f.owner == "spouse" and (f.form, f.box) in NC_WITHHELD
                ),
                total,
            ),
            "the spouse's W-2 box 17 and 1099-R box 14",
        )
    l20a = add(
        FORM,
        "20a",
        "Your NC income tax withheld" if married else "NC income tax withheld",
        total - l20b,
        ("nc_withheld less line 20b" if married else "W-2 box 17, 1099-R box 14")
        + " (Needed panel nc_withheld)"
        if withheld is not None
        else "nc_withheld: not given (left out, not zero)",
    )
    l21a = add(
        FORM,
        "21a",
        f"{year} estimated tax",
        sum(a for _, a, _ in paid),
        "; ".join(f"{d} {a:,.2f} ({o})" for d, a, o in paid)
        or "no NC payment recorded (planner paid)",
    )
    l23 = add(FORM, "23", "Total payments", l20a + l20b + l21a, "20a + 20b + 21a")
    l25 = add(
        FORM, "25", "Payments less previous refunds", l23, "line 23 (not amended)"
    )
    if l25 >= l19:
        l28 = add(FORM, "28", "Overpayment", l25 - l19, "25 - 19")
        add(FORM, "34", "Amount to be refunded", l28, "28 (nothing applied to 29-32)")
    else:
        l26a = add(FORM, "26a", "Tax due", l19 - l25, "19 - 25")
        add(FORM, "27", "Amount due", l26a, "26a (penalties and interest not drafted)")

    # Line 14 against the engine's NC taxable income: the engine does not see
    # the typed Schedule S items, so they are put back before comparing.
    want = v["nc_taxable_income"] + additions - other
    if abs(l14 - want) > TOLERANCE:
        notes.append(
            f"CHECK: D-400 line 14 against the engine's NC taxable income "
            f"(with the typed Schedule S items): {l14:,.2f} vs {want:,.2f}"
        )
    if use is None and v["nc_use_tax"]:
        notes.append(
            "D-400 line 18 is the use tax table's estimate for your income; type "
            "nc_use_tax when your records show the use tax actually owed"
        )
    if married and l20a and not l20b:
        notes.append(
            "D-400 line 20b: no NC withholding is on a document marked the "
            "spouse's; if some of line 20a is theirs, mark it (data/inbox/spouse/ or "
            "planner owner <file> spouse)"
        )
    notes.append(
        "the D-400 draft is for a full-year NC resident: a part-year or nonresident "
        "return needs Schedule PN (line 13), which is not drafted"
    )
