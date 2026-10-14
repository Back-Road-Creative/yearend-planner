"""Sign a release's .sha256 file, or make the publisher key pair.

    python scripts/sign_release.py sign <zip>.sha256
        reads the private key from UPDATE_SIGNING_KEY and writes
        <zip>.sha256.sig; refuses when the variable is unset, or when the key
        does not match public_key in config/update.yaml.
    python scripts/sign_release.py keygen config/update.yaml
        writes a new public key into that file's public_key line and prints
        the private key to stdout only, for a pipe into
        ``gh secret set UPDATE_SIGNING_KEY``; it is never written to disk.

See planner/engine/sign.py."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from planner.engine import sign  # noqa: E402

KEY_LINE = re.compile(r"^public_key:.*$", re.MULTILINE)


def trusted(config: Path) -> str:
    m = re.search(r'^public_key:\s*"?([^"\s]*)"?\s*$', config.read_text("utf-8"), re.M)
    return m.group(1) if m else ""


def sign_file(path: Path, config: Path) -> Path:
    private = os.environ.get("UPDATE_SIGNING_KEY", "").strip()
    if not private:
        raise SystemExit("UPDATE_SIGNING_KEY is not set: refusing an unsigned release")
    data = path.read_bytes()
    signature = sign.sign(private, data)
    if not sign.verify(trusted(config), data, signature):
        raise SystemExit(
            f"UPDATE_SIGNING_KEY does not match public_key in {config}: "
            "installs would hold this release"
        )
    out = path.with_name(path.name + ".sig")
    out.write_text(signature + "\n", encoding="ascii")
    return out


def keygen(config: Path) -> str:
    private, public = sign.keygen()
    text = config.read_text("utf-8")
    if not KEY_LINE.search(text):
        raise SystemExit(f"{config} has no public_key line")
    config.write_text(KEY_LINE.sub(f'public_key: "{public}"', text, count=1), "utf-8")
    return private


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("sign", "keygen"):
        raise SystemExit(__doc__)
    if argv[0] == "keygen":
        print(keygen(Path(argv[1])))
    else:
        print(
            sign_file(Path(argv[1]), ROOT / "config" / "update.yaml"), file=sys.stderr
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
