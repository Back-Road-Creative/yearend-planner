"""Unit 3d-12: Michigan MI-1040 on the draft for a full-year resident, with the
Schedule 1 lines behind it, from synthetic W-2, 1099-INT, 1099-R and SSA-1099
boxes. Line numbers, the exemption allowance, the 4.25% rate, the Michigan
Standard Deduction tiers, the Form 4884 retirement subtraction, the 30% EITC
and the rounding rule follow the 2025 MI-1040, its Schedule 1, Form 4884 and
the MI-1040 instruction book (Michigan Department of Treasury). Next year's
safe harbor (MI-2210) is line 21 less lines 26, 27, 28b, 29 and 30."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.ingest.needs import enter, need_value
from planner.ledger import db
from planner.paths import Layout
from planner.plan import esttax
from planner.taxprep import draft, mi1040, statereturn
from tests.test_forms import one

F, S1 = mi1040.FORM, mi1040.SCHED


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
        fingerprint="synthetic-mi-you",
        file_name="mi-you.pdf",
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
        ("state", "MI"),
    ):
        enter(lay, 2025, key, text)
    return lay


def _checks(d: draft.Draft) -> list[str]:
    return [n for n in d.notes if n.startswith("CHECK")]


def _lines(d: draft.Draft, form: str, *lines: str) -> tuple[float | None, ...]:
    return tuple(d.get(form, n) for n in lines)


def _ira(amount: float, withheld: float = 0.0) -> list[db.Fact]:
    return [
        db.Fact("1099-R", 2025, "IRA Custodian (synthetic)", b, c, v, 1)
        for b, c, v in (
            ("1", "", amount),
            ("2a", "", amount),
            ("7", "7", 0.0),
            ("14", "", withheld),
        )
    ]


def _source(d: draft.Draft, form: str, line: str) -> str:
    return next(ln.source for ln in d.lines if (ln.form, ln.line) == (form, line))


BANK = [
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "1", "", 500.0, 1),
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "3", "", 300.0, 1),
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "8", "", 120.0, 1),
]
W2 = {"1": 60250.0, "2": 6000.0, "17": 1000.0}


def test_mi_1040_owed_with_us_bond_interest_and_an_estimated_payment(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home, W2, BANK)
    esttax.record(lay, 2025, "mi", "2025-06-15", 100.0)
    d = draft.build(lay, 2025)
    assert _lines(d, S1, "9", "10", "29", "31") == (0.0, 300.0, 300.0, 300.0)
    assert _lines(d, F, "9a", "9f", "10", "11", "12", "13", "14") == (
        5800.0,
        5800.0,
        61050.0,
        0.0,
        61050.0,
        300.0,
        60750.0,
    )
    # 4.25% of 54,950 = 2,335.375, to the dollar
    assert _lines(d, F, "15", "16", "17", "21", "25") == (
        5800.0,
        54950.0,
        2335.0,
        2335.0,
        2335.0,
    )
    assert _lines(d, F, "31", "32", "34", "36", "37") == (
        1000.0,
        100.0,
        1100.0,
        1235.0,
        None,
    )
    assert not _checks(d), d.notes
    out = draft.render(d)
    assert "MI-1040" in out and "2025-06-15 100.00 (typed)" in out
    assert "MI-1040 Schedule 1 (Form 3423)" in out
    assert any("other states' bonds" in n for n in d.notes)
    assert any("MI-2210" in n and "110%" in n for n in d.notes)


def test_retirees_born_after_1945_take_the_form_4884_subtraction(
    planner_home: Path,
) -> None:
    more = [*_ira(50000.0), db.Fact("SSA-1099", 2025, "SSA", "5", "", 30000.0, 1)]
    lay = _lay(planner_home, {}, more, status="married_joint", born="1955-05-01")
    enter(lay, 2025, "spouse_birth_date", "1956-03-01")
    d = draft.build(lay, 2025)
    # the Form 4884 Section D amount goes on line 27, not the Tier 3 line 26
    assert _lines(d, S1, "14", "26", "27", "31") == (23850.0, None, 50000.0, 73850.0)
    assert _lines(d, F, "9a", "10", "13", "14", "16", "17") == (
        11600.0,
        73850.0,
        73850.0,
        0.0,
        0.0,
        0.0,
    )
    assert "Form 4884 Section D" in _source(d, S1, "27")
    assert not _checks(d), d.notes


def test_section_d_holds_the_subtraction_to_75_percent_of_the_limit(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home, {}, _ira(80000.0, 2000.0), born="1958-02-01")
    d = draft.build(lay, 2025)
    # 75% of 65,897 = 49,422.75: Form 4884 line 19, "maximum $49,423"
    assert _lines(d, S1, "27", "31") == (49423.0, 49423.0)
    assert _lines(d, F, "14", "16", "17") == (30577.0, 24777.0, 1053.0)
    assert _lines(d, F, "31", "34", "37", "39") == (2000.0, 2000.0, 947.0, 947.0)
    assert not _checks(d), d.notes


@pytest.mark.parametrize(
    ("born", "line", "amount"),
    [
        # Tier 3: $40,000 joint less the 9a exemptions (2 x 5,800)
        ("1955-05-01", "26", 28400.0),
        # Tier 2: $40,000 joint
        ("1950-05-01", "25", 40000.0),
    ],
)
def test_the_michigan_standard_deduction_by_birth_year(
    planner_home: Path, born: str, line: str, amount: float
) -> None:
    w2 = {"1": 50000.0, "2": 4000.0, "17": 1500.0}
    lay = _lay(planner_home, w2, status="married_joint", born=born)
    enter(lay, 2025, "spouse_birth_date", born[:3] + "6-03-01")
    d = draft.build(lay, 2025)
    assert _lines(d, S1, line, "27", "31") == (amount, None, amount)
    assert d.get(F, "14") == 50000.0 - amount
    assert not _checks(d), d.notes


def test_head_of_household_takes_the_michigan_eitc(planner_home: Path) -> None:
    w2 = {"1": 20000.0, "2": 0.0, "17": 300.0}
    lay = _lay(planner_home, w2, status="head_of_household", dependents="2018-03-02")
    d = draft.build(lay, 2025)
    assert _lines(d, F, "9a", "15", "16", "17") == (11600.0, 11600.0, 8400.0, 357.0)
    # 30% of the 4,328 federal credit = 1,298.40
    assert _lines(d, F, "28a", "28b", "31", "34") == (4328.0, 1298.0, 300.0, 1598.0)
    assert _lines(d, F, "37", "39", "36") == (1241.0, 1241.0, None)
    assert any("MI-1040CR-7" in n for n in d.notes)
    assert not _checks(d), d.notes


def test_a_typed_property_tax_credit_lands_on_line_26(planner_home: Path) -> None:
    lay = _lay(planner_home, W2, BANK)
    d = draft.build(lay, 2025)
    assert d.get(F, "26") == 0.0
    enter(lay, 2025, "mi_property_tax_credit", "600")
    d = draft.build(lay, 2025)
    assert _lines(d, F, "26", "34", "36") == (600.0, 1600.0, 735.0)
    assert not any("type mi_property_tax_credit" in n for n in d.notes)


def test_typed_adjustments_land_on_schedule_1(planner_home: Path) -> None:
    lay = _lay(planner_home, W2, BANK)
    for key, text in (("mi_additions", "1000"), ("mi_subtractions", "500")):
        enter(lay, 2025, key, text)
    d = draft.build(lay, 2025)
    assert _lines(d, S1, "9", "29", "31") == (1000.0, 800.0, 800.0)
    # 4.25% of 55,450 = 2,356.625
    assert _lines(d, F, "11", "12", "13", "14", "16", "17") == (
        1000.0,
        62050.0,
        800.0,
        61250.0,
        55450.0,
        2357.0,
    )
    assert not any("other states' bonds" in n for n in d.notes)
    assert not _checks(d), d.notes


def test_rounding_follows_the_book() -> None:
    # "49 cents or less, round down; 50 cents or more, round up"
    assert (mi1040.dollars(1.49), mi1040.dollars(2.50)) == (1.0, 3.0)


def test_mi_is_a_registry_entry() -> None:
    mi = statereturn.get("mi")
    assert mi is not None and mi.form == F and mi.template == "MI-1040"
    assert mi.tax_line == "21" and mi.carry == "state_tax"
    assert mi.prior == ("21", "-26", "-27", "-28b", "-29", "-30")
    assert set(mi.forms) == {F, S1}
    assert {"state_withheld", "mi_property_tax_credit"} <= set(mi.keys)


PAGE1 = [
    "Michigan Department of Treasury (Rev. 06-25), Page 1 of 3",
    "2025 MICHIGAN Individual Income Tax Return MI-1040",
    "f. Add lines 9a, 9b, 9c, 9d and 9e. Enter here and on line 15 ........ 9f. "
    "5,800 00",
    "10. Adjusted Gross Income from your U.S. Form 1040 (see instructions) ..... "
    "10. 61,050 00",
    "11. Additions from Schedule 1, line 9. Include Schedule 1 .......... 11. 0 00",
    "12. Total. Add lines 10 and 11......................... 12. 61,050 00",
    "13. Subtractions from Schedule 1, line 31. Include Schedule 1 ...... 13. 300 00",
    "14. Income subject to tax. Subtract line 13 from line 12. If line 13 is greater "
    "than line 12, enter 0 .... 14. 60,750 00",
    "15. Exemption allowance. Enter amount from line 9f or Schedule NR, line 19..... "
    "15. 5,800 00",
    "16. Taxable income. Subtract line 15 from line 14. If line 15 is greater than "
    "line 14, enter 0 ..... 16. 54,950 00",
    "17. Tax. Multiply line 16 by 4.25% (0.0425) ............... 17. 2,335 00",
]
PAGE2 = [
    "2025 MI-1040, Page 2 of 3",
    "If the sum of lines 18b, 19b, and 20b is greater than line 17, enter 0 "
    "...... 21. 2,335 00",
    "25. Total Tax Liability. Add lines 21 through 24 .......... 25. 2,335 00",
    "26. Property Tax Credit. Include MI-1040CR or MI-1040CR-2 ....... 26. 200 00",
    "and enter result on line 28b. ......... 28a. 400 00 28b. 120 00",
    "31. Michigan tax withheld from Schedule W, line 6. Include Schedule W (do not "
    "submit W-2s) ...... 31. 1,000 00",
    "32. Estimated tax, extension payments and 2024 credit forward ...... 32. 100 00",
    "34. Total refundable credits and payments. Add lines 26, 27, 28b, 29, 30, 31, 32 "
    "and 33c ...... 34. 1,420 00",
]
PAGE3 = [
    "2025 MI-1040, Page 3 of 3",
    "REFUND OR TAX DUE",
    "Include interest 00 and penalty 00 ............ YOU OWE 36. 915 00",
    "38. Credit Forward. Amount of line 37 to be credited to your 2026 estimated tax "
    "for your 2026 tax return ... 38. 0 00",
]


def test_a_filed_mi_1040_reads_as_one_form(tmp_path: Path) -> None:
    (f,) = one(tmp_path, "mi1040", [PAGE1, PAGE2, PAGE3])
    assert (f.form, f.issuer, f.tax_year) == ("MI-1040", "MI", 2025)
    got = {b: f.boxes[b][1] for b in ("9f", "10", "11", "12", "13", "14", "15")}
    assert got == {
        "9f": 5800.0,
        "10": 61050.0,
        "11": 0.0,
        "12": 61050.0,
        "13": 300.0,
        "14": 60750.0,
        "15": 5800.0,
    }
    got = {b: f.boxes[b][1] for b in ("16", "17", "21", "25", "26", "28b", "31")}
    assert got == {
        "16": 54950.0,
        "17": 2335.0,
        "21": 2335.0,
        "25": 2335.0,
        "26": 200.0,
        "28b": 120.0,
        "31": 1000.0,
    }
    assert {b: f.boxes[b][1] for b in ("32", "34", "36")} == {
        "32": 100.0,
        "34": 1420.0,
        "36": 915.0,
    }
    assert "37" not in f.boxes and "39" not in f.boxes


@pytest.mark.parametrize(
    ("credits", "want"),
    [((("26", 200.0), ("28b", 120.0)), 2015.0), ((), 2335.0)],
)
def test_a_filed_mi_1040_is_next_years_prior_state_tax(
    planner_home: Path, credits: tuple[tuple[str, float], ...], want: float
) -> None:
    lay = _lay(planner_home, W2)
    enter(lay, 2026, "state", "MI")
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-mi-filed",
        file_name="filed-mi1040.pdf",
        kind="pdf",
        pages=3,
        batch="b2",
        facts=[
            db.Fact("MI-1040", 2025, "MI", line, "", value, 1)
            for line, value in (("21", 2335.0), ("31", 1000.0), *credits)
        ],
    )
    try:
        # MI-2210: line 21 less the refundable credits on lines 26-30;
        # withholding (31) is a payment, not a credit
        assert need_value(conn, lay, 2026, "prior_state_tax") == want
    finally:
        conn.close()
