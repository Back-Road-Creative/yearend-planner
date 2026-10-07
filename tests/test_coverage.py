"""Phase 10, unit 2a: the coverage gate runs right after intake, before any plan
or draft. A household shape, a state with no drafted return or a document the
planner cannot read is named with what to do, its sections are tagged "not
handled", and the rest still runs; every other section is "verified" or
"estimated" from its capability row."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from planner import coverage
from planner.dashboard import page, render
from planner.ingest.needs import enter
from planner.paths import Layout
from planner.taxprep import draft, package
from tests.test_d400 import _lay
from tests.test_spending import AS_OF


def _unread(lay: Layout, name: str = "statement.pdf") -> None:
    bad = lay.data / "inbox" / "UNMATCHED"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / name).write_bytes(b"%PDF-1.4")
    (bad / f"{name}.reason.txt").write_text("no template", encoding="utf-8")


def _home(planner_home: Path, status: str = "single", state: str = "NC") -> Layout:
    home = Layout(planner_home)
    home.ensure()
    for key, text in (
        ("birth_date", "1971-06-15"),
        ("filing_status", status),
        ("state", state),
    ):
        enter(home, 2026, key, text)
    return home


def test_a_single_nc_filer_with_every_document_read_has_no_gap(
    planner_home: Path,
) -> None:
    assert coverage.gate(_home(planner_home), "NC", "SINGLE") == []


def test_each_unanswerable_fact_is_one_named_gap(planner_home: Path) -> None:
    home = _home(planner_home)
    _unread(home)
    gaps = coverage.gate(home, "SC", "JOINT")
    assert [g.area for g in gaps] == ["household", "state", "document"]
    assert all(g.reason.startswith("Not handled:") and g.needed for g in gaps)
    household, state, document = gaps
    assert "married filing jointly" in household.reason
    assert "SC return is not drafted" in state.reason
    assert "engine's estimate" in state.reason
    assert "statement.pdf was not read: no template" in document.reason
    assert "planner enter" in document.needed and "UNMATCHED" in document.needed
    # a document no template reads could hold any income: every priced figure
    assert set(coverage.PRICED) <= set(document.touches)
    assert state.touches == (coverage.STATE_RETURN,)


@pytest.mark.parametrize(
    ("status", "touched", "want"),
    [
        ("verified", False, coverage.VERIFIED),
        ("partial", False, coverage.ESTIMATED),
        ("unsupported", False, coverage.ESTIMATED),
        (None, False, coverage.ESTIMATED),
        ("verified", True, coverage.NOT_HANDLED),
    ],
)
def test_tag_follows_the_gaps_then_the_capability_row(
    status: str | None, touched: bool, want: str
) -> None:
    gaps = [coverage.Gap("document", "Not handled: x", "y", ("magi",))]
    assert coverage.tag(gaps, "magi" if touched else "calendar", status) == want


def test_page_tags_each_panel_and_lists_each_gap_as_needed(planner_home: Path) -> None:
    home = _home(planner_home)
    pg = page.gather(home, 2026, AS_OF)
    assert pg.coverage == []
    assert {p.name: p.coverage for p in pg.panels}["calendar"] == coverage.VERIFIED
    assert all(p.coverage != coverage.NOT_HANDLED for p in pg.panels)
    before = pg.needed_count

    _unread(home)
    pg = page.gather(home, 2026, AS_OF)
    (gap,) = pg.coverage
    tags = {p.name: p.coverage for p in pg.panels}
    assert all(tags[n] == coverage.NOT_HANDLED for n in coverage.PRICED if n in tags)
    assert tags["calendar"] == coverage.VERIFIED  # dates do not rest on the document
    assert pg.needed_count == before + 1
    text = render.html(pg)
    assert gap.reason in text and gap.needed in text
    assert '<span class="cov not-handled">not handled</span>' in text


def test_draft_names_the_state_gap_and_tags_each_form(planner_home: Path) -> None:
    lay = _lay(planner_home, 0.0)
    enter(lay, 2025, "state", "SC")
    d = draft.build(lay, 2025)
    (gap,) = [g for g in d.coverage if g.area == "state"]
    assert gap.reason in d.notes
    assert d.tag("1040") == coverage.VERIFIED
    # a state the draft leaves out does not stop the federal return
    assert gap.reason not in (d.not_ready or "")
    text = draft.render(d)
    assert "Form 1040 [verified]" in text


def test_an_unread_document_makes_the_draft_and_pack_not_ready(
    planner_home: Path,
) -> None:
    lay = _lay(planner_home, 2000.0)
    _unread(lay, "k1.pdf")
    d = draft.build(lay, 2025)
    assert d.tag("1040") == d.tag("D-400") == coverage.NOT_HANDLED
    assert d.not_ready is not None and "k1.pdf was not read" in d.not_ready
    assert "Form 1040 [not handled]" in draft.render(d)
    pack = package.build(lay, 2025)
    rows = list(
        csv.DictReader(
            (pack.folder / "coverage.csv").open(encoding="utf-8", newline="")
        )
    )
    by_form = {r["section"]: r for r in rows}
    assert by_form["Form 1040"]["tag"] == coverage.NOT_HANDLED
    assert "k1.pdf" in by_form["Form 1040"]["why"]
    assert any(n.startswith("NOT READY") for n in pack.notes)


# Unit 2a-2: readiness is three answers, never "no open questions" (F14).
AREA = coverage.Item("Health premium", ("ACA credit",))
MOVE = coverage.Item("Traditional IRA balance", ("Roth conversion",))


def test_the_act_outputs_are_needs_outputs() -> None:
    from planner.ingest.needs import OUTPUTS

    assert set(coverage.ACTS) <= set(OUTPUTS)


def test_nothing_open_nothing_aside_after_year_end_is_ready_three_ways() -> None:
    plan, act, prep = coverage.readiness(
        [], open_items=0, set_aside=[], estimates=[], year=2026, year_open=False
    )
    assert (plan.ready, act.ready, prep.ready) == (True, True, True)
    assert plan.line == "Ready to plan: yes"
    assert prep.line == "Ready for a preparer: yes"


def test_a_set_aside_fact_cannot_make_the_plan_ready_to_act_or_file() -> None:
    plan, act, prep = coverage.readiness(
        [],
        open_items=0,
        set_aside=[MOVE, AREA],
        estimates=[],
        year=2026,
        year_open=False,
    )
    assert plan.ready  # the figures run, tagged estimate
    assert act.blockers == ("set aside, not on hand: Traditional IRA balance",)
    assert prep.blockers == (
        "set aside, not on hand: Traditional IRA balance",
        "set aside, not on hand: Health premium",
    )
    assert (
        act.line == "Ready to act: no: set aside, not on hand: Traditional IRA balance"
    )


def test_a_waived_form_holds_back_every_move() -> None:
    _, act, _ = coverage.readiness(
        [],
        open_items=0,
        set_aside=[coverage.Item("1099-DIV from Vanguard (waived)")],
        estimates=[],
        year=2026,
        year_open=False,
    )
    assert not act.ready


def test_open_items_hold_back_all_three_and_an_open_year_the_preparer() -> None:
    plan, act, prep = coverage.readiness(
        [], open_items=3, set_aside=[], estimates=[MOVE], year=2026, year_open=True
    )
    assert plan.blockers == act.blockers == ("3 open on the Needed list",)
    assert prep.blockers == (
        "3 open on the Needed list",
        "still an estimate: Traditional IRA balance",
        "the 2026 tax year is still open; its final forms arrive in January",
    )


def test_a_state_gap_holds_back_only_the_preparer(planner_home: Path) -> None:
    gaps = coverage.gate(_home(planner_home, state="SC"), "SC", "SINGLE")
    plan, act, prep = coverage.readiness(
        gaps, open_items=0, set_aside=[], estimates=[], year=2026, year_open=False
    )
    assert plan.ready and act.ready
    assert prep.blockers == (gaps[0].reason,)


def test_an_unread_document_holds_back_all_three(planner_home: Path) -> None:
    home = _home(planner_home)
    _unread(home)
    gaps = coverage.gate(home, "NC", "SINGLE")
    answers = coverage.readiness(
        gaps, open_items=0, set_aside=[], estimates=[], year=2026, year_open=False
    )
    assert all(a.blockers == (gaps[0].reason,) for a in answers)


def test_a_long_list_is_cut_to_five_with_a_count() -> None:
    many = [coverage.Item(f"fact {i}") for i in range(8)]
    _, act, _ = coverage.readiness(
        [], open_items=0, set_aside=many, estimates=[], year=2026, year_open=False
    )
    assert act.line.endswith("set aside, not on hand: fact 4; and 3 more")


def test_page_and_dashboard_give_the_three_answers(planner_home: Path) -> None:
    home = _home(planner_home)
    pg = page.gather(home, 2026, AS_OF)
    plan, act, prep = pg.readiness
    assert not prep.ready  # 2026 is open on AS_OF
    assert "the 2026 tax year is still open" in prep.line
    html = render.html(pg)
    for a in pg.readiness:
        assert a.line in html
