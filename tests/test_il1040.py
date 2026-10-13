"""Unit 3d-9: Illinois Form IL-1040 on the draft for a full-year resident, from
synthetic W-2, 1099-INT, 1099-R and SSA-1099 boxes. Line numbers, the Line 10a
exemption chart and income exceptions, the 4.95% rate, the use tax (UT) table
and the rounding rule follow the 2025 IL-1040 and its instructions (IDOR,
il-1040.pdf and il-1040-instr.pdf, R-12/25): federal AGI plus tax-exempt
interest, retirement income and taxable social security subtracted, US bond
interest on Schedule M, every line half up to the dollar. Next year's safe
harbor (IL-2210 Step 2) is lines 14 and 22 less the credits on 15, 16, 17, 28,
29 and 30."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.ingest.needs import enter, need_value
from planner.ledger import db
from planner.paths import Layout
from planner.plan import esttax
from planner.taxprep import draft, il1040, statereturn
from tests.test_forms import one

F = il1040.FORM


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
        fingerprint="synthetic-il",
        file_name="il.pdf",
        kind="pdf",
        pages=1,
        batch="b1",
        facts=facts + (more or []),
    )
    conn.close()
    for key, text in (
        ("birth_date", born),
        ("filing_status", status),
        ("dependents", dependents),
        ("state", "IL"),
    ):
        enter(lay, 2025, key, text)
    return lay


def _checks(d: draft.Draft) -> list[str]:
    return [n for n in d.notes if n.startswith("CHECK")]


BANK = [
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "1", "", 500.0, 1),
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "3", "", 300.0, 1),
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "8", "", 120.0, 1),
]
W2 = {"1": 60250.0, "2": 6000.0, "17": 2500.0}


def test_il1040_owed_with_exempt_and_bond_interest_and_an_estimated_payment(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home, W2, BANK)
    esttax.record(lay, 2025, "il", "2025-06-15", 20.0)
    d = draft.build(lay, 2025)
    g = d.get
    assert g("1040", "11b") == 61050.0
    assert (g(F, "1"), g(F, "2"), g(F, "3"), g(F, "4")) == (
        61050.0,
        120.0,
        0.0,
        61170.0,
    )
    assert (g(F, "5"), g(F, "6"), g(F, "7"), g(F, "8")) == (0.0, 0.0, 300.0, 300.0)
    assert g(F, "9") == 60870.0
    assert (g(F, "10a"), g(F, "10b"), g(F, "10d"), g(F, "10")) == (
        2850.0,
        0.0,
        0.0,
        2850.0,
    )
    assert g(F, "11") == 58020.0
    assert g(F, "12") == 2872.0  # 58,020 x 4.95% = 2,871.99, to the dollar
    assert (g(F, "14"), g(F, "16"), g(F, "18"), g(F, "19")) == (
        2872.0,
        0.0,
        0.0,
        2872.0,
    )
    assert g(F, "21") == 31.0  # UT Table, $50,001-$75,000
    assert (g(F, "23"), g(F, "24")) == (2903.0, 2903.0)
    assert (g(F, "25"), g(F, "26"), g(F, "29"), g(F, "30")) == (2500.0, 20.0, 0.0, 0.0)
    assert g(F, "31") == 2520.0
    assert (g(F, "33"), g(F, "41")) == (383.0, 383.0)
    assert g(F, "32") is None and g(F, "38") is None
    assert not _checks(d), d.notes
    out = draft.render(d)
    assert "IL Form IL-1040" in out and "2025-06-15 20.00 (typed)" in out
    assert any("il_use_tax" in n for n in d.notes)
    assert any("IL-2210" in n for n in d.notes)


def test_retirement_income_and_social_security_come_out_of_base_income(
    planner_home: Path,
) -> None:
    more = [
        db.Fact("1099-R", 2025, "IRA Custodian (synthetic)", "1", "", 20000.0, 1),
        db.Fact("1099-R", 2025, "IRA Custodian (synthetic)", "2a", "", 20000.0, 1),
        db.Fact("1099-R", 2025, "IRA Custodian (synthetic)", "7", "7", 0.0, 1),
        db.Fact("SSA-1099", 2025, "SSA", "5", "", 30000.0, 1),
    ]
    lay = _lay(planner_home, {}, more, status="married_joint", born="1955-05-01")
    enter(lay, 2025, "spouse_birth_date", "1956-03-01")
    d = draft.build(lay, 2025)
    g = d.get
    assert g(F, "1") == 21500.0  # the IRA plus $1,500 of taxable benefits
    assert (g(F, "5"), g(F, "9")) == (21500.0, 0.0)
    assert (g(F, "10a"), g(F, "10b"), g(F, "10")) == (5700.0, 2000.0, 7700.0)
    assert (g(F, "11"), g(F, "14")) == (0.0, 0.0)
    assert g(F, "21") == 13.0  # UT Table, $20,001-$30,000
    assert (g(F, "33"), g(F, "41")) == (13.0, 13.0)
    assert not _checks(d), d.notes


def test_head_of_household_with_a_young_child_gets_the_il_eitc_and_ctc(
    planner_home: Path,
) -> None:
    w2 = {"1": 18000.0, "2": 0.0, "17": 0.0}
    lay = _lay(planner_home, w2, status="head_of_household", dependents="2018-03-02")
    d = draft.build(lay, 2025)
    g = d.get
    assert (g(F, "10a"), g(F, "10d"), g(F, "10")) == (2850.0, 2850.0, 5700.0)
    assert g(F, "11") == 12300.0
    assert g(F, "12") == 609.0  # 12,300 x 4.95% = 608.85
    assert g(F, "21") == 8.0  # UT Table, $10,001-$20,000
    assert g(F, "23") == 617.0
    assert g(F, "29") == 866.0  # 20% of the federal EITC (4,328), 865.60
    assert g(F, "30") == 346.0  # 40% of the IL EITC, 346.24
    assert g(F, "31") == 1212.0
    assert (g(F, "32"), g(F, "37"), g(F, "38")) == (595.0, 595.0, 595.0)
    assert g(F, "33") is None and g(F, "41") is None
    assert not _checks(d), d.notes


def test_no_exemption_allowance_above_the_income_exception(planner_home: Path) -> None:
    lay = _lay(planner_home, {"1": 260000.0, "2": 50000.0, "17": 12870.0})
    d = draft.build(lay, 2025)
    g = d.get
    assert (g(F, "10a"), g(F, "10")) == (0.0, 0.0)  # single, AGI over $250,000
    assert g(F, "12") == 12870.0
    assert g(F, "21") == 130.0  # above $100,000: AGI x 0.05%
    assert (g(F, "33"), g(F, "41")) == (130.0, 130.0)
    assert any("income exception" in s for s in draft.render(d).splitlines())
    assert not _checks(d), d.notes


def test_typed_schedule_m_items_and_use_tax(planner_home: Path) -> None:
    lay = _lay(planner_home, W2, BANK)
    for key, text in (("il_additions", "1000"), ("il_subtractions", "500")):
        enter(lay, 2025, key, text)
    enter(lay, 2025, "il_use_tax", "0")
    d = draft.build(lay, 2025)
    g = d.get
    assert (g(F, "3"), g(F, "7"), g(F, "9")) == (1000.0, 800.0, 61370.0)
    assert g(F, "21") == 0.0
    assert not _checks(d), d.notes
    assert not any("il_use_tax" in n for n in d.notes)


def test_rounding_follows_the_instructions() -> None:
    assert (il1040.dollars(1.49), il1040.dollars(2.50)) == (1.0, 3.0)
    assert il1040.dollars(2871.99) == 2872.0


def test_il_is_a_registry_entry() -> None:
    il = statereturn.get("il")
    assert il is not None and il.form == F and il.template == "IL-IL1040"
    assert il.tax_line == "14" and il.carry == "state_tax"
    assert il.prior == ("14", "22", "-15", "-16", "-17", "-28", "-29", "-30")
    assert {"state_withheld", "il_use_tax"} <= set(il.keys)


FRONT = [
    "Illinois Department of Revenue 2025 Form IL-1040 Individual Income Tax Return",
    "Step 2: Income",
    "1 Federal adjusted gross income from your federal Form 1040 or 1040-SR, "
    "Line 11a. 1 61,050 .00",
    "4 Total income. Add Lines 1 through 3. 4 61,170 .00",
    "9 Illinois base income. Subtract Line 8 from Line 4. 9 60,870 .00",
    "Exemption allowance. Add Lines 10a through 10d. 10 2,850 .00",
    "Nonresidents and part-year residents: Enter the Illinois net income from "
    "Schedule NR. Attach Sch. NR. 11 58,020 .00",
    "14 Income tax. Add Lines 12 and 13. Cannot be less than zero. 14 2,872 .00",
    "16 Property tax, K-12 education expense, and volunteer emergency worker "
    "credit amount from Schedule ICR. Attach Sch. ICR. 16 100 .00",
    "21 Use tax on internet, mail order, or other out-of-state purchases from UT "
    "Worksheet or UT Table in the instructions. Do not leave blank. 21 31 .00",
    "23 Total Tax. Add Lines 19, 20, 21, and 22. 23 2,803 .00",
    "IL-1040 Front (R-12/25) Printed by authority of the state of Illinois.",
]
BACK = [
    "24 Total tax from Page 1, Line 23. 24 *60012252W* 2,803 .00",
    "25 Illinois Income Tax withheld. Attach Sch. IL-WIT. 25 2,500 .00",
    "29 Earned Income Tax credit from Sch. IL-E/EITC, Step 4, Line 9. Attach "
    "Sch. IL-E/EITC. 29 50 .00",
    "31 Total payments and refundable credit. Add Lines 25 through 30. 31 2,550 .00",
    "33 If Line 24 is greater than Line 31, subtract Line 31 from Line 24. 33 253 .00",
    "is less than Line 36, subtract Line 32 from Line 36. If Lines 32 and 33 are "
    "blank (zero), enter the amount from Line 36. This is the amount you owe. See "
    "instructions. 41 253 .00",
    "IL-1040 Back (R-12/25) DR AP RR DC IR ID",
]


def test_a_filed_il1040_reads_as_one_form(tmp_path: Path) -> None:
    (f,) = one(tmp_path, "il1040", [FRONT, BACK])
    # the back prints only its revision date, R-12/25, read as 2025
    assert (f.form, f.issuer, f.tax_year) == ("IL-IL1040", "IL", 2025)
    got = {b: f.boxes[b][1] for b in ("1", "4", "9", "10", "11", "14", "16", "21")}
    assert got == {
        "1": 61050.0,
        "4": 61170.0,
        "9": 60870.0,
        "10": 2850.0,
        "11": 58020.0,
        "14": 2872.0,
        "16": 100.0,
        "21": 31.0,
    }
    got = {b: f.boxes[b][1] for b in ("23", "24", "25", "29", "31", "33", "41")}
    assert got == {
        "23": 2803.0,
        "24": 2803.0,
        "25": 2500.0,
        "29": 50.0,
        "31": 2550.0,
        "33": 253.0,
        "41": 253.0,
    }
    assert "32" not in f.boxes and "38" not in f.boxes


@pytest.mark.parametrize(
    ("credits", "want"), [((("16", 100.0), ("29", 50.0)), 2722.0), ((), 2872.0)]
)
def test_a_filed_il1040_is_next_years_prior_state_tax(
    planner_home: Path, credits: tuple[tuple[str, float], ...], want: float
) -> None:
    lay = _lay(planner_home, W2)
    enter(lay, 2026, "state", "IL")
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-il-filed",
        file_name="filed-il1040.pdf",
        kind="pdf",
        pages=2,
        batch="b2",
        facts=[
            db.Fact("IL-IL1040", 2025, "IL", line, "", value, 1)
            for line, value in (("14", 2872.0), ("25", 2500.0), *credits)
        ],
    )
    try:
        # IL-2210 Step 2: lines 14 and 22 less the credits on 15-17 and 28-30;
        # withholding (25) is a payment, not a credit
        assert need_value(conn, lay, 2026, "prior_state_tax") == want
    finally:
        conn.close()
