"""Phase 6b: the update check, staging, and the launcher-side swap.

The release is the sh-stub zip of test_update: its 'python/python' prints a
selfcheck line (or fails), so every path but the Windows folder moves runs
here; windows_proof.ps1 runs those moves for real."""

from __future__ import annotations

import json
import shutil
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from planner.cli import app
from planner.dashboard import page
from planner.engine import feed
from planner.engine import update as upd
from planner.paths import Layout
from tests.conftest import stub_interpreter, write_baseline

runner = CliRunner()
posix = pytest.mark.skipif(sys.platform == "win32", reason="sh stub interpreter")
STUB_OK = stub_interpreter()
STUB_BAD = "#!/bin/sh\necho broken >&2\nexit 1\n"


def release(
    folder: Path, version: str, ok: bool = True, engine: str | None = None
) -> Path:
    z = folder / f"yearend-planner-{version}-win64.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("VERSION", version + "\n")
        zf.writestr("planner.cmd", "rem\n")
        zf.writestr("planner/__init__.py", "")
        zf.writestr("config/thresholds.yaml", "2026:\n  a: {value: 1, source: rel}\n")
        info = zipfile.ZipInfo("python/python")
        info.external_attr = 0o755 << 16
        zf.writestr(info, STUB_OK if ok else STUB_BAD)
        if engine:
            dist = f"python/Lib/site-packages/policyengine_us-{engine}.dist-info"
            zf.writestr(
                f"{dist}/METADATA", f"Name: policyengine-us\nVersion: {engine}\n"
            )
    return z


def publish(folder: Path, z: Path, version: str) -> str:
    sha = z.with_name(z.name + ".sha256")
    sha.write_text(f"{upd.sha256(z)}  {z.name}\n", encoding="ascii")
    rel = {
        "tag_name": f"v{version}",
        "assets": [
            {"name": z.name, "browser_download_url": z.as_uri()},
            {"name": sha.name, "browser_download_url": sha.as_uri()},
        ],
    }
    path = folder / "latest.json"
    path.write_text(json.dumps(rel), encoding="utf-8")
    return path.as_uri()


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    (planner_home / "VERSION").write_text("0.1.0\n")
    (planner_home / "python").mkdir()
    lay = Layout(planner_home)
    lay.ensure()
    write_baseline(lay.root)
    return lay


def _stage_ready(root: Path) -> Path:
    """A staged candidate as stage() leaves it, file list included (write_swap
    checks the files before writing its script)."""
    cand = root / upd.CANDIDATE
    cand.mkdir(exist_ok=True)
    (cand / "VERSION").write_text("0.2.0\n")
    ready = {"version": "0.2.0", "selfcheck": "ok", "files": upd.manifest(cand)}
    (cand / upd.READY).write_text(json.dumps(ready))
    return cand


def test_swap_script_is_crlf_ascii_and_names_its_folders(lay: Layout) -> None:
    _stage_ready(lay.root)
    script = upd.write_swap(lay.root, "in", rerun=True)
    raw = script.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n") and raw.isascii()
    text = raw.decode("ascii")
    assert 'set "S=python-candidate"' in text and 'set "P=python-previous"' in text
    assert "-m planner update --finish" in text
    assert '"%R%python\\python.exe" -m planner %*' in text
    back = upd.write_swap(lay.root, "back", rerun=False).read_text(encoding="ascii")
    assert 'set "S=python-previous"' in back and "planner %*" not in back
    pending = json.loads((lay.data / "update" / "pending.json").read_text())
    assert pending == {"mode": "back", "from": "0.1.0"}


def test_finish_reports_and_tidies_after_the_launcher_moved_the_folders(
    lay: Layout,
) -> None:
    cand = _stage_ready(lay.root)
    upd.write_swap(lay.root, "in", rerun=False)
    # what swap.cmd leaves: the new release live, READY.json in the candidate
    (lay.root / "VERSION").write_text("0.2.0\n")
    (cand / "VERSION").unlink()
    ready = {"version": "0.2.0", "selfcheck": "ok", "carried": ["2027.x"]}
    (cand / upd.READY).write_text(json.dumps(ready))
    msg = upd.finish(lay.root)
    assert (
        msg
        == "updated 0.1.0 -> 0.2.0; candidate selfcheck: ok; kept your limits: 2027.x"
    )
    assert not cand.exists()
    # cmd is still reading swap.cmd (the rerun line comes after --finish):
    # deleting it here ended the script with "The batch file cannot be found"
    assert (lay.data / "update" / "swap.cmd").exists()
    with pytest.raises(upd.UpdateError, match="no update is waiting"):
        upd.finish(lay.root)


@posix
def test_manual_update_from_the_launcher_stages_then_exits_75(
    lay: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    z = release(tmp_path, "0.2.0")
    monkeypatch.setenv("PLANNER_LAUNCHER", "cmd")
    r = runner.invoke(app, ["update", str(z), "--sha256", upd.sha256(z)])
    assert r.exit_code == upd.LAUNCHER_SWAP, r.output
    assert (lay.data / "update" / "swap.cmd").exists()
    assert upd.staged(lay.root)["version"] == "0.2.0"  # type: ignore[index]
    assert (lay.root / "VERSION").read_text().strip() == "0.1.0"  # untouched
    r = runner.invoke(app, ["update", "--rollback"])
    assert r.exit_code == 1 and "nothing to roll back" in r.output


@posix
def test_feed_stages_a_newer_release_keeps_hand_limits_and_run_swaps_it(
    lay: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thresholds = lay.config / "thresholds.yaml"
    mine = yaml.safe_load(thresholds.read_text(encoding="utf-8"))
    mine[2027] = {"hsa_limit_self": {"value": 4500, "source": "typed by hand"}}
    thresholds.write_text(yaml.safe_dump(mine), encoding="utf-8")
    monkeypatch.setenv(
        "PLANNER_UPDATE_FEED", publish(tmp_path, release(tmp_path, "0.2.0"), "0.2.0")
    )
    assert feed.check(lay).startswith("update 0.2.0 is ready")
    cand = lay.root / upd.CANDIDATE
    carried = yaml.safe_load((cand / "config" / "thresholds.yaml").read_text())
    assert carried[2026]["a"]["source"] == "rel"
    assert carried[2027]["hsa_limit_self"]["value"] == 4500
    assert feed.check(lay) == ""  # already staged
    assert not list((lay.data / "update").glob("*.zip"))
    # from planner.cmd, run swaps first and reruns itself on the new release
    monkeypatch.setenv("PLANNER_LAUNCHER", "cmd")
    r = runner.invoke(app, ["run", "--quiet"])
    assert r.exit_code == upd.LAUNCHER_SWAP, r.output
    assert "planner %*" in (lay.data / "update" / "swap.cmd").read_text()
    # without the launcher (a developer clone) the swap happens in-process
    monkeypatch.delenv("PLANNER_LAUNCHER")
    msg = upd.apply_staged(lay.root)
    assert msg.startswith("updated 0.1.0 -> 0.2.0")
    assert (lay.root / upd.PREVIOUS / "VERSION").read_text().strip() == "0.1.0"
    assert feed.check(lay) == ""  # nothing newer now


@posix
@pytest.mark.engine
def test_a_failing_candidate_is_held_and_shown_until_superseded(
    lay: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = publish(tmp_path, release(tmp_path, "0.3.0", ok=False), "0.3.0")
    monkeypatch.setenv("PLANNER_UPDATE_FEED", url)
    line = feed.check(lay, date(2026, 10, 3))
    assert line.startswith("update 0.3.0 held: candidate selfcheck failed")
    assert not (lay.root / upd.CANDIDATE).exists()
    assert (lay.root / "VERSION").read_text().strip() == "0.1.0"
    assert feed.check(lay) == ""  # the same release is not retried every launch
    pg = page.gather(lay, 2026, date(2026, 10, 3))
    texts = [a.text for a in pg.alerts if a.kind == "update"]
    assert texts == [
        "engine update 0.3.0 held on 2026-10-03: candidate selfcheck failed:; "
        "still on 0.1.0"
    ]
    (lay.root / "VERSION").write_text("0.3.0\n")  # installed by hand since
    assert feed.held(lay) is None and not (lay.data / feed.HELD).exists()


def test_no_feed_offline_or_plain_http_says_nothing(
    lay: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert feed.check(lay) == ""  # PLANNER_UPDATE_FEED=off (conftest)
    for url in ("https://127.0.0.1:9/latest", "http://example.invalid/latest"):
        monkeypatch.setenv("PLANNER_UPDATE_FEED", url)
        assert feed.check(lay) == ""
    monkeypatch.delenv("PLANNER_UPDATE_FEED")
    assert feed.feed_url(lay) == (
        "https://api.github.com/repos/Back-Road-Creative/yearend-planner/releases/latest"
    )
    assert upd.version_key("v0.2.10") > upd.version_key("0.2.9")
    assert upd.version_key("dev") == ()
    shutil.rmtree(lay.root / "python")
    assert not upd.launcher_swaps(lay.root)


def opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every address the feed module opens, in order."""
    seen: list[str] = []
    real = feed._open

    def spy(url: str, timeout: int) -> bytes:
        seen.append(url)
        return real(url, timeout)

    monkeypatch.setattr(feed, "_open", spy)
    return seen


def test_check_runs_at_most_weekly(
    lay: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = publish(tmp_path, release(tmp_path, "0.1.0"), "0.1.0")  # nothing newer
    monkeypatch.setenv("PLANNER_UPDATE_FEED", url)
    seen = opens(monkeypatch)
    assert feed.check(lay, date(2026, 10, 1)) == ""
    assert feed.check(lay, date(2026, 10, 4)) == ""  # 3 days later: skipped
    assert seen == [url]
    record = json.loads((lay.data / "update" / feed.LAST_CHECK).read_text())
    assert record["date"] == "2026-10-01" and record["status"] == "current"
    assert feed.check(lay, date(2026, 10, 8)) == ""  # a week on: asks again
    assert seen == [url, url]
    assert feed.last_check(lay)["date"] == "2026-10-08"  # type: ignore[index]
    feed.check(lay, date(2026, 10, 9), force=True)  # planner update --check
    assert seen == [url, url, url]


def test_an_unreachable_feed_is_recorded_but_retried_next_launch(
    lay: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLANNER_UPDATE_FEED", (tmp_path / "gone.json").as_uri())
    seen = opens(monkeypatch)
    assert feed.check(lay, date(2026, 10, 1)) == ""
    assert feed.last_check(lay)["status"] == "offline"  # type: ignore[index]
    assert feed.check(lay, date(2026, 10, 2)) == ""
    assert len(seen) == 2  # no network: a week's wait would hide a release
    publish(tmp_path, release(tmp_path, "0.1.0"), "0.1.0")
    (tmp_path / "gone.json").write_bytes((tmp_path / "latest.json").read_bytes())
    feed.check(lay, date(2026, 10, 3))
    assert feed.last_check(lay)["status"] == "current"  # type: ignore[index]
    assert feed.check(lay, date(2026, 10, 4)) == "" and len(seen) == 3


def test_major_engine_jump_is_held(
    lay: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upd, "installed_engine", lambda: "2.21.0")
    z = release(tmp_path, "0.2.0", engine="3.0.0")
    monkeypatch.setenv("PLANNER_UPDATE_FEED", publish(tmp_path, z, "0.2.0"))
    line = feed.check(lay, date(2026, 10, 3))
    assert (
        line.startswith("update 0.2.0 held:") and "planner update --allow-major" in line
    )
    assert upd.staged(lay.root) is None and not (lay.root / upd.CANDIDATE).exists()
    assert not list((lay.data / "update").glob("*.zip"))
    assert feed.held(lay)["version"] == "0.2.0"  # type: ignore[index]
    assert feed.last_check(lay)["status"] == "held"  # type: ignore[index]
    seen = opens(monkeypatch)
    assert feed.check(lay, date(2026, 10, 20)) == ""  # held: not downloaded again
    assert [u for u in seen if u.endswith(".zip")] == []
    if sys.platform != "win32":
        # planner update --allow-major fetches and stages it past the hold
        r = runner.invoke(app, ["update", "--allow-major"])
        assert r.exit_code == 0 and "update 0.2.0 is ready" in r.output, r.output
        assert upd.staged(lay.root)["version"] == "0.2.0"  # type: ignore[index]
        assert feed.held(lay) is None


def test_manual_zip_of_a_new_major_needs_allow_major(
    lay: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upd, "installed_engine", lambda: "2.21.0")
    z = release(tmp_path, "0.2.0", engine="3.0.0")
    r = runner.invoke(app, ["update", str(z), "--sha256", upd.sha256(z)])
    assert r.exit_code == 1 and "--allow-major" in r.output
    assert not (lay.root / upd.CANDIDATE).exists()
    if sys.platform != "win32":
        r = runner.invoke(
            app, ["update", str(z), "--sha256", upd.sha256(z), "--allow-major"]
        )
        assert r.exit_code == 0, r.output  # no launcher: swapped in-process
        assert (lay.root / "VERSION").read_text().strip() == "0.2.0"
