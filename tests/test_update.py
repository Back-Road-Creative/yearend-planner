"""The update flow with a fake release zip: a 'python' folder whose interpreter is a
shell stub that prints the selfcheck line, so the swap logic is tested on any OS."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from planner.engine import update as upd

STUB_OK = "#!/bin/sh\necho 'stub selfcheck ok'\n"
STUB_BAD = "#!/bin/sh\necho broken >&2\nexit 1\n"
EXEC_MODE = 0o755


def make_release(tmp_path: Path, version: str, ok: bool = True) -> Path:
    z = tmp_path / f"rel-{version}.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("VERSION", version + "\n")
        zf.writestr("planner.cmd", "rem\n")
        zf.writestr("planner/__init__.py", "")
        info = zipfile.ZipInfo("python/python")
        info.external_attr = EXEC_MODE << 16
        zf.writestr(info, STUB_OK if ok else STUB_BAD)
    return z


def install_live(root: Path, version: str) -> None:
    (root / "python").mkdir(parents=True)
    (root / "planner").mkdir()
    (root / "VERSION").write_text(version + "\n")
    (root / "data" / "private").mkdir(parents=True)
    (root / "data" / "private" / "keep.txt").write_text("mine")


def test_bad_sha_is_refused_before_anything_is_touched(tmp_path: Path) -> None:
    z = make_release(tmp_path, "0.2.0")
    root = tmp_path / "root"
    install_live(root, "0.1.0")
    with pytest.raises(upd.UpdateError, match="sha256"):
        upd.apply(z, "00" * 32, root, python_exe="python")
    assert not (root / upd.CANDIDATE).exists()
    assert (root / "VERSION").read_text().strip() == "0.1.0"


@pytest.mark.skipif(sys.platform == "win32", reason="sh stub interpreter")
def test_apply_then_rollback_keeps_data(tmp_path: Path) -> None:
    z = make_release(tmp_path, "0.2.0")
    root = tmp_path / "root"
    install_live(root, "0.1.0")
    res = upd.apply(z, upd.sha256(z), root, python_exe="python")
    assert res.version == "0.2.0" and "stub selfcheck ok" in res.selfcheck
    assert (root / "VERSION").read_text().strip() == "0.2.0"
    assert (root / upd.PREVIOUS / "VERSION").read_text().strip() == "0.1.0"
    assert (root / "data" / "private" / "keep.txt").read_text() == "mine"
    assert not (root / upd.CANDIDATE).exists()
    assert upd.rollback(root) == "0.1.0"
    assert (root / "VERSION").read_text().strip() == "0.1.0"
    assert not (root / upd.PREVIOUS).exists()
    assert (root / "data" / "private" / "keep.txt").read_text() == "mine"


@pytest.mark.skipif(sys.platform == "win32", reason="sh stub interpreter")
def test_failed_candidate_selfcheck_leaves_live_alone(tmp_path: Path) -> None:
    z = make_release(tmp_path, "0.3.0", ok=False)
    root = tmp_path / "root"
    install_live(root, "0.1.0")
    with pytest.raises(upd.UpdateError, match="selfcheck failed"):
        upd.apply(z, upd.sha256(z), root, python_exe="python")
    assert (root / "VERSION").read_text().strip() == "0.1.0"
    assert not (root / upd.PREVIOUS).exists()


def test_rollback_with_nothing_previous_is_refused(tmp_path: Path) -> None:
    with pytest.raises(upd.UpdateError, match="nothing to roll back"):
        upd.rollback(tmp_path)


def test_zip_slip_is_refused(tmp_path: Path) -> None:
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../escape.txt", "x")
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(upd.UpdateError, match="escapes"):
        upd.extract_candidate(z, root)
    assert not (tmp_path / "escape.txt").exists()
