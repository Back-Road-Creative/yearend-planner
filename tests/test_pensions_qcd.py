"""Unit 3f-1: a 1099-R from a plan or annuity is a pension (Form 1040 lines
5a-5b), one from an IRA an IRA distribution (4a-4b), told apart by box 7b
(IRA/SEP/SIMPLE) and the box 7a code (2026 Instructions for Forms 1099-R and
5498); qualified charitable distributions come out of the taxable IRA amount
at 70 1/2 or older, up to the year's limit (Pub. 590-B; 2025 Form 1040
instructions, line 4, Exception 3). Synthetic figures only."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from planner.ingest.needs import (
    NEEDS,
    enter,
    need_value,
    need_values,
    needed,
    retirement_kind,
)
from planner.ingest.pdf import parse_texts
from planner.ledger import db
from planner.paths import Layout
from planner.plan import inputs
from planner.taxprep import draft
from tests.test_forms import F1099R, TEMPLATES

IRA = "IRA Custodian (synthetic)"

PAGE_2026 = [
    F1099R[0],
    "Tax year 2026",
    "PAYER'S name: Example Plan (synthetic)",
    "1 Gross distribution $ 9,000.00",
    "2a Taxable amount $ 9,000.00",
]


@pytest.mark.parametrize(
    ("last", "code", "ira"),
    [
        ("7a Dist. code(s) 7 7b IRA/SEP/SIMPLE X", "7", "yes"),
        ("7a Dist. code(s) 7 7b [X] IRA/SEP/SIMPLE", "7", "yes"),
        ("7a Dist. code(s) 7 7b IRA/SEP/SIMPLE", "7", "no"),
        ("7a Dist. code(s) Y7 7b IRA/SEP/SIMPLE X", "Y7", "yes"),
        ("7 Distribution code(s) G IRA/SEP/SIMPLE", "G", "no"),
        ("7 Distribution code(s) 7 IRA/ SEP/ SIMPLE X", "7", "yes"),
        ("7 Distribution code(s) 7", "7", None),
    ],
)
def test_box_7a_and_7b_read_on_both_layouts(
    last: str, code: str, ira: str | None
) -> None:
    (r,) = parse_texts(["\n".join([*PAGE_2026, last])], TEMPLATES)
    assert r.boxes["7"][1] == code
    assert (r.boxes["7b"][1] if "7b" in r.boxes else None) == ira


def _row(doc: int, box: str, text: str) -> db.FactRow:
    return db.FactRow(
        "1099-R",
        2025,
        f"Payer {doc} (synthetic)",
        box,
        "",
        0.0,
        1,
        text=text,
        document_id=doc,
    )


def test_ira_or_plan_by_7b_then_code_then_ira() -> None:
    facts = [
        _row(1, "7b", "yes"),
        _row(2, "7b", "no"),
        _row(3, "7", "G"),  # ambiguous code, no 7b read: an IRA, as before 3f-1
        _row(4, "7", "7D"),  # a plan's code (D: annuity payments, nonqualified)
        _row(5, "7", "Y7"),  # QCD: only from an IRA
        _row(5, "7b", "no"),
    ]
    assert retirement_kind(facts) == {
        1: "ira",
        2: "plan",
        3: "ira",
        4: "plan",
        5: "ira",
    }


def _r(
    issuer: str, box1: float, box2a: float, ira: str | None, code: str = "7"
) -> list[db.Fact]:
    out = [
        db.Fact("1099-R", 2025, issuer, "1", "", box1, 1),
        db.Fact("1099-R", 2025, issuer, "2a", "", box2a, 1),
        db.Fact("1099-R", 2025, issuer, "7", "", 0.0, 1, text=code),
    ]
    if ira is not None:
        out.append(db.Fact("1099-R", 2025, issuer, "7b", "", 0.0, 1, text=ira))
    return out


def _lay(home: Path, docs: list[list[db.Fact]], born: str = "1950-03-10") -> Layout:
    lay = Layout(home)
    lay.ensure()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    for i, facts in enumerate(docs):
        db.add_document(
            conn,
            fingerprint=f"synthetic-r-{i}",
            file_name=f"r{i}.pdf",
            kind="pdf",
            pages=1,
            batch="b1",
            facts=facts,
            owner="you",
        )
    conn.close()
    for key, text in (
        ("birth_date", born),
        ("filing_status", "single"),
        ("state", "TX"),
    ):
        enter(lay, 2025, key, text)
    return lay


def _lines(d: draft.Draft, *names: str) -> dict[str, float]:
    return {x.line: x.value for x in d.lines if x.form == "1040" and x.line in names}


def test_pension_and_ira_land_on_their_own_lines(planner_home: Path) -> None:
    lay = _lay(
        planner_home,
        [
            _r(IRA, 20000.0, 20000.0, "yes"),
            _r("Pension Plan (synthetic)", 35000.0, 30000.0, "no"),
        ],
    )
    conn = db.connect(lay.data / "ledger" / "planner.db")
    got = need_values(conn, lay, 2025, ("ira_distributions", "pension_income"))
    conn.close()
    assert got == {"ira_distributions": 20000.0, "pension_income": 30000.0}
    d = draft.build(lay, 2025)
    lines = _lines(d, "4a", "4b", "4c", "5a", "5b", "9", "11a")
    assert lines["4a"] == lines["4b"] == 20000.0 and "4c" not in lines
    assert lines["5a"] == 35000.0 and lines["5b"] == 30000.0
    assert lines["9"] == lines["11a"] == 50000.0


def test_fully_taxable_pension_has_no_5a(planner_home: Path) -> None:
    lay = _lay(planner_home, [_r("Pension Plan (synthetic)", 30000.0, 30000.0, "no")])
    lines = _lines(draft.build(lay, 2025), "4b", "5a", "5b")
    assert lines == {"4b": 0.0, "5b": 30000.0}


def test_qcd_comes_out_of_4b_and_shows_on_4c(planner_home: Path) -> None:
    lay = _lay(planner_home, [_r(IRA, 20000.0, 20000.0, "yes", "Y7")])
    conn = db.connect(lay.data / "ledger" / "planner.db")
    assert need_value(conn, lay, 2025, "qcd") is None  # coded Y: asked, not guessed
    conn.close()
    (st,) = [s for s in needed(lay, 2025).items if s.need.key == "qcd"]
    assert st.value is None and "coded Y" in st.origin
    enter(lay, 2025, "qcd", "5000")
    inp = inputs.build(lay, 2025)
    assert inp.qcd == {"you": 5000.0}
    assert inp.household.ira_distributions == 15000
    lines = _lines(draft.build(lay, 2025), "4a", "4b", "4c", "9")
    assert lines == {"4a": 20000.0, "4b": 15000.0, "4c": 5000.0, "9": 15000.0}


def test_qcd_capped_at_the_years_limit(planner_home: Path) -> None:
    lay = _lay(planner_home, [_r(IRA, 150000.0, 150000.0, "yes")])
    enter(lay, 2025, "qcd", "120000")
    inp = inputs.build(lay, 2025)
    assert inp.qcd == {"you": 108000.0}  # Pub. 590-B: the 2025 limit
    assert any("over the 2025 limit" in n for n in inp.notes)


def test_no_qcd_before_70_and_a_half(planner_home: Path) -> None:
    young = _lay(
        planner_home,
        [_r(IRA, 9000.0, 9000.0, "yes")],
        born="1955-08-31",
    )
    enter(young, 2025, "qcd", "4000")
    inp = inputs.build(young, 2025)
    assert inp.qcd == {} and inp.household.ira_distributions == 9000
    assert any("2026-02-28" in n for n in inp.notes)


def test_qcd_reduced_by_this_years_ira_deduction_at_70_and_a_half(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home, [_r(IRA, 20000.0, 20000.0, "yes")])
    enter(lay, 2025, "qcd", "5000")
    enter(lay, 2025, "traditional_ira_contribution", "2000")
    inp = inputs.build(lay, 2025)
    assert inp.qcd == {"you": 3000.0}
    assert any("QCD Adjustment Worksheet" in n for n in inp.notes)


@pytest.mark.parametrize(
    ("born", "half"),
    [
        ("1955-08-31", date(2026, 2, 28)),
        ("1954-06-30", date(2024, 12, 30)),
        ("1955-01-15", date(2025, 7, 15)),
    ],
)
def test_half_birthday(born: str, half: date) -> None:
    assert inputs.half_birthday(born, 70) == half


def test_spouse_qcd_asked_only_with_the_spouses_ira() -> None:
    (asked,) = [n.asked for n in NEEDS if n.key == "spouse_qcd"]
    joint = {"filing_status": "married_joint", "spouse_birth_date": "1950-01-01"}
    assert asked is not None
    assert not asked({**joint, "ira_distributions": 5000})
    assert asked({**joint, "spouse_ira_distributions": 5000})
