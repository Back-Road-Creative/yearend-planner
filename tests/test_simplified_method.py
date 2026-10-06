"""Unit 3f-2: a pension or annuity whose 1099-R leaves the taxable amount
blank (box 2a empty, or box 2b "Taxable amount not determined" marked) is
figured on the Simplified Method Worksheet (2025 Form 1040 instructions,
lines 5a and 5b, Tables 1 and 2). Synthetic figures only."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.ingest.needs import enter, need_value, needed
from planner.ingest.pdf import parse_texts
from planner.ledger import db
from planner.plan import inputs
from planner.taxprep import annuity, draft
from tests.test_forms import F1099R, TEMPLATES
from tests.test_pensions_qcd import _lay, _lines, _r

PLAN = "Pension Plan (synthetic)"
ENTRY = "annuity cost 31000 start 2019-07-01 recovered 9000"


def _left(box1: float, box9b: float | None = None) -> list[db.Fact]:
    """A plan 1099-R with 2a blank and 2b (not determined) marked."""
    out = [
        db.Fact("1099-R", 2025, PLAN, "1", "", box1, 1),
        db.Fact("1099-R", 2025, PLAN, "2b", "", 0.0, 1, text="yes"),
        db.Fact("1099-R", 2025, PLAN, "7", "", 0.0, 1, text="7"),
        db.Fact("1099-R", 2025, PLAN, "7b", "", 0.0, 1, text="no"),
    ]
    if box9b is not None:
        out.append(db.Fact("1099-R", 2025, PLAN, "9b", "", box9b, 1))
    return out


def test_boxes_2b_5_and_9b_read() -> None:
    page = [
        F1099R[0],
        "Tax year 2025",
        f"PAYER'S name: {PLAN}",
        "1 Gross distribution $ 24,000.00",
        "2a Taxable amount",
        "2b Taxable amount not determined X Total distribution",
        "5 Employee contrib./desig. Roth contrib. or insurance premiums $ 1,430.76",
        "7a Dist. code(s) 7 7b IRA/SEP/SIMPLE",
        "9b Total employee contrib. $ 22,000.00",
        "10 Amount allocable to IRR 0",
    ]
    (f,) = parse_texts(["\n".join(page)], TEMPLATES)
    got = {k: v[1] for k, v in f.boxes.items()}
    assert got == {
        "1": 24000.0,
        "2b": "yes",
        "5": 1430.76,
        "7": "7",
        "7b": "no",
        "9b": 22000.0,
    }
    unmarked = [
        *page[:4],
        "2a Taxable amount $ 24,000.00",
        "2b Taxable amount not determined",
    ]
    (f,) = parse_texts(["\n".join(unmarked)], TEMPLATES)
    assert f.boxes["2a"][1] == 24000.0 and f.boxes["2b"][1] == "no"


def test_parse_entries() -> None:
    (e,) = annuity.parse("annuities", ENTRY)
    assert e == {"cost": 31000.0, "start": "2019-07-01", "recovered": 9000.0}
    (e,) = annuity.parse(
        "annuities", "annuity spouse cost 5000 start 2025-04-01 beneficiary 60"
    )
    assert e["spouse"] is True and e["beneficiary"] == 60
    assert annuity.parse("annuities", "none") == []
    for bad, said in (
        ("annuity cost 5000", "needs start"),
        ("annuity cost 5000 start 2019-07-01 months 13", "months is 1 to 12"),
        ("annuity cost 5000 start 2019-07-01 bonus 3", "not an annuity word"),
        ("pension cost 5000 start 2019-07-01", "starts with annuity"),
        ("annuity cost 5000 start July", "is a date"),
    ):
        with pytest.raises(ValueError, match=said):
            annuity.parse("annuities", bad)


@pytest.mark.parametrize(
    ("start", "age", "beneficiary", "n"),
    [
        ("2019-07-01", 64, None, 260),  # Table 1, after November 18, 1996
        ("1996-11-18", 64, None, 240),  # Table 1, before November 19, 1996
        ("2010-01-01", 55, None, 360),
        ("2010-01-01", 56, None, 310),
        ("2010-01-01", 71, None, 160),
        ("1990-01-01", 71, None, 120),
        ("2019-07-01", 64, 62, 310),  # Table 2: combined 126
        ("2019-07-01", 50, 60, 410),  # combined 110
        ("2019-07-01", 75, 70, 210),  # combined 145
        ("1997-06-01", 64, 62, 260),  # Table 2 starts after 1997
    ],
)
def test_table_number(start: str, age: int, beneficiary: int | None, n: int) -> None:
    from datetime import date

    assert annuity.table_number(date.fromisoformat(start), age, beneficiary)[0] == n


def test_worksheet_lines() -> None:
    (e,) = annuity.parse("annuities", ENTRY)
    w = annuity.worksheet(e, 24000.0, 64, 2025)
    assert w.lines == {
        "1": 24000.0,
        "2": 31000.0,
        "3": 260.0,
        "4": 119.23,
        "5": 1430.76,
        "6": 9000.0,
        "7": 22000.0,
        "8": 1430.76,
        "9": 22569.24,
        "10": 10430.76,
        "11": 20569.24,
    }


def test_worksheet_first_year_last_of_the_cost_and_before_1987() -> None:
    (e,) = annuity.parse("annuities", "annuity cost 26000 start 2025-04-01")
    assert annuity.worksheet(e, 9000.0, 64, 2025).lines["5"] == 900.0  # 9 months
    (e,) = annuity.parse(
        "annuities", "annuity cost 10000 start 2010-01-01 recovered 9800"
    )
    w = annuity.worksheet(e, 12000.0, 60, 2025)
    assert w.lines["8"] == 200.0 and w.lines["9"] == 11800.0 and w.lines["11"] == 0.0
    (e,) = annuity.parse(
        "annuities", "annuity cost 24000 start 1986-12-01 recovered 20000"
    )
    w = annuity.worksheet(e, 12000.0, 62, 2025)
    assert w.lines["8"] == w.lines["5"] == 1200.0  # no cap at the cost
    assert not {"6", "7", "10", "11"} & set(w.lines)
    (e,) = annuity.parse("annuities", "annuity cost 24000 start 1986-07-01")
    with pytest.raises(ValueError, match="General Rule"):
        annuity.worksheet(e, 12000.0, 62, 2025)


def test_blank_taxable_amount_figured_on_the_worksheet(planner_home: Path) -> None:
    lay = _lay(planner_home, [_left(24000.0, 22000.0)], born="1955-03-10")
    conn = db.connect(lay.data / "ledger" / "planner.db")
    assert need_value(conn, lay, 2025, "pension_income") == 0.0
    conn.close()
    (st,) = [s for s in needed(lay, 2025).items if s.need.key == "annuities"]
    assert st.state == "missing" and PLAN in st.origin and "9b 22,000.00" in st.origin
    enter(lay, 2025, "annuities", ENTRY)
    inp = inputs.build(lay, 2025)
    assert inp.household.pension_income == 22569
    assert not [n for n in inp.notes if "box 9b" in n]  # 9b agrees with line 7
    d = draft.build(lay, 2025)
    assert _lines(d, "5a", "5b") == {"5a": 24000.0, "5b": 22569.0}
    sheet = {x.line: x.value for x in d.lines if x.form == annuity.FORM}
    assert sheet["3"] == 260.0 and sheet["9"] == 22569.24 and sheet["11"] == 20569.24


def test_worksheet_adds_to_a_pension_with_a_taxable_amount(planner_home: Path) -> None:
    lay = _lay(
        planner_home,
        [_r("Other Plan (synthetic)", 10000.0, 10000.0, "no"), _left(24000.0)],
        born="1955-03-10",
    )
    enter(lay, 2025, "annuities", ENTRY)
    assert _lines(draft.build(lay, 2025), "5a", "5b") == {"5a": 34000.0, "5b": 32569.0}


def test_no_lines_without_an_annuity(planner_home: Path) -> None:
    lay = _lay(planner_home, [_r(PLAN, 10000.0, 10000.0, "no")])
    (st,) = [s for s in needed(lay, 2025).items if s.need.key == "annuities"]
    assert st.state == "actual" and st.value == []
    d = draft.build(lay, 2025)
    assert not [x for x in d.lines if x.form.startswith(annuity.FORM)]


def test_none_typed_with_a_blank_taxable_amount_is_noted(planner_home: Path) -> None:
    lay = _lay(planner_home, [_left(24000.0)], born="1955-03-10")
    enter(lay, 2025, "annuities", "none")
    inp = inputs.build(lay, 2025)
    assert inp.household.pension_income == 0
    assert [n for n in inp.notes if n.startswith("annuities is none")]


def test_two_annuities_need_gross(planner_home: Path) -> None:
    lay = _lay(planner_home, [_left(24000.0)], born="1955-03-10")
    enter(lay, 2025, "annuities", f"{ENTRY}; annuity cost 5000 start 2020-01-01")
    inp = inputs.build(lay, 2025)
    assert inp.annuities == []
    assert len([n for n in inp.notes if "needs gross" in n]) == 2
