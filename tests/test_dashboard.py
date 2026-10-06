"""Phase 5a: the dashboard page, gathered from every planner and rendered as
HTML; the static export carries no forms, the live page does."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from planner.cli import app
from planner.dashboard import page, render
from planner.ingest.needs import profile_path
from planner.paths import Layout
from planner.plan import glidepath, spending
from tests.test_spending import AS_OF, lay  # noqa: F401
from tests.test_withdraw import lots  # noqa: F401

runner = CliRunner()


@pytest.mark.engine
def test_page_gathers_every_panel_with_its_tag(lots: Layout) -> None:  # noqa: F811
    pg = page.gather(lots, 2026, AS_OF)
    assert [p.name for p in pg.panels] == list(page.ORDER)
    tags = {p.name: p.tag for p in pg.panels}
    # mid-year, the projections are estimates; the calendar and wash sales are not
    assert tags["magi"] == tags["glide"] == tags["esttax"] == page.ESTIMATE
    assert tags["calendar"] == tags["washsales"] == page.ACTUAL
    assert pg.needed_count == len(pg.needed) + len(pg.late_forms)
    kinds = [a.kind for a in pg.alerts]
    assert "wash" in kinds and "stale" not in kinds and "blocked" not in kinds
    assert pg.draft is not None or pg.draft_blocked
    # long after the last import, every page says the figures may be stale;
    # in Q4 a missing next-year parameter set is flagged
    late = page.gather(lots, 2026, date(2027, 3, 1))
    assert any(a.kind == "stale" and "days ago" in a.text for a in late.alerts)
    q4 = page.gather(lots, 2026, date(2026, 11, 2))
    assert any(a.kind == "thresholds" and "2027" in a.text for a in q4.alerts)


@pytest.mark.engine
def test_static_page_escapes_and_carries_no_forms(lots: Layout) -> None:  # noqa: F811
    bad = lots.data / "inbox" / "UNMATCHED"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "scan.pdf").write_bytes(b"%PDF-1.4")
    (bad / "scan.pdf.reason.txt").write_text(
        "<script>alert(1)</script> no template", encoding="utf-8"
    )
    pg = page.gather(lots, 2026, AS_OF)
    assert any(a.kind == "unmatched" and "scan.pdf" in a.text for a in pg.alerts)
    out = render.write_static(lots, pg)
    text = out.read_text(encoding="utf-8")
    assert out == lots.out / "index.html"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "<script>alert(1)" not in text and "<script>" not in text
    assert 'action="/enter' not in text and "planner run" in text
    for panel in pg.panels:
        assert f'id="{panel.name}"' in text
    assert "http://" not in text and "https://" not in text  # no external assets


@pytest.mark.engine
def test_blocked_planner_is_unavailable_and_the_live_page_asks(
    lots: Layout,  # noqa: F811
) -> None:
    prof = yaml.safe_load(profile_path(lots).read_text(encoding="utf-8"))
    prof["spending_floor"] = None
    profile_path(lots).write_text(yaml.safe_dump(prof), encoding="utf-8")
    pg = page.gather(lots, 2026, AS_OF)
    assert pg.panel("spending").tag == page.UNAVAILABLE
    assert any(a.kind == "blocked" and "Spending band" in a.text for a in pg.alerts)
    assert "spending_floor" in [s.need.key for s in pg.needed]
    live = render.html(pg, token="t0k3n")  # noqa: S106
    assert 'action="/enter?token=t0k3n"' in live
    assert 'name="key" value="spending_floor"' in live
    assert 'action="/dont-have?token=t0k3n"' in live and "<script>" in live
    assert 'action="/taxpack?token=t0k3n"' in live


@pytest.mark.engine
def test_cold_start_page_renders_and_names_what_is_needed(planner_home: Path) -> None:
    home = Layout(planner_home)
    home.ensure()
    pg = page.gather(home, 2026, AS_OF)
    assert pg.needed and pg.needed_count >= len(pg.needed)
    assert any(a.kind == "stale" and "no document" in a.text for a in pg.alerts)
    assert page.UNAVAILABLE in {p.tag for p in pg.panels}
    assert "Needed" in render.html(pg)


@pytest.mark.engine
def test_page_alerts_a_household_the_planner_does_not_model(planner_home: Path) -> None:
    from planner.ingest.needs import enter

    home = Layout(planner_home)
    home.ensure()
    for key, text in (
        ("birth_date", "1971-06-15"),
        ("filing_status", "head_of_household"),
        ("state", "NC"),
    ):
        enter(home, 2026, key, text)
    pg = page.gather(home, 2026, AS_OF)
    (alert,) = [a for a in pg.alerts if a.kind == "scope"]
    assert alert.text.startswith("Not handled:")
    assert pg.alerts[0] == alert  # first, above every other alert
    assert "Not handled:" in render.html(pg)
    enter(home, 2026, "filing_status", "single")
    assert not [a for a in page.gather(home, 2026, AS_OF).alerts if a.kind == "scope"]


@pytest.mark.engine
def test_cli_dashboard(lots: Layout) -> None:  # noqa: F811
    r = runner.invoke(app, ["dashboard", "--year", "2026", "--as-of", "2026-07-10"])
    assert r.exit_code == 0, r.output
    assert "index.html" in r.output and (lots.out / "index.html").exists()


@pytest.mark.engine
def test_header_shows_the_last_update_check_and_the_years_covered(
    planner_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Layout(planner_home)
    home.ensure()
    off = page.gather(home, 2026, AS_OF)
    assert off.update_check == "update check off"
    assert off.years and 2026 in off.years
    text = render.html(off)
    assert "tax years" in text and "update check off" in text
    monkeypatch.setenv("PLANNER_UPDATE_FEED", "file:///nowhere/latest.json")
    assert page.gather(home, 2026, AS_OF).update_check == "update check not yet run"
    folder = home.data / "update"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "last-check.json").write_text(
        '{"date": "2026-10-01", "status": "current", "message": "up to date"}',
        encoding="utf-8",
    )
    pg = page.gather(home, 2026, AS_OF)
    assert pg.update_check == "update check 2026-10-01: up to date"
    assert "update check 2026-10-01: up to date" in render.html(pg)
    # next year absent from the engine's published years is flagged in the header
    pg.years = [y for y in pg.years if y <= 2026]
    assert "2027 is not published" in render.html(pg)
    pg.years = [*pg.years, 2027]
    assert "2027 is not published" not in render.html(pg)


@pytest.mark.engine
def test_page_renders_a_pending_ocr_document_with_a_text_box(
    planner_home: Path,
) -> None:
    from planner.ingest import ingest
    from tests.pdfgen import make_pdf
    from tests.test_forms import F1099R

    home = Layout(planner_home)
    home.ensure()
    make_pdf(home.data / "inbox" / "scan.pdf", [[]])
    words = "\n".join(F1099R[:-1] + ["7 Distribution code(s) 6"])
    ingest(home, ocr=lambda path: [words])
    pg = page.gather(home, 2026, AS_OF)
    (doc,) = pg.pending
    assert ("7", "Distribution code", "6") in [
        (box, label, value) for box, label, value in doc.values
    ]
    live = render.html(pg, token="t0k3n")  # noqa: S106
    static = render.write_static(home, pg).read_text(encoding="utf-8")
    for text in (live, static):
        assert "Distribution code" in text and "12,000.00" in text
    assert '<td class="num">6</td>' in static
    assert 'name="edit_7" value="6"' in live  # the live page lets the word be retyped


@pytest.mark.engine
def test_needed_grouped_by_document(planner_home: Path) -> None:
    from html import escape

    from planner.ingest.needs import DOCS

    home = Layout(planner_home)
    home.ensure()
    pg = page.gather(home, 2026, AS_OF)
    groups = {g.doc.key: g for g in pg.needed_groups if g.doc is not None}
    vg = groups["vg_tax"]
    # several Vanguard items close with one download
    assert {s.need.key for s in vg.items} >= {
        "interest",
        "ordinary_dividends",
        "qualified_dividends",
    }
    assert len(vg.unlocks) >= 2
    static = render.write_static(home, pg).read_text(encoding="utf-8")
    live = render.html(pg, token="t0k3n")  # noqa: S106
    for text in (static, live):
        # the download path and its outputs appear once for the whole group
        assert text.count(escape(DOCS["vg_tax"].path)) == 1
        assert text.count(f'id="doc-{vg.doc.key if vg.doc else ""}"') == 1
        for key in ("interest", "ordinary_dividends", "qualified_dividends"):
            assert f"planner enter --year 2026 {key}" in text or (
                f'name="key" value="{key}"' in text
            )
        assert (
            "Vanguard &gt; Cost basis &gt; Realized gains/losses &gt; Export CSV"
            in text
        )
        assert "unlocks:" in text
        assert "Typed answers" in text  # items no document supplies
    # the same item is never listed twice
    listed = [s.need.key for g in pg.needed_groups for s in g.items]
    assert sorted(listed) == sorted(s.need.key for s in pg.needed)


def test_glide_year_and_month_tables_on_page(lots: Layout) -> None:  # noqa: F811
    pg = page.gather(lots, 2026, AS_OF)
    g = glidepath.glide(lots, 2026, AS_OF)
    sp = spending.plan(lots, 2026, AS_OF, years=10)

    # glide panel: the age/year table, the on-track line beside the comfort-floor line
    glide_tbl = pg.panel("glide").tables[0]
    assert glide_tbl.headers[:4] == ["year", "age", "on-track real", "on-track nominal"]
    assert "comfort-floor real" in glide_tbl.headers
    assert len(glide_tbl.rows) == len(g.rows) and glide_tbl.rows[0][:2] == [
        "2026",
        "55",
    ]
    assert glide_tbl.rows[-1][1] == "95"
    i = glide_tbl.headers.index("comfort-floor real")
    assert glide_tbl.rows[0][i] == f"{g.floor_rows[0].balance_real:,.2f}"
    assert glide_tbl.rows[-1][i] == f"{g.floor_rows[-1].balance_real:,.2f}"
    assert any(ln.startswith("band: inside the band") for ln in pg.panel("glide").lines)

    # cash panel: the monthly cash line, 24 months, flagged against the target
    cash_tbl = pg.panel("cash").tables[0]
    assert len(cash_tbl.rows) == 24 and cash_tbl.rows[0][0] == "2026-01*"
    assert cash_tbl.rows[-1][0] == "2027-12"
    assert cash_tbl.headers[-2:] == ["cash", "vs target"]
    assert cash_tbl.rows[0][-2] == f"{g.months[0].cash:,.2f}"
    # the fixture's 12,000 of uncategorised deposits come once, not every month,
    # so the line crosses the 20,000 target in 2027-11 and the flag says so
    flags = {row[0]: row[-1] for row in cash_tbl.rows}
    assert [m for m, f in flags.items() if f != "ok"] == ["2027-11", "2027-12"]
    assert set(flags.values()) == {"ok", "UNDER"}

    # spending panel: the return-band table under the floor and planning returns
    band_tbl = pg.panel("spending").tables[0]
    assert len(band_tbl.rows) == len(sp.rows) == 10
    assert band_tbl.rows[1][2] == f"{sp.rows[1].balance_floor:,.2f}"
    assert band_tbl.rows[1][4] == f"{sp.rows[1].balance_track:,.2f}"

    # the page renders all three, escaped, with no external assets
    text = render.html(pg)
    for needle in (
        "Age and year table",
        ">comfort-floor real</th>",
        '<td>2026</td><td class="num">55</td>',
        "Monthly cash line",
        "<td>2026-01*</td>",
        "<td>2027-12</td>",
        "Return bands",
        f"{sp.rows[1].balance_track:,.2f}",
    ):
        assert needle in text, needle
    assert "http://" not in text and "https://" not in text


@pytest.mark.engine
def test_cash_table_flags_months_under_the_target(lots: Layout) -> None:  # noqa: F811
    from planner.ledger import portfolio

    portfolio.save_account(lots, "22222222", balance=15_000.0)
    pg = page.gather(lots, 2026, AS_OF)
    rows = pg.panel("cash").tables[0].rows
    assert rows[0][-1] == "UNDER"
    assert "UNDER" in render.html(pg)
