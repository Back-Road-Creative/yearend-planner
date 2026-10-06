"""Unit 3e-1: Form 1099-G (unemployment compensation, a state or local tax
refund) and Form 1099-C (canceled debt) into Schedule 1 lines 1, 7 and 8c.
Box labels follow the 1099-G (Rev. March 2024) and 1099-C (Rev. April 2025);
the refund worksheet follows the 2025 Form 1040 instructions, Schedule 1 line
1. Synthetic figures only."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from planner.ingest.needs import _needed, enter, need_value
from planner.ingest.pdf import Unmatched, parse_texts
from planner.ledger import db
from planner.paths import Layout
from planner.taxprep import draft, expected, refund
from tests.test_forms import TEMPLATES

KEY = "state_refund_taxable"

G_PAGE = "\n".join(
    [
        "Form 1099-G Certain Government Payments",
        "For calendar year 2025",
        "PAYER'S name: Example State Workforce Agency (synthetic)",
        "1 Unemployment compensation $ 8,000.00",
        "2 State or local income tax refunds, credits, or offsets $ 500.00",
        "4 Federal income tax withheld $ 800.00",
        "11 State income tax withheld $ 300.00",
    ]
)
C_PAGE = "\n".join(
    [
        "Form 1099-C Cancellation of Debt",
        "For calendar year 2025",
        "CREDITOR'S name: Example Card Bank (synthetic)",
        "2 Amount of debt discharged $ 1,200.00",
        "3 Interest, if included in box 2 $ 150.00",
    ]
)


def test_1099g_boxes_read() -> None:
    (f,) = parse_texts([G_PAGE], TEMPLATES)
    assert (f.form, f.tax_year) == ("1099-G", 2025)
    assert "Workforce Agency" in f.issuer
    assert {b: v for b, (_, v, *_) in f.boxes.items()} == {
        "1": 8000.0,
        "2": 500.0,
        "4": 800.0,
        "11": 300.0,
    }


def test_1099c_boxes_and_creditor_read() -> None:
    (f,) = parse_texts([C_PAGE], TEMPLATES)
    assert (f.form, f.tax_year) == ("1099-C", 2025)
    assert "Card Bank" in f.issuer
    assert f.boxes["2"][1] == 1200.0 and f.boxes["3"][1] == 150.0


def test_1099c_without_the_amount_discharged_is_unmatched() -> None:
    page = C_PAGE.replace("2 Amount of debt discharged $ 1,200.00\n", "")
    with pytest.raises(Unmatched, match="required boxes not found"):
        parse_texts([page], TEMPLATES)


@pytest.mark.parametrize(
    ("birth", "is_aged"),
    [("1960-01-01", True), ("1960-01-02", False), ("1950-07-04", True)],
)
def test_worksheet_age_box(birth: str, is_aged: bool) -> None:
    # line 6: "born before January 2, 1960" for the 2024 return
    assert refund.aged(birth, 2024) is is_aged


def test_worksheet_standard_deduction_last_year_none_taxable() -> None:
    value, why = refund.taxable_part(500.0, 14600.0, "SINGLE", 2024, 0)
    assert value == 0.0 and "none of the refund is taxable" in why
    # 65 or older: line 7 is 14,600 + 1,950
    assert refund.taxable_part(500.0, 16550.0, "SINGLE", 2024, 1)[0] == 0.0
    assert refund.taxable_part(500.0, 32300.0, "JOINT", 2024, 2)[0] == 0.0


def test_worksheet_itemized_last_year_asks_with_the_cap() -> None:
    value, why = refund.taxable_part(500.0, 14900.0, "SINGLE", 2024, 0)
    assert value is None and "up to 300 is taxable" in why
    value, why = refund.taxable_part(500.0, 40000.0, "JOINT", 2024, 0)
    assert value is None and "up to 500 is taxable" in why and "5d and 5e" in why


def test_worksheet_separate_returns_ask() -> None:
    value, why = refund.taxable_part(500.0, 1000.0, "SEPARATE", 2024, 0)
    assert value is None and "lines 5-7 are skipped" in why


def _doc(lay: Layout, name: str, facts: list[db.Fact]) -> None:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint=f"synthetic-3e1-{name}",
        file_name=f"{name}.pdf",
        kind="pdf",
        pages=1,
        batch="b1",
        facts=facts,
        owner="you",
    )
    conn.close()


def _lay(planner_home: Path, state: str = "FL") -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    _doc(
        lay,
        "w2",
        [
            db.Fact("W-2", 2025, "Employer (synthetic)", b, "", v, 1)
            for b, v in {"1": 40000.0, "2": 3000.0, "16": 40000.0}.items()
        ],
    )
    for key, text in (
        ("birth_date", "1980-05-01"),
        ("filing_status", "single"),
        ("dependents", "none"),
        ("state", state),
    ):
        enter(lay, 2025, key, text)
    return lay


def _g(lay: Layout, boxes: dict[str, float], year: int = 2025) -> None:
    _doc(
        lay,
        f"g{year}",
        [
            db.Fact("1099-G", year, "State agency (synthetic)", b, "", v, 1)
            for b, v in boxes.items()
        ],
    )


def _prior_1040(lay: Layout, line12: float) -> None:
    _doc(lay, "f1040-2024", [db.Fact("1040", 2024, "self", "12", "", line12, 1)])


def test_needs_read_the_1099g_and_1099c(planner_home: Path) -> None:
    lay = _lay(planner_home, "VA")
    _g(lay, {"1": 8000.0, "4": 800.0, "11": 300.0})
    _doc(
        lay, "c", [db.Fact("1099-C", 2025, "Card Bank (synthetic)", "2", "", 1200.0, 1)]
    )
    conn = db.connect(lay.data / "ledger" / "planner.db")
    got = {
        k: need_value(conn, lay, 2025, k)
        for k in ("unemployment", "cancelled_debt", "fed_withheld", "state_withheld")
    }
    conn.close()
    assert got == {
        "unemployment": 8000.0,
        "cancelled_debt": 1200.0,
        "fed_withheld": 3800.0,
        "state_withheld": 300.0,
    }


def test_refund_after_a_standard_deduction_year_is_not_taxable(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home)
    _g(lay, {"2": 500.0})
    _prior_1040(lay, 14600.0)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    assert need_value(conn, lay, 2025, "state_refund") == 500.0
    assert need_value(conn, lay, 2025, "state_refund_taxable") == 0.0
    conn.close()
    d = draft.build(lay, 2025)
    assert d.get("Sch 1", "1") is None


def test_refund_after_itemizing_is_asked_then_typed(planner_home: Path) -> None:
    lay = _lay(planner_home)
    _g(lay, {"2": 500.0})
    _prior_1040(lay, 20000.0)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    (st,) = [s for s in _needed(conn, lay, 2025).items if s.need.key == KEY]
    conn.close()
    assert st.state == "missing" and "up to 500 is taxable" in st.origin
    enter(lay, 2025, "state_refund_taxable", "400")
    d = draft.build(lay, 2025)
    assert d.get("Sch 1", "1") == 400.0 and d.get("Sch 1", "10") == 400.0
    assert d.get("1040", "8") == 400.0
    assert not [n for n in d.notes if n.startswith("CHECK")]


def test_no_refund_means_the_taxable_part_is_not_asked(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "state_refund", "0")
    conn = db.connect(lay.data / "ledger" / "planner.db")
    keys = {s.need.key for s in _needed(conn, lay, 2025).items}
    conn.close()
    assert "state_refund" in keys and KEY not in keys


def test_draft_schedule_1_lines_7_and_8c(planner_home: Path) -> None:
    lay = _lay(planner_home)
    _g(lay, {"1": 8000.0, "4": 800.0})
    _doc(
        lay, "c", [db.Fact("1099-C", 2025, "Card Bank (synthetic)", "2", "", 1200.0, 1)]
    )
    d = draft.build(lay, 2025)
    s1 = {n: d.get("Sch 1", n) for n in ("1", "7", "8c", "9", "10")}
    assert s1 == {"1": None, "7": 8000.0, "8c": 1200.0, "9": 1200.0, "10": 9200.0}
    assert d.get("1040", "8") == 9200.0
    assert d.get("1040", "25b") == 800.0
    assert not [n for n in d.notes if n.startswith("CHECK")]


def test_expected_forms_from_the_answers(planner_home: Path) -> None:
    lay = _lay(planner_home)
    for key in ("unemployment", "cancelled_debt"):
        enter(lay, 2025, key, "1000")
    by = {
        (e.form, e.issuer): e
        for e in expected.inventory(lay, 2025, date(2026, 3, 1)).items
    }
    assert by[("1099-G", "the state agency")].reason == "unemployment compensation"
    assert by[("1099-C", "the creditor")].reason == "canceled debt"


def test_last_years_1099c_is_not_expected_again(planner_home: Path) -> None:
    lay = _lay(planner_home)
    _doc(
        lay,
        "c24",
        [db.Fact("1099-C", 2024, "Card Bank (synthetic)", "2", "", 900.0, 1)],
    )
    _g(lay, {"2": 200.0}, 2024)
    forms = {e.form for e in expected.inventory(lay, 2025, date(2026, 3, 1)).items}
    assert "1099-G" in forms and "1099-C" not in forms
