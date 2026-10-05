"""Phase 4i: the draft return on a synthetic 2025 household (single, NC, 54 at
year end, $40,000 self-employment income, $1,000 interest with $100 withheld,
a $1,500 federal estimated payment, a marketplace plan all year whose advance
credit runs ahead of the credit allowed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.engine import tax
from planner.ingest.needs import enter, need_for, needed, parse_value
from planner.ledger import db
from planner.paths import Layout
from planner.plan import esttax, inputs
from planner.taxprep import draft
from tests.test_d400 import _lay as nc_lay

runner = CliRunner()
YEAR = 2025
PREMIUM, SLCSP, APTC = 450.0, 520.0, 540.0


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    for key, text in (
        ("birth_date", "1971-06-15"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("se_income", "40,000"),
        ("wages", "0"),
        ("ordinary_dividends", "0"),
        ("qualified_dividends", "0"),
    ):
        enter(lay, YEAR, key, text)
    facts = [
        db.Fact(
            "1099-INT", YEAR, "Example Bank (synthetic)", "1", "Interest", 1000.0, 1
        ),
        db.Fact(
            "1099-INT", YEAR, "Example Bank (synthetic)", "4", "Withheld", 100.0, 1
        ),
    ]
    for m in range(1, 13):
        for box, value in (("premium", PREMIUM), ("slcsp", SLCSP), ("aptc", APTC)):
            facts.append(
                db.Fact("1095-A", YEAR, "NC-synthetic", f"{box}_{m:02d}", box, value, 1)
            )
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic",
        file_name="synthetic.pdf",
        kind="pdf",
        pages=1,
        batch="b1",
        facts=facts,
    )
    conn.close()
    esttax.record(lay, YEAR, "fed", "2025-04-15", 1500.0)
    return lay


def test_repayment_cap_table() -> None:
    assert draft.repayment_cap(2025, "SINGLE", 150) == 375.0
    assert draft.repayment_cap(2025, "SINGLE", 299) == 975.0
    assert draft.repayment_cap(2025, "JOINT", 350) == 3250.0
    assert draft.repayment_cap(2025, "SINGLE", 400) is None
    assert draft.repayment_cap(2026, "SINGLE", 150) is None  # P.L. 119-21
    assert draft.repayment_cap(2024, "SINGLE", 150) is None  # not tabled: repay all


def test_draft_return_ties_out(lay: Layout) -> None:
    d = draft.build(lay, YEAR)

    def g(form: str, line: str) -> float:
        value = d.get(form, line)
        assert value is not None, (form, line)
        return value

    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes
    # income and the schedules that feed it
    assert g("Sch 1", "3") == 40000.0 and g("1040", "8") == 40000.0
    assert g("1040", "2b") == 1000.0
    assert g("1040", "9") == 41000.0
    se = g("Sch SE", "12")
    assert se == pytest.approx(40000 * 0.9235 * 0.153, abs=1)
    assert g("Sch 1", "15") == pytest.approx(se / 2, abs=0.01)
    assert g("1040", "11a") == pytest.approx(41000 - g("1040", "10"), abs=0.01)
    assert g("1040", "14") == pytest.approx(
        g("1040", "12e") + g("1040", "13a") + g("1040", "13b"), abs=0.01
    )
    assert g("1040", "12e") == 15750.0  # 2025 single standard deduction (P.L. 119-21)
    # Form 8962: twelve months, the advance runs ahead, the 2025 cap applies
    pct = g("8962", "5")
    assert 200 <= pct < 300
    assert 0.02 < g("8962", "7") < 0.06 and g("8962", "7") != round(g("8962", "7"), 2)
    assert g("8962", "8a") == round(g("8962", "3") * g("8962", "7"))
    assert g("8962", "8b") == round(g("8962", "8a") / 12)
    allowed = min(PREMIUM, max(SLCSP - g("8962", "8b"), 0))
    assert g("8962", "12e") == pytest.approx(allowed, abs=0.01)
    assert g("8962", "24") == pytest.approx(12 * allowed, abs=0.05)
    assert g("8962", "25") == 12 * APTC
    assert g("8962", "28") == 975.0
    assert g("8962", "29") == min(g("8962", "27"), 975.0)
    assert g("Sch 2", "1a") == g("8962", "29") and g("Sch 3", "9") == 0.0
    assert g("1040", "17") == g("Sch 2", "3")
    # payments and the bottom line
    assert g("1040", "25b") == 100.0 and g("1040", "26") == 1500.0
    assert g("1040", "24") == pytest.approx(g("1040", "22") + g("1040", "23"), abs=0.01)
    owe, refund = d.get("1040", "37"), d.get("1040", "34")
    assert (owe or 0) - (refund or 0) == pytest.approx(
        g("1040", "24") - g("1040", "33"), abs=0.01
    )
    assert "1099-NEC from each client" in d.missing
    assert "short_term_gains" in d.unknown and "interest" not in d.unknown
    src = {(ln.form, ln.line): ln.source for ln in d.lines}
    assert "1099-INT" in src[("1040", "2b")] and "1099-INT" in src[("1040", "25b")]
    text = draft.render(d)
    assert "Form 8962" in text and "Schedule SE" in text
    assert "line numbers follow the 2025 forms" in text


def test_cli_draft_json(lay: Layout) -> None:
    r = runner.invoke(app, ["draft", "--year", str(YEAR), "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["year"] == YEAR
    lines = {(ln["form"], ln["line"]): ln for ln in data["lines"]}
    assert lines[("1040", "2b")]["source"]
    assert lines[("1040", "26")]["value"] == 1500.0


@pytest.mark.engine
def test_exempt_interest_reaches_line_2a_and_aca_magi(lay: Layout) -> None:
    before = draft.build(lay, YEAR)
    assert before.get("1040", "2a") == 0.0
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-exempt",
        file_name="synthetic-exempt.pdf",
        kind="pdf",
        pages=1,
        batch="b2",
        facts=[
            db.Fact(
                "1099-INT", YEAR, "Example Credit Union (synthetic)", "8", "x", 700.0, 1
            ),
            db.Fact("1099-DIV", YEAR, "Example Fund (synthetic)", "12", "x", 300.0, 1),
        ],
    )
    conn.close()
    d = draft.build(lay, YEAR)
    assert d.get("1040", "2a") == 1000.0
    assert d.get("1040", "9") == before.get("1040", "9")  # not taxable income
    assert d.get("1040", "11a") == before.get("1040", "11a")
    line = next(ln for ln in d.lines if (ln.form, ln.line) == ("1040", "2a"))
    assert "1099-INT 2025" in line.source and "1099-DIV 2025" in line.source
    assert any("tax-exempt interest (1040 line 2a)" in n for n in d.notes)
    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes


@pytest.mark.engine
def test_draft_settles_the_se_health_deduction_with_the_credit(lay: Layout) -> None:
    """Pub. 974: Schedule 1 line 17 is the premiums less the credit on Form 8962
    line 24, and the credit is priced on the income that deduction leaves. The
    draft carries both settled; it no longer warns that it does not iterate."""
    enter(lay, YEAR, "se_health_premiums", str(int(12 * PREMIUM)))  # 1095-A col A
    d = draft.build(lay, YEAR)
    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes
    assert not [n for n in d.notes if "does not iterate" in n or "do not iterate" in n]
    line_17, credit = d.get("Sch 1", "17"), d.get("8962", "24")
    assert line_17 is not None and credit is not None
    paid = 12 * PREMIUM
    assert 0 < line_17 < paid and credit > 0
    # the two settle to the premiums (the 8962 monthly contribution is rounded)
    assert line_17 + credit == pytest.approx(paid, abs=12)
    # household income carries the settled deduction, not the full premiums
    assert d.get("8962", "2a") == pytest.approx(d.get("1040", "11a") or 0, abs=0.02)
    assert d.get("1040", "11a") == pytest.approx(
        41_000 - (d.get("Sch 1", "15") or 0) - line_17, abs=0.01
    )
    src = next(ln.source for ln in d.lines if (ln.form, ln.line) == ("Sch 1", "17"))
    assert "Pub. 974" in src


# --- Phase 9n: the draft carries Schedule C, the full Schedule SE and Schedule 1-A


def _checks(d: draft.Draft) -> list[str]:
    return [n for n in d.notes if n.startswith("CHECK")]


def _need(d: draft.Draft, form: str, line: str) -> float:
    value = d.get(form, line)
    assert value is not None, (form, line)
    return value


def _source(d: draft.Draft, form: str, line: str) -> str:
    return next(ln.source for ln in d.lines if (ln.form, ln.line) == (form, line))


def _answers(lay: Layout, **typed: str) -> None:
    for key, text in typed.items():
        enter(lay, YEAR, key, text)


BANK = """\
Transaction ID,Date,Description,Amount,Account
T-1,02/05/2025,CLIENT PAYMENT ACME,"30,000.00",Checking
T-2,06/06/2025,CLIENT PAYMENT ACME,"30,000.00",Checking
T-3,06/07/2025,CARD PURCHASE OFFICE DEPOT,(1500.10),Checking
T-4,06/09/2025,CARD PURCHASE LUNCH CAFE,(200.00),Checking
T-5,06/10/2025,GROCERY MART,(120.00),Checking
T-6,06/11/2025,REFUND CLIENT WIDGETS,(500.00),Checking
"""


@pytest.fixture
def sc_lay(planner_home: Path) -> Layout:
    """A sole proprietor whose income is Schedule C from categorised bank rows
    (synthetic payees), single, NC, 54 at year end."""
    from planner.ingest import ingest
    from planner.taxprep import schedule_c

    lay = Layout(planner_home)
    lay.ensure()
    _answers(
        lay,
        birth_date="1971-06-15",
        filing_status="single",
        state="NC",
        wages="0",
        ordinary_dividends="0",
        qualified_dividends="0",
    )
    (lay.data / "inbox" / "bank.csv").write_text(BANK, encoding="utf-8")
    ingest(lay)
    for text, category in (
        ("client payment", "receipts"),
        ("refund client", "returns"),
        ("office depot", "office"),
        ("lunch cafe", "meals"),
        ("grocery", "personal"),
    ):
        schedule_c.add_rule(lay, text, category)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        schedule_c.store(conn, lay, YEAR)
    finally:
        conn.close()
    return lay


def test_sch1_line3_links_schedule_c_line31(sc_lay: Layout) -> None:
    d = draft.build(sc_lay, YEAR)
    assert _checks(d) == [], d.notes
    # Schedule C: 60,000 receipts, 500 returned, 1,500.10 office, half of 200 meals
    want = {
        "1": 60_000.0,
        "2": 500.0,
        "3": 59_500.0,
        "7": 59_500.0,
        "18": 1_500.10,
        "24b": 100.0,
        "28": 1_600.10,
        "29": 57_899.90,
        "31": 57_899.90,
    }
    for line, value in want.items():
        assert _need(d, "Sch C", line) == pytest.approx(value, abs=0.005), line
    assert "2 bank row(s) categorised receipts" in _source(d, "Sch C", "1")
    assert "half of 1 bank row(s) categorised meals" in _source(d, "Sch C", "24b")
    # Schedule 1 line 3 and Schedule SE line 2 are Schedule C line 31
    assert _need(d, "Sch 1", "3") == _need(d, "Sch C", "31")
    assert _source(d, "Sch 1", "3") == "Sch C line 31"
    assert _need(d, "Sch SE", "2") == _need(d, "Sch C", "31")
    assert _source(d, "Sch SE", "2") == "Sch C line 31"
    assert _need(d, "1040", "8") == _need(d, "Sch 1", "10") == _need(d, "Sch C", "31")
    assert any("home office" in n and "not built" in n for n in d.notes)
    text = draft.render(d)
    assert "Schedule C [verified]\n" in text and "Schedule 1-A [verified]\n" in text


def test_a_typed_figure_that_is_not_schedule_c_is_flagged(sc_lay: Layout) -> None:
    _answers(sc_lay, se_income="50,000")
    d = draft.build(sc_lay, YEAR)
    assert _need(d, "Sch C", "31") == pytest.approx(57_899.90, abs=0.005)
    assert _need(d, "Sch 1", "3") == 50_000.0
    assert _source(d, "Sch 1", "3") != "Sch C line 31"
    assert _need(d, "Sch SE", "2") == 50_000.0
    [check] = _checks(d)
    assert "Schedule C line 31 57,899.90" in check and "50,000.00" in check


def test_no_schedule_c_sheet_without_categorised_rows(lay: Layout) -> None:
    d = draft.build(lay, YEAR)
    assert not [ln for ln in d.lines if ln.form == "Sch C"]
    assert _source(d, "Sch 1", "3") != "Sch C line 31"


def test_schedule_se_part_one_every_line(lay: Layout) -> None:
    d = draft.build(lay, YEAR)
    assert _checks(d) == [], d.notes
    net = 40_000 * 0.9235  # line 3 x 92.35%
    want = {
        "2": 40_000.0,
        "3": 40_000.0,
        "4a": net,
        "4c": net,
        "6": net,
        "7": 176_100.0,  # 2025 Schedule SE line 7, printed on the form
        "8a": 0.0,
        "8d": 0.0,
        "9": 176_100.0,
        "10": net * 0.124,
        "11": net * 0.029,
        "12": net * 0.153,
        "13": net * 0.153 / 2,
    }
    for line, value in want.items():
        assert _need(d, "Sch SE", line) == pytest.approx(value, abs=0.01), line
    assert _need(d, "Sch 2", "4") == _need(d, "Sch SE", "12")
    assert _need(d, "Sch 1", "15") == _need(d, "Sch SE", "13")
    assert "Sch SE line 13" in _source(d, "Sch 1", "15")
    assert "Sch SE line 12" in _source(d, "Sch 2", "4")


def test_schedule_se_wages_use_up_the_wage_base(lay: Layout) -> None:
    """Wages near the base leave only the rest for the 12.4% (lines 8a to 10)."""
    _answers(lay, wages="150,000")
    d = draft.build(lay, YEAR)
    assert _checks(d) == [], d.notes
    net = 40_000 * 0.9235
    assert _need(d, "Sch SE", "8a") == 150_000.0
    assert _need(d, "Sch SE", "9") == 26_100.0
    assert _need(d, "Sch SE", "10") == pytest.approx(26_100 * 0.124, abs=0.01)
    assert _need(d, "Sch SE", "11") == pytest.approx(net * 0.029, abs=0.01)
    assert _need(d, "Sch SE", "12") == pytest.approx(
        26_100 * 0.124 + net * 0.029, abs=0.01
    )


def test_schedule_se_stops_under_400_of_net_earnings(lay: Layout) -> None:
    _answers(lay, se_income="300")
    d = draft.build(lay, YEAR)
    assert _checks(d) == [], d.notes
    assert _need(d, "Sch SE", "4a") == pytest.approx(300 * 0.9235, abs=0.01)
    assert _need(d, "Sch SE", "4c") == pytest.approx(300 * 0.9235, abs=0.01)
    assert d.get("Sch SE", "6") is None and d.get("Sch SE", "10") is None
    assert _need(d, "Sch SE", "12") == 0.0 and _need(d, "Sch SE", "13") == 0.0
    assert "under $400" in _source(d, "Sch SE", "12")
    assert _need(d, "Sch 2", "4") == 0.0


def _part_total(d: draft.Draft) -> float:
    return sum(
        _need(d, "Sch 1-A", ln)
        for ln in ("13", "21", "30", "37")
        if d.get("Sch 1-A", ln) is not None
    )


def test_schedule_1a_without_a_qualifying_part_is_zero(lay: Layout) -> None:
    d = draft.build(lay, YEAR)
    assert _checks(d) == [], d.notes
    assert _need(d, "Sch 1-A", "1") == _need(d, "1040", "11b")
    assert _need(d, "Sch 1-A", "3") == _need(d, "1040", "11b")
    assert _need(d, "Sch 1-A", "38") == 0.0 == _need(d, "1040", "13b")
    assert d.get("Sch 1-A", "37") is None  # 54 at year end: Part V does not apply
    assert any("Schedule 1-A Parts II to IV" in n for n in d.notes)
    assert {"qualified_tips", "qualified_overtime", "car_loan_interest"} <= set(
        d.unknown
    )
    assert "tipped_occupation_code" not in d.unknown  # moot until there are tips


def test_schedule_1a_parts_answered_zero_leave_no_note(lay: Layout) -> None:
    _enter_amounts(lay, qualified_tips=0, qualified_overtime=0, car_loan_interest=0)
    d = draft.build(lay, YEAR)
    assert _checks(d) == [], d.notes
    assert not any("Schedule 1-A Parts II to IV" in n for n in d.notes)
    assert not {"qualified_tips", "qualified_overtime", "car_loan_interest"} & set(
        d.unknown
    )


@pytest.fixture
def senior_lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    _answers(
        lay,
        birth_date="1955-03-01",
        filing_status="single",
        state="NC",
        wages="100,000",
        se_income="0",
        ordinary_dividends="0",
        qualified_dividends="0",
    )
    return lay


def test_schedule_1a_senior_deduction_phases_out(senior_lay: Layout) -> None:
    d = draft.build(senior_lay, YEAR)
    assert _checks(d) == [], d.notes
    agi = _need(d, "1040", "11b")
    assert agi == 100_000.0
    # 2025 Schedule 1-A lines 31-37: $6,000 less 6% of the AGI over $75,000
    assert _need(d, "Sch 1-A", "31") == agi
    assert _need(d, "Sch 1-A", "32") == 75_000.0
    assert _need(d, "Sch 1-A", "33") == 25_000.0
    assert _need(d, "Sch 1-A", "34") == 1_500.0
    assert _need(d, "Sch 1-A", "35") == 4_500.0
    assert _need(d, "Sch 1-A", "36a") == 4_500.0
    assert d.get("Sch 1-A", "36b") is None
    assert _need(d, "Sch 1-A", "37") == 4_500.0
    assert _need(d, "Sch 1-A", "38") == 4_500.0 == _need(d, "1040", "13b")
    assert _source(d, "1040", "13b") == "Sch 1-A line 38"


def test_schedule_1a_senior_deduction_in_full_below_the_threshold(
    senior_lay: Layout,
) -> None:
    _answers(senior_lay, wages="60,000")
    d = draft.build(senior_lay, YEAR)
    assert _checks(d) == [], d.notes
    assert (
        d.get("Sch 1-A", "34") is None
    )  # line 33 is zero or less: 6,000 straight to 35
    assert _need(d, "Sch 1-A", "35") == 6_000.0
    assert _need(d, "1040", "13b") == 6_000.0


def test_schedule_1a_senior_deduction_gone_at_the_top(senior_lay: Layout) -> None:
    _answers(senior_lay, wages="200,000")
    d = draft.build(senior_lay, YEAR)
    assert _checks(d) == [], d.notes
    assert _need(d, "Sch 1-A", "35") == 0.0
    assert _need(d, "1040", "13b") == 0.0


def _enter_amounts(lay: Layout, **fields: int) -> None:
    """Answer the Needed panel's Schedule 1-A items (qualified tips and their
    Treasury occupation code, overtime, car loan interest) through ``enter``."""
    _answers(lay, **{key: f"{value:,}" for key, value in fields.items()})


def test_schedule_1a_parts_sum_to_1040_line_13b(senior_lay: Layout) -> None:
    _answers(senior_lay, wages="60,000")
    _enter_amounts(
        senior_lay,
        qualified_tips=5_000,
        tipped_occupation_code=101,
        qualified_overtime=4_000,
        car_loan_interest=3_000,
    )
    d = draft.build(senior_lay, YEAR)
    assert _checks(d) == [], d.notes
    assert _need(d, "Sch 1-A", "13") == 5_000.0
    assert _need(d, "Sch 1-A", "21") == 4_000.0
    assert _need(d, "Sch 1-A", "30") == 3_000.0
    assert _need(d, "Sch 1-A", "37") == 6_000.0
    assert _need(d, "Sch 1-A", "38") == 18_000.0 == _part_total(d)
    assert _need(d, "1040", "13b") == 18_000.0
    assert _need(d, "1040", "14") == pytest.approx(
        _need(d, "1040", "12e") + _need(d, "1040", "13a") + 18_000.0, abs=0.01
    )


def test_schedule_1a_tips_cap_and_phase_out_by_the_thousand(senior_lay: Layout) -> None:
    """Line 7 caps tips at $25,000; line 11 drops the MAGI excess to whole
    $1,000s, so $5,500 over the $150,000 threshold costs $500, not $550. The
    engine phases out smoothly: the draft follows the form and says the gap."""
    _answers(senior_lay, wages="155,500", birth_date="1971-06-15")
    _enter_amounts(senior_lay, qualified_tips=30_000, tipped_occupation_code=101)
    d = draft.build(senior_lay, YEAR)
    assert _checks(d) == [], d.notes
    assert _need(d, "Sch 1-A", "7") == 25_000.0
    assert _need(d, "Sch 1-A", "8") == 155_500.0
    assert _need(d, "Sch 1-A", "9") == 150_000.0
    assert _need(d, "Sch 1-A", "10") == 5_500.0
    assert _need(d, "Sch 1-A", "11") == 5.0
    assert _need(d, "Sch 1-A", "12") == 500.0
    assert _need(d, "Sch 1-A", "13") == 24_500.0 == _need(d, "1040", "13b")
    assert any("rounds the phase-out down" in n for n in d.notes), d.notes


def test_schedule_1a_car_loan_interest_rounds_the_excess_up(senior_lay: Layout) -> None:
    _answers(senior_lay, wages="105,500", birth_date="1971-06-15")
    _enter_amounts(senior_lay, car_loan_interest=12_000)
    d = draft.build(senior_lay, YEAR)
    assert _checks(d) == [], d.notes
    assert _need(d, "Sch 1-A", "24") == 10_000.0
    assert _need(d, "Sch 1-A", "27") == 5_500.0
    assert _need(d, "Sch 1-A", "28") == 6.0
    assert _need(d, "Sch 1-A", "29") == 1_200.0
    assert _need(d, "Sch 1-A", "30") == 8_800.0 == _need(d, "1040", "13b")


def test_schedule_1a_tips_without_an_occupation_code_are_not_drafted(
    senior_lay: Layout,
) -> None:
    _answers(senior_lay, wages="60,000", birth_date="1971-06-15")
    _enter_amounts(senior_lay, qualified_tips=5_000)
    d = draft.build(senior_lay, YEAR)
    assert _checks(d) == [], d.notes
    assert d.get("Sch 1-A", "13") is None
    assert _need(d, "1040", "13b") == 0.0
    assert any("tipped occupation code" in n for n in d.notes)


@pytest.mark.parametrize(
    ("fields", "line"),
    [
        ({"qualified_tips": 10_000, "tipped_occupation_code": 101}, "13"),
        ({"qualified_overtime": 4_000}, "21"),
    ],
)
def test_married_separate_tips_and_overtime_are_not_deducted_and_line_16_follows(
    senior_lay: Layout,
    fields: dict[str, int],
    line: str,
) -> None:
    """The statute gives no tips or overtime deduction to married filing
    separately; the engine does not apply that rule. Line 16 is the tax on the
    form's own line 15 ($80,000 less the $15,750 standard deduction), not on
    the engine's taxable income that still holds the deduction, priced by the
    Tax Table (row 64,250-64,300: 9,055 against the schedule's 9,049)."""
    _answers(
        senior_lay,
        wages="80,000",
        birth_date="1971-06-15",
        filing_status="married_separate",
    )
    _enter_amounts(senior_lay, **fields)
    d = draft.build(senior_lay, YEAR)
    assert _checks(d) == [], d.notes
    assert d.get("Sch 1-A", line) is None
    assert _need(d, "1040", "13b") == 0.0
    assert _need(d, "1040", "15") == 64_250.0
    assert _need(d, "1040", "16") == 9_055.0
    assert any("married filing separately" in n for n in d.notes), d.notes


def test_married_separate_keeps_car_loan_interest(senior_lay: Layout) -> None:
    _answers(
        senior_lay,
        wages="80,000",
        birth_date="1971-06-15",
        filing_status="married_separate",
    )
    _enter_amounts(senior_lay, qualified_tips=10_000, car_loan_interest=3_000)
    d = draft.build(senior_lay, YEAR)
    assert _checks(d) == [], d.notes
    assert _need(d, "Sch 1-A", "30") == 3_000.0 == _need(d, "1040", "13b")
    assert _need(d, "1040", "15") == 61_250.0


def test_the_forms_rounded_phase_out_re_prices_line_16(senior_lay: Layout) -> None:
    """The form leaves $50 more tips deduction than the engine's smooth
    phase-out: line 15 is $50 lower, so line 16 is the tax on that lower
    income ($20,507 on $115,250), not the engine's $20,519 on $115,300."""
    _answers(senior_lay, wages="155,500", birth_date="1971-06-15")
    _enter_amounts(senior_lay, qualified_tips=30_000, tipped_occupation_code=101)
    d = draft.build(senior_lay, YEAR)
    assert _checks(d) == [], d.notes
    assert _need(d, "1040", "15") == 115_250.0
    assert _need(d, "1040", "16") == 20_507.0
    assert any("rounds the phase-out down" in n and "line 16" in n for n in d.notes), (
        d.notes
    )


def test_schedule_1a_items_are_asked_with_a_document_source() -> None:
    for key in (
        "qualified_tips",
        "tipped_occupation_code",
        "qualified_overtime",
        "car_loan_interest",
    ):
        need = need_for(key)
        assert need.source and need.source != "no document supplies this; type it"
    assert parse_value(need_for("tipped_occupation_code"), "101") == 101
    with pytest.raises(ValueError):
        parse_value(need_for("tipped_occupation_code"), "1,000")
    with pytest.raises(ValueError):
        parse_value(need_for("qualified_tips"), "-5")


def test_tips_answered_through_the_panel_reach_the_household_and_the_form(
    senior_lay: Layout,
) -> None:
    _answers(senior_lay, wages="60,000", birth_date="1971-06-15")
    _enter_amounts(
        senior_lay,
        qualified_tips=5_000,
        tipped_occupation_code=101,
        qualified_overtime=4_000,
        car_loan_interest=3_000,
    )
    hh = inputs.build(senior_lay, YEAR).household
    assert (hh.qualified_tips, hh.tipped_occupation_code) == (5_000, 101)
    assert (hh.qualified_overtime, hh.car_loan_interest) == (4_000, 3_000)
    d = draft.build(senior_lay, YEAR)
    assert _checks(d) == [], d.notes
    assert _need(d, "1040", "13b") == 12_000.0


def test_tips_without_a_code_name_the_code_as_unknown(senior_lay: Layout) -> None:
    _answers(senior_lay, wages="60,000", birth_date="1971-06-15")
    _enter_amounts(senior_lay, qualified_tips=5_000)
    assert "tipped_occupation_code" in inputs.build(senior_lay, YEAR).unknown


@pytest.mark.parametrize(
    ("born", "drafted"),
    [("1961-01-01", True), ("1961-01-02", False), ("1960-12-31", True)],
)
def test_senior_deduction_counts_a_january_first_birthday(
    senior_lay: Layout, born: str, drafted: bool
) -> None:
    """Pub. 501: a filer is 65 on the day before the 65th birthday, so someone
    born January 1, 1961 is 65 for 2025 (Schedule 1-A Part V and the extra
    standard deduction); one born January 2 is not."""
    _answers(senior_lay, wages="60,000", birth_date=born)
    d = draft.build(senior_lay, YEAR)
    assert _checks(d) == [], d.notes
    assert (d.get("Sch 1-A", "37") is not None) is drafted
    assert _need(d, "1040", "13b") == (6_000.0 if drafted else 0.0)


def test_january_first_birthday_gets_the_extra_standard_deduction(
    senior_lay: Layout,
) -> None:
    _answers(senior_lay, wages="60,000", birth_date="1961-01-02")
    before = _need(draft.build(senior_lay, YEAR), "1040", "12e")
    _answers(senior_lay, birth_date="1961-01-01")
    after = _need(draft.build(senior_lay, YEAR), "1040", "12e")
    assert after - before == 2_000.0  # 2025 additional amount, unmarried 65+


def _w2_boxes(lay: Layout, **boxes: float) -> None:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-w2",
        file_name="w2.pdf",
        kind="pdf",
        pages=1,
        batch="b2",
        facts=[
            db.Fact("W-2", YEAR, "Example Employer (synthetic)", box, box, value, 1)
            for box, value in boxes.items()
        ],
    )
    conn.close()


def test_schedule_se_line_8a_reads_w2_boxes_3_and_7(lay: Layout) -> None:
    """Pre-tax 401(k) deferrals sit in box 3 but not box 1: with box 3 at
    $170,000 (box 1 $150,000) and box 7 tips of $2,000, line 8a is $172,000
    and only $4,100 of the wage base is left for the 12.4%."""
    _answers(lay, wages="150,000")
    _w2_boxes(lay, **{"1": 150_000.0, "3": 170_000.0, "7": 2_000.0})
    d = draft.build(lay, YEAR)
    assert _checks(d) == [], d.notes
    assert _need(d, "Sch SE", "8a") == 172_000.0
    assert "W-2 boxes 3 and 7" in _source(d, "Sch SE", "8a")
    assert _need(d, "Sch SE", "9") == 4_100.0
    net = 40_000 * 0.9235
    assert _need(d, "Sch SE", "10") == pytest.approx(4_100 * 0.124, abs=0.01)
    assert _need(d, "Sch SE", "12") == pytest.approx(
        4_100 * 0.124 + net * 0.029, abs=0.01
    )


def test_schedule_se_line_8a_without_w2_boxes_says_box_1_stands_in(
    lay: Layout,
) -> None:
    _answers(lay, wages="150,000")
    d = draft.build(lay, YEAR)
    assert _need(d, "Sch SE", "8a") == 150_000.0
    assert "box 1 stands in" in _source(d, "Sch SE", "8a")


def _joint(lay: Layout, year: int, birth_date: str) -> draft.Draft:
    for key, text in dict(
        birth_date=birth_date,
        filing_status="married_joint",
        state="NC",
        wages="60,000",
        se_income="0",
        ordinary_dividends="0",
        qualified_dividends="0",
    ).items():
        enter(lay, year, key, text)
    return draft.build(lay, year)


@pytest.mark.parametrize(("year", "cutoff"), [(2025, 1961), (2026, 1962), (2028, 1964)])
def test_line_36b_note_names_the_years_own_birth_date_cutoff(
    planner_home: Path, year: int, cutoff: int
) -> None:
    lay = Layout(planner_home)
    lay.ensure()
    d = _joint(lay, year, "1950-06-01")
    (note,) = [n for n in d.notes if "line 36b" in n]
    assert f"born before January 2, {cutoff}" in note


def test_line_36b_note_reaches_a_joint_return_whose_filer_is_under_65(
    planner_home: Path,
) -> None:
    lay = Layout(planner_home)
    lay.ensure()
    d = _joint(lay, 2026, "1980-06-01")  # 46: no line 36a, the spouse may be 65
    assert d.get("Sch 1-A", "36a") is None
    (note,) = [n for n in d.notes if "line 36b" in n]
    assert "born before January 2, 1962" in note


def test_a_joint_draft_says_the_spouse_is_not_handled(planner_home: Path) -> None:
    lay = Layout(planner_home)
    lay.ensure()
    d = _joint(lay, 2026, "1980-06-01")
    assert [n for n in d.notes if n.startswith("Not handled:")] == inputs.build(
        lay, 2026
    ).scope
    assert "Not handled:" in draft.render(d)


def _sched_b_lay(planner_home: Path, banks: tuple[float, float], div: float) -> Layout:
    """Two synthetic banks' interest (box 1, one with box 3 Treasury interest)
    and one fund's ordinary dividends; nothing typed for either."""
    lay = Layout(planner_home)
    lay.ensure()
    _answers(lay, birth_date="1971-06-15", filing_status="single", state="NC")
    _answers(lay, wages="50,000", se_income="0")
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-sched-b",
        file_name="synthetic-sched-b.pdf",
        kind="pdf",
        pages=1,
        batch="b1",
        facts=[
            db.Fact("1099-INT", YEAR, "First Bank (synthetic)", "1", "x", banks[0], 1),
            db.Fact("1099-INT", YEAR, "Second Bank (synthetic)", "1", "x", banks[1], 1),
            db.Fact("1099-INT", YEAR, "Second Bank (synthetic)", "3", "x", 200.0, 1),
            db.Fact("1099-DIV", YEAR, "Example Fund (synthetic)", "1a", "x", div, 1),
            db.Fact(
                "1099-DIV", YEAR, "Example Fund (synthetic)", "1b", "x", div / 2, 1
            ),
        ],
    )
    conn.close()
    return lay


def _rows_of(d: draft.Draft, form: str, line: str) -> list[draft.Line]:
    return [ln for ln in d.lines if (ln.form, ln.line) == (form, line)]


def test_schedule_b_required_over_1500(planner_home: Path) -> None:
    lay = _sched_b_lay(planner_home, (900.0, 700.0), 2_000.0)
    d = draft.build(lay, YEAR)
    assert _checks(d) == [], d.notes
    payers = _rows_of(d, "Sch B", "1")
    assert [(ln.label, ln.value) for ln in payers] == [
        ("First Bank (synthetic)", 900.0),
        ("Second Bank (synthetic)", 900.0),  # box 1 plus box 3
    ]
    assert "1099-INT box 1" in payers[0].source and "box 3" in payers[1].source
    assert _need(d, "Sch B", "2") == 1_800.0
    assert _need(d, "Sch B", "3") == 0.0
    assert _need(d, "Sch B", "4") == 1_800.0 == _need(d, "1040", "2b")
    (fund,) = _rows_of(d, "Sch B", "5")
    assert fund.label == "Example Fund (synthetic)" and "1099-DIV box 1a" in fund.source
    assert _need(d, "Sch B", "6") == 2_000.0 == _need(d, "1040", "3b")
    assert "Sch B" in draft.ORDER and "Schedule B" in draft.render(d)
    # Part III is asked, never assumed
    assert d.get("Sch B", "7a") is None
    assert any("Schedule B Part III" in n and "foreign_accounts" in n for n in d.notes)
    missing = {s.need.key for s in needed(lay, YEAR).by_state("missing")}
    assert "foreign_accounts" in missing
    enter(lay, YEAR, "foreign_accounts", "no")
    d = draft.build(lay, YEAR)
    (l7a,) = _rows_of(d, "Sch B", "7a")
    assert l7a.label.endswith("No") and "foreign_accounts" in l7a.source
    assert not any("Schedule B Part III" in n for n in d.notes)


def test_schedule_b_left_out_at_1500_or_less(planner_home: Path) -> None:
    lay = _sched_b_lay(planner_home, (800.0, 500.0), 1_500.0)  # 1,500 each: not over
    d = draft.build(lay, YEAR)
    assert _need(d, "1040", "2b") == 1_500.0 and _need(d, "1040", "3b") == 1_500.0
    assert not [ln for ln in d.lines if ln.form == "Sch B"]
    assert any("Schedule B is not required" in n for n in d.notes), d.notes
    keys = {s.need.key for s in needed(lay, YEAR).items}
    assert "foreign_accounts" not in keys  # asked only when Schedule B is required


def test_source_names_file_and_page(planner_home: Path) -> None:
    """A draft line's source names the document file and page as well as the
    form, box and issuer, so a preparer can open the very page."""
    d = draft.build(nc_lay(planner_home, 2000.0), YEAR)
    src = {(ln.form, ln.line): ln.source for ln in d.lines}
    assert "W-2 box 2 (Employer (synthetic); nc.pdf p.1)" in src[("1040", "25a")]
    assert "nc.pdf p.1" in src[("1040", "2b")]


@pytest.mark.engine
def test_line_16_tables_only_the_ordinary_part() -> None:
    """With $10,000 of preferential income on $50,000 taxable, the Tax Table
    prices the $40,000 ordinary part (row 40,000-40,050: 4,565 against the
    schedule's 4,561.50) and the worksheet total stays under the tax on all
    $50,000."""
    line, gap = tax.line_16(4000.0, 50000.0, 10000.0, YEAR, "SINGLE")
    assert line == pytest.approx(4003.5) and gap == 3.5
    line, gap = tax.line_16(9000.0, 50000.0, 10000.0, YEAR, "SINGLE")
    assert line == 5920.0  # capped at the table tax on all 50,000
