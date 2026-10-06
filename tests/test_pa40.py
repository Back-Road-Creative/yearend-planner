"""Unit 3d-8: Pennsylvania Form PA-40 on the draft for a full-year resident,
from synthetic W-2, 1099-INT and 1099-DIV boxes. Line numbers, the classes of
income, the 3.07% rate, the Schedule SP eligibility income tables and the use
tax table follow the 2025 PA-40 and its instructions (PA DOR, 2025_pa-40.pdf
and 2025_pa-40in.pdf): compensation from W-2 box 16 (elective deferrals
included), US bond interest left out of line 2, capital gain distributions on
line 3 and a loss on line 5 never netted. Next year's safe harbor (REV-1630)
is line 12 less the line 21 forgiveness."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.ingest.needs import enter, need_value
from planner.ledger import db
from planner.paths import Layout
from planner.plan import esttax
from planner.taxprep import draft, pa40, statereturn
from tests.test_forms import one

F = pa40.FORM


def _lay(
    planner_home: Path,
    w2: dict[str, float],
    more: list[db.Fact] | None = None,
    status: str = "single",
    dependents: str = "none",
) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    facts = [
        db.Fact("W-2", 2025, "Employer (synthetic)", b, "", v, 1) for b, v in w2.items()
    ]
    db.add_document(
        conn,
        fingerprint="synthetic-pa",
        file_name="pa.pdf",
        kind="pdf",
        pages=1,
        batch="b1",
        facts=facts + (more or []),
    )
    conn.close()
    for key, text in (
        ("birth_date", "1980-05-01"),
        ("filing_status", status),
        ("dependents", dependents),
        ("state", "PA"),
    ):
        enter(lay, 2025, key, text)
    return lay


def _checks(d: draft.Draft) -> list[str]:
    return [n for n in d.notes if n.startswith("CHECK")]


BANK = [
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "1", "", 500.0, 1),
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "3", "", 300.0, 1),
]
W2 = {"1": 60250.0, "2": 6000.0, "12D": 3000.0, "16": 63250.0, "17": 1900.0}


def test_pa40_owed_with_box_16_bond_interest_and_estimated_payment(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home, W2, BANK)
    esttax.record(lay, 2025, "pa", "2025-06-15", 20.0)
    d = draft.build(lay, 2025)
    g = d.get
    assert g("1040", "11b") == 61050.0
    assert (g(F, "1a"), g(F, "1b"), g(F, "1c")) == (63250.0, 0.0, 63250.0)
    assert g(F, "2") == 500.0  # US obligations interest is not PA-taxable
    assert (g(F, "3"), g(F, "4"), g(F, "5"), g(F, "6")) == (0.0, 0.0, 0.0, 0.0)
    assert (g(F, "9"), g(F, "10"), g(F, "11")) == (63750.0, 0.0, 63750.0)
    assert g(F, "12") == 1957.0  # 63,750 x 3.07% = 1,957.125, to the dollar
    assert (g(F, "13"), g(F, "15"), g(F, "18")) == (1900.0, 20.0, 20.0)
    assert g(F, "21") == 0.0  # eligibility income far past the SP tables
    assert g(F, "24") == 1920.0
    assert g(F, "25") == 23.0  # use tax table 1, rest of PA, $50,001-$75,000
    assert (g(F, "26"), g(F, "28")) == (60.0, 60.0)
    assert g(F, "29") is None
    assert not _checks(d), d.notes
    out = draft.render(d)
    assert "PA Form PA-40" in out and "2025-06-15 20.00 (typed)" in out
    assert "W-2 box 16 (Employer (synthetic))" in out
    assert any("pa_use_tax" in n for n in d.notes)
    assert any("REV-1630" in n for n in d.notes)


def test_no_box_16_adds_the_deferrals_and_a_sale_loss_is_not_netted(
    planner_home: Path,
) -> None:
    w2 = {"1": 60250.0, "2": 6000.0, "12D": 3000.0, "17": 2000.0}
    div = [
        db.Fact("1099-DIV", 2025, "Example Fund (synthetic)", "1a", "", 400.0, 1),
        db.Fact("1099-DIV", 2025, "Example Fund (synthetic)", "2a", "", 250.0, 1),
        db.Fact(
            "1099-B", 2025, "Example Broker (synthetic)", "st_proceeds", "", 5000.0, 1
        ),
        db.Fact(
            "1099-B", 2025, "Example Broker (synthetic)", "st_basis", "", 10000.0, 1
        ),
    ]
    lay = _lay(planner_home, w2, div)
    enter(lay, 2025, "pa_use_tax", "0")
    d = draft.build(lay, 2025)
    g = d.get
    assert g(F, "1a") == 63250.0  # box 1 + box 12 code D
    assert g(F, "3") == 650.0  # dividends and the box 2a distribution
    assert g(F, "5") == -5000.0  # shown, never subtracted (no $3,000 limit)
    assert g(F, "9") == 63900.0
    assert g(F, "25") == 0.0
    assert g(F, "29") == g(F, "30")
    assert not _checks(d), d.notes
    assert "box 12 deferrals" in draft.render(d)


def test_forgiveness_follows_the_eligibility_income_tables() -> None:
    f = pa40.forgiveness
    assert f(2025, "SINGLE", 6500.0, 0) == 1.0
    assert f(2025, "SINGLE", 6501.0, 0) == 0.9
    assert f(2025, "SINGLE", 8750.0, 0) == 0.1
    assert f(2025, "SINGLE", 8751.0, 0) == 0.0
    assert f(2025, "JOINT", 13000.0, 0) == 1.0  # Table 2, married
    assert f(2025, "HEAD_OF_HOUSEHOLD", 16250.0, 1) == 0.9  # 6,500 + 9,500
    assert f(2025, "JOINT", 32001.0, 2) == 0.9  # 13,000 + 2 x 9,500


def test_head_of_household_with_a_child_gets_partial_forgiveness(
    planner_home: Path,
) -> None:
    w2 = {"1": 16200.0, "2": 0.0, "16": 16200.0, "17": 0.0}
    lay = _lay(planner_home, w2, status="head_of_household", dependents="2018-03-02")
    d = draft.build(lay, 2025)
    g = d.get
    assert (g(F, "19b"), g(F, "20")) == (1.0, 16200.0)
    assert g("PA Sch SP", "IV-15") == 0.9  # $200 over $16,000: one $250 step
    assert g(F, "12") == 497.0  # 16,200 x 3.07% = 497.34
    assert g(F, "21") == 447.0  # 497 x 90% = 447.30
    assert not _checks(d), d.notes


def test_separate_filers_get_no_forgiveness_draft(planner_home: Path) -> None:
    lay = _lay(planner_home, W2, status="married_separate")
    d = draft.build(lay, 2025)
    assert d.get(F, "21") is None
    assert any("spouse's income" in n for n in d.notes)


def test_pa_is_a_registry_entry() -> None:
    pa = statereturn.get("pa")
    assert pa is not None and pa.form == F and pa.template == "PA-PA40"
    assert pa.tax_line == "12" and pa.carry == "state_tax"
    assert pa.prior == ("12", "-21") and "state_withheld" in pa.keys


SIDE1 = [
    "PA-40 Pennsylvania Income Tax Return PA-40 (EX) MOD 04-25 (FI)",
    "PA Department of Revenue Harrisburg, PA 17129 2025",
    "1a. Gross Compensation. Do not include exempt income, such as combat zone "
    "pay and qualifying retirement benefits. See the instructions. . . . 1a. 63250",
    "1c. Net Compensation. Subtract Line 1b from Line 1a. . . . 1c. 63250",
    "2. Interest Income. Complete PA Schedule A if required. . . . 2. 500",
    "9. Total PA Taxable Income. Add only the positive income amounts from Lines "
    "1c, 2, 3, 4, 5, 6, 7, and 8. DO NOT ADD any losses reported on Lines 4, 5, "
    "or 6. . . . 9. 63750",
    "11. Adjusted PA Taxable Income. Subtract Line 10 from Line 9. . . . 11. 63750",
]
SIDE2 = [
    "PA-40 2025 04-25 (FI) Social Security Number (shown first)",
    "12. PA Tax Liability. Multiply Line 11 by 3.07 percent (0.0307). . . . 12. 1957",
    "13. Total PA Tax Withheld. See the instructions. . . . 13. 1900",
    "21. Tax Forgiveness Credit from Section IV, Line 16, PA Schedule SP. . . . "
    "21. 300",
    "24. TOTAL PAYMENTS and CREDITS. Add Lines 13, 18, 21, 22, and 23. . . . 24. 2200",
    "25. USE TAX. Due on internet, mail order, or out-of-state purchases. See "
    "the instructions. 25. 23",
    "29. OVERPAYMENT. If Line 24 is more than the total of Line 12, Line 25, and "
    "Line 27 enter the difference here. . . . 29. 220",
    "30. Refund - Amount of Line 29 you want as a check mailed to you.. . . . "
    "REFUND 30. 220",
]


def test_a_filed_pa40_reads_as_one_form(tmp_path: Path) -> None:
    (f,) = one(tmp_path, "pa40", [SIDE1, SIDE2])
    assert (f.form, f.issuer, f.tax_year) == ("PA-PA40", "PA", 2025)
    got = {b: f.boxes[b][1] for b in ("1a", "2", "9", "11", "12", "13", "21")}
    assert got == {
        "1a": 63250.0,
        "2": 500.0,
        "9": 63750.0,
        "11": 63750.0,
        "12": 1957.0,
        "13": 1900.0,
        "21": 300.0,
    }
    assert (f.boxes["25"][1], f.boxes["29"][1], f.boxes["30"][1]) == (
        23.0,
        220.0,
        220.0,
    )
    assert "26" not in f.boxes and "28" not in f.boxes


@pytest.mark.parametrize(("forgiven", "want"), [(300.0, 1657.0), (0.0, 1957.0)])
def test_a_filed_pa40_is_next_years_prior_state_tax(
    planner_home: Path, forgiven: float, want: float
) -> None:
    lay = _lay(planner_home, W2)
    enter(lay, 2026, "state", "PA")
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-pa-filed",
        file_name="filed-pa40.pdf",
        kind="pdf",
        pages=2,
        batch="b2",
        facts=[
            db.Fact("PA-PA40", 2025, "PA", line, "", value, 1)
            for line, value in (("12", 1957.0), ("21", forgiven), ("13", 1900.0))
        ],
    )
    try:
        # REV-1630: the prior year's line 12 less its line 21 forgiveness
        assert need_value(conn, lay, 2026, "prior_state_tax") == want
    finally:
        conn.close()
