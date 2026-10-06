"""Unit 3e-6b2: Schedule D lines 18 and 19 from synthetic lots and forms: a
bullion trust's long-term gain (code C), a washed silver loss (code CW), a fund's
1099-DIV boxes 2b and 2d, a trust's K-1 boxes 4b and 4c and a typed unrecaptured
section 1250 gain, through the 28% Rate Gain and Unrecaptured Section 1250 Gain
Worksheets (2025 Schedule D instructions) onto the engine's Schedule D Tax
Worksheet."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest import ingest
from planner.ingest.needs import enter, need_for, needed, parse_value
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.plan import inputs
from planner.taxprep import capgains, draft, k1
from tests.test_csv import TXN_HEADER, drop

runner = CliRunner()
TAXABLE, IRA = "55556666", "77778888"
LOTS = "\n".join(
    [
        "Account Number,Investment Name,Symbol,Date Acquired,Date Sold,Shares,"
        "Proceeds,Cost Basis,Term",
        f"{TAXABLE},Gold Trust,GLD,03/02/2020,02/10/2025,50,10000,6000,Long-term",
        f"{TAXABLE},Total Stock,VTSAX,03/02/2020,02/10/2025,100,15000,10000,Long-term",
        f"{TAXABLE},Silver Trust,SLV,03/02/2020,06/02/2025,100,2000,3000,Long-term",
        "",
    ]
)
BUYS = "\n".join(
    [
        TXN_HEADER,
        f"{IRA},06/10/2025,06/11/2025,Buy,Buy,Silver,SLV,40,20,800,0,-800,0,IRA,",
        "",
    ]
)
TRUST_K1 = "trust nonpassive ltgain 1000 collectibles 200 unrecaptured1250 500"


def _facts(lay: Layout, facts: list[db.Fact]) -> None:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        db.add_document(
            conn,
            fingerprint=f"synthetic-{len(facts)}-{facts[0].box}",
            file_name="forms.pdf",
            kind="pdf",
            pages=1,
            batch="b1",
            facts=facts,
        )
    finally:
        conn.close()


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    portfolio.save_account(lay, TAXABLE, type="taxable")
    portfolio.save_account(lay, IRA, type="trad_ira")
    drop(lay, "lots.csv", LOTS)
    drop(lay, "buys.csv", BUYS)
    ingest(lay)
    fund = "Fund Co (synthetic)"
    _facts(
        lay,
        [
            db.Fact("1099-DIV", 2025, fund, "2a", "Cap gain", 1500.0, 1),
            db.Fact("1099-DIV", 2025, fund, "2b", "1250 gain", 700.0, 1),
            db.Fact("1099-DIV", 2025, fund, "2d", "28% gain", 300.0, 1),
        ],
    )
    enter(lay, 2025, "collectibles", "gld, slv")
    enter(lay, 2025, "k1s", TRUST_K1)
    enter(lay, 2025, "unrecaptured_1250", "800")
    return lay


def _store(lay: Layout) -> capgains.CapGains:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        return capgains.store(conn, lay, 2025, date(2026, 2, 1))
    finally:
        conn.close()


def test_worksheets_fill_lines_18_and_19(lay: Layout) -> None:
    cg = _store(lay)
    codes = {lt.description: lt.code for lt in cg.lots}
    assert codes == {"50 sh GLD": "C", "100 sh VTSAX": "", "100 sh SLV": "CW"}
    assert cg.lines == {
        "7": 0.0,
        "8b": 8400.0,  # 4,000 + 5,000 - 600 (400 of the SLV loss washed)
        "12": 1000.0,
        "13": 1500.0,
        "15": 10900.0,
        "16": 10900.0,
        "18": 3900.0,  # lots 3,400 + box 2d 300 + K-1 box 4b 200
        "19": 2000.0,  # typed 800 + box 2b 700 + K-1 box 4c 500
    }
    assert "Form 8949 Part II code C" in cg.sources["18"]
    assert "1099-DIV box 2d (Fund Co (synthetic))" in cg.sources["18"]
    assert "K-1 box 4b" in cg.sources["18"]
    assert "unrecaptured_1250 (typed)" in cg.sources["19"]
    assert "K-1 box 4c" in cg.sources["19"]
    assert "Unrecaptured Section 1250 Gain Worksheet line 4" in (" ".join(cg.notes))


def test_losses_use_up_the_28_percent_gain_first(lay: Layout) -> None:
    enter(lay, 2025, "lt_loss_carryover", "4,000")
    cg = _store(lay)
    assert cg.lines["15"] == 6900.0
    # 28%: 3,400 + 500 - 4,000 is below zero, so no line 18; the 100 left over
    # comes off the section 1250 gain (worksheet line 17).
    assert "18" not in cg.lines
    assert cg.lines["19"] == 1900.0


def test_no_lines_18_19_unless_15_and_16_are_gains(lay: Layout) -> None:
    enter(lay, 2025, "lt_loss_carryover", "20,000")
    cg = _store(lay)
    assert cg.lines["15"] < 0
    assert "18" not in cg.lines and "19" not in cg.lines


def test_symbols_and_when_asked() -> None:
    need = need_for("collectibles")
    assert parse_value(need, "gld, slv,GLD") == ["GLD", "SLV"]
    assert parse_value(need, "None") == []
    with pytest.raises(ValueError, match="symbols separated by commas"):
        parse_value(need, "G$D")
    assert need.asked is not None and need.asked({"long_term_gains": 10})
    assert not need.asked({"long_term_gains": -10})
    asked = need_for("unrecaptured_1250").asked
    assert asked is not None
    assert asked({"long_term_gains": 10, "k1s": [{"kind": "partnership"}]})
    assert asked({"long_term_gains": 10, "rentals": [{"kind": "rental"}]})
    assert not asked({"long_term_gains": 10, "k1s": [{"kind": "trust"}]})
    assert not asked({"long_term_gains": 0, "rentals": [{"kind": "rental"}]})


def test_k1_collectibles_and_trust_1250() -> None:
    got = k1.portfolio(k1.parse("k1s", TRUST_K1))
    assert ("collectibles", 200.0, "K-1 box 4b") in [(w, a, s) for w, _, a, s in got]
    assert ("unrecaptured1250", 500.0, "K-1 box 4c") in [
        (w, a, s) for w, _, a, s in got
    ]
    pship = k1.portfolio(k1.parse("k1s", "partnership nonpassive collectibles -300"))
    assert pship[0][2:] == (-300.0, "K-1 box 9b")
    pship = k1.portfolio(k1.parse("k1s", "partnership nonpassive unrecaptured1250 5"))
    assert pship[0][2:] == (5.0, "K-1 box 9c")  # through Form 4797 (unit 3e-6c)


def test_draft_prices_by_the_schedule_d_tax_worksheet(lay: Layout) -> None:
    for key, text in (
        ("birth_date", "1971-06-15"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("wages", "60,000"),
    ):
        enter(lay, 2025, key, text)
    inp = inputs.build(lay, 2025)
    assert inp.household.tax_unit_inputs == {
        "capital_gains_28_percent_rate_gain": 3900.0,
        "unrecaptured_section_1250_gain": 2000.0,
    }
    d = draft.build(lay, 2025)
    assert d.get("Sch D", "18") == 3900.0 and d.get("Sch D", "19") == 2000.0
    assert d.get("1040", "15") == 55150.0  # 60,000 + 10,900 - 15,750
    # Schedule D Tax Worksheet by hand: line 21 is 50,150 (the gains at 28% and
    # 25% sit in the 22% bracket, so ordinary rates), line 30 is 5,000 at 15%;
    # line 44 is the Tax Table on 50,150 (5,953), line 45 = 750 + 5,953 under
    # line 46 (the table on 55,150: 7,053).
    assert d.get("1040", "16") == 6703.0
    rows = [ln for ln in d.lines if ln.form == "8949"]
    assert any("code CW +400.00" in r.source for r in rows)
    r = runner.invoke(app, ["gains", "--year", "2025"])
    assert r.exit_code == 0, r.output
    assert " CW " in r.output and "line 18" in r.output


def test_box_2c_and_untracked_collectibles_named(planner_home: Path) -> None:
    lay = Layout(planner_home)
    lay.ensure()
    _facts(
        lay,
        [
            db.Fact("1099-B", 2025, "Broker", "lt_proceeds", "", 5000.0, 1),
            db.Fact("1099-B", 2025, "Broker", "lt_basis", "", 2000.0, 1),
            db.Fact("1099-DIV", 2025, "Fund", "2a", "", 400.0, 1),
            db.Fact("1099-DIV", 2025, "Fund", "2c", "", 100.0, 1),
        ],
    )
    enter(lay, 2025, "collectibles", "GLD")
    cg = _store(lay)
    notes = " ".join(cg.notes)
    assert "the 28% Rate Gain Worksheet line 2 is left out" in notes
    assert "Schedule D line 18 leaves them out" in notes
    assert "18" not in cg.lines
    keys = {s.need.key for s in needed(lay, 2025).items}
    assert "collectibles" in keys and "unrecaptured_1250" not in keys
