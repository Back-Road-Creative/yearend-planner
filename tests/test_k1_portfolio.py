"""Unit 3e-3b: the K-1 portfolio boxes onto Schedules B, D and E line 4 and
the engine's interest, dividends and gains. Synthetic figures only."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.ingest.needs import enter
from planner.plan import inputs
from planner.taxprep import draft, k1
from tests.test_income_1099g import _lay

PSHIP = (
    "partnership passive interest 2000 dividends 1000 qualified 600 royalties 500 "
    "stgain -300 ltgain 4000"
)
PAYER = "Schedule K-1 (Form 1065), the 1st partnership"


def _rows(d: draft.Draft, form: str, line: str) -> list[tuple[str, float]]:
    return [(x.label, x.value) for x in d.lines if (x.form, x.line) == (form, line)]


def test_parse_portfolio_words() -> None:
    (e,) = k1.parse("k1s", "scorp nonpassive royalties 700 stgain -50 ltgain 9")
    assert (e["royalties"], e["stgain"], e["ltgain"]) == (700, -50, 9)
    with pytest.raises(ValueError, match="not a trust word"):
        k1.parse("k1s", "trust passive royalties 5")
    with pytest.raises(ValueError, match="more qualified than dividends"):
        k1.parse("k1s", "partnership passive dividends 5 qualified 6")
    with pytest.raises(ValueError, match="out of range"):
        k1.parse("k1s", "trust passive interest -5")


def test_portfolio_names_each_k1() -> None:
    entries = k1.parse(
        "k1s", PSHIP + "; trust passive interest 40; scorp passive ordinary 1"
    )
    got = k1.portfolio(entries)
    assert got[0] == ("interest", PAYER, 2000.0, "K-1 box 5")
    assert (
        "interest",
        "Schedule K-1 (Form 1041), the 1st trust",
        40.0,
        "K-1 box 1",
    ) in got
    assert k1.royalties(entries) == [
        {"kind": "royalty", "royalties": 500, "source": f"{PAYER}, K-1 box 7"}
    ]
    # A K-1 of portfolio boxes only has no Schedule E Part II or III row.
    assert [r.kind for r in k1.rows(entries)] == ["scorp"]


def test_draft_portfolio_lines(planner_home: Path) -> None:
    lay = _lay(planner_home)  # 40,000 of wages
    enter(lay, 2025, "k1s", PSHIP)
    d = draft.build(lay, 2025)
    assert (d.get("1040", "2b"), d.get("1040", "3b"), d.get("1040", "3a")) == (
        2000.0,
        1000.0,
        600.0,
    )
    assert _rows(d, "Sch B", "1") == [(f"{PAYER}: write its name", 2000.0)]
    assert _rows(d, "Sch B", "5") == [(f"{PAYER}: write its name", 1000.0)]
    assert (d.get("Sch E", "4A"), d.get("Sch 1", "5")) == (500.0, 500.0)
    assert not [x for x in d.lines if x.form == "Sch E" and x.line.startswith("28")]
    assert (d.get("Sch D", "5"), d.get("Sch D", "12"), d.get("Sch D", "16")) == (
        -300.0,
        4000.0,
        3700.0,
    )
    assert d.get("1040", "7a") == 3700.0
    assert not [n for n in d.notes if n.startswith("CHECK")]


def test_k1_dividends_add_to_the_1099s(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "ordinary_dividends", "500")
    enter(lay, 2025, "qualified_dividends", "200")
    enter(lay, 2025, "interest", "30")
    enter(lay, 2025, "k1s", "trust passive interest 70 dividends 1000 qualified 600")
    out = inputs.build(lay, 2025)
    hh = out.household
    assert (hh.interest, hh.qualified_dividends, hh.non_qualified_dividends) == (
        100,
        800,
        700,
    )
    assert any("take the 1099s only" in n for n in out.notes)
    assert out.schedule_k1 is None


def test_typed_gain_replacing_k1_gain_is_a_check(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "short_term_gains", "100")
    enter(lay, 2025, "k1s", "scorp passive stgain 900")
    notes = inputs.build(lay, 2025).notes
    assert any(n.startswith("CHECK: a typed short_term_gains") for n in notes)


def test_k1_royalty_beside_a_rental(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "rentals", "rental rents 9000 expenses 2000 days 365 personal 0")
    enter(lay, 2025, "k1s", "scorp nonpassive royalties 800")
    d = draft.build(lay, 2025)
    assert (d.get("Sch E", "3A"), d.get("Sch E", "4B")) == (9000.0, 800.0)
    (row,) = [x for x in d.lines if (x.form, x.line) == ("Sch E", "4B")]
    assert row.source == "Schedule K-1 (Form 1120-S), the 1st scorp, K-1 box 6"
    assert d.get("Sch 1", "5") == 7800.0
