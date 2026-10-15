"""Release gate: refuse a tag unless main's CI already passed the tagged commit.

Run by the `gate` job in .github/workflows/release.yml before anything is built.
It checks two things about the commit the tag names:

1. it is on main (an ancestor of origin/main), and
2. ci.yml has a completed run for that exact commit that succeeded.

Any problem is printed and the exit code is 1, so the release job never starts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any


def gate_problems(sha: str, *, on_main: bool, runs: list[dict[str, Any]]) -> list[str]:
    """What stops `sha` from being released; empty means it may go ahead."""
    problems = []
    if not on_main:
        problems.append(
            f"commit {sha} is not on main: tag a commit that was merged to main"
        )
    passed = any(
        r.get("head_sha") == sha
        and r.get("status") == "completed"
        and r.get("conclusion") == "success"
        for r in runs
    )
    if not passed:
        problems.append(
            f"no successful ci.yml run for commit {sha}: wait for main's CI to "
            "pass (or start it with `gh workflow run ci --ref main`), then re-run "
            "this release"
        )
    return problems


def _on_main(sha: str) -> bool:
    done = subprocess.run(  # noqa: S603
        ["git", "merge-base", "--is-ancestor", sha, "origin/main"],  # noqa: S607
        check=False,
    )
    return done.returncode == 0


def _ci_runs(repo: str, sha: str) -> list[dict[str, Any]]:
    out = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "gh",
            "api",
            f"repos/{repo}/actions/workflows/ci.yml/runs?head_sha={sha}&per_page=100",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    runs: list[dict[str, Any]] = json.loads(out)["workflow_runs"]
    return runs


def main() -> int:
    sha = os.environ["GITHUB_SHA"]
    repo = os.environ["GITHUB_REPOSITORY"]
    problems = gate_problems(sha, on_main=_on_main(sha), runs=_ci_runs(repo, sha))
    for p in problems:
        print(f"release refused: {p}", file=sys.stderr)
    if not problems:
        print(f"release gate passed: {sha} is on main and ci.yml passed it")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
