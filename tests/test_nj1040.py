"""Unit 3d-13: New Jersey NJ-1040 on the draft for a full-year resident, from
synthetic W-2 and 1099-INT boxes. Line numbers, New Jersey's own categories of
income, the exemptions, Worksheet F, Worksheet H's property tax deduction or
credit, the Tax Table, the filing threshold, the 40% EITC and the rounding rule
follow the 2025 NJ-1040 and its instructions (NJ Division of Taxation). Next
year's safe harbor (NJ-2210) is line 50."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.ingest.needs import enter, need_value
from planner.ledger import db
from planner.paths import Layout
from planner.plan import esttax
from planner.taxprep import draft, nj1040, statereturn
from tests.test_forms import one

F = nj1040.FORM


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
        fingerprint="synthetic-nj-you",
        file_name="nj-you.pdf",
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
        ("state", "NJ"),
    ):
        enter(lay, 2025, key, text)
    return lay


def _checks(d: draft.Draft) -> list[str]:
    return [n for n in d.notes if n.startswith("CHECK")]


def _lines(d: draft.Draft, *lines: str) -> tuple[float | None, ...]:
    return tuple(d.get(F, n) for n in lines)


def _source(d: draft.Draft, line: str) -> str:
    return next(ln.source for ln in d.lines if (ln.form, ln.line) == (F, line))


BANK = [
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "1", "", 500.0, 1),
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "3", "", 300.0, 1),
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "8", "", 120.0, 1),
]
# box 16 is box 1 plus a 403(b) deferral New Jersey taxes
W2 = {"1": 60250.0, "2": 6000.0, "12E": 1000.0, "16": 61250.0, "17": 1500.0}


def test_nj_1040_with_bank_interest_property_taxes_and_an_estimated_payment(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home, W2, BANK)
    enter(lay, 2025, "nj_property_taxes", "8000")
    esttax.record(lay, 2025, "nj", "2025-06-15", 100.0)
    d = draft.build(lay, 2025)
    # US savings bond interest (1099-INT box 3) is not New Jersey income
    assert _lines(d, "6", "13", "15", "16a", "16b", "17", "27", "29") == (
        1000.0,
        1000.0,
        61250.0,
        500.0,
        120.0,
        0.0,
        61750.0,
        61750.0,
    )
    # Worksheet H: the $8,000 deduction saves more tax than the $50 credit
    assert _lines(d, "38", "39", "40a", "41", "42", "56") == (
        1000.0,
        60750.0,
        8000.0,
        8000.0,
        52750.0,
        None,
    )
    # the Tax Table row 52,750-52,800 prices 52,775: 1,423.30
    assert _lines(d, "43", "50", "54", "55", "57", "66") == (
        1423.0,
        1423.0,
        1423.0,
        1500.0,
        100.0,
        1600.0,
    )
    assert _lines(d, "67", "68", "80") == (None, 177.0, 177.0)
    assert "W-2 box 16" in _source(d, "15")
    assert not _checks(d), d.notes
    out = draft.render(d)
    assert "NJ-1040" in out and "2025-06-15 100.00 (typed)" in out
    assert any("other states' bonds" in n for n in d.notes)
    assert any("NJ-2210" in n and "80%" in n for n in d.notes)


def test_a_small_property_tax_takes_the_credit(planner_home: Path) -> None:
    w2 = {"1": 16000.0, "2": 800.0, "16": 16000.0, "17": 200.0}
    lay = _lay(planner_home, w2)
    enter(lay, 2025, "nj_property_taxes", "2000")
    d = draft.build(lay, 2025)
    # 2,000 off 15,000 saves 1.4% of it, 28: less than the $50 credit
    assert _lines(d, "39", "40a", "41", "42", "43", "56") == (
        15000.0,
        2000.0,
        None,
        15000.0,
        210.0,
        50.0,
    )
    # the NJ EITC: 40% of the childless federal credit, 237.43
    assert _lines(d, "55", "58", "66", "67") == (200.0, 95.0, 345.0, None)
    assert d.get(F, "68") == 135.0


def test_separate_filers_halve_the_credit(planner_home: Path) -> None:
    w2 = {"1": 16000.0, "2": 800.0, "16": 16000.0, "17": 200.0}
    lay = _lay(planner_home, w2, status="married_separate")
    enter(lay, 2025, "nj_property_taxes", "2000")
    d = draft.build(lay, 2025)
    # the 28 saved is at least the $25 credit, so the deduction is taken
    assert _lines(d, "41", "42", "43", "56") == (2000.0, 13000.0, 182.0, None)
    assert any("$7,500 and $25" in n for n in d.notes)


def test_under_the_filing_threshold_there_is_no_tax_but_the_eitc(
    planner_home: Path,
) -> None:
    w2 = {"1": 18000.0, "2": 0.0, "17": 200.0}
    lay = _lay(
        planner_home,
        w2,
        status="married_joint",
        dependents="2019-03-02",
        born="1985-01-01",
    )
    d = draft.build(lay, 2025)
    assert _lines(d, "6", "10", "13", "29", "39", "43", "50") == (
        2000.0,
        1500.0,
        3500.0,
        18000.0,
        14500.0,
        0.0,
        0.0,
    )
    # 40% of the 4,328 federal credit = 1,731.20
    assert _lines(d, "55", "58", "66", "68") == (200.0, 1731.0, 1931.0, 1931.0)
    assert "filing threshold" in _source(d, "43")
    assert any("20,000 filing threshold" in n for n in d.notes)
    assert not _checks(d), d.notes


def test_seniors_get_the_exemption_and_a_typed_medical_deduction(
    planner_home: Path,
) -> None:
    w2 = {"1": 90000.0, "2": 8000.0, "17": 3000.0}
    lay = _lay(planner_home, w2, status="married_joint", born="1960-01-01")
    d = draft.build(lay, 2025)
    # the engine's estimate: the standard Part B premium over 2% of 90,000
    assert _lines(d, "6", "7", "13", "31") == (2000.0, 1000.0, 3000.0, 420.0)
    assert "engine estimate" in _source(d, "31")
    assert any("type nj_medical_expenses" in n for n in d.notes)
    assert not _checks(d), d.notes
    enter(lay, 2025, "nj_medical_expenses", "5000")
    enter(lay, 2025, "nj_veterans", "1")
    d = draft.build(lay, 2025)
    # Worksheet F: 5,000 less 2% of 90,000; line 9 is $6,000 a veteran
    assert _lines(d, "9", "13", "31", "38", "39") == (
        6000.0,
        9000.0,
        3200.0,
        12200.0,
        77800.0,
    )
    assert not any("type nj_medical_expenses" in n for n in d.notes)


def test_typed_items_land_on_their_lines(planner_home: Path) -> None:
    lay = _lay(planner_home, W2, BANK)
    for key, text in (
        ("nj_other_interest", "120"),
        ("nj_other_dividends", "75"),
        ("nj_other_deductions", "250"),
        ("nj_property_taxes", "0"),
        ("nj_use_tax", "30"),
    ):
        enter(lay, 2025, key, text)
    d = draft.build(lay, 2025)
    assert _lines(d, "16a", "16b", "17", "27", "36", "38", "40a", "41") == (
        620.0,
        None,
        75.0,
        61945.0,
        250.0,
        1250.0,
        None,
        None,
    )
    l50 = d.get(F, "50")
    assert l50 is not None and _lines(d, "51", "54") == (30.0, l50 + 30.0)
    assert not any("other states' bonds" in n for n in d.notes)
    assert not _checks(d), d.notes


@pytest.mark.parametrize(
    ("income", "status", "tax"),
    [
        # Tax Table rows from the 2025 instructions
        (39875.0, "JOINT", 628.0),
        (54975.0, "SINGLE", 1545.0),
        (75.0, "SINGLE", 1.0),
        (175.0, "SINGLE", 2.0),
        (1025.0, "SINGLE", 14.0),
        # $100,000 and over: the rate schedule, not a band midpoint
        (100000.0, "SINGLE", 4244.0),
    ],
)
def test_the_tax_table(income: float, status: str, tax: float) -> None:
    assert nj1040.tax_on(income, status, 2025) == tax


def test_rounding_is_half_up() -> None:
    assert (nj1040.dollars(1.49), nj1040.dollars(2.50)) == (1.0, 3.0)


def test_nj_is_a_registry_entry() -> None:
    nj = statereturn.get("nj")
    assert nj is not None and nj.form == F and nj.template == "NJ-1040"
    assert nj.tax_line == "50" and nj.carry == "state_tax" and nj.prior == ("50",)
    assert set(nj.forms) == {F}
    assert {"state_withheld", "nj_property_taxes", "nj_veterans"} <= set(nj.keys)


PAGE1 = [
    "2025 NJ-1040",
    "New Jersey Resident",
    "Income Tax Return",
    "13. Total Exemption Amount (Add totals from the lines at 6 through 12) "
    "......... 13. 1,000.00",
]
PAGE2 = [
    "Name(s) as shown on Form NJ-1040 Your Social Security Number",
    "Page 2",
    "15. Wages, salaries, tips, and other employee compensation (State wages from",
    "Box 16 of enclosed W-2(s)) (See instructions) ........... 15. 61,250.00",
    "16a. Taxable interest income (Enclose federal Schedule B if over $1,500)",
    "(See instructions) ........................ 16a. 500.00",
    "17. Dividends ................................... 17. 0.00",
    "27. Total Income (Add lines 15, 16a, 17 through 20a, and 21 through 26) "
    "...... 27. 61,750.00",
    "29. New Jersey Gross Income (Subtract line 28c from line 27)",
    "(See instructions) .......................... 29. 61,750.00",
    "38. Total Exemptions and Deductions (Add lines 30 through 37c) ..... 38. 1,000.00",
    "39. Taxable Income (Subtract line 38 from line 29) ........ 39. 60,750.00",
    "40a. Total Property Taxes (18% of Rent) Paid (See instructions page 25) "
    ".... 40a. 8,000.00",
    "40b. Indicate your residency status during 2025 (fill in only one oval)",
    "41. Property Tax Deduction (From Worksheet H) (See instructions) "
    ".... 41. 8,000.00",
]
PAGE3 = [
    "Name(s) as shown on Form NJ-1040 Your Social Security Number",
    "Page 3",
    "42. New Jersey Taxable Income (Subtract line 41 from line 39) ..... 42. 52,750.00",
    "43. Tax on amount on line 42 (Tax Table page 54) ......... 43. 1,423.00",
    "50. Balance of Tax After Credits",
    "(Subtract line 49 from line 45) If zero or less, make no entry ..... 50. 1,423.00",
    "54. Total Tax Due (Add lines 50 through 53c) ........ 54. 1,423.00",
    "55. Total NJ Income Tax Withheld",
    "(Enclose Forms W-2 and 1099)(Part-year residents, see instr.) ..... 55. 1,500.00",
    "57. New Jersey Estimated Tax Payments/Credit from 2024 tax return .... 57. 100.00",
    "58. New Jersey Earned Income Tax Credit (See instructions) ...... 58. 0.00",
    "65. New Jersey Child Tax Credit (See instructions) ..... younger on 12/31/25 "
    "..... 65. 0.00",
    "66. Total Withholdings, Credits, and Payments (Add lines 55 through 65) "
    "..... 66. 1,600.00",
]


def test_a_filed_nj_1040_reads_as_one_form(tmp_path: Path) -> None:
    (f,) = one(tmp_path, "nj1040", [PAGE1, PAGE2, PAGE3])
    assert (f.form, f.issuer, f.tax_year) == ("NJ-1040", "NJ", 2025)
    got = {b: f.boxes[b][1] for b in ("13", "15", "16a", "17", "27", "29", "38")}
    assert got == {
        "13": 1000.0,
        "15": 61250.0,
        "16a": 500.0,
        "17": 0.0,
        "27": 61750.0,
        "29": 61750.0,
        "38": 1000.0,
    }
    got = {b: f.boxes[b][1] for b in ("39", "40a", "41", "42", "43", "50", "54")}
    assert got == {
        "39": 60750.0,
        "40a": 8000.0,
        "41": 8000.0,
        "42": 52750.0,
        "43": 1423.0,
        "50": 1423.0,
        "54": 1423.0,
    }
    got = {b: f.boxes[b][1] for b in ("55", "57", "58", "65", "66")}
    assert got == {"55": 1500.0, "57": 100.0, "58": 0.0, "65": 0.0, "66": 1600.0}
    assert "67" not in f.boxes


def test_a_filed_nj_1040_is_next_years_prior_state_tax(planner_home: Path) -> None:
    lay = _lay(planner_home, W2)
    enter(lay, 2026, "state", "NJ")
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-nj-filed",
        file_name="filed-nj1040.pdf",
        kind="pdf",
        pages=3,
        batch="b2",
        facts=[
            db.Fact("NJ-1040", 2025, "NJ", line, "", value, 1)
            for line, value in (("50", 1423.0), ("55", 1500.0), ("58", 300.0))
        ],
    )
    try:
        # NJ-2210 line 4b: last year's line 50; the EITC (58) and withholding
        # (55) count as payments, not as less tax
        assert need_value(conn, lay, 2026, "prior_state_tax") == 1423.0
    finally:
        conn.close()
