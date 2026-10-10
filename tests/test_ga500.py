"""Unit 3d-11: Georgia Form 500 on the draft for a full-year resident, with the
Schedule 1 lines behind it, from synthetic W-2, 1099-INT, 1099-R and SSA-1099
boxes. Line numbers, the standard deduction, the dependent exemption, the low
income credit table, the itemizer and child care credits and the rounding rule
follow the 2025 Form 500, its Schedule 1 and the IT-511 booklet (Georgia
Department of Revenue). Next year's safe harbor (Form 500 UET) is line 23 less
line 27."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.ingest.needs import enter, need_value
from planner.ledger import db
from planner.paths import Layout
from planner.plan import esttax
from planner.taxprep import draft, ga500, statereturn
from tests.test_forms import one

F, S1 = ga500.FORM, ga500.SCHED


def _lay(
    planner_home: Path,
    w2: dict[str, float],
    more: list[db.Fact] | None = None,
    status: str = "single",
    dependents: str = "none",
    born: str = "1980-05-01",
) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    facts = [
        db.Fact("W-2", 2025, "Employer (synthetic)", b, "", v, 1) for b, v in w2.items()
    ]
    db.add_document(
        conn,
        fingerprint="synthetic-ga-you",
        file_name="ga-you.pdf",
        kind="pdf",
        pages=1,
        batch="b1",
        facts=facts + (more or []),
        owner="you",
    )
    conn.close()
    for key, text in (
        ("birth_date", born),
        ("filing_status", status),
        ("dependents", dependents),
        ("state", "GA"),
    ):
        enter(lay, 2025, key, text)
    return lay


def _checks(d: draft.Draft) -> list[str]:
    return [n for n in d.notes if n.startswith("CHECK")]


def _lines(d: draft.Draft, form: str, *lines: str) -> tuple[float | None, ...]:
    return tuple(d.get(form, n) for n in lines)


BANK = [
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "1", "", 500.0, 1),
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "3", "", 300.0, 1),
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "8", "", 120.0, 1),
]
W2 = {"1": 60250.0, "2": 6000.0, "17": 1000.0}


def test_form_500_owed_with_us_bond_interest_and_an_estimated_payment(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home, W2, BANK)
    esttax.record(lay, 2025, "ga", "2025-06-15", 100.0)
    d = draft.build(lay, 2025)
    assert _lines(d, S1, "6", "10", "13", "14") == (0.0, 300.0, 300.0, -300.0)
    assert _lines(d, F, "8", "9", "10", "11", "13") == (
        61050.0,
        -300.0,
        60750.0,
        12000.0,
        48750.0,
    )
    # 5.19% of 48,750 = 2,530.13, to the dollar
    assert _lines(d, F, "14", "15c", "16", "22", "23") == (
        0.0,
        48750.0,
        2530.0,
        0.0,
        2530.0,
    )
    assert _lines(d, F, "24", "26", "28", "29", "45", "30") == (
        1000.0,
        100.0,
        1100.0,
        1430.0,
        1430.0,
        None,
    )
    assert d.get(F, "12c") is None
    assert not _checks(d), d.notes
    out = draft.render(d)
    assert "GA Form 500" in out and "2025-06-15 100.00 (typed)" in out
    assert "GA Form 500 Schedule 1" in out
    assert any("other states' municipal bonds" in n for n in d.notes)
    assert any("Form 500 UET" in n and "no 110% rule" in n for n in d.notes)


def test_retirees_exclude_retirement_income_and_social_security(
    planner_home: Path,
) -> None:
    more = [
        db.Fact("1099-R", 2025, "IRA Custodian (synthetic)", "1", "", 50000.0, 1),
        db.Fact("1099-R", 2025, "IRA Custodian (synthetic)", "2a", "", 50000.0, 1),
        db.Fact("1099-R", 2025, "IRA Custodian (synthetic)", "7", "7", 0.0, 1),
        db.Fact("SSA-1099", 2025, "SSA", "5", "", 30000.0, 1),
    ]
    lay = _lay(planner_home, {}, more, status="married_joint", born="1955-05-01")
    enter(lay, 2025, "spouse_birth_date", "1956-03-01")
    d = draft.build(lay, 2025)
    # 65 or older: up to $65,000 of their own retirement income each
    assert _lines(d, S1, "7", "8", "13", "14") == (50000.0, 23850.0, 73850.0, -73850.0)
    assert _lines(d, F, "8", "10", "11", "13") == (73850.0, 0.0, 24000.0, -24000.0)
    assert _lines(d, F, "16", "17c", "23", "29", "30") == (0.0, 0.0, 0.0, None, None)
    assert not _checks(d), d.notes


def test_low_income_credit_follows_the_worksheet_table(planner_home: Path) -> None:
    lay = _lay(planner_home, {"1": 14000.0, "2": 0.0, "17": 50.0})
    d = draft.build(lay, 2025)
    assert _lines(d, F, "13", "15c", "16") == (2000.0, 2000.0, 104.0)  # 103.80
    # federal AGI $10,000 to $14,999: $8 an exemption
    assert _lines(d, F, "17a", "17b", "17c", "22", "23") == (1.0, 8.0, 8.0, 8.0, 96.0)
    assert _lines(d, F, "24", "28", "29") == (50.0, 50.0, 46.0)
    assert not _checks(d), d.notes


def test_head_of_household_takes_the_exemption_and_child_care_credit(
    planner_home: Path,
) -> None:
    w2 = {"1": 30000.0, "2": 0.0, "17": 500.0}
    lay = _lay(planner_home, w2, status="head_of_household", dependents="2018-03-02")
    enter(lay, 2025, "care_expenses", "3000")
    d = draft.build(lay, 2025)
    assert _lines(d, F, "11", "14", "15c", "16") == (12000.0, 4000.0, 14000.0, 727.0)
    # IND-CR 202: half the federal credit claimed (637.50, held to the
    # federal tax), 318.75
    assert _lines(d, F, "17c", "20", "22", "23") == (0.0, 319.0, 319.0, 408.0)
    assert _lines(d, F, "28", "30", "46", "29") == (500.0, 92.0, 92.0, None)
    assert not _checks(d), d.notes


def test_federal_itemizers_itemize_and_take_the_itemizer_credit(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home, {"1": 150000.0, "2": 25000.0, "17": 6000.0})
    for key, text in (("mortgage_interest", "20000"), ("real_estate_taxes", "6000")):
        enter(lay, 2025, key, text)
    d = draft.build(lay, 2025)
    a = d.get(F, "12a")
    assert a and a > 26000.0 and d.get(F, "11") is None
    assert _lines(d, F, "12b", "12c") == (0.0, a)
    assert d.get(F, "13") == 150000.0 - a
    assert _lines(d, F, "19", "22") == (300.0, 300.0)
    assert any("ga_itemized_adjustment" in n for n in d.notes)
    assert not _checks(d), d.notes
    enter(lay, 2025, "ga_itemized_adjustment", "1000")
    d = draft.build(lay, 2025)
    assert _lines(d, F, "12b", "12c") == (1000.0, a - 1000.0)
    assert not any("ga_itemized_adjustment" in n for n in d.notes)


def test_typed_adjustments_land_on_schedule_1(planner_home: Path) -> None:
    lay = _lay(planner_home, W2, BANK)
    for key, text in (("ga_additions", "1000"), ("ga_subtractions", "500")):
        enter(lay, 2025, key, text)
    d = draft.build(lay, 2025)
    assert _lines(d, S1, "6", "12", "13", "14") == (1000.0, 500.0, 800.0, 200.0)
    assert _lines(d, F, "9", "10") == (200.0, 61250.0)
    assert not any("other states' municipal bonds" in n for n in d.notes)
    assert not _checks(d), d.notes


def test_rate_and_rounding_follow_the_form() -> None:
    assert (ga500.dollars(1.49), ga500.dollars(2.50), ga500.dollars(-2.5)) == (
        1.0,
        3.0,
        -3.0,
    )


def test_ga_is_a_registry_entry() -> None:
    ga = statereturn.get("ga")
    assert ga is not None and ga.form == F and ga.template == "GA-500"
    assert ga.tax_line == "23" and ga.carry == "state_tax"
    assert ga.prior == ("23", "-27")
    assert set(ga.forms) == {F, S1}
    assert {"state_withheld", "ga_additions", "ga_itemized_adjustment"} <= set(ga.keys)


HEAD = [
    "500",
    "Georgia Form",
    "Individual Income Tax Return",
    "Georgia Department of Revenue YOUR SOCIAL SECURITY NUMBER",
    "2025",
]
PAGE2 = HEAD + [
    "Page 2",
    "8. Federal adjusted gross income (From Federal Form 1040)...........8. 61,050",
    "9. Adjustments from Form 500 Schedule 1 (See IT-511 Tax Booklet) ......9. -300",
    "10. Georgia adjusted gross income (Net total of Line 8 and Line 9)......10. "
    "60,750",
    "11. Standard Deduction (Do not use FEDERAL STANDARD DEDUCTION).......11. 12,000",
    "13. Subtract either Line 11 or Line 12c from Line 10; enter balance....13. 48,750",
]
PAGE3 = HEAD + [
    "Page 3",
    "14. Enter the number from Lin e 7 c . M ultiply by $4,000..... ......14 . 0",
    "15a. Income before GA NOL (Line 13 less Line 14 or Schedule 3, Line 14)...15a. "
    "48,750",
    "15c. Georgia Taxable Income (Subtract Line 15b from Line 15a)......15c. 48,750",
    "16. Tax (Multiply Line 15c by 5.19%. Round to the nearest dollar) .....16. 2,530",
    "17. Low Income Credit 17a. 1 17b. 8 ..........17c. 8",
    "22. Total Credits Used (sum of Lines 17-21) cannot ex ceed Line 16 .....22. 8",
    "23. B alance(S ubtract Line 22 from Line 16)if zero or less than zero, enter "
    "ze ro.... 23. 2,522",
]
PAGE4 = [
    "500",
    "Georgia Form",
    "Individual Income Tax Return",
    "Georgia Department of Revenue",
    "YOUR SOCIAL SECURITY NUMBER",
    "2025",
    "Page 4",
    "24. Georgia Income Tax Withheld on Wages and 1099s .........24. 1,000",
    "26. Estimated Tax paid for 2025 and Form IT-560 ............26. 100",
    "27. Schedule 2B Refundable Tax Credits....................27. 50",
    "28. T otal prep a yment credits (Add Lines 24, 25, 26 and 27).....28. 1,150",
    "29. If Line 23 exceeds Line 28, subtract Line 28 from Line 23 and enter",
    "balance due ......29. 1,372",
    "31. Amount to be credited to 2026 ESTIMATED TAX ..........31. 0",
]


def test_a_filed_form_500_reads_as_one_form(tmp_path: Path) -> None:
    (f,) = one(tmp_path, "ga500", [PAGE2, PAGE3, PAGE4])
    assert (f.form, f.issuer, f.tax_year) == ("GA-500", "GA", 2025)
    got = {b: f.boxes[b][1] for b in ("8", "9", "10", "11", "13", "14", "15a")}
    assert got == {
        "8": 61050.0,
        "9": -300.0,
        "10": 60750.0,
        "11": 12000.0,
        "13": 48750.0,
        "14": 0.0,
        "15a": 48750.0,
    }
    got = {b: f.boxes[b][1] for b in ("15c", "16", "17c", "22", "23", "24", "26")}
    assert got == {
        "15c": 48750.0,
        "16": 2530.0,
        "17c": 8.0,
        "22": 8.0,
        "23": 2522.0,
        "24": 1000.0,
        "26": 100.0,
    }
    assert {b: f.boxes[b][1] for b in ("27", "28", "29")} == {
        "27": 50.0,
        "28": 1150.0,
        "29": 1372.0,
    }
    assert "30" not in f.boxes and "12c" not in f.boxes


@pytest.mark.parametrize(("credits", "want"), [((("27", 50.0),), 2472.0), ((), 2522.0)])
def test_a_filed_form_500_is_next_years_prior_state_tax(
    planner_home: Path, credits: tuple[tuple[str, float], ...], want: float
) -> None:
    lay = _lay(planner_home, W2)
    enter(lay, 2026, "state", "GA")
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-ga-filed",
        file_name="filed-ga500.pdf",
        kind="pdf",
        pages=3,
        batch="b2",
        facts=[
            db.Fact("GA-500", 2025, "GA", line, "", value, 1)
            for line, value in (("23", 2522.0), ("24", 1000.0), *credits)
        ],
    )
    try:
        # Form 500 UET: line 23 less the Schedule 2B refundable credits on
        # line 27; withholding (24) is a payment, not a credit
        assert need_value(conn, lay, 2026, "prior_state_tax") == want
    finally:
        conn.close()
