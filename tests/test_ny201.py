"""Unit 3d-7: New York Form IT-201 on the draft for a full-year resident, from
synthetic W-2 and 1099-INT boxes. Line numbers, the Tax Table, the rate
schedules, the tax computation worksheets, the standard deduction and the sales
and use tax chart follow the 2025 Form IT-201 and its instructions (NYS DTF,
it201_fill_in.pdf, it201i.pdf and the 2025 NYS tax table): US bond interest
subtracted on line 28, the Tax Table below $65,000 of taxable income, NYS
withholding (W-2 box 17), an estimated payment and the use tax on line 59.
Next year's safe harbor (IT-2105.9-I line 16 worksheet) is lines 46 and 58
less the refundable credits on lines 63-71."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from planner.engine import tax
from planner.ingest.needs import enter, need_value
from planner.ledger import db
from planner.paths import Layout
from planner.plan import esttax
from planner.taxprep import draft, ny201, statereturn
from tests.test_d400 import _joint
from tests.test_forms import one

F = ny201.FORM


def _lay(planner_home: Path, withheld: float, status: str = "single") -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    facts = [
        db.Fact("W-2", 2025, "Employer (synthetic)", "1", "", 60250.0, 1),
        db.Fact("W-2", 2025, "Employer (synthetic)", "2", "", 6000.0, 1),
        db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "1", "", 500.0, 1),
        db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "3", "", 300.0, 1),
    ]
    if withheld:
        facts.append(
            db.Fact("W-2", 2025, "Employer (synthetic)", "17", "", withheld, 1)
        )
    db.add_document(
        conn,
        fingerprint="synthetic-ny",
        file_name="ny.pdf",
        kind="pdf",
        pages=1,
        batch="b1",
        facts=facts,
    )
    conn.close()
    for key, text in (
        ("birth_date", "1980-05-01"),
        ("filing_status", status),
        ("state", "NY"),
    ):
        enter(lay, 2025, key, text)
    return lay


def _checks(d: draft.Draft) -> list[str]:
    return [n for n in d.notes if n.startswith("CHECK")]


def test_it201_refund_with_bond_interest_and_estimated_payment(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home, 3000.0)
    esttax.record(lay, 2025, "ny", "2025-06-15", 200.0)
    d = draft.build(lay, 2025)
    g = d.get
    assert g(F, "19") == g("1040", "11b") == 61050.0
    assert (g(F, "24"), g(F, "28"), g(F, "32"), g(F, "33")) == (
        61050.0,
        300.0,
        300.0,
        60750.0,
    )
    assert g(F, "34") == 8000.0  # 2025 single standard deduction, IT-201-I
    assert g(F, "37") == g(F, "38") == 52750.0
    assert g(F, "39") == 2738.0  # Tax Table row 52,750-52,800, single
    assert (g(F, "40"), g(F, "44"), g(F, "46")) == (0.0, 2738.0, 2738.0)
    assert g(F, "59") == 15.0  # use tax chart, FAGI $50,001-$75,000
    assert (g(F, "61"), g(F, "62")) == (2753.0, 2753.0)
    assert (g(F, "72"), g(F, "75"), g(F, "76")) == (3000.0, 200.0, 3200.0)
    assert (g(F, "77"), g(F, "78")) == (447.0, 447.0)
    assert g(F, "80") is None
    assert not _checks(d), d.notes
    out = draft.render(d)
    assert "NY Form IT-201" in out and "2025-06-15 200.00 (typed)" in out
    assert "Tax Table" in out
    assert any("IT-203" in n for n in d.notes)
    assert any("ny_use_tax" in n for n in d.notes)
    assert any("STAR" in n for n in d.notes)


def test_it201_owed_with_typed_use_tax_and_items(planner_home: Path) -> None:
    lay = _lay(planner_home, 0.0)
    enter(lay, 2025, "ny_use_tax", "40")
    enter(lay, 2025, "ny_additions", "400")
    enter(lay, 2025, "ny_subtractions", "1000")
    d = draft.build(lay, 2025)
    g = d.get
    assert (g(F, "23"), g(F, "24"), g(F, "31"), g(F, "33")) == (
        400.0,
        61450.0,
        1000.0,
        60150.0,
    )
    assert g(F, "37") == 52150.0 and g(F, "39") == 2705.0  # row 52,150-52,200
    assert g(F, "59") == 40.0 and g(F, "72") == 0.0
    assert g(F, "77") is None and g(F, "80") == 2745.0
    assert not any("ny_use_tax" in n for n in d.notes)
    assert not _checks(d), d.notes


@pytest.mark.parametrize(
    ("status", "deduction", "income", "owed"),
    [
        ("married_joint", 16050.0, 44700.0, 2127.0),  # joint row 44,700-44,750
        ("head_of_household", 11200.0, 49550.0, 2478.0),  # HOH row 49,550-49,600
    ],
)
def test_other_statuses_use_their_deduction_and_column(
    planner_home: Path, status: str, deduction: float, income: float, owed: float
) -> None:
    lay = _lay(planner_home, 3000.0, status)
    if status == "married_joint":
        _joint(lay, 0.0)
    d = draft.build(lay, 2025)
    g = d.get
    assert (g(F, "34"), g(F, "37"), g(F, "39")) == (deduction, income, owed)
    assert not _checks(d), d.notes


@pytest.mark.parametrize(
    ("income", "status", "want"),
    [
        # 2025 NYS tax table rows (single/MFS, MFJ/QSS, HOH columns)
        (0.0, "SINGLE", 0.0),
        (12.0, "SINGLE", 0.0),
        (13.0, "JOINT", 1.0),
        (49.0, "HEAD_OF_HOUSEHOLD", 2.0),
        (17_650.0, "SINGLE", 808.0),
        (17_699.0, "HEAD_OF_HOUSEHOLD", 731.0),
        (17_650.0, "JOINT", 710.0),
        # the joint column's 5.25% bracket, priced from the unrounded $976.25
        (23_600.0, "JOINT", 978.0),
        (23_649.0, "SURVIVING_SPOUSE", 978.0),
        (23_600.0, "SINGLE", 1135.0),
        (23_600.0, "HEAD_OF_HOUSEHOLD", 1051.0),
        (52_750.0, "SEPARATE", 2738.0),
        (64_950.0, "SINGLE", 3409.0),
        (64_999.99, "JOINT", 3241.0),
        (64_950.0, "HEAD_OF_HOUSEHOLD", 3325.0),
        # $65,000 and up: the rate schedule ($600 plus 5.5% over $13,900;
        # $4,271 plus 6% over $80,650)
        (65_000.0, "SINGLE", 3411.0),
        (100_000.0, "SINGLE", 5432.0),
    ],
)
def test_tax_follows_the_2025_table_and_schedule(
    income: float, status: str, want: float
) -> None:
    amount, source = ny201.tax(2025, status, income, income)
    assert amount == want
    assert ("Tax Table" in source) is (income < 65_000)


@pytest.mark.parametrize(
    ("status", "income", "nyagi", "want"),
    [
        # worksheet 1: $5,432 + 0.2470 x ($6,000 - $5,432)
        ("SINGLE", 100_000.0, 120_000.0, 5572.0),
        # worksheet 1 at NYAGI of $157,650 or more: 6% of line 38
        ("SINGLE", 100_000.0, 200_000.0, 6000.0),
        # worksheet 7: $7,917.50 + 0.4470 x ($8,250 - $7,917.50)
        ("JOINT", 150_000.0, 130_000.0, 8066.0),
        # worksheet 8: schedule + $333 recapture + all of the $807 benefit
        ("JOINT", 300_000.0, 330_000.0, 18000.0),
        # worksheet 13: schedule + $787 + all of $2,289
        ("HEAD_OF_HOUSEHOLD", 500_000.0, 520_000.0, 34250.0),
        # worksheet 11 shape: 10.3% at $5 million, 10.9% over $25 million NYAGI
        ("JOINT", 6_000_000.0, 6_000_000.0, 618000.0),
        ("SINGLE", 30_000_000.0, 30_000_000.0, 3270000.0),
    ],
)
def test_nyagi_over_107650_uses_the_worksheets(
    status: str, income: float, nyagi: float, want: float
) -> None:
    amount, source = ny201.tax(2025, status, income, nyagi)
    assert amount == want and "worksheet" in source


def test_2025_schedules_print_the_instructions_bases() -> None:
    printed = {  # status: (bracket lower edges, the bases IT-201-I prints)
        "SINGLE": (
            [8500, 11700, 13900, 80650, 215400, 1077550, 5e6, 25e6],
            [340, 484, 600, 4271, 12356, 71413, 449929, 2509929],
        ),
        "JOINT": (
            [17150, 23600, 27900, 161550, 323200, 2155350, 5e6, 25e6],
            [686, 976, 1202, 8553, 18252, 143754, 418263, 2478263],
        ),
        "HEAD_OF_HOUSEHOLD": (
            [12800, 17650, 20900, 107650, 269300, 1616450, 5e6, 25e6],
            [512, 730, 901, 5672, 15371, 107651, 434163, 2494163],
        ),
    }
    for status, (edges, bases) in printed.items():
        scale = ny201._scale(status, 2025)
        assert [low for low, _ in scale][1:] == edges
        # a bracket's printed base is the schedule a hair above its lower edge
        assert [round(ny201.schedule(scale, e + 1e-6)) for e in edges] == bases
    top = tax.brackets("gov.states.ny.tax.income.main.single", 2025)[-1]
    assert top[1] == 0.109


@pytest.mark.parametrize(
    ("fagi", "want"),
    [
        (0.0, 3.0),
        (15_000.0, 3.0),
        (15_000.01, 6.0),
        (61_050.0, 15.0),
        (200_000.0, 34.0),
        (250_000.0, 44.0),  # 0.0175% of $250,000, whole dollars
        (2_000_000.0, 125.0),  # capped
    ],
)
def test_use_tax_chart(fagi: float, want: float) -> None:
    assert ny201.use_tax(fagi) == want


def _lines(
    v: Mapping[str, float], typed: Mapping[str, Any], status: str
) -> tuple[dict[str, float], list[str]]:
    got: dict[str, float] = {}
    notes: list[str] = []

    def add(form: str, line: str, label: str, value: float, source: str) -> float:
        got[line] = round(value, 2)
        return got[line]

    ny201.lay_lines(add, notes, v, [], [], 2025, 80_000.0, typed, status)
    return got, notes


def _engine(**over: float) -> dict[str, float]:
    v = dict.fromkeys(ny201.ENGINE, 0.0)
    v.update(ny_deductions=16_050.0, ny_taxable_income=63_950.0)
    v.update(over)
    return v


def test_the_529_subtraction_is_capped_and_put_back_for_the_check() -> None:
    # the engine subtracts the whole $12,000; the line holds $10,000 joint
    v = _engine(investment_in_529_plan=12_000.0, ny_taxable_income=51_950.0)
    got, notes = _lines(v, {}, "JOINT")
    assert got["30"] == 10_000.0 and got["33"] == 70_000.0 and got["37"] == 53_950.0
    assert not [n for n in notes if n.startswith("CHECK")], notes
    single, _ = _lines(v, {}, "SINGLE")
    assert single["30"] == 5_000.0


def test_refundable_credits_and_a_wrong_engine_figure_is_a_check() -> None:
    v = _engine(ny_ctc=330.0, ny_eitc=100.0, ny_supplemental_eitc=20.0)
    got, notes = _lines(v, {"state_withheld": 1000.0}, "JOINT")
    assert (got["63"], got["65"], got["72"]) == (330.0, 120.0, 1000.0)
    assert got["76"] == 1450.0
    assert not [n for n in notes if n.startswith("CHECK")], notes
    _, notes = _lines(_engine(ny_taxable_income=60_000.0), {}, "JOINT")
    assert any(n.startswith("CHECK: NY IT-201 line 37") for n in notes)


def test_ny_is_a_registry_entry() -> None:
    ny = statereturn.get("ny")
    assert ny is not None and ny.form == F and ny.template == "NY-IT201"
    assert ny.tax_line == "46" and ny.carry == "state_tax"
    assert "state_withheld" in ny.keys and F in ny.forms
    assert ny.prior[:2] == ("46", "58") and "-69a" in ny.prior


def test_the_draft_carries_the_safe_harbor_lines() -> None:
    ny = statereturn.get("NY")
    assert ny is not None
    laid = {"46": 2738.0, "63": 330.0, "65": 120.0}

    def get(form: str, line: str) -> float | None:
        return laid.get(line) if form == F else None

    assert statereturn.carried(get, ny) == 2288.0
    assert statereturn.carried(lambda form, line: None, ny) is None


PAGE2 = [
    "Page 2 of 4 IT-201 (2025) Your Social Security number",
    "19 Federal adjusted gross income (subtract line 18 from line 17) . . . "
    "19 61,050.00",
    "24 Add lines 19 through 23 . . . 24 61,050.00",
    "32 Add lines 25 through 31 . . . 32 300.00",
    "33 New York adjusted gross income (subtract line 32 from line 24) . . . "
    "33 60,750.00",
    "34 Enter your standard deduction or your itemized deduction (from Form "
    "IT-196) Mark an X in the appropriate box: Standard - or - Itemized 34 8,000.00",
    "37 Taxable income (subtract line 36 from line 35) . . . 37 52,750.00",
]
PAGE3 = [
    "Names as shown on page 1 Your Social Security number IT-201 (2025) Page 3 of 4",
    "38 Taxable income (from line 37 on page 2) . . . 38 52,750.00",
    "39 NYS tax on line 38 amount . . . 39 2,738.00",
    "40 NYS household credit . . . 40 0.00",
    "46 Total New York State taxes (add lines 44 and 45) . . . 46 2,738.00",
    "58 Total New York City and Yonkers taxes / surcharges and MCTMT (add lines "
    "54 and 54e through 57). . . 58 0.00",
    "59 Sales or use tax (do not leave blank) . . . 59 15.00",
    "61 Total New York State, New York City, Yonkers, and sales or use taxes, "
    "MCTMT, and voluntary contributions (add lines 46, 58, 59, and 60) . . . "
    "61 2,753.00",
]
PAGE4 = [
    "Page 4 of 4 IT-201 (2025) Your Social Security number",
    "62 Enter amount from line 61 . . . 62 2,753.00",
    "63 Empire State child credit . . . 63 100.00",
    "65 NYS earned income credit (EIC) . . . 65 50.00",
    "69a NYC school tax credit (rate reduction amount) . . . 69a 0.00",
    "72 Total New York State tax withheld . . . 72 3,000.00",
    "75 Total estimated tax payments and amount paid with Form IT-370 75 200.00",
    "76 Total payments (add lines 63 through 75) . . . 76 3,350.00",
    "77 Amount overpaid (if line 76 is more than line 62, subtract line 62 from "
    "line 76) . . . 77 597.00",
    "78 Amount of line 77 available for refund (subtract line 79 from line 77) "
    ". . . 78 597.00",
]


def test_a_filed_it201_reads_as_one_form(tmp_path: Path) -> None:
    (f,) = one(tmp_path, "ny201", [PAGE2, PAGE3, PAGE4])
    assert (f.form, f.issuer, f.tax_year) == ("NY-IT201", "NY", 2025)
    got = {b: f.boxes[b][1] for b in ("19", "33", "34", "37", "39", "46", "58")}
    assert got == {
        "19": 61050.0,
        "33": 60750.0,
        "34": 8000.0,
        "37": 52750.0,
        "39": 2738.0,
        "46": 2738.0,
        "58": 0.0,
    }
    got = {b: f.boxes[b][1] for b in ("63", "65", "69a", "75", "76", "78")}
    assert got == {
        "63": 100.0,
        "65": 50.0,
        "69a": 0.0,
        "75": 200.0,
        "76": 3350.0,
        "78": 597.0,
    }
    assert "80" not in f.boxes and "64" not in f.boxes


def test_a_filed_it201_is_next_years_prior_state_tax(planner_home: Path) -> None:
    lay = _lay(planner_home, 0.0)
    enter(lay, 2026, "state", "NY")
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-ny-filed",
        file_name="filed-it201.pdf",
        kind="pdf",
        pages=3,
        batch="b2",
        facts=[
            db.Fact("NY-IT201", 2025, "NY", line, "", value, 1)
            for line, value in (("46", 2738.0), ("58", 0.0), ("59", 15.0))
            + (("63", 100.0), ("65", 50.0), ("72", 3000.0))
        ],
    )
    try:
        # IT-2105.9-I line 16: 46 + 58 less the credits; use tax and payments out
        assert need_value(conn, lay, 2026, "prior_state_tax") == 2588.0
    finally:
        conn.close()
