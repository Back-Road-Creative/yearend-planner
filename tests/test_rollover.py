"""Phase 7: rolling a synthetic 2025 into 2026: the carry (draft first, the
filed return once closed), next year's Needed items and Schedule D reading it,
snapshots, the checklist, idempotence, a corrected 1099 re-opening the year,
the dashboard's alert and button, and the CLI."""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.dashboard import page, serve
from planner.ingest.needs import enter, needed
from planner.ledger import db
from planner.paths import Layout
from planner.plan import rollover
from planner.taxprep import capgains, close, draft
from tests.test_capgains import lay as cg_lay  # noqa: F401  (the fixture)

runner = CliRunner()
JAN = date(2026, 1, 20)


@pytest.fixture
def lay(cg_lay: Layout) -> Layout:  # noqa: F811
    for key, text in (
        ("birth_date", "1971-06-15"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("wages", "60,000"),
        ("lt_loss_carryover", "10,000"),
    ):
        enter(cg_lay, 2025, key, text)
    return cg_lay


def _add(lay: Layout, name: str, facts: list[db.Fact]) -> None:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        db.add_document(
            conn,
            fingerprint=f"synthetic-{name}",
            file_name=name,
            kind="pdf",
            pages=1,
            batch=name,
            facts=facts,
        )
    finally:
        conn.close()


def _status(lay: Layout, year: int) -> dict[str, tuple[str, object]]:
    return {s.need.key: (s.state, s.value) for s in needed(lay, year).items}


def _sch_d(lay: Layout, year: int) -> capgains.CapGains:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        return capgains.build(conn, lay, year)
    finally:
        conn.close()


def test_roll_carries_the_draft_and_is_idempotent(lay: Layout) -> None:
    d = draft.build(lay, 2025)
    assert d.get("Carryover", "13") == 3500.0
    assert rollover.due(lay, JAN) == 2025
    assert rollover.active_year(lay, JAN) == 2026  # today's year until rolled
    with pytest.raises(rollover.NotEndedError):
        rollover.roll(lay, 2025, date(2025, 12, 31))

    ro = rollover.roll(lay, 2025, JAN)
    assert (ro.version, ro.new, ro.carry.basis) == (1, True, "draft")
    assert ro.carry.values["lt"] == 3500.0 and ro.carry.values["st"] == 0.0
    assert ro.carry.values["total_tax"] == d.get("1040", "24")
    assert rollover.active_year(lay, date(2027, 2, 1)) == 2026  # until 2026 rolls
    assert rollover.due(lay, JAN) is None
    assert rollover.due(lay, date(2027, 2, 1)) == 2026

    st = _status(lay, 2026)
    assert st["prior_total_tax"] == ("estimate", d.get("1040", "24"))
    assert st["prior_agi"] == ("estimate", d.get("1040", "11a"))
    assert st["lt_loss_carryover"] == ("estimate", 3500.0)
    assert st["prior_capital_loss_carryforward"] == ("estimate", 3500.0)
    cg = _sch_d(lay, 2026)
    assert cg.lines["14"] == -3500.0
    assert cg.sources["14"] == "lt_loss_carryover (carried by planner rollover)"

    assert ro.snapshot == lay.data / "snapshots" / "2025-v1"
    assert (ro.snapshot / "planner.db").stat().st_size > 0
    assert "2025" in (ro.snapshot / "dashboard.html").read_text(encoding="utf-8")
    assert ro.checklist == lay.out / "rollover-2026.txt"
    text = ro.checklist.read_text(encoding="utf-8")
    for item in ("specific-ID", "dividend reinvestment off", "ACA plan", "2026 limits"):
        assert item in text
    assert "[ ] Answer in the Needed panel: ss_estimate_67" in text
    assert "rolled 2025 -> 2026" in rollover.render(ro)

    again = rollover.roll(lay, 2025, JAN)
    assert (again.version, again.new) == (1, False)
    assert not (lay.data / "snapshots" / "2025-v2").exists()
    assert rollover.refresh(lay, JAN).new is False  # type: ignore[union-attr]


def test_corrected_1099_then_the_filed_return_reopen_the_year(lay: Layout) -> None:
    rollover.roll(lay, 2025, JAN)
    # a corrected 1099-DIV: capital gain distributions 300 -> 800
    _add(
        lay,
        "corrected-div.pdf",
        [db.Fact("1099-DIV", 2025, "Fund Co (synthetic)", "2a", "Cap gain", 800.0, 1)],
    )
    ro = rollover.refresh(lay, date(2026, 3, 1))
    assert ro is not None and (ro.version, ro.new) == (2, True)
    assert ro.changed == ["lt"] and ro.carry.values["lt"] == 3000.0
    assert "2025 re-opened (lt): version 2" in rollover.render(ro)
    assert _status(lay, 2026)["lt_loss_carryover"] == ("estimate", 3000.0)
    assert (lay.data / "snapshots" / "2025-v1" / "planner.db").exists()  # kept

    d = draft.build(lay, 2025)
    g = d.get
    _add(
        lay,
        "filed-2025.pdf",
        [
            db.Fact("1040", 2025, "self", "7", "Capital gain", g("1040", "7a") or 0, 1),
            db.Fact("1040", 2025, "self", "11", "AGI", g("1040", "11a") or 0, 1),
            db.Fact("1040", 2025, "self", "15", "Taxable", g("1040", "15") or 0, 1),
            db.Fact("1040", 2025, "self", "24", "Total tax", g("1040", "24") or 0, 2),
            db.Fact("1040-SCHD", 2025, "self", "7", "ST", g("Sch D", "7") or 0, 1),
            db.Fact("1040-SCHD", 2025, "self", "15", "LT", g("Sch D", "15") or 0, 1),
            db.Fact("1040-SCHD", 2025, "self", "16", "Net", g("Sch D", "16") or 0, 1),
            db.Fact("NC-D400", 2025, "NC", "15", "NC tax", g("D-400", "15") or 0, 1),
        ],
    )
    close.close(lay, 2025)
    ro = rollover.roll(lay, 2025, date(2026, 4, 20))
    assert (ro.version, ro.carry.basis) == (3, "filed")
    assert ro.changed[0] == "basis draft -> filed"
    assert ro.carry.values == {"st": 0.0, "lt": 3000.0}
    st = _status(lay, 2026)
    assert st["prior_total_tax"] == ("actual", g("1040", "24"))
    assert st["lt_loss_carryover"] == ("actual", 3000.0)
    assert [v["version"] for v in rollover.versions(lay, 2025)] == [1, 2, 3]


def test_dashboard_alert_and_button_roll_the_year(lay: Layout) -> None:
    app_ = serve.App(lay, 2025, JAN)
    html = app_.html()
    assert "Roll over to 2026" in html and "2025 has ended" in html
    msg = app_.rollover()
    assert msg.startswith("rolled 2025 -> 2026") and app_.year == 2026
    assert app_.rollover() == "nothing to roll over"
    pg = page.gather(lay, 2026, JAN)
    assert not [a for a in pg.alerts if a.kind == "rollover"]


def test_cli_rollover_asks_for_next_years_figures(lay: Layout) -> None:
    r = runner.invoke(app, ["rollover", "--year", "2025", "--as-of", "2025-12-01"])
    assert r.exit_code == 2 and "2025 has not ended" in r.output
    answers = ["", "", "", "", "", "3,000", "", "", "", ""]
    r = runner.invoke(
        app,
        ["rollover", "--as-of", "2026-01-20", "--ask"],
        input="\n".join(answers) + "\n",
    )
    assert r.exit_code == 0, r.output
    assert "rolled 2025 -> 2026" in r.output and "checklist:" in r.output
    assert "saved ss_estimate_67 = 3000" in r.output
    assert _status(lay, 2026)["ss_estimate_67"] == ("actual", 3000.0)
    r = runner.invoke(app, ["rollover", "--as-of", "2026-01-21", "--no-ask"])
    assert r.exit_code == 0 and "2025 unchanged: rolled as version 1" in r.output
