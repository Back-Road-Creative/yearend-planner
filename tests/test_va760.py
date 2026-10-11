"""Unit 3d-14: Virginia Form 760, read from a filed return. Line numbers and
labels follow the 2025 Form 760 (Virginia Department of Taxation)."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.ingest.needs import enter, need_value
from planner.ingest.pdf import Unmatched
from planner.ledger import db
from planner.paths import Layout
from planner.plan import esttax
from planner.taxprep import draft, statereturn, va760
from tests.test_forms import one

F = va760.FORM


def _doc(lay: Layout, name: str, facts: list[db.Fact], owner: str) -> None:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint=f"synthetic-va-{name}",
        file_name=f"va-{name}.pdf",
        kind="pdf",
        pages=1,
        batch="b1",
        facts=facts,
        owner=owner,
    )
    conn.close()


def _w2(w2: dict[str, float], who: str) -> list[db.Fact]:
    return [
        db.Fact("W-2", 2025, f"Employer {who} (synthetic)", b, "", v, 1)
        for b, v in w2.items()
    ]


def _lay(
    planner_home: Path,
    w2: dict[str, float],
    status: str = "single",
    spouse: dict[str, float] | None = None,
) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    _doc(lay, "you", _w2(w2, "A"), "you")
    if spouse:
        _doc(lay, "spouse", _w2(spouse, "B"), "spouse")
    for key, text in (
        ("birth_date", "1980-05-01"),
        ("filing_status", status),
        ("dependents", "none"),
        ("state", "VA"),
    ):
        enter(lay, 2025, key, text)
    if spouse:
        enter(lay, 2025, "spouse_birth_date", "1981-03-01")
    return lay


def _checks(d: draft.Draft) -> list[str]:
    return [n for n in d.notes if n.startswith("CHECK")]


def _lines(d: draft.Draft, *lines: str) -> tuple[float | None, ...]:
    return tuple(d.get(F, n) for n in lines)


def _source(d: draft.Draft, line: str) -> str:
    return next(ln.source for ln in d.lines if (ln.form, ln.line) == (F, line))


W2 = {"1": 60000.0, "2": 6000.0, "16": 60000.0, "17": 2800.0}


def test_form_760_with_withholding_and_an_estimated_payment(planner_home: Path) -> None:
    lay = _lay(planner_home, W2)
    esttax.record(lay, 2025, "va", "2025-06-15", 100.0)
    d = draft.build(lay, 2025)
    assert _lines(d, "1", "2", "3", "8", "9", "10", "11", "12", "13", "14", "15") == (
        60000.0,
        0.0,
        60000.0,
        0.0,
        60000.0,
        None,
        8750.0,
        930.0,
        0.0,
        9680.0,
        50320.0,
    )
    # 720 + 5.75% of (50,320 - 17,000) = 2,635.90
    assert _lines(d, "16", "17", "18", "19a", "20", "23", "26") == (
        2636.0,
        0.0,
        2636.0,
        2800.0,
        100.0,
        0.0,
        2900.0,
    )
    assert _lines(d, "27", "28", "33", "34", "35", "36") == (
        None,
        264.0,
        0.0,
        0.0,
        None,
        264.0,
    )
    assert "W-2 box 17" in _source(d, "19a")
    assert "va_use_tax: not given" in _source(d, "33")
    assert not _checks(d), d.notes
    out = draft.render(d)
    assert "Form 760" in out and "2025-06-15 100.00 (typed)" in out
    assert any("Form 760C" in n and "90%" in n for n in d.notes)
    assert any("full-year VA resident" in n for n in d.notes)


def test_under_the_filing_threshold_there_is_no_tax_but_the_refundable_credit(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home, {"1": 11000.0, "16": 11000.0})
    d = draft.build(lay, 2025)
    assert d.get(F, "9") == 11000.0 and d.get(F, "16") == 0.0
    assert "11,950 filing threshold" in _source(d, "16")
    credit = d.get(F, "23")
    assert credit and credit > 0 and "refundable" in _source(d, "23")
    assert _lines(d, "26", "28", "36") == (credit, credit, credit)
    assert not _checks(d), d.notes


def test_a_low_income_filer_takes_the_low_income_credit(planner_home: Path) -> None:
    lay = _lay(planner_home, {"1": 14000.0, "16": 14000.0})
    d = draft.build(lay, 2025)
    # 14,000 - 8,750 - 930 = 4,320: 60 + 3% of 1,320 = 99.60
    assert _lines(d, "15", "16", "18") == (4320.0, 100.0, 100.0)
    # $300 an exemption, not more than line 18; better than 20% of the EITC
    assert d.get(F, "23") == 100.0 and "low-income credit" in _source(d, "23")
    assert _lines(d, "26", "28", "35", "36") == (100.0, 0.0, None, None)


def test_joint_filers_take_the_spouse_tax_adjustment(planner_home: Path) -> None:
    lay = _lay(
        planner_home,
        {"1": 70000.0, "16": 70000.0, "17": 3000.0},
        status="married_joint",
        spouse={"1": 50000.0, "16": 50000.0, "17": 2000.0},
    )
    d = draft.build(lay, 2025)
    l16, l17, l18 = _lines(d, "16", "17", "18")
    assert l16 and l17 and l17 > 0 and l18 == l16 - l17
    assert d.get(F, "11") == 17500.0 and d.get(F, "12") == 1860.0
    assert not _checks(d), d.notes


def test_typed_items_land_on_their_lines(planner_home: Path) -> None:
    lay = _lay(planner_home, W2)
    for key, text in (
        ("va_additions", "400"),
        ("va_subtractions", "250"),
        ("va_deductions", "300"),
        ("va_use_tax", "35"),
    ):
        enter(lay, 2025, key, text)
    d = draft.build(lay, 2025)
    assert _lines(d, "2", "7", "9", "13", "15") == (
        400.0,
        250.0,
        60150.0,
        300.0,
        50170.0,
    )
    assert _lines(d, "33", "34") == (35.0, 35.0)
    assert "typed" in _source(d, "2") and not _checks(d), d.notes


@pytest.mark.parametrize(
    ("income", "tax"),
    [
        (0.0, 0.0),
        (3000.0, 60.0),
        (5000.0, 120.0),
        (17000.0, 720.0),
        (50320.0, 2636.0),
        (90000.0, 4918.0),  # the booklet's example: 4,917.50
    ],
)
def test_the_tax_rate_schedule(income: float, tax: float) -> None:
    assert va760.tax_on(income, 2025) == tax


def test_rounding_is_half_up() -> None:
    assert va760.dollars(4917.50) == 4918.0 and va760.dollars(4917.49) == 4917.0


def test_va_is_a_registry_entry() -> None:
    va = statereturn.get("va")
    assert va is not None and va.form == F and va.template == "VA-760"
    assert va.tax_line == "18" and va.carry == "state_tax"
    assert va.prior == ("18", "-23", "-24", "-25") and set(va.forms) == {F}
    assert {"state_withheld", "va_additions", "va_use_tax"} <= set(va.keys)


PAGE1 = [
    "2025",
    "Virginia Form 760",
    "Resident Income Tax Return",
    "1. Adjusted Gross Income from federal return - Not federal taxable income "
    "........ 1 61,050.00",
    "2. Additions from enclosed Schedule ADJ, Line 3. ........ 2 0.00",
    "3. Add Lines 1 and 2 ........ 3 61,050.00",
    "You 0.00 + Spouse 0.00 = 4 0.00",
    "7. Subtractions from enclosed Schedule ADJ, Line 7 ........ 7 300.00",
    "8. Add Lines 4, 5, 6, and 7 ........ 8 300.00",
    "9. Virginia Adjusted Gross Income (VAGI) - Subtract Line 8 from Line 3.",
    "Note: If less than $11,950 for Filing Status 1 or 3; or $23,900 for Filing "
    "Status 2, your tax is $0.00 ....9 60,750.00",
    "11. If you do not claim itemized deductions on Line 10, enter standard "
    "deduction. See instructions. ...... 11 8,750.00",
    "12. Exemptions. Sum of total from Exemption Section A plus Exemption Section "
    "B ........ 12 930.00",
    "14. Add Lines 10, 11, 12, and 13 ........ 14 9,680.00",
    "15. Virginia Taxable Income - Subtract Line 14 from Line 9 ........ 15 51,070.00",
]
PAGE2 = [
    "Social Security Number",
    "2025 Form 760",
    "16. Amount of Tax from Tax Table or Tax Rate Schedule (round to whole "
    "dollars) ........ 16 2,679.00",
    "17. Spouse Tax Adjustment (STA). Filing Status 2",
    "and STA amount on Line 17. 17 0.00",
    "18. Net Amount of Tax - Subtract Line 17 from Line 16 ........ 18 2,679.00",
    "19a. Your Virginia withholding ........ 19a 2,800.00",
    "20. Estimated tax payments for taxable year 2025 (from Form 760ES) "
    "........ 20 100.00",
    "23. Tax Credit for Low-Income Individuals or Earned Income Credit from Sch. "
    "ADJ, Line 17 ...... 23 0.00",
    "26. Add Lines 19a through 25 ........ 26 2,900.00",
    "28. If Line 18 is less than Line 26, subtract Line 18 from Line 26. This is "
    "Your Tax Overpayment ........ 28 221.00",
    "36. If Line 28 is greater than Line 34, subtract Line 34 from Line 28 "
    "........YOUR REFUND ........ 36 221.00",
]


def test_a_filed_form_760_reads_as_one_form(tmp_path: Path) -> None:
    (f,) = one(tmp_path, "va760", [PAGE1, PAGE2])
    assert (f.form, f.issuer, f.tax_year) == ("VA-760", "VA", 2025)
    got = {b: f.boxes[b][1] for b in ("1", "2", "3", "4", "7", "8", "9", "11")}
    assert got == {
        "1": 61050.0,
        "2": 0.0,
        "3": 61050.0,
        "4": 0.0,
        "7": 300.0,
        "8": 300.0,
        "9": 60750.0,
        "11": 8750.0,
    }
    got = {b: f.boxes[b][1] for b in ("12", "14", "15", "16", "17", "18", "19a")}
    assert got == {
        "12": 930.0,
        "14": 9680.0,
        "15": 51070.0,
        "16": 2679.0,
        "17": 0.0,
        "18": 2679.0,
        "19a": 2800.0,
    }
    got = {b: f.boxes[b][1] for b in ("20", "23", "26", "28", "36")}
    assert got == {"20": 100.0, "23": 0.0, "26": 2900.0, "28": 221.0, "36": 221.0}
    assert "27" not in f.boxes and "10" not in f.boxes


def test_a_part_year_form_760py_is_not_read_as_form_760(tmp_path: Path) -> None:
    page = [line.replace("Virginia Form 760", "Virginia Form 760PY") for line in PAGE1]
    with pytest.raises(Unmatched, match="no tax year"):
        one(tmp_path, "va760py", [page])


def test_a_filed_form_760_is_next_years_prior_state_tax(planner_home: Path) -> None:
    lay = _lay(planner_home, W2)
    enter(lay, 2026, "state", "VA")
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-va-filed",
        file_name="filed-va760.pdf",
        kind="pdf",
        pages=2,
        batch="b2",
        facts=[
            db.Fact("VA-760", 2025, "VA", line, "", value, 1)
            for line, value in (("18", 2636.0), ("19a", 2800.0), ("23", 100.0))
        ],
    )
    try:
        # Form 760C: the liability after the Spouse Tax Adjustment and the
        # credits on lines 23-25; withholding is a payment
        assert need_value(conn, lay, 2026, "prior_state_tax") == 2536.0
    finally:
        conn.close()
