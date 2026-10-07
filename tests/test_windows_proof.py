"""The Windows proof's throwaway account password must always pass Windows'
complexity rule (three character classes). A plain 24-of-62 draw can miss
digits, and New-LocalUser then throws InvalidPasswordException (PR #28's
windows-proof job). Every zip proof also runs the synthetic inbox."""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "windows_proof.ps1"


def _function() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"^function New-ProofPassword \{.*?^\}", text, re.M | re.S)
    assert m, "windows_proof.ps1 defines New-ProofPassword"
    return m.group(0)


def test_standard_user_password_comes_from_the_class_safe_generator() -> None:
    _function()
    text = SCRIPT.read_text(encoding="utf-8")
    assert "$pw = New-ProofPassword" in text
    assert "Get-Random -Count 24" not in text


@pytest.mark.skipif(
    not (shutil.which("pwsh") or shutil.which("powershell")),
    reason="needs PowerShell (runs on the windows-latest job)",
)
def test_every_generated_password_has_upper_lower_and_digit() -> None:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell
    probe = _function() + "\n1..500 | ForEach-Object { New-ProofPassword }\n"
    out = subprocess.run(  # noqa: S603
        [shell, "-NoProfile", "-Command", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout.split()
    assert len(out) == 500
    for pw in out:
        assert len(pw) >= 24
        assert re.search(r"[A-Z]", pw) and re.search(r"[a-z]", pw)
        assert re.search(r"[0-9]", pw)


def test_every_zip_proof_runs_the_synthetic_inbox() -> None:
    """The tagged release proves the exact zip it ships with the same inbox run
    as CI (master plan F15): every step that calls windows_proof.ps1 builds the
    synthetic inbox and passes it with -Inbox."""
    root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    proofs = []
    for name in ("ci.yml", "release.yml"):
        flow = yaml.safe_load((root / name).read_text(encoding="utf-8"))
        for job in flow["jobs"].values():
            for step in job.get("steps", []):
                run = step.get("run", "")
                if "windows_proof.ps1" in run:
                    proofs.append((name, run))
    assert {name for name, _ in proofs} == {"ci.yml", "release.yml"}
    for name, run in proofs:
        assert "scripts/synthetic_inbox.py" in run, name
        assert "-Inbox $inbox" in run, name


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _flow(name: str) -> dict[str, Any]:
    flow: dict[str, Any] = yaml.safe_load(
        (WORKFLOWS / name).read_text(encoding="utf-8")
    )
    return flow


def test_ci_proves_the_zip_on_both_windows_lines() -> None:
    """Phase 10, unit 7f: Server 2025 (Windows 11 24H2's build) keeps the
    required check name windows-proof; Server 2022 runs as windows-proof-2022.
    One leg uploads the zip."""
    job = _flow("ci.yml")["jobs"]["windows-proof"]
    assert job["strategy"]["matrix"]["os"] == ["windows-2025", "windows-2022"]
    assert job["strategy"]["fail-fast"] is False
    assert job["runs-on"] == "${{ matrix.os }}"
    name = job["name"]
    assert "matrix.os == 'windows-2025' && 'windows-proof'" in name
    assert "'windows-proof-2022'" in name
    uploads = [s for s in job["steps"] if "upload-artifact" in s.get("uses", "")]
    assert [s.get("if") for s in uploads] == ["matrix.os == 'windows-2025'"]


def test_release_drafts_only_after_both_windows_lines_prove_the_same_zip() -> None:
    jobs = _flow("release.yml")["jobs"]
    assert jobs["release"]["runs-on"] == "windows-2025"
    older = jobs["prove-windows-2022"]
    assert older["runs-on"] == "windows-2022" and older["needs"] == "release"
    assert any("download-artifact" in s.get("uses", "") for s in older["steps"])
    assert not any("build_release" in s.get("run", "") for s in older["steps"])
    assert jobs["draft"]["needs"] == ["release", "prove-windows-2022"]
    drafts = [
        (job, s["run"])
        for job, spec in jobs.items()
        for s in spec["steps"]
        if "gh release create" in s.get("run", "")
    ]
    assert len(drafts) == 1 and drafts[0][0] == "draft"
    assert "--draft" in drafts[0][1] and '"$zip.sha256"' in drafts[0][1]


def _step(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end)]


def test_proof_runs_under_defender_a_deep_folder_and_dead_stops() -> None:
    """Unit 7f's proof steps: real-time protection on (runner exclusions
    removed) before the zip is unpacked and still on at the end; a folder past
    the limit refused with long paths off and working with them on, the setting
    put back; runs killed at two of run's step lines, then a clean run."""
    from planner.cli import RUN_STEPS

    text = SCRIPT.read_text(encoding="utf-8")
    defender = _step(text, "== Defender", "== 0.")
    for needed in (
        "Remove-MpPreference -ExclusionPath",
        "Set-MpPreference -DisableRealtimeMonitoring $false",
        "RealTimeProtectionEnabled",
        "Start-MpScan -ScanType CustomScan",
        "Get-MpThreatDetection",
    ):
        assert needed in defender, needed
    assert text.index("Set-MpPreference") < text.index("ExtractToDirectory")
    deep = _step(text, "== 9.", "== 10.")
    assert "python\\LONGEST_PATH" in deep and "too deep for Windows" in deep
    assert "-Name LongPathsEnabled -Value 0" in deep
    assert "-Name LongPathsEnabled -Value 1" in deep
    assert "} finally {" in deep and "-Value $was" in deep
    cut = _step(text, "== 10.", "PROOF PASSED")
    for name in ("inbox", "page"):
        mark = f"[{RUN_STEPS.index(name) + 1}/{len(RUN_STEPS)}] {name}"
        assert f"'{mark}'" in cut, mark
    assert "taskkill /F /T /PID" in cut and "PYTHONUNBUFFERED" in cut
    assert "Example Bank (synthetic)" in cut
    assert "went off during the proof" in cut


def test_release_build_records_its_deepest_file(tmp_path: Path) -> None:
    from tests.test_privacy import _load_build_release

    br = _load_build_release()
    deep = tmp_path / "python" / "Lib" / "site-packages" / "pkg" / "a_long_module.py"
    deep.parent.mkdir(parents=True)
    deep.write_text("", encoding="utf-8")
    (tmp_path / "planner.cmd").write_text("", encoding="utf-8")
    rel = br.record_longest(tmp_path)
    assert rel == "python\\Lib\\site-packages\\pkg\\a_long_module.py"
    recorded = (tmp_path / "python" / "LONGEST_PATH").read_text(encoding="utf-8")
    assert recorded == rel + "\n"
    assert "record_longest(stage)" in (
        SCRIPT.parents[0] / "build_release.py"
    ).read_text(encoding="utf-8")
