"""Master plan unit 7a: ``planner health`` gives five separate answers (app,
data completeness, calculation, decision readiness, tax-pack readiness) on the
synthetic layout of test_spending, and writes nothing."""

from __future__ import annotations

import hashlib
import os
import time
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner import __version__, health
from planner.backup import backup
from planner.cli import app
from planner.dashboard import page
from planner.paths import Layout
from tests.test_spending import AS_OF, lay  # noqa: F401

runner = CliRunner()


def _files(root: Path) -> dict[str, str]:
    """Every file's bytes, hashed: the ledger's idempotent schema check moves
    its mtime, never its content."""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_answer_status_and_render() -> None:
    ok = health._answer("App", ["planner x"], [])
    bad = health._answer("Calculation", ["draft waits"], ["engine broke"])
    assert (ok.status, bad.status) == (health.OK, health.ATTENTION)
    text = health.render(2026, date(2026, 7, 10), [ok, bad])
    assert text.splitlines() == [
        "Health for 2026 as of 2026-07-10 (reports only; repairs nothing)",
        "App: ok",
        "  planner x",
        "Calculation: attention",
        "  draft waits",
        "  - engine broke",
    ]


def test_app_answer_flags_config_engine_year_and_backup(lay: Layout) -> None:  # noqa: F811
    got = health.app(lay, 2026, AS_OF, "1.0", [2025, 2026], "update check off")
    assert got.facts[:2] == (f"planner {__version__}", "tax engine policyengine-us 1.0")
    assert got.problems == ("no backup yet (planner backup)",)
    zipped = backup(lay)
    old = time.mktime(date(2026, 5, 1).timetuple())
    os.utime(zipped, (old, old))
    late = health.app(lay, 2026, AS_OF, "1.0", [2025], "update check off")
    assert late.problems == (
        "the tax engine does not publish 2026 (planner update)",
        "last backup 70 days ago (planner backup)",
    )
    os.utime(zipped, (time.mktime(AS_OF.timetuple()),) * 2)
    (lay.config / "thresholds.yaml").write_text("2026: [not, a, mapping\n", "utf-8")
    broken = health.app(lay, 2026, AS_OF, "1.0", [2026], "update check off")
    assert broken.status == health.ATTENTION
    assert broken.problems[0].startswith("config does not load: ")


@pytest.mark.engine
def test_five_answers_kept_apart_and_nothing_written(lay: Layout) -> None:  # noqa: F811
    backup(lay)
    # Any read of the portfolio records its all-time high (db.record_peak), the
    # dashboard's too; once that is set, health writes nothing of its own.
    plan, act, prep = page.gather(lay, 2026, AS_OF).readiness
    before = _files(lay.root)
    answers = health.summary(lay, 2026, AS_OF)
    assert _files(lay.root) == before  # reports only
    assert [a.name for a in answers] == list(health.NAMES)
    by = {a.name: a for a in answers}
    assert by["Decision readiness"].facts == (plan.line, act.line)
    assert by["Decision readiness"].problems == act.blockers
    assert by["Tax-pack readiness"].facts == (prep.line,)
    assert by["Tax-pack readiness"].status == health.ATTENTION  # 2026 is open
    calc = by["Calculation"]
    assert calc.facts[0].startswith("engine self-check: 2026 federal tax ")
    assert any(
        f.startswith(("draft return builds", "draft return waits")) for f in calc.facts
    )
    data = by["Data completeness"]
    assert data.facts[0].endswith("open on the Needed list")
    res = runner.invoke(app, ["health", "--year", "2026", "--as-of", AS_OF.isoformat()])
    assert res.exit_code == 0, res.output
    lines = res.output.splitlines()
    assert lines[0].startswith("Health for 2026 as of 2026-07-10")
    assert [ln.split(":")[0] for ln in lines if not ln.startswith(" ")][1:] == list(
        health.NAMES
    )
