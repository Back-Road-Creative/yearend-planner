"""Phase 8c privacy audit: the repository tracks no personal data, and the
release zip is built from tracked files only, so a sentinel planted in data/,
out/ or an untracked config file never reaches it."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SENTINEL = b"PRIVATE-SENTINEL-7f3a9c"
PRIVATE_TOPS = ("data/", "out/", "dist/", "restore-staging/", "data-previous/")


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_COUNT": "1"}
    env |= {"GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}
    git = shutil.which("git") or "git"
    return subprocess.run(  # noqa: S603  (fixed argv)
        [git, *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout


def _load_build_release() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_release", ROOT / "scripts" / "build_release.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="needs a git checkout")
def test_the_repository_tracks_no_personal_data() -> None:
    tracked = _git(ROOT, "ls-files").splitlines()
    assert not [p for p in tracked if p.startswith(PRIVATE_TOPS)]
    assert not [p for p in tracked if p.endswith((".db", ".zip", ".sqlite"))]
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for top in PRIVATE_TOPS:
        assert top in ignored.split(), top


def test_release_zip_ships_tracked_files_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    br = _load_build_release()
    repo = tmp_path / "checkout"
    for name in (*br.SHIP, *br.SHIP_DIRS):
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, repo / name, ignore=shutil.ignore_patterns("__py*"))
        else:
            repo.mkdir(exist_ok=True)
            shutil.copy2(src, repo / name)
    shutil.copy2(ROOT / "pyproject.toml", repo / "pyproject.toml")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    # personal files a developer's own planner folder holds, never added to git
    for rel in (
        "data/ledger/planner.db",
        "out/index.html",
        "config/my-notes.yaml",
        "planner/scratch.py",
    ):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(SENTINEL)
    monkeypatch.setattr(br, "ROOT", repo)
    monkeypatch.setattr(br, "DIST", tmp_path / "dist")
    monkeypatch.setattr(br, "STAGE", tmp_path / "dist" / "stage")
    monkeypatch.setattr(br, "build_python", lambda stage: None)  # no download

    out = br.zip_stage(br.stage_tree())
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "planner/cli.py" in names and "config/thresholds.yaml" in names
        assert not [n for n in names if n.startswith(PRIVATE_TOPS)]
        assert "config/my-notes.yaml" not in names
        assert "planner/scratch.py" not in names
        for n in names:
            assert SENTINEL not in zf.read(n), n

    # the gate stops a staged tree that holds private content anyway
    leak = br.STAGE / "data" / "planner.db"
    leak.parent.mkdir(parents=True)
    leak.write_bytes(SENTINEL)
    with pytest.raises(SystemExit, match="data/planner.db"):
        br.audit(br.STAGE)
