"""The Windows proof's throwaway account password must always pass Windows'
complexity rule (three character classes). A plain 24-of-62 draw can miss
digits, and New-LocalUser then throws InvalidPasswordException (PR #28's
windows-proof job). Every zip proof also runs the synthetic inbox."""

import re
import shutil
import subprocess
from pathlib import Path

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
