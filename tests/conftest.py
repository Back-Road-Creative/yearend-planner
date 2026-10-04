from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_update_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never reach the real release feed; test_feed sets its own."""
    monkeypatch.setenv("PLANNER_UPDATE_FEED", "off")
    monkeypatch.delenv("PLANNER_LAUNCHER", raising=False)


@pytest.fixture
def repo_root() -> Path:
    return ROOT


@pytest.fixture
def planner_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway planner folder with the repo's config/ copied in."""
    import shutil

    home = tmp_path / "planner home ü"
    home.mkdir()
    shutil.copytree(ROOT / "config", home / "config")
    monkeypatch.setenv("PLANNER_HOME", str(home))
    return home


# The sh-stub interpreter of a fake release zip (test_update, test_feed) prints
# one selfcheck line and, when asked for the regression, one reference value.
STUB_CASE_KEY = "stub_case.fed_total_tax"
STUB_BASELINE_VALUE = 1000.0


def stub_interpreter(regression: float | None = STUB_BASELINE_VALUE) -> str:
    """The shell script a fake release ships as its python/python."""
    script = "#!/bin/sh\necho 'stub selfcheck ok'\n"
    if regression is not None:
        script += (
            'case "$*" in *--regression*) '
            f"echo 'regression {STUB_CASE_KEY} {regression:.2f}';; esac\n"
        )
    return script


def write_baseline(root: Path, engine: str = "2.21.0") -> None:
    """data/engine-baseline.json as a first ``planner run`` leaves it, with the
    value the stub interpreter reproduces."""
    import json

    path = root / "data" / "engine-baseline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "pinned": engine,
        "engines": {
            engine: {
                "recorded": "2026-10-01",
                "values": {STUB_CASE_KEY: STUB_BASELINE_VALUE},
            }
        },
    }
    path.write_text(json.dumps(record), encoding="utf-8")
