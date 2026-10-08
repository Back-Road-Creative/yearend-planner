"""Phase 10 unit 3a-7: on a joint return each spouse with an HSA has their own
Form 8889, from the documents marked theirs. Synthetic 2025 households only.
2025 Instructions for Form 8889: "Complete a separate Form 8889 for each
spouse" and add both line 13s on Schedule 1 line 13; if either spouse has
family HDHP coverage both are treated as having it (Part I); spouses with
separate HSAs divide the family limit equally unless they agree otherwise
(line 6); each spouse 55 or older adds their own $1,000 (line 7). 2025 family
limit $8,550 (Rev. Proc. 2024-25)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest.needs import enter
from planner.ledger import db
from planner.paths import Layout
from planner.taxprep import draft, hsa

YEAR = 2025
runner = CliRunner()


def _doc(
    lay: Layout, name: str, owner: str, facts: list[tuple[str, str, float]]
) -> None:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint=f"synthetic-{name}",
        file_name=f"{name}.pdf",
        kind="pdf",
        pages=1,
        batch=name,
        owner=owner,
        facts=[
            db.Fact(form, YEAR, f"{form} issuer (synthetic)", box, "", v, 1)
            for form, box, v in facts
        ],
    )
    conn.close()


def _joint(home: Path, spouse_hsa: bool = True, **typed: str) -> Layout:
    lay = Layout(home)
    lay.ensure()
    base = {
        "state": "NC",
        "birth_date": "1980-05-01",  # 45 at the end of 2025: no catch-up
        "filing_status": "married_joint",
        "spouse_birth_date": "1968-02-01",  # 57: their own $1,000 catch-up
        "se_income": "0",
        "ordinary_dividends": "0",
        "qualified_dividends": "0",
        "dependents": "none",
        "hsa_coverage": "family",
        "spouse_hsa_coverage": "self",  # treated as family: the head's plan is
    }
    for key, text in {**base, **typed}.items():
        enter(lay, YEAR, key, text)
    _doc(
        lay,
        "head",
        "you",
        [
            ("W-2", "1", 60_000.0),
            ("W-2", "2", 5_000.0),
            ("W-2", "12W", 1_000.0),
            ("5498-SA", "2", 4_000.0),  # 3,000 of theirs + the 1,000 code W
        ],
    )
    if spouse_hsa:
        _doc(
            lay,
            "spouse",
            "spouse",
            [("5498-SA", "2", 4_000.0), ("1099-SA", "1", 500.0)],
        )
    return lay


def _build(lay: Layout, who: str = "you") -> hsa.HSA:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        return hsa.build(conn, lay, YEAR, who)
    finally:
        conn.close()


def test_each_spouse_has_their_own_form_8889(planner_home: Path) -> None:
    lay = _joint(planner_home, spouse_hsa_qualified_expenses="300")
    you, sp = _build(lay), _build(lay, "spouse")
    assert {k: you.lines[k] for k in ("3", "6", "7", "8", "9", "2", "12", "13")} == {
        "3": 8550.0,
        "6": 4275.0,
        "7": 0.0,
        "8": 4275.0,
        "9": 1000.0,
        "2": 3000.0,
        "12": 3275.0,
        "13": 3000.0,
    }
    assert "14a" not in you.lines  # the spouse's 1099-SA is on their form only
    assert sp.lines == {
        "2": 4000.0,
        "3": 8550.0,
        "6": 4275.0,
        "7": 1000.0,
        "8": 5275.0,
        "9": 0.0,
        "12": 5275.0,
        "13": 4000.0,
        "14a": 500.0,
        "14c": 500.0,
        "15": 300.0,
        "16": 200.0,
        "17b": 40.0,
    }
    assert "family" in sp.sources["3"] and "spouse" in sp.sources["6"]
    assert not [n for n in you.notes + sp.notes if "split" in n], you.notes + sp.notes


def test_an_agreed_split_gives_one_spouse_the_whole_family_limit(
    planner_home: Path,
) -> None:
    lay = _joint(planner_home, hsa_family_share="100")
    you, sp = _build(lay), _build(lay, "spouse")
    assert (you.lines["6"], sp.lines["6"]) == (8550.0, 0.0)
    assert (sp.lines["7"], sp.lines["12"], sp.lines["13"]) == (1000.0, 1000.0, 1000.0)
    assert any("3,000.00 went into the HSA over" in n for n in sp.notes), sp.notes


def test_with_no_spouse_hsa_on_file_line_6_takes_the_whole_limit(
    planner_home: Path,
) -> None:
    lay = _joint(planner_home, spouse_hsa=False, spouse_hsa_coverage="none")
    you, sp = _build(lay), _build(lay, "spouse")
    assert you.lines["6"] == 8550.0
    assert sp.lines == {}
    assert any("planner owner" in n for n in you.notes), you.notes


def test_the_draft_adds_both_forms(planner_home: Path) -> None:
    d = draft.build(_joint(planner_home, spouse_hsa_qualified_expenses="300"), YEAR)
    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes
    assert d.get(hsa.SPOUSE_FORM, "13") == 4000.0
    assert d.get("8889", "13") == 3000.0
    assert d.get("Sch 1", "13") == 7000.0
    assert d.get("Sch 1", "8f") == 200.0
    assert d.get("Sch 2", "17c") == 40.0


def test_planner_hsa_shows_the_spouses_form(planner_home: Path) -> None:
    _joint(planner_home)
    out = runner.invoke(app, ["hsa", "--year", str(YEAR)])
    assert out.exit_code == 0, out.output
    assert "the spouse's" in out.output and "line 7" in out.output


@pytest.mark.parametrize(
    ("key", "text"), [("spouse_hsa_months", "13"), ("hsa_family_share", "101")]
)
def test_spouse_hsa_answers_are_bounded(
    planner_home: Path, key: str, text: str
) -> None:
    lay = _joint(planner_home)
    with pytest.raises(ValueError):
        enter(lay, YEAR, key, text)


def test_a_spouses_self_employment_loss_can_be_typed(planner_home: Path) -> None:
    lay = _joint(planner_home)
    enter(lay, YEAR, "spouse_se_income", "-2,000")
