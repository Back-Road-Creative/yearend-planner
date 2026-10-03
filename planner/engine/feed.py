"""The update check: is a newer release published, and does it pass its own
selfcheck here?

``config/update.yaml`` names the feed: a GitHub "latest release" API address
whose release carries ``*-win64.zip`` and ``*-win64.zip.sha256`` assets
(``PLANNER_UPDATE_FEED`` overrides it; ``off`` disables the check). The check
sends one plain GET with no personal data. Offline, a refused address or a
feed with no newer release is not an error: the check says nothing. A newer
release is downloaded to ``data/update/``, verified and self-checked into
``python-candidate/``; the next start swaps it in. A candidate that fails its
selfcheck is recorded in ``data/update-held.json`` and shown on the dashboard;
the live install is never touched."""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from planner.config import load_yaml
from planner.engine import update as upd
from planner.paths import Layout

HELD = "update-held.json"
TIMEOUT = 10


@dataclass(frozen=True)
class Offer:
    version: str
    zip_url: str
    sha_url: str


def feed_url(lay: Layout) -> str | None:
    env = os.environ.get("PLANNER_UPDATE_FEED")
    if env is not None:
        return None if env.strip().lower() in ("", "off") else env.strip()
    cfg = lay.config / "update.yaml"
    url = load_yaml(cfg).get("feed") if cfg.exists() else None
    return str(url) if url else None


def _open(url: str, timeout: int) -> bytes:
    if urlsplit(url).scheme not in ("https", "file"):
        raise ValueError(f"feed address must be https or file: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "yearend-planner"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        data: bytes = resp.read()
    return data


def latest(url: str) -> Offer | None:
    """The feed's release, or None when it lacks a win64 zip and its sha256."""
    rel: dict[str, Any] = json.loads(_open(url, TIMEOUT))
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    zips = [n for n in assets if n.endswith("-win64.zip")]
    if not zips or zips[0] + ".sha256" not in assets:
        return None
    return Offer(
        str(rel["tag_name"]).lstrip("v"), assets[zips[0]], assets[zips[0] + ".sha256"]
    )


def held(lay: Layout) -> dict[str, Any] | None:
    path = lay.data / HELD
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if upd.version_key(data["version"]) <= upd.version_key(
        upd.installed_version(lay.root)
    ):
        path.unlink()  # installed by hand since, or superseded
        return None
    return data


def check(lay: Layout, today: date | None = None) -> str:
    """One update check; returns a line for the user, or "" when there is
    nothing to say (no feed, offline, nothing newer, already staged)."""
    url = feed_url(lay)
    if url is None:
        return ""
    try:
        offer = latest(url)
    except (OSError, ValueError, KeyError):
        return ""
    current = upd.installed_version(lay.root)
    if offer is None or upd.version_key(offer.version) <= upd.version_key(current):
        return ""
    ready = upd.staged(lay.root)
    if ready and ready["version"] == offer.version:
        return ""
    known = held(lay)
    if known and known["version"] == offer.version:
        return ""
    folder = lay.data / "update"
    folder.mkdir(parents=True, exist_ok=True)
    zip_path = folder / Path(urlsplit(offer.zip_url).path).name
    try:
        zip_path.write_bytes(_open(offer.zip_url, 600))
        sha = _open(offer.sha_url, TIMEOUT).decode("ascii").split()[0]
    except (OSError, ValueError, UnicodeDecodeError, IndexError):
        return ""
    exe = "python.exe" if sys.platform == "win32" else "python"
    try:
        upd.stage(zip_path, sha, lay.root, python_exe=exe)
    except upd.UpdateError as exc:
        shutil.rmtree(lay.root / upd.CANDIDATE, ignore_errors=True)
        record = {
            "version": offer.version,
            "installed": current,
            "reason": str(exc).splitlines()[0],
            "date": (today or date.today()).isoformat(),
        }
        (lay.data / HELD).write_text(json.dumps(record), encoding="utf-8")
        return f"update {offer.version} held: {record['reason']}; still on {current}"
    finally:
        zip_path.unlink(missing_ok=True)
    return (
        f"update {offer.version} is ready; it is swapped in the next time you "
        "start the planner"
    )
