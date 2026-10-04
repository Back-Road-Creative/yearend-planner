"""The update flow with a fake release zip: a 'python' folder whose interpreter is a
shell stub that prints the selfcheck line, so the swap logic is tested on any OS."""

from __future__ import annotations

import json
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest

from planner.engine import update as upd
from tests.conftest import (
    STUB_BASELINE_VALUE,
    STUB_CASE_KEY,
    stub_interpreter,
    write_baseline,
)

STUB_BAD = "#!/bin/sh\necho broken >&2\nexit 1\n"
EXEC_MODE = 0o755


def make_release(
    tmp_path: Path,
    version: str,
    ok: bool = True,
    years: str | None = None,
    engine: str | None = None,
    regression: float | None = STUB_BASELINE_VALUE,
) -> Path:
    """``years`` is the tax-years line the stub selfcheck prints after its first
    line; ``engine`` the policyengine-us version in the candidate's dist-info;
    ``regression`` the reference value its regression run prints (None: none)."""
    z = tmp_path / f"rel-{version}.zip"
    stub = stub_interpreter(regression)
    if years is not None:
        stub += f"echo 'tax years published: {years}'\n"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("VERSION", version + "\n")
        zf.writestr("planner.cmd", "rem\n")
        zf.writestr("planner/__init__.py", "")
        info = zipfile.ZipInfo("python/python")
        info.external_attr = EXEC_MODE << 16
        zf.writestr(info, stub if ok else STUB_BAD)
        if engine:
            dist = f"python/Lib/site-packages/policyengine_us-{engine}.dist-info"
            zf.writestr(
                f"{dist}/METADATA",
                f"Metadata-Version: 2.1\nName: policyengine-us\nVersion: {engine}\n",
            )
    return z


def install_live(root: Path, version: str) -> None:
    (root / "python").mkdir(parents=True)
    (root / "planner").mkdir()
    (root / "VERSION").write_text(version + "\n")
    (root / "data" / "private").mkdir(parents=True)
    (root / "data" / "private" / "keep.txt").write_text("mine")
    write_baseline(root)


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


@pytest.mark.skipif(sys.platform == "win32", reason="sh stub interpreter")
def test_stage_reports_tax_years(tmp_path: Path) -> None:
    today = date(2026, 10, 3)
    root = tmp_path / "root"
    install_live(root, "0.1.0")
    z = make_release(tmp_path, "0.2.0", years="2015, 2018-2026")
    res = upd.stage(z, upd.sha256(z), root, python_exe="python", today=today)
    assert res.years == (2015, *range(2018, 2027))
    assert res.missing_next == 2027
    assert "tax years 2015, 2018-2026" in res.years_note
    assert "2027 is not modelled yet" in res.years_note
    # the note survives into the swap message
    assert "2027 is not modelled yet" in upd.apply_staged(root)

    root2 = tmp_path / "root2"
    install_live(root2, "0.1.0")
    z2 = make_release(tmp_path, "0.3.0", years="2018-2027")
    res2 = upd.stage(z2, upd.sha256(z2), root2, python_exe="python", today=today)
    assert res2.years[-1] == 2027 and res2.missing_next is None
    assert "not modelled" not in res2.years_note

    root3 = tmp_path / "root3"
    install_live(root3, "0.1.0")
    z3 = make_release(tmp_path, "0.4.0")  # an older release that prints no years
    res3 = upd.stage(z3, upd.sha256(z3), root3, python_exe="python", today=today)
    assert res3.years == () and res3.missing_next is None
    assert "does not report" in res3.years_note


def test_candidate_engine_version_is_read_from_its_dist_info(tmp_path: Path) -> None:
    z = make_release(tmp_path, "0.2.0", engine="3.0.1")
    cand = upd.extract_candidate(z, tmp_path)
    assert upd.candidate_engine(cand) == "3.0.1"
    bare = upd.extract_candidate(make_release(tmp_path, "0.2.1"), tmp_path)
    assert upd.candidate_engine(bare) is None


def test_a_new_major_engine_or_planner_is_held_unless_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upd, "installed_engine", lambda: "2.21.0")
    root = tmp_path / "root"
    install_live(root, "0.1.0")
    z = make_release(tmp_path, "0.2.0", engine="3.0.0")
    with pytest.raises(upd.MajorUpdateHeld, match=r"planner update --allow-major"):
        upd.stage(z, upd.sha256(z), root, python_exe="python")
    assert not (root / upd.CANDIDATE).exists()  # nothing staged
    # a minor engine step and the same major pass the gate
    ok = make_release(tmp_path, "0.2.1", engine="2.22.0")
    if sys.platform != "win32":
        assert (
            upd.stage(ok, upd.sha256(ok), root, python_exe="python").version == "0.2.1"
        )
    # a new planner major is held the same way
    big = make_release(tmp_path, "1.0.0")
    with pytest.raises(upd.MajorUpdateHeld, match=r"planner 1.0.0"):
        upd.stage(big, upd.sha256(big), root, python_exe="python")
    if sys.platform != "win32":
        again = upd.stage(
            big, upd.sha256(big), root, python_exe="python", allow_major=True
        )
        assert again.version == "1.0.0"


@pytest.mark.skipif(sys.platform == "win32", reason="sh stub interpreter")
@pytest.mark.parametrize(("printed", "held"), [(1006.0, True), (1004.0, False)])
def test_candidate_off_baseline_by_6_is_held(
    tmp_path: Path, printed: float, held: bool
) -> None:
    """The reference value is 1000.00 in the baseline: a candidate that prints
    $6 off is refused, $4 off passes (the limit is $5)."""
    root = tmp_path / "root"
    install_live(root, "0.1.0")
    z = make_release(tmp_path, "0.2.0", regression=printed)
    if held:
        with pytest.raises(upd.UpdateError, match="regression") as err:
            upd.stage(z, upd.sha256(z), root, python_exe="python")
        text = str(err.value)
        assert STUB_CASE_KEY in text and "1,000.00" in text and "1,006.00" in text
        assert upd.staged(root) is None
        assert (root / "VERSION").read_text().strip() == "0.1.0"
    else:
        assert upd.stage(z, upd.sha256(z), root, python_exe="python").version == "0.2.0"


@pytest.mark.skipif(sys.platform == "win32", reason="sh stub interpreter")
def test_the_limit_is_five_dollars_exactly(tmp_path: Path) -> None:
    root = tmp_path / "root"
    install_live(root, "0.1.0")
    z = make_release(tmp_path, "0.2.0", regression=STUB_BASELINE_VALUE + 5.0)
    assert upd.stage(z, upd.sha256(z), root, python_exe="python").version == "0.2.0"
    z2 = make_release(tmp_path, "0.2.1", regression=STUB_BASELINE_VALUE - 5.01)
    with pytest.raises(upd.UpdateError, match="regression"):
        upd.stage(z2, upd.sha256(z2), root, python_exe="python")


@pytest.mark.skipif(sys.platform == "win32", reason="sh stub interpreter")
def test_a_candidate_that_runs_no_regression_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    install_live(root, "0.1.0")
    z = make_release(tmp_path, "0.2.0", regression=None)
    with pytest.raises(upd.UpdateError, match="no regression values"):
        upd.stage(z, upd.sha256(z), root, python_exe="python")
    assert not (root / upd.PREVIOUS).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="sh stub interpreter")
def test_a_candidate_missing_a_baseline_value_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    install_live(root, "0.1.0")
    path = root / "data" / "engine-baseline.json"
    record = json.loads(path.read_text())
    record["engines"]["2.21.0"]["values"]["other_case.agi"] = 5.0
    path.write_text(json.dumps(record))
    z = make_release(tmp_path, "0.2.0")
    with pytest.raises(upd.UpdateError, match=r"other_case\.agi.*not in the candidate"):
        upd.stage(z, upd.sha256(z), root, python_exe="python")


@pytest.mark.skipif(sys.platform == "win32", reason="sh stub interpreter")
def test_a_regression_failure_is_checked_against_the_pinned_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no baseline yet, staging records the running engine's output first
    and holds the candidate to it."""
    from planner.engine import verify

    monkeypatch.setattr(verify, "engine_version", lambda: "2.21.0")
    monkeypatch.setattr(
        verify,
        "regression_values",
        lambda path=verify.REFERENCE: {STUB_CASE_KEY: 1000.0},
    )
    root = tmp_path / "root"
    install_live(root, "0.1.0")
    (root / "data" / "engine-baseline.json").unlink()
    z = make_release(tmp_path, "0.2.0", regression=1009.0)
    with pytest.raises(upd.UpdateError, match="regression"):
        upd.stage(z, upd.sha256(z), root, python_exe="python")
    assert verify.load_baseline(root)["pinned"] == "2.21.0"  # type: ignore[index]
