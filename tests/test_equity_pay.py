"""Unit 3e-8: equity pay from synthetic forms, by Pub. 525, the 2025 Form 6251
and its instructions, the Form 8949 instructions (code B) and the April 2025
Forms 3921 and 3922."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from planner.engine import tax
from planner.engine.household import Household
from planner.ingest.needs import enter, need_for, need_values
from planner.ingest.pdf import load_templates, parse_texts
from planner.ledger import db
from planner.taxprep import capgains, draft, f6251
from tests.test_income_1099g import _doc, _lay

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = load_templates(ROOT / "templates" / "forms")
F3921 = "\n".join(
    [
        "Form 3921 (Rev. April 2025) Exercise of an Incentive Stock Option Under "
        "Section 422(b)",
        "TRANSFEROR'S name, street address, city or town: Example Tech Inc (synthetic)",
        "1 Date option granted 03/01/2021",
        "2 Date option exercised 06/15/2025",
        "3 Exercise price per share $ 12.5000",
        "4 Fair market value per share on exercise date $ 87.2500",
        "5 No. of shares transferred 400",
    ]
)
F3922 = "\n".join(
    [
        "Form 3922 (Rev. April 2025) Transfer of Stock Acquired Through an Employee "
        "Stock Purchase Plan Under Section 423(c)",
        "CORPORATION'S name, street address: Example Tech Inc (synthetic)",
        "1 Date option granted 01/01/2025",
        "2 Date option exercised 06/30/2025",
        "3 Fair market value per share on grant date $ 40.1250",
        "4 Fair market value per share on exercise date $ 50.0000",
        "5 Exercise price paid per share $ 34.1063",
        "6 No. of shares transferred 125.5",
        "7 Date legal title transferred 06/30/2025",
        "8 Exercise price per share determined as if the option was exercised on "
        "the date shown in box 1 $ 34.1063",
    ]
)


def _boxes(page: str) -> tuple[str, int, str, dict[str, float]]:
    (f,) = parse_texts([page], TEMPLATES)
    return f.form, f.tax_year, f.issuer, {k: float(v[1]) for k, v in f.boxes.items()}


def test_templates_read_forms_3921_and_3922() -> None:
    assert _boxes(F3921) == (
        "3921",
        2025,
        "Example Tech Inc (synthetic) (granted 03/01/2021, exercised 06/15/2025)",
        {"3": 12.5, "4": 87.25, "5": 400.0},
    )
    form, year, _, boxes = _boxes(F3922)
    assert (form, year) == ("3922", 2025)
    assert boxes == {"3": 40.125, "4": 50.0, "5": 34.1063, "6": 125.5, "8": 34.1063}


def test_one_form_per_exercise_not_a_correction() -> None:
    second = F3921.replace("03/01/2021", "03/01/2022").replace("12.5000", "20.0000")
    forms = parse_texts([F3921, second], TEMPLATES)
    assert [f.boxes["3"][1] for f in forms] == [12.5, 20.0]
    assert len({f.issuer for f in forms}) == 2  # the ledger keeps both
    # a corrected copy of the same exercise is the same key, so it replaces
    (fixed,) = parse_texts([F3921.replace("400", "380")], TEMPLATES)
    assert fixed.issuer == forms[0].issuer


def test_w2_box_12_code_v() -> None:
    page = "\n".join(
        [
            "Form W-2 Wage and Tax Statement 2025",
            "a Employee's social security number XXX-XX-1234",
            "b Employer identification number (EIN) 12-3456789",
            "c Employer's name, address, and ZIP code Example Employer Inc (synthetic)",
            "1 Wages, tips, other compensation 52,000.00",
            "2 Federal income tax withheld 6,000.00",
            "12b V $ 7,500.00",
        ]
    )
    (f,) = parse_texts([page], TEMPLATES)
    assert f.boxes["12V"][1] == 7500.0


def _amt(iso: float = 0.0, age: int = 45, status: str = "SINGLE") -> dict[str, float]:
    hh = Household(
        age=age,
        filing_status=status,
        state="FL",
        wages=150000,
        tax_unit_inputs={tax.ISO: iso},
    )
    names = ("amt_income", "alternative_minimum_tax", "amt_separate_addition")
    return tax.values(2025, hh, names)


def test_engine_adds_the_iso_spread_and_the_senior_deduction() -> None:
    base = _amt()
    iso = _amt(iso=300000.0)
    assert iso["amt_income"] == base["amt_income"] + 300000.0
    assert iso["alternative_minimum_tax"] > 0 == base["alternative_minimum_tax"]
    senior = _amt(iso=300000.0, age=70)
    # Form 6251 line 1a takes Schedule 1-A line 37 out of the deductions
    assert senior["amt_income"] == iso["amt_income"]
    mfs = _amt(iso=900000.0, status="SEPARATE")
    line4 = mfs["amt_income"] - mfs["amt_separate_addition"]
    assert mfs["amt_separate_addition"] == round(
        min(0.25 * (line4 - 900350.0), 68500.0), 2
    )


def _3921(
    lay: object,
    year: int,
    price: float,
    fmv: float,
    shares: float,
    issuer: str = "Example Tech Inc (synthetic)",
) -> None:
    _doc(
        lay,  # type: ignore[arg-type]
        f"3921-{year}-{price}",
        [
            db.Fact("3921", year, issuer, b, "", v, 1)
            for b, v in {"3": price, "4": fmv, "5": shares}.items()
        ],
    )


def test_needed_panel_takes_the_spread_and_leads_the_rest(planner_home: Path) -> None:
    lay = _lay(planner_home)
    got = need_values(
        _conn(lay),
        lay,
        2025,
        ("iso_amt_adjustment", "stock_option_income", "equity_basis_short"),
    )
    # nothing on file: asked, like any rare item ("type 0 if none")
    assert got == dict.fromkeys(got)
    assert need_for("iso_amt_adjustment").doc == "equity"
    _3921(lay, 2025, 10.0, 110.0, 3000.0)
    _3921(lay, 2025, 20.0, 15.0, 100.0, "Other Co (synthetic)")  # underwater
    got = need_values(
        _conn(lay),
        lay,
        2025,
        ("iso_amt_adjustment", "stock_option_income", "equity_basis_long"),
    )
    assert got["iso_amt_adjustment"] == 300000.0
    assert got["stock_option_income"] is None  # a Form 3921 asks, not assumes
    assert got["equity_basis_long"] is None
    assert need_for("stock_option_income").doc == "equity"


def _conn(lay: object) -> sqlite3.Connection:
    return db.connect(lay.data / "ledger" / "planner.db")  # type: ignore[attr-defined]


def _b(lay: object, boxes: dict[str, float]) -> None:
    _doc(
        lay,  # type: ignore[arg-type]
        "1099b",
        [
            db.Fact("1099-B", 2025, "Broker (synthetic)", b, "", v, 1)
            for b, v in boxes.items()
        ],
    )


def test_code_b_moves_the_summary_to_line_1b(planner_home: Path) -> None:
    lay = _lay(planner_home)
    _b(
        lay,
        {
            "st_proceeds": 10000.0,
            "st_basis": 4000.0,
            "lt_proceeds": 5000.0,
            "lt_basis": 1000.0,
        },
    )
    enter(lay, 2025, "equity_basis_short", "3000")
    conn = _conn(lay)
    cg = capgains.build(conn, lay, 2025)
    assert cg.lines["1b"] == 3000.0  # 10,000 - 4,000 - 3,000
    assert "1a" not in cg.lines
    assert cg.lines["8a"] == 4000.0
    (lot,) = [lt for lt in cg.lots if lt.box == "A"]
    assert (lot.code, lot.adjustment, lot.gain) == ("B", -3000.0, 3000.0)
    assert any("code B" in n for n in cg.notes)


def test_draft_lays_form_6251_and_line_8k(planner_home: Path) -> None:
    lay = _lay(planner_home)  # 40,000 of wages
    _3921(lay, 2025, 10.0, 110.0, 3000.0)
    enter(lay, 2025, "stock_option_income", "2500")
    enter(lay, 2025, "equity_basis_short", "0")
    enter(lay, 2025, "equity_basis_long", "0")
    d = draft.build(lay, 2025)
    assert d.get("Sch 1", "8k") == 2500.0
    got = {k: d.get(f6251.FORM, k) for k in ("1a", "1b", "2a", "2i", "4", "5", "11")}
    assert got["2i"] == 300000.0
    assert got["1a"] == d.get("1040", "14")
    assert got["1b"] == round(d.get("1040", "11b") - got["1a"], 2)  # type: ignore[operator]
    assert got["4"] == round(got["1b"] + got["2a"] + got["2i"], 2)  # type: ignore[operator]
    assert got["5"] == 88100.0
    assert got["11"] == d.get("Sch 2", "2")
    assert (got["11"] or 0) > 0
    # 1040 line 16 by the Tax Table sets line 10, so line 11 is the form's
    l10 = d.get("1040", "16")
    assert got["11"] == round(d.get(f6251.FORM, "9") - l10, 2)  # type: ignore[operator]
    assert d.get("1040", "17") == d.get("Sch 2", "3") == got["11"]
    assert not [n for n in d.notes if n.startswith("CHECK: Form 6251")]
    assert any("Form 8801" in n for n in d.notes)
    assert "Form 6251 (alternative minimum tax)" in draft.render(d)


def test_no_form_6251_without_amt_or_options(planner_home: Path) -> None:
    d = draft.build(_lay(planner_home), 2025)
    assert d.get(f6251.FORM, "11") is None
