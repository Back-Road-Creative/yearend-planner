"""Unit 3d-6: California Form 540 on the draft for a full-year resident, from
synthetic W-2 and 1099-INT boxes. Line numbers, the Tax Table, the rate
schedules, the standard deduction and the exemption credits follow the 2025
Form 540 and its booklet (FTB, 2025-540.pdf and 2025-540-booklet.pdf): US
Treasury interest subtracted on Schedule CA, the Tax Table up to $100,000 of
taxable income and the schedule above it, CA withholding (W-2 box 17), an
estimated payment and the use tax on line 91."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.engine import tax
from planner.ingest.needs import enter, need_value
from planner.ledger import db
from planner.paths import Layout
from planner.plan import esttax
from planner.taxprep import ca540, draft, statereturn
from tests.test_d400 import _joint
from tests.test_forms import one

F = ca540.FORM


def _lay(planner_home: Path, ca_withheld: float) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    facts = [
        db.Fact("W-2", 2025, "Employer (synthetic)", "1", "", 60250.0, 1),
        db.Fact("W-2", 2025, "Employer (synthetic)", "2", "", 6000.0, 1),
        db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "1", "", 500.0, 1),
        db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "3", "", 300.0, 1),
    ]
    if ca_withheld:
        facts.append(
            db.Fact("W-2", 2025, "Employer (synthetic)", "17", "", ca_withheld, 1)
        )
    db.add_document(
        conn,
        fingerprint="synthetic-ca",
        file_name="ca.pdf",
        kind="pdf",
        pages=1,
        batch="b1",
        facts=facts,
    )
    conn.close()
    for key, text in (
        ("birth_date", "1980-05-01"),
        ("filing_status", "single"),
        ("state", "CA"),
    ):
        enter(lay, 2025, key, text)
    return lay


def _checks(d: draft.Draft) -> list[str]:
    return [n for n in d.notes if n.startswith("CHECK")]


def test_540_refund_with_treasury_interest_and_estimated_payment(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home, 2000.0)
    enter(lay, 2025, "ca_use_tax", "25")
    esttax.record(lay, 2025, "ca", "2025-06-15", 200.0)
    d = draft.build(lay, 2025)
    g = d.get
    assert g(F, "13") == g("1040", "11b") == 61050.0
    assert (g(F, "14"), g(F, "15"), g(F, "16"), g(F, "17")) == (
        300.0,
        60750.0,
        0.0,
        60750.0,
    )
    assert g(F, "18") == 5706.0  # 2025 single standard deduction, booklet
    assert g(F, "19") == 55044.0
    assert g(F, "31") == 1835.0  # Tax Table row 54,951-55,050, single
    assert (g(F, "32"), g(F, "33"), g(F, "35")) == (153.0, 1682.0, 1682.0)
    assert (g(F, "47"), g(F, "48"), g(F, "64")) == (0.0, 1682.0, 1682.0)
    assert (g(F, "71"), g(F, "72"), g(F, "78")) == (2000.0, 200.0, 2200.0)
    assert (g(F, "91"), g(F, "93"), g(F, "95")) == (25.0, 2175.0, 2175.0)
    assert (g(F, "97"), g(F, "99"), g(F, "115")) == (493.0, 493.0, 493.0)
    assert g(F, "100") is None and g(F, "111") is None
    assert not _checks(d), d.notes
    out = draft.render(d)
    assert "CA Form 540" in out and "2025-06-15 200.00 (typed)" in out
    assert "Tax Table" in out
    assert any("540NR" in n for n in d.notes)
    assert any("FTB 3853" in n for n in d.notes)


def test_540_owed_with_use_tax_from_the_table(planner_home: Path) -> None:
    d = draft.build(_lay(planner_home, 0.0), 2025)
    g = d.get
    assert g(F, "71") == 0.0 and g(F, "97") is None
    use = g(F, "91")
    assert use is not None and use > 0
    assert g(F, "93") == 0.0 and g(F, "94") == use
    assert g(F, "100") == g(F, "64") == 1682.0
    assert g(F, "111") == pytest.approx(use + 1682.0)
    assert any("use tax" in n and "ca_use_tax" in n for n in d.notes)
    assert not _checks(d), d.notes


def test_typed_schedule_ca_items_move_line_19(planner_home: Path) -> None:
    lay = _lay(planner_home, 2000.0)
    enter(lay, 2025, "ca_subtractions", "1000")
    enter(lay, 2025, "ca_additions", "400")
    d = draft.build(lay, 2025)
    g = d.get
    assert (g(F, "14"), g(F, "16"), g(F, "17")) == (1300.0, 400.0, 60150.0)
    assert g(F, "19") == 54444.0 and g(F, "31") == 1799.0
    assert not _checks(d), d.notes


def test_joint_540_uses_the_joint_deduction_table_and_two_credits(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home, 2000.0)
    _joint(lay, 0.0)
    d = draft.build(lay, 2025)
    g = d.get
    assert g(F, "18") == 11412.0 and g(F, "32") == 306.0
    assert g(F, "19") == 49338.0
    assert g(F, "31") == 764.0  # Tax Table row 49,251-49,350, joint
    assert not _checks(d), d.notes


@pytest.mark.parametrize(
    ("income", "status", "want"),
    [
        # 2025 booklet Tax Table rows (the tax at the row's midpoint)
        (55_000.0, "SINGLE", 1835.0),
        (87_200.0, "SINGLE", 4548.0),
        (87_200.0, "JOINT", 2302.0),
        (87_200.0, "HEAD_OF_HOUSEHOLD", 2673.0),
        (87_200.0, "SURVIVING_SPOUSE", 2302.0),
        (87_200.0, "SEPARATE", 4548.0),
        (100_000.0, "SINGLE", 5736.0),
        (100_000.0, "JOINT", 3068.0),
        (100_000.0, "HEAD_OF_HOUSEHOLD", 3708.0),
        (22_200.0, "SINGLE", 333.0),
        (22_151.0, "JOINT", 222.0),
        (22_250.0, "HEAD_OF_HOUSEHOLD", 222.0),
        (51.0, "SINGLE", 1.0),
        (50.0, "SINGLE", 0.0),
        (0.0, "SINGLE", 0.0),
        # over $100,000: the rate schedule (booklet's Schedule Y example)
        (125_000.0, "JOINT", 4768.0),
    ],
)
def test_tax_follows_the_2025_table_and_schedules(
    income: float, status: str, want: float
) -> None:
    amount, source = ca540.tax(2025, status, income)
    assert amount == want
    assert ("Tax Table" in source) is (income <= 100_000)


def test_2025_schedules_match_the_booklet() -> None:
    single = tax.brackets("gov.states.ca.tax.income.rates.single", 2025)
    assert single == [
        (0.0, 0.01),
        (11079.0, 0.02),
        (26264.0, 0.04),
        (41452.0, 0.06),
        (57542.0, 0.08),
        (72724.0, 0.093),
        (371479.0, 0.103),
        (445771.0, 0.113),
        (742953.0, 0.123),
    ]
    joint = [t for t, _ in tax.brackets("gov.states.ca.tax.income.rates.joint", 2025)]
    assert joint[1:] == [
        22158.0,
        52528.0,
        82904.0,
        115084.0,
        145448.0,
        742958.0,
        891542.0,
        1485906.0,
    ]
    hoh = tax.brackets("gov.states.ca.tax.income.rates.head_of_household", 2025)
    assert [t for t, _ in hoh][1:] == [
        22173.0,
        52530.0,
        67716.0,
        83805.0,
        98990.0,
        505208.0,
        606251.0,
        1010417.0,
    ]


def test_a_later_year_says_its_schedule_is_a_projection() -> None:
    _, source = ca540.tax(2026, "SINGLE", 55_000.0)
    assert "projection" in source


def test_ca_is_a_registry_entry() -> None:
    ca = statereturn.get("ca")
    assert ca is not None and ca.form == F and ca.template == "CA-540"
    assert ca.tax_line == "64" and ca.carry == "state_tax"
    assert "state_withheld" in ca.keys and F in ca.forms


SIDE2 = [
    "Side 2 Form 540 2025",
    "13 Enter federal adjusted gross income (AGI) from federal Form 1040 or "
    "1040-SR, line 11b. . . 13 61,050.00",
    "17 California adjusted gross income. Combine line 15 and line 16 . . . "
    "17 60,750.00",
    "19 Subtract line 18 from line 17. This is your taxable income. 19 55,044.00",
    "31 Tax. Check the box if from: Tax Table Tax Rate Schedule FTB 3800 "
    "FTB 3803 . . . . 31 1,835.00",
]
SIDE3 = [
    "Form 540 2025 Side 3",
    "48 Subtract line 47 from line 35. If less than zero, enter -0- . . . 48 1,682.00",
    "61 Alternative Minimum Tax. Attach Schedule P (540) . . . 61 100.00",
    "62 Behavioral Health Services Tax. See instructions . . . 62 0.00",
    "63 Other taxes and credit recapture. See instructions . . . 63 50.00",
    "64 Add line 48, line 61, line 62, and line 63. This is your total tax. "
    ". . . 64 1,832.00",
    "71 California income tax withheld. See instructions . . . 71 2,000.00",
    "72 2025 California estimated tax and other payments. See instructions "
    ". . . 72 200.00",
    "78 Add line 71 through line 77. These are your total payments. 78 2,200.00",
    "97 Overpaid tax. If line 95 is more than line 64, subtract line 64 from "
    "line 95. . . . 97 493.00",
]
SIDE4 = [
    "Side 4 Form 540 2025",
    "99 Overpaid tax available this year. Subtract line 98 from line 97 . . . "
    "99 493.00",
]
SIDE5 = [
    "Form 540 2025 Side 5",
    "115 REFUND OR NO AMOUNT DUE. Subtract the sum of line 110, line 112, and "
    "line 113 from line 99. See instructions.",
    "Mail to: FRANCHISE TAX BOARD, PO BOX 942840, SACRAMENTO CA 94240-0001. "
    ". . . 115 493.00",
]


def test_a_filed_540_reads_as_one_form(tmp_path: Path) -> None:
    (f,) = one(tmp_path, "ca540", [SIDE2, SIDE3, SIDE4, SIDE5])
    assert (f.form, f.issuer, f.tax_year) == ("CA-540", "CA", 2025)
    got = {b: f.boxes[b][1] for b in ("13", "19", "31", "61", "63", "64", "97", "115")}
    assert got == {
        "13": 61050.0,
        "19": 55044.0,
        "31": 1835.0,
        "61": 100.0,
        "63": 50.0,
        "64": 1832.0,
        "97": 493.0,
        "115": 493.0,
    }
    assert "100" not in f.boxes


def test_a_filed_540_is_next_years_prior_state_tax(planner_home: Path) -> None:
    lay = _lay(planner_home, 0.0)
    enter(lay, 2026, "state", "CA")
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-ca-filed",
        file_name="filed-540.pdf",
        kind="pdf",
        pages=4,
        batch="b2",
        facts=[
            db.Fact("CA-540", 2025, "CA", line, "", value, 1)
            for line, value in (("48", 1682.0), ("61", 100.0), ("62", 0.0))
            + (("63", 50.0), ("64", 1832.0))
        ],
    )
    try:
        # 540-ES worksheet line 19b: lines 48, 61 and 62; line 63 is left out
        assert need_value(conn, lay, 2026, "prior_state_tax") == 1782.0
    finally:
        conn.close()
