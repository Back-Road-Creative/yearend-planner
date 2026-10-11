"""Unit 3e-6c: Form 4797 from synthetic sales and K-1 boxes, by the 2025 Form
4797 and its instructions and the Unrecaptured Section 1250 Gain Worksheet lines
1-9 (Schedule D instructions)."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.ingest.needs import enter, need_for
from planner.plan import inputs
from planner.taxprep import draft, f4797, k1
from tests.test_income_1099g import _lay

BUILDING = "1250 acquired 2015-03-01 sold 2025-06-30 price 300000 basis 200000 "
BUILDING += "depreciation 50000"
TRUCK = "1245 acquired 2020-01-15 sold 2025-08-01 price 8000 basis 20000 "
TRUCK += "depreciation 15000"
LAND = "land acquired 2015-03-01 sold 2025-06-30 price 60000 basis 40000"
TOOL = "1245 acquired 2025-01-10 sold 2025-11-01 price 1000 basis 3000 "
TOOL += "depreciation 500"
K1S = (
    "partnership nonpassive section1231 5000 unrecaptured1250 4000; "
    "scorp passive section1231 -700"
)


def _form(sales: str, k1s: str = "", lookback: float | None = 0.0) -> f4797.Result:
    entries = k1.parse("k1s", k1s) if k1s else []
    r = f4797.compute(
        f4797.parse("business_sales", sales), entries, k1.portfolio(entries), lookback
    )
    assert r is not None
    return r


def _lines(r: f4797.Result) -> dict[str, float]:
    return {ln: v for ln, _, v, _ in r.lines}


def test_parts_i_ii_iii_and_the_worksheet() -> None:
    r = _form("; ".join((BUILDING, TRUCK, LAND, TOOL)), K1S, lookback=10000)
    got = _lines(r)
    # Part III: the building (A), the truck (B)
    assert (got["23A"], got["24A"], got["26aA"], got["26gA"]) == (150e3, 150e3, 0, 0)
    assert (got["23B"], got["24B"], got["25aB"], got["25bB"]) == (5e3, 3e3, 15e3, 3e3)
    assert (got["30"], got["31"], got["32"]) == (153e3, 3e3, 150e3)
    # Part I: the land, the partnership's box 10, line 6; the passive loss is not
    assert [v for ln, _, v, _ in r.lines if ln == "2"] == [20e3, 5e3]
    assert (got["6"], got["7"], got["8"], got["9"]) == (150e3, 175e3, 10e3, 165e3)
    assert any("passive section 1231 loss of 700.00" in n for n in r.notes)
    # Part II: the tool held under a year, line 8 recaptured, line 31
    assert (got["10"], got["12"], got["13"]) == (-1500, 10e3, 3e3)
    assert (got["17"], got["18b"]) == (11500, 11500)
    assert (r.schedule_d, r.line18b) == (165e3, 11500)
    # Worksheet: (50,000 - 0) + 4,000 (line 5), not over 175,000, less 10,000
    assert r.worksheet == 44000
    assert any("worksheet line 5" in w for w in r.worksheet_from)
    assert not r.unknown


def test_a_line_7_loss_is_ordinary() -> None:
    r = _form(
        "1245 acquired 2020-01-15 sold 2025-08-01 price 1000 basis 10000 "
        "depreciation 4000"
    )
    got = _lines(r)
    assert (got["2"], got["7"], got["11"], got["18b"]) == (-5000, -5000, -5000, -5000)
    assert "8" not in got and (r.schedule_d, r.worksheet) == (0.0, 0.0)


def test_lookback_recaptures_the_whole_gain() -> None:
    r = _form(LAND.replace("price 60000", "price 43000"), lookback=5000)
    got = _lines(r)
    assert (got["7"], got["8"], got["9"], got["12"]) == (3000, 3000, 0, 3000)
    assert (r.schedule_d, r.line18b) == (0.0, 3000)


def test_lookback_not_given_is_named() -> None:
    r = _form(LAND, lookback=None)
    assert r.unknown and "section_1231_lookback" in r.unknown[0]
    assert r.schedule_d == 20000


def test_parse_and_holding_period() -> None:
    assert f4797.parse("business_sales", "none") == []
    for bad, why in (
        ("house acquired 2015-03-01 sold 2025-06-30 price 1 basis 1", "kind"),
        (
            "land acquired 2015-03-01 sold 2025-06-30 price 1 basis 1 depreciation 5",
            "not depreciated",
        ),
        ("1250 acquired 2025-03-01 sold 2024-06-30 price 1 basis 1", "before"),
        ("1250 acquired 2015-03-01 sold 2025-06-30 basis 1", "no price"),
        (
            "1245 acquired 2015-03-01 sold 2025-06-30 price 1 basis 1 additional 1",
            "for 1250",
        ),
        ("1250 acquired 2015-03-01 sold 2025-06-30 price -1 basis 1", "amount"),
    ):
        with pytest.raises(ValueError, match=why):
            f4797.parse("business_sales", bad)
    assert not f4797.long_term("2024-06-30", "2025-06-30")
    assert f4797.long_term("2024-06-30", "2025-07-01")
    assert f4797.long_term("2024-02-29", "2025-03-02")
    assert f4797.compute([], [], [], None) is None


def test_when_asked() -> None:
    sales = need_for("business_sales").asked
    lookback = need_for("section_1231_lookback").asked
    assert sales is not None and lookback is not None
    assert sales({"rentals": [{"kind": "rental"}]}) and not sales({})
    assert lookback({"business_sales": [{"kind": "land"}]})
    assert lookback({"k1s": [{"kind": "partnership", "section1231": 10}]})
    assert not lookback({"k1s": [{"kind": "partnership"}]})


def test_draft_lays_form_4797(planner_home: Path) -> None:
    lay = _lay(planner_home)  # 40,000 of wages
    enter(lay, 2025, "business_sales", f"{BUILDING}; {TRUCK}")
    enter(lay, 2025, "section_1231_lookback", "0")
    inp = inputs.build(lay, 2025)
    assert inp.household.tax_unit_inputs == {
        "unrecaptured_section_1250_gain": 50000.0,
        "other_net_gain": 3000.0,
    }
    d = draft.build(lay, 2025)
    assert (d.get("4797", "7"), d.get("4797", "9"), d.get("4797", "18b")) == (
        150000.0,
        150000.0,
        3000.0,
    )
    assert (d.get("Sch D", "11"), d.get("Sch D", "16")) == (150000.0, 150000.0)
    assert d.get("Sch D", "19") == 50000.0
    assert (d.get("Sch 1", "4"), d.get("Sch 1", "10")) == (3000.0, 3000.0)
    assert d.get("1040", "11a") == 193000.0
    assert not [n for n in d.notes if n.startswith("CHECK")]
    assert "Form 4797 (sales of business property)" in draft.render(d)
