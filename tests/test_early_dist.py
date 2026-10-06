"""Unit 3f-5: Form 5329 Part I, the additional tax on early distributions
(2025 Form 5329 and its instructions; 2026 Instructions for Forms 1099-R and
5498, box 7 codes 1, 2 and S). 10% of a code 1 distribution, 25% of a SIMPLE
IRA's coded S, nothing on a code 2 (a governmental 457(b) not attributable to
a rollover), less the typed exceptions, to Schedule 2 line 8. Synthetic
figures only."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.ingest.needs import enter, needed
from planner.ledger import db
from planner.paths import Layout
from planner.plan import inputs
from planner.taxprep import draft, f5329
from tests.test_pensions_qcd import _lay, _r

IRA = "IRA Custodian (synthetic)"
PLAN = "Employer Plan (synthetic)"
SIMPLE = "SIMPLE Custodian (synthetic)"
GOV457 = "County 457(b) Plan (synthetic)"
YOUNG = "1985-04-20"  # 40 in 2025: every distribution early


def _sch2(d: draft.Draft) -> dict[str, float]:
    return {x.line: x.value for x in d.lines if x.form == "Sch 2"}


def _form(d: draft.Draft, form: str = f5329.FORM) -> dict[str, float]:
    return {x.line: x.value for x in d.lines if x.form == form}


def _spouse_doc(lay: Layout, facts: list[db.Fact]) -> None:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-spouse-r",
        file_name="spouse-r.pdf",
        kind="pdf",
        pages=1,
        batch="b2",
        facts=facts,
        owner="spouse",
    )
    conn.close()


def test_parse() -> None:
    assert f5329.parse("early_exception", f5329.EXAMPLE) == [
        {"number": "05", "amount": 2500.0, "simple": False},
        {"number": "19", "amount": 1000.0, "simple": True},
    ]
    assert f5329.parse("early_exception", "3 4,000") == [
        {"number": "03", "amount": 4000.0, "simple": False}
    ]
    assert f5329.parse("early_exception", "None") == []
    for bad, said in (
        ("05", "its number and the amount"),
        ("24 100", "not an exception number"),
        ("05 lots", "not a number"),
        ("05 0", "out of range"),
        ("01 500 simple", "not for IRAs"),
        ("09 6000; 09 5000", "09 is up to 10,000"),
    ):
        with pytest.raises(ValueError, match=said):
            f5329.parse("early_exception", bad)


def test_ten_percent_and_twenty_five_for_simple() -> None:
    items = [
        f5329.Early(PLAN, "plan", False, 8000.0),
        f5329.Early(SIMPLE, "ira", True, 4000.0),
    ]
    f = f5329.figure("you", items, None, aged=False)
    assert f.lines == {"1": 12000.0, "3": 12000.0, "4": 1800.0}  # 800 + 1,000
    typed = f5329.parse("early_exception", "05 3000; 19 1000 simple")
    f = f5329.figure("you", items, typed, aged=False)
    # 10% of 5,000 + 25% of 3,000
    assert f.lines == {"1": 12000.0, "2": 4000.0, "3": 8000.0, "4": 1250.0}
    assert f.number == "99"
    f = f5329.figure("you", items, f5329.parse("k", "02 8000"), aged=False)
    assert f.number == "02" and f.tax == 1000.0


def test_aged_all_year_is_exception_twelve() -> None:
    items = [f5329.Early(IRA, "ira", False, 6000.0)]
    f = f5329.figure("you", items, f5329.parse("k", "05 100"), aged=True)
    assert f.lines == {"1": 6000.0, "2": 6000.0, "3": 0.0, "4": 0.0}
    assert f.number == "12"


def test_exceptions_cannot_cover_more_than_they_apply_to() -> None:
    plan = [f5329.Early(PLAN, "plan", False, 5000.0)]
    ira = [f5329.Early(IRA, "ira", False, 5000.0)]
    for items, typed, said in (
        (plan, "05 6000", "more than the early distributions not coded S"),
        (plan, "05 100 simple", "more than the SIMPLE IRA"),
        (ira, "01 1000", "apply only to plans"),
        (plan, "08 1000", "apply only to IRAs"),
    ):
        with pytest.raises(ValueError, match=said):
            f5329.figure("you", items, f5329.parse("k", typed), aged=False)


def test_code_one_reaches_schedule_two_line_eight(planner_home: Path) -> None:
    lay = _lay(planner_home, [_r(PLAN, 10000.0, 10000.0, "no", "1")], born=YOUNG)
    (st,) = [s for s in needed(lay, 2025).items if s.need.key == "early_exception"]
    assert st.state == "missing"
    inp = inputs.build(lay, 2025)
    assert [n for n in inp.notes if "early_exception not given" in n]
    d = draft.build(lay, 2025)
    assert _form(d) == {"1": 10000.0, "3": 10000.0, "4": 1000.0}
    s2 = _sch2(d)
    assert s2["8"] == 1000.0
    assert s2["21"] == s2["4"] + s2["11"] + s2["12"] + 1000.0
    line23 = [x for x in d.lines if x.form == "1040" and x.line == "23"]
    assert line23[0].value == s2["21"]
    enter(lay, 2025, "early_exception", "03 10000")
    d = draft.build(lay, 2025)
    assert _form(d) == {"1": 10000.0, "2": 10000.0, "3": 0.0, "4": 0.0}
    assert "8" not in _sch2(d)


def test_simple_ira_in_its_first_two_years_is_twenty_five(planner_home: Path) -> None:
    lay = _lay(planner_home, [_r(SIMPLE, 4000.0, 4000.0, "yes", "S")], born=YOUNG)
    enter(lay, 2025, "early_exception", "none")
    d = draft.build(lay, 2025)
    assert _form(d)["4"] == 1000.0 and _sch2(d)["8"] == 1000.0


def test_code_two_owes_nothing(planner_home: Path) -> None:
    lay = _lay(planner_home, [_r(GOV457, 9000.0, 9000.0, "no", "2")], born=YOUNG)
    keys = {s.need.key: s for s in needed(lay, 2025).items}
    assert keys["early_exception"].state == "actual"
    assert keys["early_exception"].value == []
    d = draft.build(lay, 2025)
    assert _form(d) == {} and "8" not in _sch2(d)


def test_fifty_nine_and_a_half_all_year_owes_nothing(planner_home: Path) -> None:
    # 59 1/2 on 2024-10-20: a code 1 in 2025 is the payer's mistake (exception 12)
    lay = _lay(planner_home, [_r(IRA, 5000.0, 5000.0, "yes", "1")], born="1965-04-20")
    assert {s.need.key: s for s in needed(lay, 2025).items}[
        "early_exception"
    ].state == "actual"
    d = draft.build(lay, 2025)
    assert _form(d) == {"1": 5000.0, "2": 5000.0, "3": 0.0, "4": 0.0}


def test_turning_fifty_nine_and_a_half_in_the_year_is_noted(planner_home: Path) -> None:
    lay = _lay(planner_home, [_r(IRA, 5000.0, 5000.0, "yes", "1")], born="1966-02-10")
    inp = inputs.build(lay, 2025)
    assert [n for n in inp.notes if "59 1/2 on 2025-08-10" in n]
    assert inp.f5329[0].tax == 500.0


def test_ira_basis_share_is_not_early_income(planner_home: Path) -> None:
    lay = _lay(planner_home, [_r(IRA, 20000.0, 20000.0, "yes", "1")], born=YOUNG)
    enter(lay, 2025, "ira_basis", "basis 12000 value 80000")  # line 10 = 0.12
    enter(lay, 2025, "early_exception", "none")
    d = draft.build(lay, 2025)
    assert _form(d) == {"1": 17600.0, "3": 17600.0, "4": 1760.0}


def test_bad_exception_is_noted_and_taxed_in_full(planner_home: Path) -> None:
    lay = _lay(planner_home, [_r(PLAN, 5000.0, 5000.0, "no", "1")], born=YOUNG)
    enter(lay, 2025, "early_exception", "08 1000")
    inp = inputs.build(lay, 2025)
    assert [n for n in inp.notes if "apply only to IRAs" in n]
    assert inp.f5329[0].tax == 500.0


def test_spouse_files_their_own(planner_home: Path) -> None:
    lay = _lay(planner_home, [], born=YOUNG)
    enter(lay, 2025, "filing_status", "married_joint")
    enter(lay, 2025, "spouse_birth_date", "1987-09-12")
    _spouse_doc(lay, _r(PLAN, 6000.0, 6000.0, "no", "1"))
    keys = {s.need.key: s for s in needed(lay, 2025).items}
    assert "early_exception" not in keys  # the head took no distribution
    assert keys["spouse_early_exception"].state == "missing"
    enter(lay, 2025, "spouse_early_exception", "19 2000")
    d = draft.build(lay, 2025)
    assert _form(d) == {}
    assert _form(d, f5329.SPOUSE_FORM) == {
        "1": 6000.0,
        "2": 2000.0,
        "3": 4000.0,
        "4": 400.0,
    }
    assert _sch2(d)["8"] == 400.0
