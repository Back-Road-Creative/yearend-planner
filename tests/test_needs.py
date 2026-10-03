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
from tests.pdfgen import make_pdf
from tests.test_csv import BANK, REALIZED, drop
from tests.test_forms import F1040_P1, F1040_P2, SSA

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
    # a YTD figure stands in as an estimate until the 1099-B / Schedule D arrives
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        ytd = {f.box: f.value for f in db.facts_for(conn, 2025, "YTD")}
    finally:
        conn.close()
    assert ytd["lt_gain"] == 9000.0
    assert (
        needed(lay, 2025).items
        and {s.need.key: s for s in needed(lay, 2025).items}["long_term_gains"].state
        == "estimate"
    )
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
