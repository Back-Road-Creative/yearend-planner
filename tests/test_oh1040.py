"""Unit 3d-10: Ohio Form IT 1040 on the draft for a full-year resident, with the
Schedule of Adjustments, Schedule of Business Income and Schedule of Credits
lines behind it, from synthetic W-2, 1099-INT, 1099-NEC, 1099-R and SSA-1099
boxes. Line numbers, the exemption amounts, the credits and the rounding rule
follow the 2025 IT 1040, its schedules and instructions (Ohio Department of
Taxation, 1040-bundle.pdf and it1040-booklet.pdf); the tax follows R.C.
5747.02(A)(3). Next year's safe harbor (IT/SD 2210) is line 10 less line 16."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.ingest.needs import enter, need_value
from planner.ledger import db
from planner.paths import Layout
from planner.plan import esttax
from planner.taxprep import draft, oh1040, statereturn
from tests.test_forms import one

F, ADJ, BUS, CR = oh1040.FORM, oh1040.ADJ, oh1040.BUS, oh1040.CRED


def _lay(
    planner_home: Path,
    w2: dict[str, float],
    more: list[db.Fact] | None = None,
    status: str = "single",
    dependents: str = "none",
    born: str = "1980-05-01",
    spouse_w2: dict[str, float] | None = None,
) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    docs = [("you", w2, more or [])]
    if spouse_w2:
        docs.append(("spouse", spouse_w2, []))
    for owner, boxes, extra in docs:
        facts = [
            db.Fact("W-2", 2025, "Employer (synthetic)", b, "", v, 1)
            for b, v in boxes.items()
        ]
        db.add_document(
            conn,
            fingerprint=f"synthetic-oh-{owner}",
            file_name=f"oh-{owner}.pdf",
            kind="pdf",
            pages=1,
            batch="b1",
            facts=facts + extra,
            owner=owner,
        )
    conn.close()
    for key, text in (
        ("birth_date", born),
        ("filing_status", status),
        ("dependents", dependents),
        ("state", "OH"),
    ):
        enter(lay, 2025, key, text)
    return lay


def _checks(d: draft.Draft) -> list[str]:
    return [n for n in d.notes if n.startswith("CHECK")]


def _lines(d: draft.Draft, form: str, *lines: str) -> tuple[float | None, ...]:
    return tuple(d.get(form, n) for n in lines)


BANK = [
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "1", "", 500.0, 1),
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "3", "", 300.0, 1),
    db.Fact("1099-INT", 2025, "Example Bank (synthetic)", "8", "", 120.0, 1),
]
W2 = {"1": 60250.0, "2": 6000.0, "17": 1000.0}


def test_it1040_owed_with_us_bond_interest_and_an_estimated_payment(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home, W2, BANK)
    esttax.record(lay, 2025, "oh", "2025-06-15", 20.0)
    d = draft.build(lay, 2025)
    assert _lines(d, ADJ, "12", "26", "47") == (0.0, 300.0, 300.0)
    assert _lines(d, F, "1", "2a", "2b", "3") == (61050.0, 0.0, 300.0, 60750.0)
    assert _lines(d, F, "4", "5", "6", "7") == (2150.0, 58600.0, 0.0, 58600.0)
    # 342 + 2.75% of 32,550 = 1,237.13, to the dollar
    assert _lines(d, F, "8a", "8b", "8c", "9", "10") == (
        1237.0,
        0.0,
        1237.0,
        0.0,
        1237.0,
    )
    assert _lines(d, F, "12", "13", "14", "15", "16", "17") == (
        0.0,
        1237.0,
        1000.0,
        20.0,
        0.0,
        1020.0,
    )
    assert _lines(d, F, "19", "20", "22", "23") == (1020.0, 217.0, 217.0, None)
    assert not _checks(d), d.notes
    out = draft.render(d)
    assert "OH Form IT 1040" in out and "2025-06-15 20.00 (typed)" in out
    assert "OH Schedule of Adjustments" in out and "OH Schedule of Credits" in out
    assert any("oh_use_tax" in n for n in d.notes)
    assert any("IT/SD 2210" in n for n in d.notes)


def test_retirees_deduct_social_security_and_take_the_retirement_credits(
    planner_home: Path,
) -> None:
    more = [
        db.Fact("1099-R", 2025, "IRA Custodian (synthetic)", "1", "", 50000.0, 1),
        db.Fact("1099-R", 2025, "IRA Custodian (synthetic)", "2a", "", 50000.0, 1),
        db.Fact("1099-R", 2025, "IRA Custodian (synthetic)", "7", "7", 0.0, 1),
        db.Fact("SSA-1099", 2025, "SSA", "5", "", 30000.0, 1),
    ]
    lay = _lay(planner_home, {}, more, status="married_joint", born="1955-05-01")
    enter(lay, 2025, "spouse_birth_date", "1956-03-01")
    d = draft.build(lay, 2025)
    assert _lines(d, ADJ, "16", "47") == (23850.0, 23850.0)
    assert _lines(d, F, "1", "3", "4", "5") == (73850.0, 50000.0, 4300.0, 45700.0)
    assert d.get(F, "8a") == 882.0  # 342 + 2.75% of 19,650 = 882.38
    assert _lines(d, CR, "2", "4", "10", "11", "12") == (200.0, 50.0, 250.0, 632.0, 0.0)
    assert _lines(d, F, "9", "10", "22") == (250.0, 632.0, 632.0)
    assert any("joint filing credit" in n for n in d.notes)
    assert not _checks(d), d.notes


def test_two_earners_take_the_joint_filing_credit(planner_home: Path) -> None:
    lay = _lay(
        planner_home,
        {"1": 50000.0, "2": 5000.0, "17": 1250.0},
        status="married_joint",
        spouse_w2={"1": 40000.0, "2": 4000.0, "17": 1000.0},
    )
    enter(lay, 2025, "spouse_birth_date", "1981-05-01")
    enter(lay, 2025, "oh_use_tax", "0")
    d = draft.build(lay, 2025)
    assert _lines(d, F, "3", "4", "7", "8c") == (90000.0, 3800.0, 86200.0, 1996.0)
    # MAGI less exemptions 86,200: 5% of line 11, 99.80
    assert _lines(d, CR, "11", "12", "36", "40") == (1996.0, 100.0, 100.0, 100.0)
    assert _lines(d, F, "10", "14", "23", "26") == (1896.0, 2250.0, 354.0, 354.0)
    assert not any("joint filing credit" in n for n in d.notes)
    assert not any("oh_use_tax" in n for n in d.notes)
    assert not _checks(d), d.notes


def test_head_of_household_takes_the_exemption_and_earned_income_credits(
    planner_home: Path,
) -> None:
    w2 = {"1": 18000.0, "2": 0.0, "17": 0.0}
    lay = _lay(planner_home, w2, status="head_of_household", dependents="2018-03-02")
    d = draft.build(lay, 2025)
    assert _lines(d, F, "4", "5", "8c") == (4800.0, 13200.0, 0.0)  # 2 x 2,400
    # $20 an exemption; 30% of the federal EIC (4,328), 1,298.40
    assert _lines(d, CR, "9", "13", "36", "40") == (40.0, 1298.0, 1298.0, 1338.0)
    assert _lines(d, F, "9", "10", "13") == (1338.0, 0.0, 0.0)
    assert d.get(F, "20") is None and d.get(F, "23") is None
    assert not _checks(d), d.notes


def test_schedule_c_income_takes_the_business_income_deduction(
    planner_home: Path,
) -> None:
    nec = [db.Fact("1099-NEC", 2025, "Client (synthetic)", "1", "", 20000.0, 1)]
    lay = _lay(planner_home, {"1": 60250.0, "2": 6000.0, "17": 2000.0}, nec)
    d = draft.build(lay, 2025)
    assert _lines(d, BUS, "2", "10", "11", "13", "15", "16") == (
        20000.0,
        20000.0,
        20000.0,
        20000.0,
        0.0,
        0.0,
    )
    assert _lines(d, ADJ, "13", "47") == (20000.0, 20000.0)
    assert _lines(d, F, "1", "3", "5", "6") == (78837.0, 58837.0, 56687.0, 0.0)
    assert _lines(d, F, "8a", "10", "23", "26") == (1185.0, 1185.0, 815.0, 815.0)
    assert any("oh_business_income" in n for n in d.notes)
    assert not _checks(d), d.notes


def test_business_income_over_the_deduction_cap_is_taxed_at_three_percent(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home, {"1": 60000.0, "2": 6000.0, "17": 1000.0})
    for key, text in (("oh_business_income", "300000"), ("oh_use_tax", "15")):
        enter(lay, 2025, key, text)
    d = draft.build(lay, 2025)
    assert _lines(d, BUS, "10", "11", "13", "15", "16") == (
        300000.0,
        60000.0,  # not more than IT 1040 line 1
        60000.0,
        0.0,
        0.0,
    )
    assert d.get(F, "12") == 15.0
    assert not any("oh_use_tax" in n for n in d.notes)


def test_typed_adjustments_land_on_lines_2a_and_2b(planner_home: Path) -> None:
    lay = _lay(planner_home, W2, BANK)
    for key, text in (("oh_additions", "1000"), ("oh_deductions", "500")):
        enter(lay, 2025, key, text)
    d = draft.build(lay, 2025)
    assert _lines(d, ADJ, "12", "47") == (1000.0, 800.0)
    assert _lines(d, F, "2a", "2b", "3") == (1000.0, 800.0, 61250.0)
    assert not _checks(d), d.notes


def test_line_8a_follows_the_statute_above_100000(planner_home: Path) -> None:
    lay = _lay(planner_home, {"1": 160000.0, "2": 30000.0, "17": 4000.0})
    d = draft.build(lay, 2025)
    assert _lines(d, F, "4", "7") == (1900.0, 158100.0)
    assert d.get(F, "8a") == 4210.0  # 2,394.32 + 3.125% of 58,100 = 4,209.95
    assert any("$18.69" in n for n in d.notes)


def test_tax_rates_and_rounding_follow_the_booklet() -> None:
    assert oh1040.nonbusiness_tax(26050.0, 2025) == 0.0
    assert oh1040.nonbusiness_tax(100000.0, 2025) == pytest.approx(2375.625)
    assert oh1040.nonbusiness_tax(100001.0, 2025) == pytest.approx(2394.35125)
    assert [oh1040.joint_rate(x) for x in (25000, 25001, 75001, 750000)] == [
        0.20,
        0.15,
        0.05,
        0.0,
    ]
    assert (oh1040.dollars(1.49), oh1040.dollars(2.50)) == (1.0, 3.0)


def test_oh_is_a_registry_entry() -> None:
    oh = statereturn.get("oh")
    assert oh is not None and oh.form == F and oh.template == "OH-IT1040"
    assert oh.tax_line == "10" and oh.carry == "state_tax"
    assert oh.prior == ("10", "-16")
    assert set(oh.forms) == {F, ADJ, BUS, CR}
    assert {"state_withheld", "oh_use_tax", "oh_business_income"} <= set(oh.keys)


PAGE1 = [
    "Ohio IT 1040 2025 Individual Income Tax Return",
    '1. Federal adjusted gross income (federal 1040 or 1040-SR, line 11a). Place a "-" '
    "in the box if negative .....1. 61,050",
    "2 b. Deductions - Ohio Schedule of Adjustments, line 47 (include schedule) "
    "...2b. 300",
    '3. Ohio adjusted gross income (line 1 plus line 2a minus line 2b). Place a "-" '
    "in the box if negative .. ....3. 60,750",
    "4. Exemption amount (include Schedule of Dependents if applicable) ....4. 2,150",
    "5. Ohio income tax base (line 3 minus line 4; if negative, enter zero)"
    "....5. 58,600",
    "7. Taxable nonbusiness income (line 5 minus line 6; if negative, enter zero) "
    "....7. 58,600",
    "2025 IT 1040 - page 1 of 2",
]
PAGE2 = [
    "Ohio IT 1040 2025 Individual Income Tax Return",
    "7a. Amount from line 7 on page 1 ....7a. 58,600",
    "8c. Income tax liability before credits (line 8a plus line 8b) ....8c. 1,237",
    "9. Ohio nonrefundable credits - Ohio Schedule of Credits, line 40 (include "
    "schedule) ....9. 37",
    "10. Tax liability after nonrefundable credits (line 8c minus line 9; if "
    "negative, enter zero) ....10. 1,200",
    "14. Ohio income tax withheld - Schedule of Ohio Withholding, part A, line 1 "
    "(include schedule and income statements) ....14. 1,000",
    "16. Refundable credits - Ohio Schedule of Credits, line 47 (include schedule) "
    "....16. 50",
    "17. Total Ohio tax payments (add lines 14, 15, and 16) ....17. 1,050",
    '20. Tax due (line 13 minus line 19). If line 19 is negative, ignore the "-" '
    "and add line 19 to line 13 ....20. 150",
    "22. TOTAL AMOUNT DUE (line 20 plus line 21). Pay electronically or include "
    "the OUPC with your check....AMOUNT DUE22. 150",
    "2025 IT 1040 - page 2 of 2",
]


def test_a_filed_it1040_reads_as_one_form(tmp_path: Path) -> None:
    (f,) = one(tmp_path, "oh1040", [PAGE1, PAGE2])
    assert (f.form, f.issuer, f.tax_year) == ("OH-IT1040", "OH", 2025)
    got = {b: f.boxes[b][1] for b in ("1", "2b", "3", "4", "5", "7", "7a")}
    assert got == {
        "1": 61050.0,
        "2b": 300.0,
        "3": 60750.0,
        "4": 2150.0,
        "5": 58600.0,
        "7": 58600.0,
        "7a": 58600.0,
    }
    got = {b: f.boxes[b][1] for b in ("8c", "9", "10", "14", "16", "17", "20", "22")}
    assert got == {
        "8c": 1237.0,
        "9": 37.0,
        "10": 1200.0,
        "14": 1000.0,
        "16": 50.0,
        "17": 1050.0,
        "20": 150.0,
        "22": 150.0,
    }
    assert "23" not in f.boxes and "26" not in f.boxes


@pytest.mark.parametrize(("credits", "want"), [((("16", 50.0),), 1150.0), ((), 1200.0)])
def test_a_filed_it1040_is_next_years_prior_state_tax(
    planner_home: Path, credits: tuple[tuple[str, float], ...], want: float
) -> None:
    lay = _lay(planner_home, W2)
    enter(lay, 2026, "state", "OH")
    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint="synthetic-oh-filed",
        file_name="filed-oh1040.pdf",
        kind="pdf",
        pages=2,
        batch="b2",
        facts=[
            db.Fact("OH-IT1040", 2025, "OH", line, "", value, 1)
            for line, value in (("10", 1200.0), ("14", 1000.0), *credits)
        ],
    )
    try:
        # IT/SD 2210: line 10 less the refundable credits on line 16;
        # withholding (14) is a payment, not a credit
        assert need_value(conn, lay, 2026, "prior_state_tax") == want
    finally:
        conn.close()
