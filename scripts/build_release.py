"""Build the Windows release zip: ``dist/yearend-planner-<version>-win64.zip``.

Runs on any OS (uv installs Windows wheels for the target platform), but the
proof that the result works is the Windows CI leg (``scripts/windows_proof.ps1``).

Layout inside the zip (contents at the zip root, no top-level folder):
    planner.cmd  LICENSE  README.md  VERSION
    python/      python.org embeddable 3.12 + Lib/site-packages from uv.lock
    planner/     the package          config/  templates/
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
STAGE = DIST / "stage"
CACHE = DIST / "cache"

PYTHON_VERSION = "3.12.10"
PYTHON_EMBED_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
    f"python-{PYTHON_VERSION}-embed-amd64.zip"
)
PYTHON_EMBED_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"

# sys.path for the embedded interpreter. ``..`` is the planner folder, so
# ``python -m planner`` and ``config/`` resolve from any location the user picks.
PTH = "python312.zip\n.\nLib\\site-packages\n..\nimport site\n"

SHIP = ("planner.cmd", "LICENSE", "README.md")
SHIP_DIRS = ("planner", "config", "templates")


def version() -> str:
    ns: dict[str, str] = {}
    exec((ROOT / "planner" / "__init__.py").read_text(encoding="utf-8"), ns)  # noqa: S102
    return ns["__version__"]


def fetch_embed() -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / PYTHON_EMBED_URL.rsplit("/", 1)[1]
    if not target.exists():
        print(f"downloading {PYTHON_EMBED_URL}")
        urllib.request.urlretrieve(PYTHON_EMBED_URL, target)  # noqa: S310
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != PYTHON_EMBED_SHA256:
        target.unlink()
        raise SystemExit(f"embeddable python sha256 mismatch: {digest}")
    return target


def run(*cmd: str) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)  # noqa: S603


def build_python(stage: Path) -> None:
    py = stage / "python"
    with zipfile.ZipFile(fetch_embed()) as zf:
        zf.extractall(py)
    (py / "python312._pth").write_text(PTH, encoding="utf-8")
    reqs = DIST / "requirements-release.txt"
    run(
        "uv",
        "export",
        "--frozen",
        "--no-dev",
        "--no-emit-project",
        "--no-editable",
        "-o",
        str(reqs),
    )
    run(
        "uv",
        "pip",
        "install",
        "--no-config",
        "--require-hashes",
        "--python-platform",
        "x86_64-pc-windows-msvc",
        "--python-version",
        "3.12",
        "--target",
        str(py / "Lib" / "site-packages"),
        "-r",
        str(reqs),
    )


def stage_tree() -> Path:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    for name in SHIP:
        shutil.copy2(ROOT / name, STAGE / name)
    for name in SHIP_DIRS:
        src = ROOT / name
        if src.exists():
            shutil.copytree(
                src,
                STAGE / name,
                ignore=shutil.ignore_patterns("__pycache__", "thresholds.engine.yaml"),
            )
    (STAGE / "VERSION").write_text(version() + "\n", encoding="utf-8")
    build_python(STAGE)
    return STAGE


def zip_stage(stage: Path) -> Path:
    out = DIST / f"yearend-planner-{version()}-win64.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(stage).as_posix())
    return out


def main() -> int:
    out = zip_stage(stage_tree())
    # published beside the zip: the update check verifies the download with it
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    out.with_name(out.name + ".sha256").write_text(
        f"{digest}  {out.name}\n", encoding="ascii"
    )
    print(f"built {out} ({out.stat().st_size / 1e6:.1f} MB) sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
