"""Phase 10, unit 3c-3: Form 8880, the credit for qualified retirement savings
contributions (the saver's credit; the 2025 Form 8880 carries its own
instructions), from the typed IRA, Roth IRA and ABLE contributions, the W-2 box
12 elective deferrals and the testing period's distributions.

Oracles: the form's line 9 table, checked at every edge (married filing jointly
$47,500 / $51,000 / $79,000, head of household $35,625 / $38,250 / $59,250, every
other status $23,750 / $25,500 / $39,500; a rate holds up to and including its
top), the $2,000 cap per person (line 6), line 4's distributions coming off the
contributions (line 5), the age, student and dependent bars, and the Credit Limit
Worksheet (line 11, the tax on 1040 line 18 less Schedule 3 lines 1-3).
Synthetic households only."""

# ruff: noqa: E501  (fixture lines mirror the printed forms verbatim)

from __future__ import annotations

from pathlib import Path

import pytest

from planner.engine import tax
from planner.engine.household import Dependent, Household, Person
from planner.ingest.needs import enter, need_for, needed
from planner.ingest.pdf import load_templates, parse_texts
from planner.paths import Layout
from planner.plan import inputs
from planner.taxprep import draft

YEAR = 2025
SAVERS = ("savers_credit", "savers_credit_potential")
TEMPLATES = load_templates(
    Path(__file__).resolve().parent.parent / "templates" / "forms"
)
# 2025 Form 8880 line 9: (top AGI, decimal) per filing status.
TABLE = {
    "JOINT": [(47_500, 0.5), (51_000, 0.2), (79_000, 0.1)],
    "HEAD_OF_HOUSEHOLD": [(35_625, 0.5), (38_250, 0.2), (59_250, 0.1)],
    "SINGLE": [(23_750, 0.5), (25_500, 0.2), (39_500, 0.1)],
    "SEPARATE": [(23_750, 0.5), (25_500, 0.2), (39_500, 0.1)],
    "SURVIVING_SPOUSE": [(23_750, 0.5), (25_500, 0.2), (39_500, 0.1)],
}


def _values(**kw: object) -> dict[str, float]:
    base: dict[str, object] = {
        "age": 30,
        "filing_status": "SINGLE",
        "state": "TX",
        "wages": 20_000,
    }
    return tax.values(YEAR, Household(**(base | kw)), SAVERS)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", sorted(TABLE))
def test_the_line_9_table_is_the_forms(status: str) -> None:
    assert tax.savers_credit_table(YEAR, status) == TABLE[status]
    assert tax.savers_credit_cap(YEAR) == 2_000


@pytest.mark.parametrize("status", sorted(TABLE))
def test_the_engine_rate_at_every_table_edge(status: str) -> None:
    """$2,000 of Roth IRA contributions per person at each top and a dollar
    past it (AGI is all wages, so line 8 is the wages)."""
    joint = status == "JOINT"
    kw: dict[str, object] = {"filing_status": status}
    if joint:
        kw["spouse"] = Person(age=30, roth_ira_contribution=2_000)
    if status in ("HEAD_OF_HOUSEHOLD", "SURVIVING_SPOUSE"):
        kw["dependents"] = (Dependent(age=10),)
    base = 4_000 if joint else 2_000
    rates = [r for _, r in TABLE[status]] + [0.0]
    for (top, rate), after in zip(TABLE[status], rates[1:], strict=True):
        for agi, want in ((top, rate), (top + 1, after)):
            v = _values(wages=agi, roth_ira_contribution=2_000, **kw)
            assert v["savers_credit_potential"] == pytest.approx(base * want), agi


def test_the_cap_the_distributions_and_the_bars() -> None:
    """Line 6 caps each person at $2,000; line 4 comes off line 3 first; the
    year's IRA distributions are always on line 4; a barred person gets none."""
    v = _values(traditional_ira_contribution=1_500, elective_deferrals=3_000)
    assert v["savers_credit_potential"] == pytest.approx(1_000)  # 2,000 x 0.5
    v = _values(roth_ira_contribution=2_000, savers_distributions=1_500)
    assert v["savers_credit"] == pytest.approx(250)  # (2,000 - 1,500) x 0.5
    v = _values(roth_ira_contribution=2_000, ira_distributions=1_800)
    assert v["savers_credit"] == pytest.approx(100)  # (2,000 - 1,800) x 0.5
    v = _values(roth_ira_contribution=2_000, savers_eligible=False)
    assert v["savers_credit_potential"] == 0
    v = _values(age=17, roth_ira_contribution=2_000)  # the engine's own age test
    assert v["savers_credit_potential"] == 0


def test_the_credit_limit_is_the_tax() -> None:
    """$20,000 of wages less the $15,750 standard deduction is $425 of tax, so
    the $1,000 credit stops at $425 (Credit Limit Worksheet)."""
    v = _values(roth_ira_contribution=1_000, elective_deferrals=3_000)
    assert v["savers_credit_potential"] == pytest.approx(1_000)
    assert v["savers_credit"] == pytest.approx(425)


def test_the_savers_questions() -> None:
    roth = need_for("roth_ira_contribution")
    assert roth.asked is None and ("5498", "10") in roth.boxes
    deferrals = need_for("elective_deferrals")
    assert {b for _, b in deferrals.boxes} == {
        f"12{c}" for c in ("D", "E", "F", "G", "H", "S", "AA", "BB", "EE")
    }
    for key in ("savers_distributions", "savers_barred"):
        asked = need_for(key).asked
        assert asked is not None
        assert asked({"roth_ira_contribution": 500})
        assert asked({"elective_deferrals": 1})
        assert asked({"traditional_ira_contribution": 1})
        assert not asked({"roth_ira_contribution": 0, "elective_deferrals": 0})
        assert not asked({})


def _home(home: Path, **extra: str) -> Layout:
    lay = Layout(home)
    lay.ensure()
    for key, text in {
        "birth_date": "1990-04-01",
        "filing_status": "single",
        "state": "NC",
        "wages": "20,000",
        "traditional_ira_contribution": "0",
        "roth_ira_contribution": "1,000",
        "elective_deferrals": "3,000",
        "savers_distributions": "0",
        "savers_barred": "no",
        **extra,
    }.items():
        enter(lay, YEAR, key, text)
    return lay


def test_the_draft_carries_form_8880(planner_home: Path) -> None:
    lay = _home(planner_home)
    d = draft.build(lay, YEAR)
    assert [d.get("8880", f"{n}(a)") for n in range(1, 7)] == [
        1_000,
        3_000,
        4_000,
        0,
        4_000,
        2_000,
    ]
    assert d.get("8880", "7") == 2_000 and d.get("8880", "8") == 20_000
    assert d.get("8880", "9") == 0.5 and d.get("8880", "10") == 1_000
    # 1040 line 16 is the Tax Table's $428 on $4,250 (the engine's exact $425).
    assert d.get("8880", "11") == pytest.approx(428)
    assert d.get("8880", "12") == pytest.approx(428)
    assert d.get("Sch 3", "4") == pytest.approx(428)
    assert d.get("1040", "22") == 0
    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes
    assert "Form 8880 (credit for qualified retirement savings" in draft.render(d)


def test_the_draft_joint_columns(planner_home: Path) -> None:
    """Both spouses' columns: $2,500 capped at $2,000 and $1,000; AGI $40,000
    is 0.5, so $1,500, held to the Tax Table's $853 on $8,500."""
    lay = _home(
        planner_home,
        filing_status="married_joint",
        wages="30,000",
        roth_ira_contribution="2,500",
        elective_deferrals="0",
        spouse_birth_date="1991-05-01",
        spouse_wages="10,000",
        spouse_roth_ira_contribution="0",
        spouse_elective_deferrals="1,000",
        spouse_savers_distributions="0",
        spouse_savers_barred="no",
    )
    d = draft.build(lay, YEAR)
    assert d.get("8880", "6(a)") == 2_000 and d.get("8880", "6(b)") == 1_000
    assert d.get("8880", "10") == pytest.approx(1_500)
    assert d.get("8880", "12") == pytest.approx(853)
    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes


def test_a_barred_filer_and_one_under_18(planner_home: Path) -> None:
    lay = _home(planner_home, savers_barred="yes")
    assert inputs.build(lay, YEAR).household.savers_eligible is False
    d = draft.build(lay, YEAR)
    assert d.get("8880", "6(a)") == 0 and d.get("8880", "12") == 0
    assert not [n for n in d.notes if n.startswith("CHECK")], d.notes
    # Born on January 2, 2008: not yet 18 for the credit (born after January 1, 2008).
    lay = _home(planner_home, savers_barred="no", birth_date="2008-01-02")
    assert inputs.build(lay, YEAR).household.savers_eligible is False
    lay = _home(planner_home, birth_date="2008-01-01")
    assert inputs.build(lay, YEAR).household.savers_eligible is True


def test_the_answers_are_tax_items(planner_home: Path) -> None:
    lay = Layout(planner_home)
    lay.ensure()
    for key, text in (
        ("birth_date", "1990-04-01"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("wages", "20,000"),
    ):
        enter(lay, YEAR, key, text)
    unknown = inputs.build(lay, YEAR).tax_unknown
    assert {"roth_ira_contribution", "elective_deferrals"} <= set(unknown)
    states = {s.need.key: s.state for s in needed(lay, YEAR).items}
    assert "savers_barred" not in states  # nothing saved yet
    lay = _home(planner_home, roth_ira_contribution="0", elective_deferrals="0")
    assert not [ln for ln in draft.build(lay, YEAR).lines if ln.form == "8880"]


def test_the_w2_box_12_deferral_codes() -> None:
    w2 = "\n".join(
        [
            "Form W-2 Wage and Tax Statement 2025",
            "c Employer's name, address, and ZIP code: Example Employer Inc (synthetic)",
            "1 Wages, tips, other compensation $ 30,000.00",
            "2 Federal income tax withheld $ 2,400.00",
            "12a Code D $ 3,000.00",
            "12b Code AA $ 500.00",
            "12c Code W $ 1,200.00",
        ]
    )
    (w,) = parse_texts([w2], TEMPLATES)
    assert w.boxes["12D"][1] == 3_000.0
    assert w.boxes["12AA"][1] == 500.0
    assert w.boxes["12W"][1] == 1_200.0
    assert "12E" not in w.boxes
