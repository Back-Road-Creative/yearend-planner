"""Unit 3e-2b: Schedule E Part I, rental real estate and royalties, with the
Pub. 527 vacation-home rules, its passive losses through Form 8582 (unit
3e-4b). Synthetic figures only."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from planner.ingest.needs import _needed, enter
from planner.ledger import db
from planner.plan import inputs
from planner.taxprep import draft, expected, f8582, sche
from tests.test_income_1099g import _doc, _lay


def _cols(text: str) -> list[sche.Column]:
    return sche.columns(sche.parse("rentals", text))


def _lines(r: sche.Result) -> dict[str, float]:
    return {ln: round(v, 2) for ln, _, v, _ in r.lines}


def _run(
    text: str,
    *,
    magi: float = 50_000.0,
    separate: str | None = None,
    active: bool = True,
) -> sche.Result:
    cols = _cols(text)
    form = f8582.compute(
        sche.activities(cols, active=active), magi=magi, separate=separate
    )
    r = sche.schedule(cols, form.allowed)
    r.notes.extend(form.notes)
    return r


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
        separate="apart",
    )
    assert _lines(r)["22A"] == -7500.0  # 50% of (75,000 - 60,000), at most 12,500
    together = _run(
        "rental rents 0 expenses 20000 days 365 personal 0", separate="together"
    )
    assert _lines(together)["22A"] == 0.0


def test_no_active_participation_gets_no_allowance() -> None:
    # Part V: a loss is allowed only against passive income.
    r = _run(
        "rental rents 0 expenses 8000 days 365 personal 0; "
        "rental rents 5000 expenses 2000 days 365 personal 0",
        active=False,
    )
    got = _lines(r)
    assert (got["22A"], got["24"], got["25"], got["26"]) == (
        -3000.0,
        3000.0,
        -3000.0,
        0.0,
    )
    assert any("5,000.00 of passive loss is not allowed" in n for n in r.notes)


def test_prior_year_loss_comes_off_line_22() -> None:
    # Net income 4,000 covers the 3,000 prior-year loss: Form 8582 line 3 is
    # income, so all of it is allowed.
    (c,) = _cols("rental rents 9000 expenses 5000 days 365 personal 0 prior 3000")
    assert c.prior == 3000.0
    r = _run("rental rents 9000 expenses 5000 days 365 personal 0 prior 3000")
    got = _lines(r)
    assert (got["21A"], got["22A"], got["24"], got["25"], got["26"]) == (
        4000.0,
        -3000.0,
        4000.0,
        -3000.0,
        1000.0,
    )


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
    assert "rentals" in keys and "rental_active" not in keys
    enter(lay, 2025, "rentals", "rental rents 1000 expenses 9000 days 365 personal 0")
    keys = {s.need.key for s in _needed(conn, lay, 2025).items}
    assert "rental_active" in keys and "lived_apart" not in keys
    enter(lay, 2025, "filing_status", "married_separate")
    keys = {s.need.key for s in _needed(conn, lay, 2025).items}
    conn.close()
    assert "lived_apart" in keys


def test_inputs_magi_and_rental_income(planner_home: Path) -> None:
    lay = _lay(planner_home)  # 40,000 of wages
    enter(lay, 2025, "rentals", "rental rents 1000 expenses 9000 days 365 personal 0")
    inp = inputs.build(lay, 2025)
    assert inp.household.rental_income == 0
    assert any("until rental_active" in n for n in inp.notes)
    enter(lay, 2025, "rental_active", "yes")
    inp = inputs.build(lay, 2025)
    assert inp.household.rental_income == -8000
    assert inp.form_8582 is not None and inp.form_8582.allowed == {"rental A": 8000.0}
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
    enter(lay, 2025, "rental_active", "yes")
    d = draft.build(lay, 2025)
    assert (d.get("Sch E", "22A"), d.get("Sch 1", "5")) == (-5000.0, -5000.0)
    assert (d.get("Form 8582", "1d"), d.get("Form 8582", "9")) == (-5000.0, 5000.0)
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


def test_rendered_draft_and_package_carry_schedule_e_and_8582(
    planner_home: Path,
) -> None:
    from planner.taxprep import package

    lay = _lay(planner_home)
    enter(lay, 2025, "rentals", "rental rents 2000 expenses 7000 days 365 personal 0")
    enter(lay, 2025, "rental_active", "yes")
    d = draft.build(lay, 2025)
    text = draft.render(d)
    assert "Schedule E [" in text and "Form 8582 (passive activity loss" in text
    assert text.index("Schedule 1 [") < text.index("Schedule E [")
    html = package.html_page(d)
    assert "Schedule E <small>" in html and "Form 8582 (passive" in html
    # A form drafted but missing from the ORDER list still prints, last.
    d.lines.append(draft.Line("Form 9999", "1", "Synthetic", 1.0, "test"))
    assert "Form 9999" in draft.render(d) and "Form 9999" in package.html_page(d)
