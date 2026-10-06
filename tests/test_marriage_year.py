"""Phase 10, unit 3b-4: Form 8962's alternative calculation for the year of
marriage (2025 Instructions for Form 8962, Table 4 and Worksheet 3; Pub. 974,
Alternative Calculation for Year of Marriage, Worksheets I-V and Steps 1-8).

The oracle is Pub. 974's own example, Paulette Oak and Quentin Cedar: married
July 18, 2025, household income $117,000, tax family of 4 (line 4 $31,200,
line 5 375%, line 7 0.0788, line 8b $768). Each had their own policy January
to July, Quentin's covering his two children; one joint policy August to
December. Combined, January-July is $1,500 premium, $1,266 SLCSP, $794
advance; August-December $1,350, $1,167, $573. Without the election line 24 is
$5,481 and the excess $2,942; with it Paulette's alternative contribution is
$400 (family of 1), Quentin's $148 (family of 3), Worksheet V is $5,026
against $3,486, line 24 is $7,021 and line 29 $1,402. Pub. 974 gives only the
combined monthly amounts, so the split between the two January-July policies
below is synthetic, chosen to reproduce them. Synthetic households only."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from planner.engine import tax
from planner.ingest.needs import enter, need_for, needed, parse_value
from planner.ledger import db
from planner.paths import Layout
from planner.plan import inputs
from planner.taxprep import draft

YEAR = 2025
OAK = {
    "tax_unit_fpg@prior": 31_200.0,
    "aca_magi": 117_000.0,
    "aca_magi_fraction": 3.75,
    "aca_required_contribution_percentage": 0.07875,
    "tax_unit_size": 4.0,
    "is_aca_ptc_eligible": 1.0,
}
# owner, first month, last month, premium, SLCSP, advance
PAULETTE = ("you", 1, 7, 500.0, 500.0, 300.0)
QUENTIN = ("spouse", 1, 7, 1_000.0, 766.0, 494.0)
JOINT = ("you", 8, 12, 1_350.0, 1_167.0, 573.0)


def _facts(*policies: tuple[str, int, int, float, float, float]) -> list[db.FactRow]:
    out = []
    for n, (owner, first, last, premium, slcsp, aptc) in enumerate(policies):
        for m in range(first, last + 1):
            for box, value in (("premium", premium), ("slcsp", slcsp), ("aptc", aptc)):
                out.append(
                    db.FactRow(
                        "1095-A",
                        YEAR,
                        f"Marketplace {n} (synthetic)",
                        f"{box}_{m:02d}",
                        box,
                        value,
                        1,
                        owner=owner,
                    )
                )
    return out


def _run(
    facts: list[db.FactRow],
    married: draft.Marriage | None,
    fs: str = "JOINT",
) -> tuple[draft.Draft, tuple[float, float]]:
    d = draft.Draft(YEAR, "synthetic")
    got = draft._form_8962(draft._Sheet(d), d, OAK, facts, fs, married)
    return d, got


OAK_MARRIAGE = draft.Marriage(date(2025, 7, 18), spouse_kids=2, kids=2, state="NC")


def test_the_poverty_line_for_any_family_size() -> None:
    """i8962 Table 1-1 (the 2024 guidelines): $31,200 for 4 in the 48 states,
    $15,060 for 1 and $25,820 for 3 (Pub. 974 Worksheets I and III, line 3)."""
    assert tax.poverty_line(YEAR, 4, "NC") == 31_200
    assert tax.poverty_line(YEAR, 1, "NC") == 15_060
    assert tax.poverty_line(YEAR, 3, "NC") == 25_820
    assert tax.poverty_line(YEAR, 1, "AK") == 18_810


@pytest.mark.parametrize(
    ("year", "pct", "figure"),
    [
        (2025, 375, 0.0788),  # Pub. 974 example, line 7
        (2025, 388, 0.0820),  # Paulette's Worksheet I
        (2025, 226, 0.0304),  # Quentin's Worksheet III
        (2025, 140, 0.0),
        (2025, 401, 0.085),  # no 400% line through 2025
        (2026, 401, None),  # from 2026 no credit over 400%
    ],
)
def test_the_applicable_figure_at_a_whole_percent(
    year: int, pct: int, figure: float | None
) -> None:
    assert tax.applicable_figure(year, pct) == figure


def test_the_pub_974_example_elects_the_alternative_calculation() -> None:
    d, (net, repay) = _run(_facts(PAULETTE, QUENTIN, JOINT), OAK_MARRIAGE)
    assert d.get("8962", "8b") == 768
    for line, value in (("35a", 1), ("35b", 400), ("35c", 1), ("35d", 7)):
        assert d.get("8962", line) == value, line
    for line, value in (("36a", 3), ("36b", 148), ("36c", 1), ("36d", 7)):
        assert d.get("8962", line) == value, line
    for ln in range(12, 19):  # January to July: Worksheets II + IV, Worksheet V
        assert d.get("8962", f"{ln}c") == 548
        assert d.get("8962", f"{ln}e") == 718
        assert d.get("8962", f"{ln}f") == 794
    for ln in range(19, 24):  # married all month: the regular calculation
        assert d.get("8962", f"{ln}e") == 399
        assert d.get("8962", f"{ln}f") == 573
    assert d.get("8962", "24") == 7_021
    assert d.get("8962", "25") == 8_423
    assert d.get("8962", "26") == 0
    assert d.get("8962", "27") == 1_402
    assert d.get("8962", "28") == 3_250
    assert (net, repay) == (0.0, 1_402)
    note = next(n for n in d.notes if "year of marriage" in n)
    assert "2,942" in note and "1,402" in note and "line 9" in note


def test_without_the_marriage_the_regular_calculation_stands() -> None:
    d, (net, repay) = _run(_facts(PAULETTE, QUENTIN, JOINT), None)
    assert d.get("8962", "24") == 5_481
    assert d.get("8962", "35a") is None
    assert (net, repay) == (0.0, 2_942)


@pytest.mark.parametrize(
    "married",
    [
        draft.Marriage(date(2025, 1, 1), spouse_kids=2, kids=2, state="NC"),
        draft.Marriage(date(2024, 7, 18), spouse_kids=2, kids=2, state="NC"),
    ],
    ids=["married-on-january-1", "married-the-year-before"],
)
def test_a_couple_married_on_january_1_is_not_eligible(
    married: draft.Marriage,
) -> None:
    """Table 4 question 1: each unmarried on January 1 of the year."""
    d, (_, repay) = _run(_facts(PAULETTE, QUENTIN, JOINT), married)
    assert d.get("8962", "35a") is None
    assert repay == 2_942


def test_a_separate_return_is_not_eligible() -> None:
    """Table 4 question 3: filing a joint return."""
    d, _ = _run(_facts(PAULETTE, QUENTIN, JOINT), OAK_MARRIAGE, fs="SEPARATE")
    assert d.get("8962", "35a") is None


def test_no_coverage_before_the_first_full_month_is_not_eligible() -> None:
    """Table 4 question 4: married July 1, so July is the first full month;
    coverage only from July is not before it."""
    late = ("you", 7, 12, 1_350.0, 1_167.0, 900.0)
    married = draft.Marriage(date(2025, 7, 1), spouse_kids=0, kids=0, state="NC")
    d, _ = _run(_facts(late), married)
    assert d.get("8962", "35a") is None


def test_no_excess_advance_is_not_eligible() -> None:
    """Worksheet 3 line 14: the regular credit is not below the advance."""
    small = [(o, f, la, p, s, 0.0) for o, f, la, p, s, _ in (PAULETTE, QUENTIN)]
    d, (net, repay) = _run(_facts(*small), OAK_MARRIAGE)
    assert d.get("8962", "35a") is None
    assert net > 0 and repay == 0


def test_an_alternative_no_larger_is_not_elected() -> None:
    """Worksheet V line 14: premiums below the credit leave the credit at the
    premiums either way, so column A is not more than column B."""
    cheap = ("you", 1, 7, 50.0, 1_200.0, 300.0), ("spouse", 1, 7, 100.0, 1_200.0, 300.0)
    d, (_, repay) = _run(_facts(*cheap), OAK_MARRIAGE)
    assert d.get("8962", "35a") is None
    assert d.get("8962", "24") == 7 * 150
    assert repay == 7 * 600 - 7 * 150
    assert any("does not lower" in n for n in d.notes), d.notes


def _home(home: Path, married: str, spouse_kids: str | None = None) -> Layout:
    lay = Layout(home)
    lay.ensure()
    for key, text in (
        ("birth_date", "1980-04-01"),
        ("filing_status", "married_joint"),
        ("spouse_birth_date", "1982-09-30"),
        ("state", "NC"),
        ("wages", "0"),
        ("se_income", "60,000"),
        ("ordinary_dividends", "0"),
        ("qualified_dividends", "0"),
        ("dependents", "2015-05-05, 2017-08-08"),
        ("spouse_death_date", "none"),
        ("marriage_date", married),
    ):
        enter(lay, YEAR, key, text)
    if spouse_kids is not None:
        enter(lay, YEAR, "spouse_premarriage_dependents", spouse_kids)
    return lay


def test_a_joint_filer_is_asked_the_marriage_date(planner_home: Path) -> None:
    lay = _home(planner_home, "none")
    asked_for = need_for("marriage_date").asked
    assert asked_for is not None
    assert asked_for({"filing_status": "married_joint"})
    assert not asked_for({"filing_status": "single"})
    asked = {s.need.key: s.state for s in needed(lay, YEAR).items}
    assert asked["marriage_date"] != "missing"
    assert "spouse_premarriage_dependents" not in asked


def test_a_marriage_asks_whose_the_children_were(planner_home: Path) -> None:
    lay = _home(planner_home, "2025-07-18")
    asked = {s.need.key: s.state for s in needed(lay, YEAR).items}
    assert asked["spouse_premarriage_dependents"] == "missing"
    assert parse_value(need_for("marriage_date"), "None") == "none"
    assert parse_value(need_for("marriage_date"), "2025-07-18") == "2025-07-18"
    assert parse_value(need_for("spouse_premarriage_dependents"), "2") == 2


def test_the_marriage_reaches_the_inputs(planner_home: Path) -> None:
    inp = inputs.build(_home(planner_home, "2025-07-18", "1"), YEAR)
    assert (inp.married, inp.spouse_kids) == ("2025-07-18", 1)
    later = inputs.build(_home(planner_home, "2024-07-18", "1"), YEAR)
    assert later.married is None


def test_more_of_the_spouses_children_than_dependents_is_capped(
    planner_home: Path,
) -> None:
    inp = inputs.build(_home(planner_home, "2025-07-18", "5"), YEAR)
    assert inp.spouse_kids == 2
    assert any("spouse_premarriage_dependents" in n for n in inp.notes), inp.notes


def test_the_draft_return_runs_the_year_of_marriage(planner_home: Path) -> None:
    lay = _home(planner_home, "2025-07-18", "0")
    conn = db.connect(lay.data / "ledger" / "planner.db")
    for owner, policy in (
        ("you", ("you", 1, 7, 500.0, 600.0, 600.0)),
        ("spouse", ("spouse", 1, 7, 500.0, 600.0, 600.0)),
    ):
        db.add_document(
            conn,
            fingerprint=f"synthetic-{owner}",
            file_name=f"{owner}.pdf",
            kind="pdf",
            pages=1,
            batch="b1",
            facts=list(_facts(policy)),
            owner=owner,
        )
    conn.close()
    d = draft.build(lay, YEAR)
    assert any("year of marriage" in n for n in d.notes), d.notes
