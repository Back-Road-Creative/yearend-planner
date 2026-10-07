"""Master plan unit 7d: update safety. A staged candidate whose files change
before the swap, an older release, a release or rollback that cannot read the
live ledger, a candidate that fails, and one with older tax rules. The fake
releases are test_update's: a shell stub interpreter, so these skip on Windows
(windows_proof drives the real release there)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import _swap_staged, app
from planner.engine import update as upd
from planner.ledger import db
from planner.paths import Layout
from tests.test_update import install_live, make_release

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="sh stub interpreter")
runner = CliRunner()


@pytest.fixture(autouse=True)
def _engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upd, "live_years", lambda: ())
    monkeypatch.setattr(upd, "installed_engine", lambda: "2.21.0")


def _staged(tmp_path: Path, version: str = "0.2.0") -> Path:
    root = tmp_path / "root"
    install_live(root, "0.1.0")
    z = make_release(tmp_path, version)
    upd.stage(z, upd.sha256(z), root, python_exe="python")
    return root


@pytest.mark.parametrize(
    ("touch", "how"),
    [
        (lambda c: (c / "planner" / "__init__.py").write_text("x = 1\n"), "changed"),
        (lambda c: (c / "planner.cmd").unlink(), "missing"),
        (lambda c: (c / "python" / "evil.dll").write_bytes(b"MZ"), "added"),
    ],
)
def test_a_candidate_changed_after_its_selfcheck_is_removed_not_swapped(
    tmp_path: Path, touch: object, how: str
) -> None:
    root = _staged(tmp_path)
    touch(root / upd.CANDIDATE)  # type: ignore[operator]
    with pytest.raises(upd.UpdateError, match=f"changed after its selfcheck.*{how}"):
        upd.apply_staged(root)
    assert not (root / upd.CANDIDATE).exists()
    assert (root / "VERSION").read_text().strip() == "0.1.0"
    assert not (root / upd.PREVIOUS).exists()


def test_the_launcher_swap_checks_the_files_before_writing_its_script(
    tmp_path: Path,
) -> None:
    root = _staged(tmp_path)
    (root / upd.CANDIDATE / "python" / "python").write_text("#!/bin/sh\n")
    with pytest.raises(upd.UpdateError, match="python/python: changed"):
        upd.write_swap(root, "in", rerun=True)
    assert not (root / "data" / "update" / "swap.cmd").exists()
    root2 = tmp_path / "two"
    root2.mkdir()
    clean = _staged(root2)
    assert upd.write_swap(clean, "in", rerun=True).exists()


def test_at_start_a_changed_candidate_is_dropped_and_this_release_runs_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _staged(tmp_path)
    (root / upd.CANDIDATE / "VERSION").write_text("9.9.9\n")
    monkeypatch.delenv("PLANNER_LAUNCHER", raising=False)
    _swap_staged(Layout(root))  # returns: the launch carries on
    assert "update not applied: the staged update 0.2.0 changed" in (
        capsys.readouterr().err
    )
    assert (root / "VERSION").read_text().strip() == "0.1.0"
    assert upd.staged(root) is None


def test_an_older_release_waits_for_allow_downgrade(tmp_path: Path) -> None:
    root = tmp_path / "root"
    install_live(root, "0.3.0")
    z = make_release(tmp_path, "0.2.0")
    with pytest.raises(upd.UpdateError, match=r"0\.2\.0 is older than the installed"):
        upd.stage(z, upd.sha256(z), root, python_exe="python")
    assert not (root / upd.CANDIDATE).exists()
    upd.stage(z, upd.sha256(z), root, python_exe="python", allow_downgrade=True)
    assert "updated 0.3.0 -> 0.2.0" in upd.apply_staged(root)


def test_the_command_refuses_a_downgrade_and_names_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    install_live(root, "0.3.0")
    monkeypatch.setenv("PLANNER_HOME", str(root))
    monkeypatch.delenv("PLANNER_LAUNCHER", raising=False)
    z = make_release(tmp_path, "0.2.0")
    args = ["update", str(z), "--sha256", upd.sha256(z)]
    res = runner.invoke(app, args)
    assert res.exit_code == 1 and "--allow-downgrade installs it anyway" in res.output
    res = runner.invoke(app, [*args, "--allow-downgrade"])
    assert res.exit_code == 0, res.output
    assert (root / "VERSION").read_text().strip() == "0.2.0"


def _ledger(root: Path) -> Path:
    path = root / "data" / "ledger" / "planner.db"
    db.connect(path).close()
    return path


def test_a_release_that_reads_an_older_ledger_is_refused_even_when_allowed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    install_live(root, "0.3.0")
    _ledger(root)
    old = make_release(tmp_path, "0.2.0", schema=db.SCHEMA_VERSION - 1)
    with pytest.raises(upd.UpdateError, match="would refuse it"):
        upd.stage(old, upd.sha256(old), root, python_exe="python", allow_downgrade=True)
    nameless = make_release(tmp_path, "0.4.0")  # names no schema: cannot be shown
    with pytest.raises(upd.UpdateError, match=r"schema \(none named\)"):
        upd.stage(nameless, upd.sha256(nameless), root, python_exe="python")
    assert not (root / upd.CANDIDATE).exists()
    same = make_release(tmp_path, "0.5.0", schema=db.SCHEMA_VERSION)
    upd.stage(same, upd.sha256(same), root, python_exe="python")
    assert upd.staged(root) is not None


def test_a_rollback_the_ledger_has_outgrown_is_refused_and_names_the_copy(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    install_live(root, "0.1.0")
    old = db.SCHEMA_VERSION - 1
    (root / "planner" / "ledger").mkdir(parents=True)
    (root / "planner" / "ledger" / "db.py").write_text(f"SCHEMA_VERSION = {old}\n")
    z = make_release(tmp_path, "0.2.0", schema=db.SCHEMA_VERSION)
    upd.apply(z, upd.sha256(z), root, python_exe="python")
    ledger = _ledger(root)
    ledger.with_name(f"planner.db.schema{old}.bak").write_bytes(b"")
    with pytest.raises(upd.UpdateError, match=rf"planner\.db\.schema{old}\.bak"):
        upd.rollback(root)
    with pytest.raises(upd.UpdateError, match="previous release reads"):
        upd.write_swap(root, "back", rerun=False)
    assert (root / "VERSION").read_text().strip() == "0.2.0"
    assert (root / upd.PREVIOUS / "VERSION").read_text().strip() == "0.1.0"


def test_a_failed_or_unfinished_candidate_is_never_swapped(tmp_path: Path) -> None:
    root = tmp_path / "root"
    install_live(root, "0.1.0")
    bad = make_release(tmp_path, "0.3.0", ok=False)
    with pytest.raises(upd.UpdateError, match="selfcheck"):
        upd.stage(bad, upd.sha256(bad), root, python_exe="python")
    assert not (root / upd.CANDIDATE).exists()
    # a stage cut off after the unzip writes no READY.json: nothing swaps in
    upd.extract_candidate(make_release(tmp_path, "0.2.0"), root)
    assert upd.staged(root) is None
    with pytest.raises(upd.UpdateError, match="no update is staged"):
        upd.apply_staged(root)
    assert (root / "VERSION").read_text().strip() == "0.1.0"


def test_older_tax_rules_wait_for_allow_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upd, "live_years", lambda: tuple(range(2018, 2027)))
    root = tmp_path / "root"
    install_live(root, "0.1.0")
    stale = make_release(tmp_path, "0.2.0", years="2018-2025")
    with pytest.raises(upd.UpdateError, match="does not publish 2026"):
        upd.stage(stale, upd.sha256(stale), root, python_exe="python")
    engine = make_release(tmp_path, "0.2.1", years="2018-2026", engine="2.20.0")
    with pytest.raises(upd.UpdateError, match=r"2\.20\.0 is older than the installed"):
        upd.stage(engine, upd.sha256(engine), root, python_exe="python")
    fresh = make_release(tmp_path, "0.2.2", years="2018-2027", engine="2.22.0")
    assert upd.stage(fresh, upd.sha256(fresh), root, python_exe="python").years
    upd.stage(stale, upd.sha256(stale), root, python_exe="python", allow_downgrade=True)
    assert upd.staged(root) is not None
