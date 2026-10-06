"""Unit 3f-3: Form 8606, Parts I and II. Basis in traditional IRAs makes a
share of each distribution and Roth conversion tax free (2025 Form 8606 and
its instructions; Pub. 590-B), and this year's nondeductible contributions come
off the IRA deduction. Synthetic figures only."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.ingest.needs import enter, needed
from planner.plan import inputs
from planner.taxprep import draft, f8606
from tests.test_pensions_qcd import _lay, _lines, _r

IRA = "IRA Custodian (synthetic)"


def test_parse() -> None:
    assert f8606.parse("ira_basis", f8606.EXAMPLE) == {
        "basis": 12000.0,
        "value": 80000.0,
        "nondeductible": 7000.0,
    }
    assert f8606.parse("ira_basis", "None") == {}
    for bad, said in (
        ("value 80000", "needs basis"),
        ("basis 1000 value", "pair each word"),
        ("basis 1000 cost 5", "not a word here"),
        ("basis 1000 basis 5", "typed twice"),
        ("basis lots", "not a number"),
        ("basis 0 nondeductible 500 late 600", "late is part of nondeductible"),
    ):
        with pytest.raises(ValueError, match=said):
            f8606.parse("ira_basis", bad)


def test_part_one_and_two_lines() -> None:
    e = f8606.parse("ira_basis", "basis 10000 nondeductible 6000 late 1000 value 85000")
    f = f8606.figure("you", e, 10000.0, 5000.0)
    assert f.lines == {
        "1": 6000.0,
        "2": 10000.0,
        "3": 16000.0,
        "4": 1000.0,
        "5": 15000.0,
        "6": 85000.0,
        "7": 10000.0,
        "8": 5000.0,
        "9": 100000.0,
        "10": 0.15,
        "11": 750.0,
        "12": 1500.0,
        "13": 2250.0,
        "14": 13750.0,
        "15a": 8500.0,
        "15c": 8500.0,
        "16": 5000.0,
        "17": 750.0,
        "18": 4250.0,
    }
    assert f.nontaxable == (1500.0, 750.0)


def test_no_distribution_carries_the_basis_and_share_tops_at_one() -> None:
    e = f8606.parse("ira_basis", "basis 3000 nondeductible 7000")
    assert f8606.figure("you", e, 0.0, 0.0).lines == {
        "1": 7000.0,
        "2": 3000.0,
        "3": 10000.0,
        "14": 10000.0,
    }
    e = f8606.parse("ira_basis", "basis 50000 value 0")
    f = f8606.figure("you", e, 0.0, 20000.0)
    assert f.lines["10"] == 1.0 and f.lines["18"] == 0.0 and f.lines["14"] == 30000.0
    with pytest.raises(ValueError, match="needs value"):
        f8606.figure("you", f8606.parse("ira_basis", "basis 5000"), 1000.0, 0.0)


def test_basis_makes_part_of_the_distribution_tax_free(planner_home: Path) -> None:
    lay = _lay(planner_home, [_r(IRA, 20000.0, 20000.0, "yes")], born="1962-03-10")
    (st,) = [s for s in needed(lay, 2025).items if s.need.key == "ira_basis"]
    assert st.state == "missing"
    inp = inputs.build(lay, 2025)
    assert inp.f8606 == [] and [n for n in inp.notes if "ira_basis not given" in n]
    enter(lay, 2025, "ira_basis", "basis 12000 value 80000")
    inp = inputs.build(lay, 2025)
    assert inp.household.ira_distributions == 17600
    d = draft.build(lay, 2025)
    assert _lines(d, "4a", "4b", "11a") == {
        "4a": 20000.0,
        "4b": 17600.0,
        "11a": 17600.0,
    }
    form = {x.line: x.value for x in d.lines if x.form == f8606.FORM}
    assert form["10"] == 0.12 and form["12"] == 2400.0 and form["14"] == 9600.0


def test_nondeductible_contribution_leaves_the_deduction(planner_home: Path) -> None:
    lay = _lay(planner_home, [_r(IRA, 20000.0, 20000.0, "yes")], born="1962-03-10")
    enter(lay, 2025, "traditional_ira_contribution", "7000")
    enter(lay, 2025, "ira_basis", "none")
    assert _lines(draft.build(lay, 2025), "11a") == {"11a": 13000.0}
    enter(lay, 2025, "ira_basis", "basis 0 nondeductible 7000 value 50000")
    inp = inputs.build(lay, 2025)
    assert inp.household.nondeductible_ira_contribution == 7000
    assert inp.household.traditional_ira_contribution == 7000  # Form 8880 line 1
    # line 10 = 7,000 / (50,000 + 20,000) = 0.1; 18,000 taxable, no deduction
    assert _lines(draft.build(lay, 2025), "4b", "11a") == {
        "4b": 18000.0,
        "11a": 18000.0,
    }


def test_conversion_share(planner_home: Path) -> None:
    lay = _lay(planner_home, [], born="1962-03-10")
    enter(lay, 2025, "roth_conversion", "30000")
    enter(lay, 2025, "ira_basis", "basis 6000 value 30000")
    inp = inputs.build(lay, 2025)
    # line 10 = 6,000 / (30,000 + 30,000) = 0.1: 3,000 of the conversion is basis
    assert inp.household.roth_conversion == 27000
    d = draft.build(lay, 2025)
    assert _lines(d, "4a", "4b") == {"4a": 30000.0, "4b": 27000.0}
    form = {x.line: x.value for x in d.lines if x.form == f8606.FORM}
    assert form["16"] == 30000.0 and form["17"] == 3000.0 and form["18"] == 27000.0


def test_missing_value_is_noted_and_taxed_in_full(planner_home: Path) -> None:
    lay = _lay(planner_home, [_r(IRA, 20000.0, 20000.0, "yes")], born="1962-03-10")
    enter(lay, 2025, "ira_basis", "basis 5000")
    inp = inputs.build(lay, 2025)
    assert inp.household.ira_distributions == 20000 and inp.f8606 == []
    assert [n for n in inp.notes if "needs value" in n]


def test_nondeductible_over_the_contribution_is_noted(planner_home: Path) -> None:
    lay = _lay(planner_home, [], born="1962-03-10")
    enter(lay, 2025, "traditional_ira_contribution", "2000")
    enter(lay, 2025, "ira_basis", "basis 0 nondeductible 5000")
    inp = inputs.build(lay, 2025)
    assert inp.household.nondeductible_ira_contribution == 2000
    assert [
        n
        for n in inp.notes
        if "is more than your 2,000 traditional IRA contribution" in n
    ]


def test_spouse_files_their_own(planner_home: Path) -> None:
    lay = _lay(planner_home, [], born="1962-03-10")
    enter(lay, 2025, "filing_status", "married_joint")
    enter(lay, 2025, "spouse_birth_date", "1961-05-20")
    asked = {s.need.key for s in needed(lay, 2025).items}
    assert "spouse_ira_basis" not in asked
    enter(lay, 2025, "spouse_ira_distributions", "10000")
    assert "spouse_ira_basis" in {s.need.key for s in needed(lay, 2025).items}
    enter(lay, 2025, "spouse_ira_basis", "basis 5000 value 40000")
    inp = inputs.build(lay, 2025)
    assert inp.household.spouse is not None
    assert inp.household.spouse.ira_distributions == 9000  # 5,000 / 50,000 = 0.1
    d = draft.build(lay, 2025)
    form = {x.line: x.value for x in d.lines if x.form == f8606.SPOUSE_FORM}
    assert form["12"] == 1000.0 and form["15c"] == 9000.0
    assert not [x for x in d.lines if x.form == f8606.FORM]
