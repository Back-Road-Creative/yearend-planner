"""Unit 3e-3a: Schedules K-1 (Forms 1065, 1120-S, 1041) onto Schedule E Parts
II and III, one Form 8582 allowance with Part I, and box 14 code A onto
Schedule SE. Synthetic figures only."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from planner.ingest.needs import _needed, enter
from planner.ledger import db
from planner.plan import inputs
from planner.taxprep import draft, expected, k1, sche
from tests.test_income_1099g import _lay

MIXED = (
    "partnership passive ordinary -4000 rental 1200; scorp nonpassive ordinary "
    "30000 section179 2000"
)


def _lines(r: k1.Result) -> dict[str, float]:
    return {ln: round(v, 2) for ln, _, v, _ in r.lines}


def test_parse_entries() -> None:
    got = k1.parse(
        "k1s",
        "partnership passive ordinary -4,000 rental 1200; spouse partnership "
        "nonpassive guaranteed 9000 se 9000",
    )
    assert got == [
        {
            "kind": "partnership",
            "passive": True,
            "spouse": False,
            "ordinary": -4000,
            "rental": 1200,
        },
        {
            "kind": "partnership",
            "passive": False,
            "spouse": True,
            "guaranteed": 9000,
            "se": 9000,
        },
    ]
    assert k1.parse("k1s", "none") == []


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("llc passive ordinary 5", "starts with partnership, scorp or trust"),
        ("scorp active ordinary 5", "passive or nonpassive"),
        ("scorp passive ordinary", "pairs each word"),
        ("scorp passive guaranteed 5", "not a scorp word"),
        ("trust passive ordinary 5 ordinary 6", "typed twice"),
        ("trust passive portfolio -5", "out of range"),
        ("trust passive", "has no amounts"),
        ("spouse scorp nonpassive ordinary 5", "has no se"),
    ],
)
def test_parse_errors(text: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        k1.parse("k1s", text)


def test_rows_place_each_box() -> None:
    pship, scorp = k1.rows(k1.parse("k1s", MIXED))
    assert (pship.letter, pship.passive) == ("A", [("box 1", -4000), ("box 2", 1200)])
    assert (scorp.letter, scorp.income, scorp.section179) == ("A", 30000, 2000)
    # Rentals stay passive even for a material participant; box 9 is a
    # deduction of the trust's own character.
    (t,) = k1.rows(
        k1.parse(
            "k1s",
            "trust nonpassive ordinary 500 rental 300 deductions 700 portfolio 100",
        )
    )
    assert (t.passive, t.loss, t.income) == ([("box 7", 300.0)], 700.0, 600.0)
    assert k1.passive_totals([pship, scorp, t]) == (1500.0, 4000.0)
    assert k1.nonpassive_net([pship, scorp, t]) == 27900.0


def test_schedule_part2_lines() -> None:
    r = k1.schedule(k1.rows(k1.parse("k1s", MIXED)), 1.0, "all")
    got = _lines(r)
    assert (got["28A(g)"], got["28A(h)"], got["28A(k)"], got["28A(j)"]) == (
        4000.0,
        1200.0,
        30000.0,
        2000.0,
    )
    assert (got["30"], got["31"], got["32"]) == (31200.0, -6000.0, 25200.0)
    assert r.by_kind == {"partnership": -2800.0, "scorp": 28000.0, "trust": 0.0}
    assert r.passive_pships == -2800.0
    assert any("Form 7203" not in n and "basis" in n for n in r.notes)


def test_schedule_part3_lines() -> None:
    r = k1.schedule(
        k1.rows(k1.parse("k1s", "trust passive ordinary 800 portfolio 300")), 1.0, ""
    )
    got = _lines(r)
    assert (got["33A(d)"], got["33A(f)"], got["37"]) == (800.0, 300.0, 1100.0)
    assert "32" not in got


def _allow(
    cols: list[sche.Column], allowed: float | None
) -> tuple[float, str, list[str]]:
    return sche.allowance(
        cols,
        magi=50_000.0,
        separate=False,
        simple="yes",
        allowed=allowed,
        k1_income=1200.0,
        k1_losses=4000.0,
    )


def test_allowance_with_k1_passive_takes_form_8582() -> None:
    ratio, src, notes = _allow([], None)
    assert ratio == pytest.approx(0.3) and src.startswith("passive income only")
    assert any("2,800.00 of passive loss is not allowed" in n for n in notes)
    ratio, src, _ = _allow([], 3000.0)
    assert ratio == pytest.approx(0.75) and src.startswith("passive_loss_allowed")
    _, _, notes = _allow([], 9000.0)
    assert any(n.startswith("CHECK passive_loss_allowed") for n in notes)
    # Rental income covers a K-1 passive loss: all of it is allowed.
    cols = sche.columns(sche.parse("rentals", "rental rents 9000 days 365 personal 0"))
    assert _allow(cols, None)[0] == 1.0


def test_k1_passive_loss_asks_form_8582(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "rentals", "rental rents 1000 expenses 3000 days 365 personal 0")
    enter(lay, 2025, "k1s", "partnership passive ordinary -500")
    conn = db.connect(lay.data / "ledger" / "planner.db")
    keys = {s.need.key for s in _needed(conn, lay, 2025).items}
    conn.close()
    assert "passive_loss_allowed" in keys and "rental_passive_simple" not in keys


def test_inputs_engine_fields(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(
        lay,
        2025,
        "k1s",
        "partnership nonpassive ordinary 6000 guaranteed 10000 se 16000 qbi 6000; "
        "scorp passive ordinary 2000; trust passive ordinary 700; "
        "spouse partnership nonpassive se 100",
    )
    hh = inputs.build(lay, 2025).household
    assert (hh.partnership_income, hh.s_corp_income, hh.trust_income) == (
        6000,
        2000,
        700,
    )
    assert (hh.guaranteed_payments, hh.passive_pass_through, hh.k1_se) == (
        10000,
        2000,
        16000,
    )
    assert hh.k1_qbi and not hh.trust_qbi
    notes = inputs.build(lay, 2025).notes
    assert any("the spouse is not on this return" in n for n in notes)
    assert any("the section 199A statements give 6,000" in n for n in notes)


def test_draft_k1_lines_reach_agi_and_schedule_se(planner_home: Path) -> None:
    lay = _lay(planner_home)  # 40,000 of wages
    enter(
        lay,
        2025,
        "k1s",
        "partnership nonpassive ordinary 5000 guaranteed 10000 se 15000; "
        "trust nonpassive ordinary 1000",
    )
    d = draft.build(lay, 2025)
    assert (d.get("Sch E", "32"), d.get("Sch E", "37")) == (15000.0, 1000.0)
    assert (d.get("Sch E", "41"), d.get("Sch 1", "5")) == (16000.0, 16000.0)
    assert d.get("1040", "8") == 16000.0
    assert d.get("Sch SE", "2") == 15000.0
    assert d.get("Sch SE", "12") == pytest.approx(15000 * 0.9235 * 0.153, abs=1)
    assert not [n for n in d.notes if n.startswith("CHECK")]


def test_draft_k1_with_rentals_shares_the_allowance(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "rentals", "rental rents 1000 expenses 3000 days 365 personal 0")
    enter(lay, 2025, "k1s", "scorp passive ordinary -2000 rental 1000")
    enter(lay, 2025, "passive_loss_allowed", "2000")
    d = draft.build(lay, 2025)
    # Losses 4,000 (2,000 rental, 2,000 box 1) and 2,000 allowed: half each.
    assert (d.get("Sch E", "22A"), d.get("Sch E", "28A(g)")) == (-1000.0, 1000.0)
    assert d.get("Sch E", "41") == -1000.0 + 1000.0 - 1000.0
    assert d.get("Sch 1", "5") == -1000.0
    assert not [n for n in d.notes if n.startswith("CHECK")]


def test_trust_loss_reaches_agi(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "k1s", "trust nonpassive ordinary -1500")
    d = draft.build(lay, 2025)
    assert (d.get("Sch E", "33A(e)"), d.get("Sch 1", "5")) == (1500.0, -1500.0)
    assert d.get("1040", "11a") == 38500.0
    assert not [n for n in d.notes if n.startswith("CHECK")]


def test_qbi_only_from_the_199a_statement(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "k1s", "scorp nonpassive ordinary 10000")
    assert draft.build(lay, 2025).get("1040", "13a") == 0.0
    enter(lay, 2025, "k1s", "scorp nonpassive ordinary 10000 qbi 10000")
    assert draft.build(lay, 2025).get("1040", "13a") == 2000.0  # 20% of 10,000
    # Guaranteed payments are not QBI.
    enter(lay, 2025, "k1s", "partnership nonpassive guaranteed 10000 se 10000 qbi 0")
    assert draft.build(lay, 2025).get("1040", "13a") == 0.0


def test_expected_k1_forms(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "k1s", "partnership passive rental 100; trust passive ordinary 5")
    inv = expected.inventory(lay, 2025, date(2026, 3, 1))
    due = {e.form: e.due for e in inv.items if e.form.startswith("K-1")}
    assert due["K-1 (1065)"].startswith("2026-03-1")
    assert due["K-1 (1041)"].startswith("2026-04-1")
