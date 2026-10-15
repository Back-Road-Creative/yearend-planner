"""A tag publishes only a commit main's CI already passed (plan YE-E2).

release.yml used to build, prove and sign whatever commit a v* tag named: a tag
on a branch nobody merged, or on a main commit whose ci.yml run failed, still
reached a draft release. The gate job runs first and refuses both."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import release_gate as rg

SHA = "a" * 40
WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _run(conclusion: str | None, status: str = "completed") -> dict[str, Any]:
    return {"head_sha": SHA, "status": status, "conclusion": conclusion}


def test_a_main_commit_with_a_passing_ci_run_is_released() -> None:
    assert rg.gate_problems(SHA, on_main=True, runs=[_run("success")]) == []


def test_a_passing_run_beside_a_failed_retry_still_counts() -> None:
    runs = [_run("failure"), _run("success")]
    assert rg.gate_problems(SHA, on_main=True, runs=runs) == []


def test_a_tag_off_main_is_refused() -> None:
    problems = rg.gate_problems(SHA, on_main=False, runs=[_run("success")])
    assert len(problems) == 1 and "not on main" in problems[0]


@pytest.mark.parametrize(
    "runs",
    [
        [],
        [_run("failure")],
        [_run("cancelled")],
        [_run(None, status="in_progress")],
        [{**_run("success"), "head_sha": "b" * 40}],
    ],
)
def test_no_passing_ci_run_for_the_tagged_commit_is_refused(
    runs: list[dict[str, Any]],
) -> None:
    problems = rg.gate_problems(SHA, on_main=True, runs=runs)
    assert len(problems) == 1 and "no successful ci.yml run" in problems[0]
    assert SHA in problems[0]


def test_both_problems_are_named_together() -> None:
    assert len(rg.gate_problems(SHA, on_main=False, runs=[])) == 2


def test_release_waits_on_the_gate_job() -> None:
    flow = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))
    gate = flow["jobs"]["gate"]
    assert gate["runs-on"] == "ubuntu-latest"
    assert gate["permissions"] == {"contents": "read", "actions": "read"}
    checkout = next(s for s in gate["steps"] if "checkout" in s.get("uses", ""))
    assert checkout["with"]["fetch-depth"] == 0
    assert any("scripts/release_gate.py" in s.get("run", "") for s in gate["steps"])
    assert flow["jobs"]["release"]["needs"] == "gate"
