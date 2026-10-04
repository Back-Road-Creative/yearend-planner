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
SENTINEL = b"PRIVATE-" + b"SENTINEL-7f3a9c"  # split: the scan below finds it
# ID-shaped strings, split so this file passes its own scan
ACCT = "44172" + "03398"
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
        # the candidate's regression runs on these cases, so they ship
        assert "planner/engine/reference.yaml" in names
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


def test_tracked_files_hold_no_real_looking_figures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every tracked text file is scanned for ID-shaped strings (an SSN or EIN
    shape, an account-number run of 9-17 digits) and the planted sentinel; the
    release build runs the same scan before it stages anything."""
    br = _load_build_release()
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")  # a worktree owned by another user
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "*")
    if (ROOT / ".git").exists():
        assert br.figure_scan(ROOT) == []
    repo = tmp_path / "checkout"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "notes.md").write_text("synthetic only\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "pdfgen.py").write_text("x = '0000000000'\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    assert br.figure_scan(repo) == []  # a run of zeros is a placeholder
    for text in (
        f"Brokerage account {ACCT} at year end",
        "SSN 123-45-" + "6789",
        "employer EIN 12-" + "3456789",
        SENTINEL.decode(),
    ):
        (repo / "docs" / "notes.md").write_text(text + "\n", encoding="utf-8")
        (hit,) = br.figure_scan(repo)
        assert hit.startswith("docs/notes.md:1: "), hit
    (repo / "docs" / "notes.md").write_text("synthetic only\n", encoding="utf-8")
    (repo / "untracked.md").write_text(f"acct {ACCT}\n", encoding="utf-8")
    assert br.figure_scan(repo) == []  # only what git would publish


def test_release_build_scans_before_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    br = _load_build_release()
    monkeypatch.setattr(br, "figure_scan", lambda root: [f"docs/x.md:1: {ACCT}"])
    monkeypatch.setattr(br, "stage_tree", lambda: pytest.fail("staged anyway"))
    with pytest.raises(SystemExit, match="docs/x.md:1"):
        br.main()


def test_windows_proof_runs_the_synthetic_inbox_offline() -> None:
    """The release proof drops the synthetic PDFs (never a real document) in the
    unzipped release's inbox with the network blocked and checks the page."""
    proof = (ROOT / "scripts" / "windows_proof.ps1").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/synthetic_inbox.py" in ci and "-Inbox" in ci
    step = proof[proof.index("== 4. network blocked") : proof.index("== 5.")]
    for needed in ("data\\inbox", "run --quiet --no-update-check", "index.html"):
        assert needed in step, needed
    assert "Example Bank (synthetic)" in step


def test_synthetic_inbox_writes_only_synthetic_pdfs(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "synthetic_inbox", ROOT / "scripts" / "synthetic_inbox.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    written = module.write(tmp_path / "inbox")
    assert sorted(p.name for p in written) == ["div.pdf", "int.pdf"]
    assert all(b"synthetic" in p.read_bytes() for p in written)
