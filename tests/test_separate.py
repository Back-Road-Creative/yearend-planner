"""Phase 10, unit 3b-2: married filing jointly against married filing
separately. Each spouse's separate return is priced from their own lines: their
own documents (``owner``) and their own Form 8889, with what no document places
split equally and said so. Rules: 2025 Form 1040 instructions, Married Filing
Separately ("If your spouse itemizes deductions, you must also itemize"); 2025
Form 8962 instructions, Allocation Situation 2 (married at year end, filing
separately, one policy: each spouse takes 50% of the advance credit, and with
no exception neither takes the credit); Pub. 555 (community property states).
Synthetic household only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.engine.tax import compute
from planner.ingest.needs import enter
from planner.ledger import db
from planner.paths import Layout
from planner.plan import inputs, levers, separate

YEAR = 2026


def _doc(conn: sqlite3.Connection, name: str, facts: list[db.Fact], owner: str) -> None:
    db.add_document(
        conn,
        fingerprint=name,
        file_name=name,
        kind="pdf",
        pages=1,
        batch="b1",
        owner=owner,
        facts=facts,
    )


def _home(planner_home: Path, *extra: tuple[str, str], state: str = "NC") -> Layout:
    home = Layout(planner_home)
    home.ensure()
    for key, text in (
        ("birth_date", "1978-06-15"),
        ("filing_status", "married_joint"),
        ("spouse_birth_date", "1980-01-01"),
        ("state", state),
        ("mortgage_interest", "12,000"),
        *extra,
    ):
        enter(home, YEAR, key, text)
    conn = db.connect(home.data / "ledger" / "planner.db")
    try:
        for who, wages, interest in (
            ("you", 90_000.0, 3_000.0),
            ("spouse", 40_000.0, 1_000.0),
        ):
            _doc(
                conn,
                f"w2-{who}.pdf",
                [db.Fact("W-2", YEAR, "Acme", "1", "Wages", wages, 1)],
                who,
            )
            _doc(
                conn,
                f"int-{who}.pdf",
                [db.Fact("1099-INT", YEAR, "Bank", "1", "Interest", interest, 1)],
                who,
            )
    finally:
        conn.close()
    return home


@pytest.mark.engine
def test_each_spouse_files_alone_on_their_own_lines(planner_home: Path) -> None:
    home = _home(planner_home)
    c = separate.compare(home, YEAR)
    you, sp = c.you.household, c.spouse.household
    assert (you.filing_status, sp.filing_status) == ("SEPARATE", "SEPARATE")
    assert you.spouse is None and sp.spouse is None
    assert (you.age, sp.age) == (48, 46)
    assert (you.wages, sp.wages) == (90_000, 40_000)
    assert (you.interest, sp.interest) == (3_000, 1_000)  # by whose 1099-INT
    assert (you.mortgage_interest, sp.mortgage_interest) == (6_000, 6_000)
    assert any("mortgage_interest" in n and "equally" in n for n in c.notes)
    joint = compute(YEAR, inputs.build(home, YEAR).household)
    assert c.joint_cost == levers.cost(joint)
    assert c.separate_cost == pytest.approx(c.you.cost + c.spouse.cost, abs=0.01)
    assert c.saving == pytest.approx(c.separate_cost - c.joint_cost, abs=0.01)
    assert c.saving > 0  # two incomes this far apart: joint is cheaper
    # one spouse itemizing binds the other: both or neither, the cheaper pair
    assert set(c.priced) == {True, False}
    assert c.separate_cost == min(c.priced.values())
    assert c.itemize == min(c.priced, key=lambda k: c.priced[k])
    for ret in (c.you, c.spouse):
        assert ret.household.tax_unit_inputs["tax_unit_itemizes"] == c.itemize
        assert ret.household.tax_unit_inputs["separate_filer_itemizes"] == c.itemize


@pytest.mark.engine
def test_a_separate_return_takes_half_the_advance_and_no_credit(
    planner_home: Path,
) -> None:
    c = separate.compare(_home(planner_home, ("aptc", "6,000")), YEAR)
    for ret in (c.you, c.spouse):
        assert ret.household.aptc == 3_000
        assert ret.result.aca_ptc == 0
        assert ret.result.aptc_repayment == pytest.approx(3_000, abs=0.01)


@pytest.mark.engine
def test_the_children_go_on_the_cheaper_return(planner_home: Path) -> None:
    c = separate.compare(_home(planner_home, ("dependents", "2018-03-02")), YEAR)
    assert c.dependents_with in ("you", "spouse")
    with_kids = c.you if c.dependents_with == "you" else c.spouse
    without = c.spouse if c.dependents_with == "you" else c.you
    assert (len(with_kids.household.dependents), len(without.household.dependents)) == (
        1,
        0,
    )
    assert len(c.priced) == 4
    assert c.separate_cost == min(c.priced.values())


def test_community_property_states_are_not_handled(planner_home: Path) -> None:
    with pytest.raises(separate.NotHandled, match="community property"):
        separate.compare(_home(planner_home, state="CA"), YEAR)


def test_only_a_joint_couple_is_compared(planner_home: Path) -> None:
    home = Layout(planner_home)
    home.ensure()
    for key, text in (
        ("birth_date", "1978-06-15"),
        ("filing_status", "single"),
        ("state", "NC"),
    ):
        enter(home, YEAR, key, text)
    with pytest.raises(separate.NotHandled, match="married_joint"):
        separate.compare(home, YEAR)


@pytest.mark.engine
def test_the_cli_shows_both_ways_to_file(planner_home: Path) -> None:
    _home(planner_home)
    out = CliRunner().invoke(app, ["separate", "--year", str(YEAR)])
    assert out.exit_code == 0, out.output
    for word in (
        "married filing jointly",
        "you, separately",
        "spouse, separately",
        "joint saves",
    ):
        assert word in out.output
