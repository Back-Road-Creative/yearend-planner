"""The Needed panel (Phase 2d): needs engine, typed answers, don't-have."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest import ingest
from planner.ingest.needs import NEEDS, dont_have, enter, need_for, needed, parse_value
from planner.ledger import db
from planner.paths import Layout
from planner.plan import rollover
from planner.taxprep import hsa
from tests.pdfgen import make_pdf
from tests.test_csv import BANK, REALIZED, drop
from tests.test_forms import F1040_FILED_P1, F1040_P1, F1040_P2, M1098, SSA

runner = CliRunner()


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    return lay


def states(lay: Layout, year: int = 2026) -> dict[str, str]:
    return {s.need.key: s.state for s in needed(lay, year).items}


def test_registry_keys_are_unique_and_every_box_names_a_template(
    repo_root: Path,
) -> None:
    keys = [n.key for n in NEEDS]
    assert len(keys) == len(set(keys))
    forms: dict[str, dict[str, object]] = {}
    for p in (repo_root / "templates" / "forms").glob("*.yaml"):
        t = yaml.safe_load(p.read_text(encoding="utf-8"))
        forms.setdefault(str(t["form"]), {}).update(t["boxes"])
    forms["YTD"] = {}
    forms["SCH-C"] = {}  # stored by planner categorize, not read from a PDF
    forms["SCH-D"] = {}  # stored by planner gains from the lots
    forms["8889"] = dict.fromkeys(hsa.LABELS)  # stored by planner hsa
    forms[rollover.FILED] = dict.fromkeys(rollover.LABELS)  # planner rollover
    for n in NEEDS:
        for form, box in n.boxes:
            assert form in forms, (n.key, form)
            if forms[form]:
                assert box in forms[form], (n.key, form, box)


def test_fresh_folder_needs_everything_and_copies_the_example_profile(
    lay: Layout,
) -> None:
    rep = needed(lay, 2026)
    assert not rep.done
    assert {s.state for s in rep.items} == {"missing"}
    assert (lay.data / "profile" / "assumptions.yaml").exists()


def test_documents_and_rows_cover_needs(lay: Layout) -> None:
    make_pdf(lay.data / "inbox" / "return.pdf", [F1040_P1, F1040_P2])
    make_pdf(lay.data / "inbox" / "ssa.pdf", [SSA])
    drop(lay, "realized.csv", REALIZED)
    drop(lay, "bank.csv", BANK)
    ingest(lay)
    rep = needed(lay, 2026)
    by = {s.need.key: s for s in rep.items}
    assert by["prior_agi"].state == "actual" and by["prior_agi"].value == 120534.56
    assert by["prior_total_tax"].value == 18696.0
    assert (
        by["ss_estimate_67"].state == "actual" and by["ss_estimate_67"].value == 2640.0
    )
    assert by["prior_nc_tax"].state == "missing"
    # the YTD facts stay; once the year has ended Schedule D from the lots is actual
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        ytd = {f.box: f.value for f in db.facts_for(conn, 2025, "YTD")}
    finally:
        conn.close()
    assert ytd["lt_gain"] == 9000.0
    lt = {s.need.key: s for s in needed(lay, 2025).items}["long_term_gains"]
    assert (lt.state, lt.value, lt.origin) == ("actual", 9000.0, "SCH-D 2025 planner")
    assert "2025" in by["prior_agi"].origin


def test_enter_profile_and_year_answers_then_dont_have(lay: Layout) -> None:
    assert enter(lay, 2026, "birth_date", "1972-05-04") == "1972-05-04"
    assert enter(lay, 2026, "filing_status", "Single") == "single"
    assert enter(lay, 2026, "withdrawal_rate", "3.5%") == 0.035
    assert enter(lay, 2026, "wages", "$1,250.49") == 1250
    assert enter(lay, 2026, "short_term_gains", "(300)") == -300
    prof = yaml.safe_load((lay.data / "profile" / "assumptions.yaml").read_text())
    assert prof["birth_date"] == "1972-05-04" and prof["withdrawal_rate"] == 0.035
    assert prof["state"] is None
    manual = yaml.safe_load((lay.data / "manual" / "2026.yaml").read_text())
    assert manual["values"] == {"wages": 1250, "short_term_gains": -300}
    st = states(lay)
    assert st["birth_date"] == "actual" and st["wages"] == "actual"
    assert st["county"] == "missing" and st["hsa_contribution"] == "missing"
    dont_have(lay, 2026, "county")
    dont_have(lay, 2026, "hsa_contribution")
    st = states(lay)
    assert st["county"] == "dont_have" and st["hsa_contribution"] == "dont_have"
    assert states(lay, 2027)["hsa_contribution"] == "missing"  # year-scoped
    enter(lay, 2026, "county", "Wake")  # answering clears the mark
    assert states(lay)["county"] == "actual"


def test_bad_answers_are_refused_not_guessed(lay: Layout) -> None:
    with pytest.raises(ValueError):
        parse_value(need_for("filing_status"), "married")
    with pytest.raises(ValueError):
        parse_value(need_for("birth_date"), "5/4/72")
    with pytest.raises(ValueError):
        parse_value(need_for("inflation"), "250%")
    with pytest.raises(KeyError):
        enter(lay, 2026, "net_worth", "1")


def test_cli_needed_enter_dont_have(lay: Layout) -> None:
    r = runner.invoke(app, ["needed", "--year", "2026"])
    assert r.exit_code == 0, r.output
    assert "needed    birth_date" in r.output and "type: planner enter" in r.output
    assert r.output.strip().endswith(f"{len(NEEDS)} needed")
    r = runner.invoke(app, ["enter", "spending_floor", "36,000", "--year", "2026"])
    assert r.exit_code == 0 and "entered spending_floor = 36000" in r.output
    r = runner.invoke(app, ["enter", "filing_status", "widowed", "--year", "2026"])
    assert r.exit_code == 2 and "refused" in r.output
    r = runner.invoke(app, ["dont-have", "ss_estimate_62", "--year", "2026"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["needed", "--year", "2026", "--all"])
    assert "actual    spending_floor" in r.output
    assert "dont-have ss_estimate_62" in r.output
    assert f"{len(NEEDS) - 2} needed" in r.output


def test_loop_ends_when_nothing_is_missing(lay: Layout) -> None:
    for n in NEEDS:
        dont_have(lay, 2026, n.key)
    rep = needed(lay, 2026)
    assert rep.done
    r = runner.invoke(app, ["needed", "--year", "2026"])
    assert r.output.strip() == "nothing needed"


def test_filed_1040_answers_filing_status_and_state(lay: Layout) -> None:
    make_pdf(lay.data / "inbox" / "return.pdf", [F1040_FILED_P1, F1040_P2])
    ingest(lay)
    by = {s.need.key: s for s in needed(lay, 2026).items}
    assert (by["filing_status"].state, by["filing_status"].value) == (
        "actual",
        "married_joint",
    )
    assert by["filing_status"].origin == "1040 2025 self"
    assert (by["state"].state, by["state"].value) == ("actual", "NC")
    assert by["county"].state == "missing"  # ZIP 27000 is in no county
    assert not [k for k in ("filing_status", "state") if by[k].state == "missing"]


def test_typed_answer_beats_the_filed_return(lay: Layout) -> None:
    make_pdf(lay.data / "inbox" / "return.pdf", [F1040_FILED_P1, F1040_P2])
    ingest(lay)
    enter(lay, 2026, "filing_status", "single")
    st = {s.need.key: s for s in needed(lay, 2026).items}["filing_status"]
    assert (st.value, st.origin) == ("single", "profile")


def test_a_return_with_no_check_mark_leaves_filing_status_to_be_typed(
    lay: Layout,
) -> None:
    unchecked = [
        line.replace("[X]", "[ ]") if line.startswith("[ ] Single") else line
        for line in F1040_FILED_P1
    ]
    make_pdf(lay.data / "inbox" / "return.pdf", [unchecked, F1040_P2])
    ingest(lay)
    st = states(lay)
    assert st["filing_status"] == "missing" and st["state"] == "actual"


def test_latest_return_wins_and_later_years_do_not_answer_earlier_plans(
    lay: Layout,
) -> None:
    old = [
        line.replace("(2025)", "(2024)").replace("[ ] Single [X]", "[X] Single [ ]")
        for line in F1040_FILED_P1
    ]
    old_p2 = [line.replace("(2025)", "(2024)") for line in F1040_P2]
    make_pdf(lay.data / "inbox" / "old.pdf", [old, old_p2])
    make_pdf(lay.data / "inbox" / "new.pdf", [F1040_FILED_P1, F1040_P2])
    ingest(lay)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    assert {f.tax_year for f in db.facts_for(conn, form="1040", text=True)} == {
        2024,
        2025,
    }
    conn.close()
    by = {s.need.key: s for s in needed(lay, 2026).items}
    assert by["filing_status"].value == "married_joint"
    assert by["filing_status"].origin == "1040 2025 self"
    by = {s.need.key: s for s in needed(lay, 2025).items}
    assert by["filing_status"].origin == "1040 2025 self"
    by = {s.need.key: s for s in needed(lay, 2024).items}
    assert by["filing_status"].value == "single"
    assert by["filing_status"].origin == "1040 2024 self"


def _1098(year: int, interest: str, principal: str, lender: str) -> list[str]:
    return [
        "Form 1098 Mortgage Interest Statement",
        f"Tax year {year}",
        f"RECIPIENT'S/LENDER'S name: {lender}",
        f"1 Mortgage interest received from payer(s)/borrower(s) $ {interest}",
        f"2 Outstanding mortgage principal $ {principal}",
    ]


def test_two_consecutive_1098s_give_principal_and_interest(lay: Layout) -> None:
    make_pdf(
        lay.data / "inbox" / "a.pdf",
        [_1098(2024, "6,500.00", "180,000.00", "Example Mortgage Co (synthetic)")],
    )
    make_pdf(
        lay.data / "inbox" / "b.pdf",
        [_1098(2025, "6,200.00", "175,000.00", "Example Mortgage Co (synthetic)")],
    )
    ingest(lay)
    st = {s.need.key: s for s in needed(lay, 2026).items}["mortgage_monthly"]
    # 2024: interest 6,500 + principal paid (180,000 - 175,000) = 11,500 a year
    assert (st.state, st.value) == ("estimate", 958.33)
    assert "P&I" in st.origin and "2024" in st.origin and "escrow" in st.origin
    enter(lay, 2026, "mortgage_monthly", "1,400")  # P&I plus escrow, typed
    st = {s.need.key: s for s in needed(lay, 2026).items}["mortgage_monthly"]
    assert (st.state, st.value) == ("actual", 1400)


def test_one_1098_or_a_balance_that_grew_derives_nothing(lay: Layout) -> None:
    make_pdf(lay.data / "inbox" / "a.pdf", [M1098])
    ingest(lay)
    assert states(lay)["mortgage_monthly"] == "missing"
    make_pdf(
        lay.data / "inbox" / "b.pdf",
        [_1098(2026, "6,200.00", "190,000.00", "Example Mortgage Co (synthetic)")],
    )
    ingest(lay)  # 2025 -> 2026: the balance rose (refinance); no P&I is inferred
    assert states(lay, 2027)["mortgage_monthly"] == "missing"


def test_1098s_from_different_lenders_are_not_paired(lay: Layout) -> None:
    make_pdf(
        lay.data / "inbox" / "a.pdf",
        [_1098(2024, "6,500.00", "180,000.00", "First Lender (synthetic)")],
    )
    make_pdf(
        lay.data / "inbox" / "b.pdf",
        [_1098(2025, "6,200.00", "175,000.00", "Second Lender (synthetic)")],
    )
    ingest(lay)
    assert states(lay)["mortgage_monthly"] == "missing"


def test_a_refinanced_loan_counts_only_the_current_lender(lay: Layout) -> None:
    old, new = "Old Lender (synthetic)", "New Lender (synthetic)"
    statements = [
        (2024, "7,000.00", "200,000.00", old),
        (2025, "6,800.00", "195,000.00", old),  # paid off by the 2025 refinance
        (2025, "6,000.00", "190,000.00", new),
        (2026, "5,900.00", "185,000.00", new),
    ]
    for i, (year, interest, principal, lender) in enumerate(statements):
        make_pdf(
            lay.data / "inbox" / f"s{i}.pdf", [_1098(year, interest, principal, lender)]
        )
    ingest(lay)
    st = {s.need.key: s for s in needed(lay, 2027).items}["mortgage_monthly"]
    # only the pair ending in 2026 is current: (6,000 + 190,000 - 185,000) / 12
    assert (st.state, st.value) == ("estimate", 916.67)
    assert "New Lender" in st.origin and "Old Lender" not in st.origin
    # a plan year before 2026 does not read the 2026 statement
    st = {s.need.key: s for s in needed(lay, 2025).items}["mortgage_monthly"]
    # 2024-2025 old lender: (7,000 + 200,000 - 195,000) / 12
    assert (st.state, st.value) == ("estimate", 1000.0)
    assert "Old Lender" in st.origin and "New Lender" not in st.origin
