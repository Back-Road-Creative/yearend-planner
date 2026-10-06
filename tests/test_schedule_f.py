"""Unit 3e-5: Schedule F, profit or loss from farming, cash and accrual, with
the conservation limit and a passive farm through Form 8582. Synthetic
figures only."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from planner.ingest.needs import _needed, enter
from planner.ledger import db
from planner.plan import inputs
from planner.taxprep import draft, expected, f8582, schf
from tests.test_income_1099g import _lay


def _farms(text: str) -> list[schf.Farm]:
    return schf.farms(schf.parse("farms", text))


def _lines(r: schf.Result, form: str = "Sch F") -> dict[str, float]:
    return {ln: round(v, 2) for f, ln, _, v, _ in r.lines if f == form}


def _run(text: str, *, magi: float = 50_000.0) -> schf.Result:
    fs = _farms(text)
    form = f8582.compute(schf.activities(fs), magi=magi, separate=None)
    return schf.schedule(fs, form.allowed)


def test_parse_entries() -> None:
    got = schf.parse(
        "farms",
        "farm raised 60,000 feed $12000; farm spouse accrual nonmaterial sales "
        "9000 begin 100 end 50 prior 400",
    )
    assert got == [
        {"raised": 60000, "feed": 12000},
        {
            "spouse": True,
            "accrual": True,
            "nonmaterial": True,
            "sales": 9000,
            "begin": 100,
            "end": 50,
            "prior": 400,
        },
    ]
    assert schf.parse("farms", "none") == []


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("ranch raised 10", "starts with farm"),
        ("farm raised", "pairs each word"),
        ("farm crops 10", "not a farm word"),
        ("farm raised 10 raised 20", "typed twice"),
        ("farm raised ten", "not a number"),
        ("farm raised -5", "out of range"),
        ("farm sales 10", "not on the cash method"),
        ("farm accrual raised 10", "not on the accrual method"),
        ("farm program 100 programtaxable 200", "programtaxable is more than"),
        ("farm raised 10 prior 50", "goes with nonmaterial"),
        ("farm accrual", "has no amounts"),
        ("; ".join(["farm raised 1"] * 7), "at most 6 farms"),
    ],
)
def test_parse_errors(text: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        schf.parse("farms", text)


def test_cash_method_lines() -> None:
    r = _run(
        "farm resale 9000 basis 6000 raised 50000 coop 1000 cooptaxable 800 "
        "program 2000 cropins 3000 custom 500 feed 12000 fertilizer 4000 "
        "fuel 3000 depreciation 8000 other 700"
    )
    got = _lines(r)
    assert (got["1a"], got["1b"], got["1c"]) == (9000, 6000, 3000)
    assert (got["3a"], got["3b"], got["4a"], got["4b"]) == (1000, 800, 2000, 2000)
    assert (got["6a"], got["6b"], got["7"]) == (3000, 3000, 500)
    assert got["9"] == 3000 + 50000 + 800 + 2000 + 3000 + 500
    assert got["33"] == 12000 + 4000 + 3000 + 8000 + 700
    assert got["34"] == got["9"] - got["33"]
    assert r.by_owner == {"you": got["34"], "spouse": 0.0}
    assert any("cash method; line E" in n and "Yes" in n for n in r.notes)


def test_ccc_loans_elected_leave_forfeited_untaxed() -> None:
    got = _lines(_run("farm cccelected 5000 cccforfeited 5000"))
    assert (got["5a"], got["5b"], got["5c"], got["9"]) == (5000, 5000, 0, 5000)


def test_accrual_method_part_iii() -> None:
    got = _lines(
        _run(
            "farm accrual sales 50000 program 1000 begin 10000 purchased 5000 "
            "end 8000 feed 20000"
        )
    )
    assert (got["37"], got["39a"], got["39b"], got["44"]) == (50000, 1000, 1000, 51000)
    assert (got["45"], got["46"], got["47"], got["48"]) == (10000, 5000, 15000, 8000)
    assert (got["49"], got["50"], got["9"]) == (7000, 44000, 44000)
    assert got["34"] == 24000
    assert "1a" not in got and "2" not in got


def test_accrual_ending_inventory_above_line_47() -> None:
    got = _lines(_run("farm accrual sales 1000 begin 100 end 400"))
    assert (got["47"], got["49"], got["50"]) == (100, 300, 1300)


def test_conservation_capped_at_a_quarter_of_gross() -> None:
    r = _run("farm raised 40000 conservation 9000 conservationcarry 3000")
    got = _lines(r)
    assert (got["12"], got["33"], got["34"]) == (10000, 10000, 30000)
    assert any("2,000.00 of conservation" in n for n in r.notes)


def test_passive_farm_loss_waits_on_form_8582() -> None:
    r = _run("farm nonmaterial raised 1000 feed 6000")
    assert _lines(r)["34"] == 0
    assert any("line E (material participation) is No" in n for n in r.notes)


def test_passive_farm_loss_against_passive_farm_income() -> None:
    r = _run(
        "farm nonmaterial raised 10000 feed 4000; "
        "farm nonmaterial raised 1000 feed 6000 prior 500"
    )
    assert _lines(r)["34"] == 6000
    b = _lines(r, "Sch F (B)")
    assert b["34"] == -5500
    assert r.by_owner["you"] == 500


def test_passive_farm_profit_with_its_prior_loss() -> None:
    assert _lines(_run("farm nonmaterial raised 3000 prior 1000"))["34"] == 2000


def test_spouse_farm_and_not_at_risk_note() -> None:
    r = _run("farm raised 1000; farm spouse notatrisk raised 1000 feed 3000")
    assert r.by_owner == {"you": 1000.0, "spouse": -2000.0}
    assert any("Form 6198" in n for n in r.notes)


def test_needs_ask_farms() -> None:
    from planner.ingest.needs import NEEDS

    need = next(n for n in NEEDS if n.key == "farms")
    assert need.kind == "farms" and "Schedule F" in need.label


def test_inputs_farm_income(planner_home: Path) -> None:
    lay = _lay(planner_home)  # 40,000 of wages
    enter(lay, 2025, "farms", "farm raised 30000 feed 10000")
    inp = inputs.build(lay, 2025)
    assert inp.household.farm_income == 20000
    assert inp.schedule_f is not None


def test_inputs_passive_farm_through_form_8582(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "farms", "farm nonmaterial raised 1000 feed 5000")
    inp = inputs.build(lay, 2025)
    assert inp.household.farm_income == 0
    assert inp.form_8582 is not None and inp.form_8582.allowed == {"farm A": 0.0}


def test_draft_farm_profit_reaches_schedule_se(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "farms", "farm raised 30000 feed 10000")
    d = draft.build(lay, 2025)
    assert (d.get("Sch F", "9"), d.get("Sch F", "34")) == (30000.0, 20000.0)
    assert (d.get("Sch 1", "6"), d.get("Sch 1", "10")) == (20000.0, 20000.0)
    assert (d.get("Sch SE", "1a"), d.get("Sch SE", "3")) == (20000.0, 20000.0)
    assert d.get("1040", "8") == 20000.0
    assert not [n for n in d.notes if n.startswith("CHECK")]


def test_draft_passive_farm_loss_on_form_8582(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "farms", "farm nonmaterial raised 1000 feed 5000")
    d = draft.build(lay, 2025)
    assert d.get("Sch F", "34") == 0.0
    assert d.get("Form 8582", "3") == -4000.0
    assert d.get("Sch 1", "6") is None
    assert not [n for n in d.notes if n.startswith("CHECK")]


def test_expected_1099patr_and_program_1099g(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "farms", "farm raised 1000 coop 300 program 200")
    inv = expected.inventory(lay, 2025, date(2026, 3, 1))
    forms = {(e.form, e.reason) for e in inv.items}
    assert ("1099-PATR", "cooperative distributions (Schedule F line 3a)") in forms
    assert any(
        f == "1099-G" and "agricultural program payments" in why for f, why in forms
    )


def test_needed_lists_farms(planner_home: Path) -> None:
    lay = _lay(planner_home)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    keys = {s.need.key for s in _needed(conn, lay, 2025).items}
    conn.close()
    assert "farms" in keys
