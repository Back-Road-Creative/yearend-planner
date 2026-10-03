"""Phase 4k: the NC D-400 and Schedule S on the draft, from synthetic W-2 and
1099-INT boxes: US Treasury interest taxed federally and subtracted for NC, the
NC standard deduction, the flat rate, withholding and an estimated payment."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.engine import tax
from planner.engine.household import Household
from planner.ingest.needs import enter
from planner.ledger import db
from planner.paths import Layout
from planner.plan import esttax
from planner.taxprep import d400, draft


def _lay(planner_home: Path, nc_withheld: float) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    facts = [
        db.Fact("W-2", 2025, "Employer (synthetic)", "1", "", 60250.0, 1),
        db.Fact("W-2", 2025, "Employer (synthetic)", "2", "", 6000.0, 1),
        db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "1", "", 500.0, 1),
        db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "3", "", 300.0, 1),
    ]
    if nc_withheld:
        facts.append(
            db.Fact("W-2", 2025, "Employer (synthetic)", "17", "", nc_withheld, 1)
        )
    db.add_document(
        conn,
        fingerprint="synthetic-nc",
        file_name="nc.pdf",
        kind="pdf",
        pages=1,
        batch="b1",
        facts=facts,
    )
    conn.close()
    for key, text in (
        ("birth_date", "1980-05-01"),
        ("filing_status", "single"),
        ("state", "NC"),
    ):
        enter(lay, 2025, key, text)
    return lay


def test_d400_refund_with_treasury_interest_and_estimated_payment(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home, 2000.0)
    enter(lay, 2025, "nc_use_tax", "25")
    esttax.record(lay, 2025, "nc", "2025-06-15", 200.0)
    d = draft.build(lay, 2025)
    g = d.get
    assert g("1040", "2b") == 800.0  # box 3 is taxable federally
    assert g("1040", "11a") == 61050.0
    assert (g("Sch S", "16"), g("Sch S", "18"), g("Sch S", "41")) == (0.0, 300.0, 300.0)
    assert g("D-400", "6") == 61050.0 and g("D-400", "9") == 300.0
    assert g("D-400", "10b") == 0.0
    assert g("D-400", "11") == 12750.0  # 2025 single, D-401 p. 14
    assert g("D-400", "12a") == 13050.0 and g("D-400", "14") == 48000.0
    assert g("D-400", "15") == 2040.0  # 48,000 x 4.25%
    assert (g("D-400", "18"), g("D-400", "19")) == (25.0, 2065.0)
    assert (g("D-400", "20a"), g("D-400", "21a"), g("D-400", "23")) == (
        2000.0,
        200.0,
        2200.0,
    )
    assert (g("D-400", "28"), g("D-400", "34")) == (135.0, 135.0)
    assert g("D-400", "26a") is None
    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes
    out = draft.render(d)
    assert "NC Form D-400" in out and "NC D-400 Schedule S" in out
    assert "2025-06-15 200.00 (typed)" in out


def test_d400_owed_with_use_tax_from_the_table(planner_home: Path) -> None:
    lay = _lay(planner_home, 0.0)
    d = draft.build(lay, 2025)
    assert d.get("D-400", "20a") == 0.0 and d.get("D-400", "28") is None
    use, total = d.get("D-400", "18"), d.get("D-400", "19")
    assert use is not None and use > 0
    assert total == pytest.approx(2040.0 + use)
    assert d.get("D-400", "26a") == d.get("D-400", "27") == d.get("D-400", "19")
    assert any("use tax table" in n for n in d.notes)
    assert any("Schedule PN" in n for n in d.notes)
    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes


def test_typed_schedule_s_items_move_line_14(planner_home: Path) -> None:
    lay = _lay(planner_home, 2000.0)
    enter(lay, 2025, "nc_additions", "1000")
    enter(lay, 2025, "nc_other_deductions", "4000")  # e.g. a Bailey pension
    d = draft.build(lay, 2025)
    assert d.get("D-400", "7") == 1000.0 and d.get("D-400", "9") == 4300.0
    assert d.get("D-400", "14") == 45000.0
    assert d.get("D-400", "15") == 1912.5
    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes


def test_no_d400_outside_nc(planner_home: Path) -> None:
    lay = _lay(planner_home, 0.0)
    enter(lay, 2025, "state", "SC")
    d = draft.build(lay, 2025)
    assert not [ln for ln in d.lines if ln.form in ("D-400", "Sch S")]
    assert any("no state return drafted for SC" in n for n in d.notes)


@pytest.mark.parametrize("year", sorted(d400.RATE))
def test_rate_matches_the_engine(year: int) -> None:
    hh = Household(age=45, filing_status="SINGLE", state="NC", wages=80000)
    v = tax.values(year, hh, ("nc_taxable_income", "nc_income_tax_before_credits"))
    rate, _ = d400.rate(year)
    assert v["nc_income_tax_before_credits"] == pytest.approx(
        v["nc_taxable_income"] * rate, abs=0.01
    )


def test_rate_falls_back_and_says_so() -> None:
    rate, why = d400.rate(2027)
    assert rate == d400.RATE[2026][0] and "2027 not on file" in why
    with pytest.raises(ValueError):
        d400.rate(2020)
