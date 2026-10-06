"""Unit 3e-2b: Schedule E Part I, rental real estate and royalties, with the
Pub. 527 vacation-home rules and the Form 8582 special allowance. Synthetic
figures only."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from planner.ingest.needs import _needed, enter
from planner.ledger import db
from planner.plan import inputs
from planner.taxprep import draft, expected, sche
from tests.test_income_1099g import _doc, _lay


def _cols(text: str) -> list[sche.Column]:
    return sche.columns(sche.parse("rentals", text))


def _lines(r: sche.Result) -> dict[str, float]:
    return {ln: round(v, 2) for ln, _, v, _ in r.lines}


def _run(text: str, **kw: object) -> sche.Result:
    args: dict[str, object] = {
        "magi": 50_000.0,
        "separate": False,
        "simple": "yes",
        "allowed": None,
    }
    return sche.schedule(_cols(text), **(args | kw))  # type: ignore[arg-type]


def test_parse_entries() -> None:
    got = sche.parse(
        "rentals",
        "rental rents 18,000 mortgage 4000 days 300 personal 0; royalty royalties 1200",
    )
    assert got == [
        {
            "kind": "rental",
            "rents": 18000,
            "mortgage": 4000,
            "days": 300,
            "personal": 0,
        },
        {"kind": "royalty", "royalties": 1200},
    ]
    assert sche.parse("rentals", "none") == []


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("condo rents 100", "starts with rental or royalty"),
        ("rental rents 100 days", "pairs each word"),
        ("rental rents 100 days 30 personal 0 royalties 5", "not a rental word"),
        ("rental rents 100 days 300", "needs personal"),
        ("rental rents 100 days 300 personal 70", "pass 366"),
        ("royalty royalties 5 royalties 6", "typed twice"),
        ("royalty royalties x", "not a number"),
    ],
)
def test_parse_errors(text: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        sche.parse("rentals", text)


def test_rental_income_lines() -> None:
    r = _run(
        "rental rents 18000 mortgage 4000 taxes 2000 expenses 3000 "
        "depreciation 3000 days 365 personal 0"
    )
    got = _lines(r)
    assert {k: got[k] for k in ("3A", "12A", "16A", "18A", "19A", "20A", "21A")} == {
        "3A": 18000.0,
        "12A": 4000.0,
        "16A": 2000.0,
        "18A": 3000.0,
        "19A": 3000.0,
        "20A": 12000.0,
        "21A": 6000.0,
    }
    assert (got["23a"], got["24"], got["25"], got["26"]) == (
        18000.0,
        6000.0,
        0.0,
        6000.0,
    )
    assert r.total == 6000.0 and "22A" not in got


def test_personal_use_under_the_home_test_splits_by_days() -> None:
    # 20 personal days is not more than 10% of 300 rental days: not a home,
    # but every expense is split 300/320 (Pub. 527 ch. 5).
    (c,) = _cols("rental rents 9000 mortgage 3200 expenses 1600 days 300 personal 20")
    assert not c.home and c.passive
    assert (c.lines["12"], c.lines["19"]) == (3000.0, 1500.0)
    assert c.personal == (200.0, 0.0)


def test_home_rented_under_15_days_reports_nothing() -> None:
    r = _run("rental rents 4000 mortgage 9000 days 10 personal 60")
    assert r.total == 0.0 and "3A" not in _lines(r)
    assert any("rented under 15 days" in n for n in r.notes)


def test_home_worksheet_5_1_limits_expenses_to_rents() -> None:
    # 30 personal days beats max(14, 10% of 60): a home. F = 60/90.
    (c,) = _cols(
        "rental rents 6000 mortgage 6000 taxes 1500 direct 200 expenses 3000 "
        "depreciation 3000 days 60 personal 30"
    )
    assert c.home and not c.passive and c.worksheet
    assert c.lines == {
        "3": 6000.0,
        "12": 4000.0,
        "16": 1000.0,
        "18": 0.0,  # line 5 is 0 after the operating expenses
        "19": 1000.0,  # direct 200 + line 4f 800
        "20": 6000.0,
        "21": 0.0,
    }
    assert c.carry == (1200.0, 2000.0)  # lines 7a, 7b


def test_passive_loss_inside_the_special_allowance() -> None:
    r = _run("rental rents 5000 expenses 25000 days 365 personal 0", magi=90_000.0)
    got = _lines(r)
    assert (got["21A"], got["22A"], got["26"]) == (-20000.0, -20000.0, -20000.0)
    assert not any("not allowed" in n for n in r.notes)


def test_special_allowance_phases_out() -> None:
    # Form 8582: line 8 = 50% of (150,000 - 130,000) = 10,000; line 10 adds
    # property B's passive income 4,000; property A's 20,000 loss gets 14,000.
    r = _run(
        "rental rents 5000 expenses 25000 days 365 personal 0; "
        "rental rents 9000 expenses 5000 days 365 personal 0",
        magi=130_000.0,
    )
    got = _lines(r)
    assert (got["22A"], got["24"], got["25"], got["26"]) == (
        -14000.0,
        4000.0,
        -14000.0,
        -10000.0,
    )
    assert any("6,000.00 of passive loss is not allowed" in n for n in r.notes)


def test_separate_lived_apart_allowance() -> None:
    r = _run(
        "rental rents 0 expenses 20000 days 365 personal 0",
        magi=60_000.0,
        separate=True,
    )
    assert _lines(r)["22A"] == -7500.0  # 50% of (75,000 - 60,000), at most 12,500


def test_conditions_fail_takes_form_8582() -> None:
    text = "rental rents 0 expenses 8000 days 365 personal 0"
    r = _run(text, simple="no", allowed=3000.0)
    assert _lines(r)["22A"] == -3000.0
    r = _run(text, simple="no", allowed=9000.0)
    assert _lines(r)["22A"] == -8000.0
    assert any(n.startswith("CHECK passive_loss_allowed") for n in r.notes)
    r = _run(text, simple=None)
    assert _lines(r)["22A"] == 0.0
    assert any("until rental_passive_simple" in n for n in r.notes)


def test_royalty_loss_is_not_passive() -> None:
    r = _run("royalty royalties 100 expenses 400")
    got = _lines(r)
    assert (got["4A"], got["21A"], got["25"], got["26"]) == (
        100.0,
        -300.0,
        -300.0,
        -300.0,
    )
    assert "22A" not in got


def test_passive_questions_asked_only_for_a_loss(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "rentals", "rental rents 9000 expenses 1000 days 365 personal 0")
    conn = db.connect(lay.data / "ledger" / "planner.db")
    keys = {s.need.key for s in _needed(conn, lay, 2025).items}
    assert "rentals" in keys and "rental_passive_simple" not in keys
    enter(lay, 2025, "rentals", "rental rents 1000 expenses 9000 days 365 personal 0")
    enter(lay, 2025, "rental_passive_simple", "no")
    keys = {s.need.key for s in _needed(conn, lay, 2025).items}
    conn.close()
    assert {"rental_passive_simple", "passive_loss_allowed"} <= keys


def test_inputs_magi_and_rental_income(planner_home: Path) -> None:
    lay = _lay(planner_home)  # 40,000 of wages
    enter(lay, 2025, "rentals", "rental rents 1000 expenses 9000 days 365 personal 0")
    enter(lay, 2025, "rental_passive_simple", "yes")
    inp = inputs.build(lay, 2025)
    assert inp.household.rental_income == -8000
    assert any("modified AGI 40,000" in n for n in inp.notes)


def test_draft_rental_income(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(
        lay,
        2025,
        "rentals",
        "rental rents 12000 mortgage 3000 taxes 1000 expenses 2000 "
        "depreciation 2000 days 365 personal 0",
    )
    d = draft.build(lay, 2025)
    assert d.get("Sch E", "26") == 4000.0
    assert (d.get("Sch 1", "5"), d.get("Sch 1", "10")) == (4000.0, 4000.0)
    assert d.get("1040", "8") == 4000.0
    assert not [n for n in d.notes if n.startswith("CHECK")]


def test_draft_rental_loss_reaches_agi(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "rentals", "rental rents 2000 expenses 7000 days 365 personal 0")
    enter(lay, 2025, "rental_passive_simple", "yes")
    d = draft.build(lay, 2025)
    assert (d.get("Sch E", "22A"), d.get("Sch 1", "5")) == (-5000.0, -5000.0)
    assert d.get("1040", "8") == -5000.0
    assert d.get("1040", "11a") == 35000.0
    assert not [n for n in d.notes if n.startswith("CHECK")]


def test_1099misc_rents_above_the_typed_rents(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "rentals", "rental rents 1000 days 365 personal 0")
    _doc(
        lay,
        "misc",
        [db.Fact("1099-MISC", 2025, "Manager (synthetic)", "1", "", 2400.0, 1)],
    )
    d = draft.build(lay, 2025)
    assert any(n.startswith("CHECK: 1099-MISC box 1 shows 2,400.00") for n in d.notes)


def test_expected_1099misc_for_royalties(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "rentals", "royalty royalties 800")
    inv = expected.inventory(lay, 2025, date(2026, 3, 1))
    assert any(e.form == "1099-MISC" and e.reason == "royalties" for e in inv.items)


def test_rental_income_is_qbi_only_when_answered_yes(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "rentals", "rental rents 12000 expenses 2000 days 365 personal 0")
    assert draft.build(lay, 2025).get("1040", "13a") == 0.0
    enter(lay, 2025, "rental_qbi", "yes")
    assert draft.build(lay, 2025).get("1040", "13a") == 2000.0  # 20% of 10,000
