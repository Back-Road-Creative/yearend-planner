"""Unit 3e-7: the foreign tax credit from synthetic 1099s, by the 2025 Form 1116
and its instructions (the election, lines 1a-35, the qualified dividend
adjustment, its exception and the Worksheet for Line 18) and Pub. 514."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from planner.engine import tax
from planner.ingest.needs import enter, need_for
from planner.ledger import db
from planner.plan import inputs
from planner.taxprep import draft, f1116
from tests.test_income_1099g import _doc, _lay

QDCGTW = "Qualified Dividends and Capital Gain Tax Worksheet"
BASE = f1116.Facts(
    paid=1500.0,
    income=10000.0,
    qualified=0.0,
    carryover=0.0,
    joint=False,
    line20=6000.0,
    taxable=50000.0,
    senior=0.0,
    deduction=15000.0,
    deduction_src="1040 line 12e",
    adjustments=0.0,
    gross=75000.0,
    mortgage=0.0,
    other_interest=0.0,
    top24=197300.0,
    bands=None,
)


def _lines(r: f1116.Result, form: str = f1116.FORM) -> dict[str, float]:
    return {ln: v for f, ln, _, v, _, _ in r.lines if f == form}


def test_the_election_without_form_1116() -> None:
    r = f1116.compute(replace(BASE, paid=250.0, line20=5000.0))
    assert (r.credit, r.form, r.lines) == (250.0, False, [])
    assert "without Form 1116" in r.notes[0]
    assert f1116.compute(replace(BASE, paid=250.0, line20=100.0)).credit == 100.0
    assert not f1116.compute(replace(BASE, paid=550.0, joint=True)).form
    assert f1116.compute(replace(BASE, paid=350.0)).form
    assert f1116.compute(replace(BASE, paid=250.0, carryover=100.0)).form
    assert f1116.compute(replace(BASE, paid=0.0)).credit == 0.0


def test_the_limit_binds_and_carries_over() -> None:
    r = f1116.compute(BASE)
    got = _lines(r)
    assert (got["1a"], got["3c"], got["3d"], got["3e"]) == (10e3, 15e3, 10e3, 75e3)
    assert (got["3f"], got["3g"], got["4a"], got["4b"]) == (0.1333, 1999.5, 0, 0)
    assert (got["6"], got["7"], got["14"], got["18"]) == (1999.5, 8000.5, 1500, 50e3)
    assert (got["19"], got["20"], got["21"], got["24"]) == (0.16, 6000, 960, 960)
    assert (got["33"], got["35"], r.credit, r.form) == (960, 960, 960, True)
    assert any("540.00 of foreign tax is over the limit" in n for n in r.notes)
    assert r.notes[-1] == f1116.NOT_DRAFTED


def test_interest_over_5000_takes_the_mortgage_share() -> None:
    r = f1116.compute(replace(BASE, mortgage=9000.0, other_interest=500.0))
    assert _lines(r)["4a"] == round(9000 * 0.1333, 2)
    assert any("line 4b takes a share" in n for n in r.notes)
    small = f1116.compute(replace(BASE, income=5000.0, mortgage=9000.0))
    assert _lines(small)["4a"] == 0


def test_qualified_dividends_adjusted_by_the_worksheet_for_line_18() -> None:
    bands = f1116.Bands(0, 20000, 0, 0, 0, 400000, QDCGTW, True)
    facts = replace(
        BASE,
        paid=2000.0,
        qualified=10000.0,
        taxable=420000.0,
        gross=430000.0,
        line20=100000.0,
        bands=bands,
    )
    r = f1116.compute(facts)
    got, w18 = _lines(r), _lines(r, f1116.W18)
    assert got["1a"] == 4054  # 10,000 x 0.4054, all at 15%
    assert (w18["1"], w18["8"], w18["9"], w18["11"]) == (420e3, 20e3, 11892, 11892)
    assert "2" not in w18  # lines 2-5 are the Schedule D Tax Worksheet's
    assert (got["7"], got["18"], got["19"], got["21"]) == (3704.5, 408108, 0.0091, 910)
    assert r.credit == 910  # 24% bracket exceeded: no exception


def test_the_adjustment_exception_takes_the_larger_credit() -> None:
    bands = f1116.Bands(0, 20000, 0, 0, 0, 100000, QDCGTW, True)
    facts = replace(
        BASE,
        paid=2000.0,
        qualified=10000.0,
        taxable=120000.0,
        gross=130000.0,
        line20=20000.0,
        bands=bands,
    )
    r = f1116.compute(facts)
    got = _lines(r)
    assert (got["1a"], got["18"], got["19"], got["21"]) == (10e3, 120e3, 0.0737, 1474)
    assert r.credit == 1474  # adjusted would be 536
    assert any("adjustment exception applies" in n for n in r.notes)
    assert not _lines(r, f1116.W18)


def test_dividends_across_rates_are_split_and_flagged() -> None:
    bands = f1116.Bands(10000, 10000, 0, 0, 0, 40000, QDCGTW, True)
    facts = replace(BASE, income=30000.0, qualified=20000.0, bands=bands)
    r = f1116.compute(facts)
    assert _lines(r)["1a"] == 14054  # 10,000 + 10,000 x 0.4054, none at 0%
    assert any("splits the foreign qualified dividends" in n for n in r.notes)


def test_missing_income_is_named() -> None:
    r = f1116.compute(replace(BASE, income=None))
    assert r.credit == 0
    assert any("foreign_source_income not given" in n for n in r.notes)


def test_bands_from_the_qualified_dividends_worksheet() -> None:
    v = {
        **dict.fromkeys(tax.SDTW_VARS, 0.0),
        "adjusted_net_capital_gain": 20000.0,
        "dwks09": 0.0,
        "dwks10": 0.0,
    }
    high = f1116.bands(2025, "SINGLE", 100000.0, v)
    assert high is not None and high.sheet == QDCGTW and high.lower
    assert (high.zero, high.fifteen, high.twenty, high.ordinary) == (0, 20e3, 0, 80e3)
    low = f1116.bands(2025, "SINGLE", 40000.0, v)
    assert low is not None and (low.zero, low.fifteen) == (20e3, 0)
    assert (
        f1116.bands(2025, "SINGLE", 40e3, {**v, "adjusted_net_capital_gain": 0.0})
        is None
    )


def test_when_asked() -> None:
    paid = need_for("foreign_tax_paid").asked
    form = need_for("foreign_source_income").asked
    carry = need_for("foreign_tax_carryover").asked
    assert paid is not None and form is not None and carry is not None
    assert paid({"ordinary_dividends": 10}) and not paid({})
    assert carry({"foreign_tax_paid": 1}) and not carry({"foreign_tax_paid": 0})
    assert form({"foreign_tax_paid": 301}) and not form({"foreign_tax_paid": 300})
    joint = {"filing_status": "married_joint"}
    assert not form({**joint, "foreign_tax_paid": 600})
    assert form({**joint, "foreign_tax_paid": 601})
    assert form({"foreign_tax_paid": 100, "foreign_tax_carryover": 5})


def _div(lay: object, boxes: dict[str, float]) -> None:
    _doc(
        lay,  # type: ignore[arg-type]
        "div",
        [
            db.Fact("1099-DIV", 2025, "Intl Fund (synthetic)", b, "", v, 1)
            for b, v in boxes.items()
        ],
    )


def test_draft_takes_the_election(planner_home: Path) -> None:
    lay = _lay(planner_home)  # 40,000 of wages
    _div(lay, {"1a": 3000.0, "1b": 2000.0, "7": 120.0})
    inp = inputs.build(lay, 2025)
    assert inp.household.tax_unit_inputs == {"foreign_tax_credit_potential": 120.0}
    d = draft.build(lay, 2025)
    assert d.get("Sch 3", "1") == 120.0
    assert d.get("1116", "35") is None
    l18 = d.get("1040", "18")
    assert l18 is not None and d.get("1040", "22") == round(l18 - 120.0, 2)
    assert d.get("Sch 3", "8") == 120.0


def test_draft_lays_form_1116(planner_home: Path) -> None:
    lay = _lay(planner_home)
    _div(lay, {"1a": 3000.0, "1b": 2000.0, "7": 400.0})
    for key, text in (
        ("foreign_source_income", "3000"),
        ("foreign_qualified_dividends", "2000"),
        ("foreign_tax_carryover", "0"),
    ):
        enter(lay, 2025, key, text)
    d = draft.build(lay, 2025)
    got = {k: d.get("1116", k) for k in ("1a", "3e", "4a", "18", "19", "20", "35")}
    assert got["1a"] == 3000.0  # the exception: unadjusted is larger
    assert got["3e"] == d.get("1040", "9")
    assert got["4a"] == 0.0  # foreign gross income under 5,000
    assert got["18"] == d.get("1040", "15")
    assert got["20"] == d.get("1040", "16")
    credit = got["35"]
    assert credit is not None and 0 < credit < 400
    assert credit == round(got["20"] * got["19"], 2)  # type: ignore[operator]
    assert d.get("Sch 3", "1") == credit == d.get("Sch 3", "8")
    l18 = d.get("1040", "18")
    assert l18 is not None and d.get("1040", "22") == round(l18 - credit, 2)
    assert any("over the limit" in n for n in d.notes)
    assert "Form 1116 (foreign tax credit)" in draft.render(d)


def test_bands_from_the_schedule_d_tax_worksheet() -> None:
    v = {
        tax.SDTW_VARS[0]: 0.0,
        tax.SDTW_VARS[1]: 10000.0,
        "adjusted_net_capital_gain": 10000.0,
        "dwks09": 10000.0,
        "dwks10": 10000.0,
    }
    b = f1116.bands(2025, "SINGLE", 100000.0, v)
    # within the 24% bracket the 1250 gain is taxed by the rate schedule
    # (worksheet line 21), so the worksheet taxes no less than line 46
    assert b is not None and b.sheet == "Schedule D Tax Worksheet"
    assert (b.ordinary, b.twenty_five, b.twenty_eight, b.lower) == (90e3, 0, 0, False)
