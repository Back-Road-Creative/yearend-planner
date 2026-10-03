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
def test_cli_dashboard(lots: Layout) -> None:  # noqa: F811
    r = runner.invoke(app, ["dashboard", "--year", "2026", "--as-of", "2026-07-10"])
    assert r.exit_code == 0, r.output
    assert "index.html" in r.output and (lots.out / "index.html").exists()
