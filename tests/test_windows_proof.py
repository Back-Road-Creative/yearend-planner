"""The Windows proof's throwaway account password must always pass Windows'
complexity rule (three character classes). A plain 24-of-62 draw can miss
digits, and New-LocalUser then throws InvalidPasswordException (PR #28's
windows-proof job)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

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
