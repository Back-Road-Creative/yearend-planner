"""Unit 7e: speed. planner run's page is served before any slow step (the
last page, under the step that is running), a reload with nothing new reuses
the gathered page, and ``planner timing`` measures the budgets with a made-up
household in a throwaway folder."""

from __future__ import annotations

import csv
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner import timing as tm
from planner.cli import app
from planner.dashboard import page as dash
from planner.dashboard import render, serve
from planner.engine import verify
from planner.paths import Layout
from tests.test_serve import call, live, post  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()
PREVIOUS = "<!doctype html><html><head><title>t</title></head><body><h1>old</h1>"


def test_progress_names_each_step_with_its_time() -> None:
    said: list[str] = []
    prog = serve.Progress(("one", "two"), said.append, started=time.monotonic())
    assert prog.status() == "starting"
    prog.step("two")
    assert said == ["[2/2] two (0 s)"]
    assert prog.status().startswith("refreshing: step 2 of 2, two (")


def test_warming_page_keeps_the_last_page_and_reloads_itself() -> None:
    got = serve.warming_page(PREVIOUS, "step <1>")
    assert got.index('http-equiv="refresh"') < got.index("</head>")
    assert got.index(serve.WARMING) < got.index("<h1>old</h1>")
    assert "step &lt;1&gt;" in got and "<1>" not in got
    first = serve.warming_page(None, "starting")
    assert serve.WARMING in first and "first run in this folder" in first


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    calls: list[int] = []

    def gather(lay: Layout, year: int, as_of: date | None = None) -> object:
        calls.append(year)
        return object()

    monkeypatch.setattr(dash, "gather", gather)
    monkeypatch.setattr(render, "html", lambda pg, token, msg: f"page {msg}")
    yield calls


def test_a_reload_with_nothing_new_reuses_the_page(
    live: tuple[serve.App, int],  # noqa: F811
    counted: list[int],
) -> None:
    a, port = live
    for _ in range(2):
        assert call(port, "GET", "/?token=tok")[2] == "page "
    assert len(counted) == 1
    note = a.lay.data / "note.txt"
    note.write_text("changed", encoding="utf-8")  # a file under data/ moved
    call(port, "GET", "/?token=tok")
    assert len(counted) == 2
    assert post(port, "/dont-have", key="nope")[0] == 303  # any action
    call(port, "GET", "/?token=tok")
    assert len(counted) == 3


def test_while_refreshing_the_page_waits_and_actions_are_held(
    live: tuple[serve.App, int],  # noqa: F811
    counted: list[int],
) -> None:
    a, port = live
    a.ready, a.progress = False, serve.Progress(("inbox",), lambda s: None)
    a.progress.step("inbox")
    first = call(port, "GET", "/?token=tok")[2]
    assert "first run in this folder" in first and "step 1 of 1, inbox" in first
    a.lay.out.mkdir(parents=True, exist_ok=True)
    (a.lay.out / "index.html").write_text(PREVIOUS, encoding="utf-8")
    again = call(port, "GET", "/?token=tok")[2]
    assert "<h1>old</h1>" in again and serve.WARMING in again
    status, _, text = post(port, "/dont-have", key="filing_status")
    assert status == 503 and "still refreshing" in text
    assert counted == []  # nothing gathered, nothing written
    a.ready = True
    assert call(port, "GET", "/?token=tok")[2] == "page "


def test_run_serves_before_its_first_slow_step(
    planner_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    servers: list[tuple[serve.App, int]] = []
    seen: list[str] = []
    real = serve.server

    def server(a: serve.App, port: int = 0) -> object:
        srv = real(a, port)
        servers.append((a, srv.server_address[1]))
        return srv

    def baseline(root: Path, today: date | None = None) -> str:
        a, port = servers[0]
        seen.append(call(port, "GET", f"/?token={a.token}")[2])
        raise KeyboardInterrupt  # stop the run here, as Ctrl+C would

    monkeypatch.setattr(serve, "server", server)
    monkeypatch.setattr(verify, "ensure_baseline", baseline)
    r = runner.invoke(app, ["run", "--no-open", "--no-update-check"])
    assert r.exit_code == 0, r.output
    assert r.output.index("serving http://127.0.0.1:") < r.output.index("[1/6]")
    assert "stopped" in r.output
    assert serve.WARMING in seen[0] and "engine baseline" in seen[0]


def test_timing_lines_and_record(planner_home: Path) -> None:
    lay = Layout(planner_home)
    results = [
        tm.Result("first screen", 1.25, 3.0),
        tm.Result("cold run", 95.0, 90.0),
        tm.Result(tm.FIRST_RUN, 120.0, None),
    ]
    assert [r.line() for r in results] == [
        "first screen: 1.2 s (within the 3 s budget)",
        "cold run: 95.0 s (OVER the 90 s budget)",
        f"{tm.FIRST_RUN}: 120.0 s",
    ]
    path = tm.record(lay, results, date(2026, 10, 7))
    tm.record(lay, results[:1], date(2026, 10, 8))
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert [r["measure"] for r in rows] == [*(r.name for r in results), "first screen"]
    assert rows[1]["budget"] == "90" and rows[2]["budget"] == ""
    assert rows[0]["machine"] == tm.machine() and rows[3]["date"] == "2026-10-08"


@pytest.mark.parametrize(("cold", "code"), [(60.0, 0), (95.0, 1)])
def test_timing_command_reports_and_exits_on_a_miss(
    planner_home: Path, monkeypatch: pytest.MonkeyPatch, cold: float, code: int
) -> None:
    got = [tm.Result("cold run", cold, 90.0)]
    monkeypatch.setattr(tm, "measure", lambda release: got)
    r = runner.invoke(app, ["timing"])
    assert r.exit_code == code, r.output
    assert "measuring on " in r.output and "cold run: " in r.output
    assert (planner_home / "data" / "timing.csv").is_file()


def test_timing_names_a_failed_step(
    planner_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(release: Path) -> list[tm.Result]:
        raise tm.TimingError("planner run stopped")

    monkeypatch.setattr(tm, "measure", broken)
    r = runner.invoke(app, ["timing"])
    assert r.exit_code == 1 and "planner run stopped" in r.output


@pytest.mark.engine
def test_the_real_run_serves_at_once_then_goes_live(tmp_path: Path) -> None:
    env = tm._folder(ROOT, tmp_path)
    run = ("run", "--year", tm.YEAR, "--as-of", tm.AS_OF, "--no-update-check")
    first, refresh = tm.served(
        tm.planner_cmd(*run, "--no-open"), env, ROOT, tmp_path / "run.log"
    )
    log = (tmp_path / "run.log").read_text(encoding="utf-8")
    assert log.index("serving http://") < log.index("[1/6] engine baseline")
    assert "[6/6] page" in log and "done in " in log
    assert first < 30 and refresh < 120  # the budgets are planner timing's
