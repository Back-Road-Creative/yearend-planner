"""The update check: is a newer release published, and does it pass its own
selfcheck here?

``config/update.yaml`` names the feed: a GitHub "latest release" API address
whose release carries ``*-win64.zip``, ``*-win64.zip.sha256`` and
``*-win64.zip.sha256.sig`` assets (``PLANNER_UPDATE_FEED`` overrides it;
``off`` disables the check). The ``.sha256`` is trusted only when its ``.sig``
verifies against ``public_key`` in the same file (:mod:`planner.engine.sign`);
a release signed by any other key, or an install with no key, is held before
the zip is downloaded. The check
sends one plain GET with no personal data. Offline, a refused address or a
feed with no newer release is not an error: the check says nothing. A newer
release is downloaded to ``data/update/``, verified and self-checked into
``python-candidate/``; the next start swaps it in. A candidate that fails its
selfcheck, or that jumps a major version of policyengine-us or the planner, is
recorded in ``data/update-held.json`` and shown on the dashboard; the live
install is never touched. A major jump waits for ``planner update --allow-major``.

The check runs at most once every 7 days: each answer from the feed is written
to ``data/update/last-check.json`` (shown in the page header). An unreachable
feed is recorded too but does not start the week, so the next launch asks again.
``planner update --check`` ignores the throttle."""

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
from planner.engine import sign
from planner.engine import update as upd
from planner.paths import Layout

HELD = "update-held.json"
LAST_CHECK = "last-check.json"
TIMEOUT = 10
WEEK = 7  # days between automatic checks
THROTTLED = ("current", "ready", "held")  # outcomes that count as "checked"


@dataclass(frozen=True)
class Offer:
    version: str
    zip_url: str
    sha_url: str
    sig_url: str


def public_key(lay: Layout) -> str:
    """The publisher key the install trusts; "" when none is configured."""
    cfg = lay.config / "update.yaml"
    key = load_yaml(cfg).get("public_key") if cfg.exists() else None
    return str(key or "").strip()


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
    """The feed's release, or None when it lacks a win64 zip, its sha256 or
    the sha256's signature."""
    rel: dict[str, Any] = json.loads(_open(url, TIMEOUT))
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    zips = [n for n in assets if n.endswith("-win64.zip")]
    if not zips:
        return None
    sha, sig = zips[0] + ".sha256", zips[0] + ".sha256.sig"
    if sha not in assets or sig not in assets:
        return None
    return Offer(
        str(rel["tag_name"]).lstrip("v"), assets[zips[0]], assets[sha], assets[sig]
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


def last_check(lay: Layout) -> dict[str, Any] | None:
    """The record of the newest check: date, status, message."""
    path = lay.data / "update" / LAST_CHECK
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        date.fromisoformat(data["date"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return data


def status_line(lay: Layout) -> str:
    """The page header's words for the update check."""
    if feed_url(lay) is None:
        return "update check off"
    rec = last_check(lay)
    if rec is None:
        return "update check not yet run"
    return f"update check {rec['date']}: {rec.get('message', rec.get('status', ''))}"


def _record(lay: Layout, today: date, status: str, message: str) -> None:
    folder = lay.data / "update"
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (LAST_CHECK + ".tmp")
    record = {"date": today.isoformat(), "status": status, "message": message}
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.replace(folder / LAST_CHECK)


def _due(lay: Layout, today: date) -> bool:
    rec = last_check(lay)
    if rec is None or rec.get("status") not in THROTTLED:
        return True
    age = (today - date.fromisoformat(rec["date"])).days
    return not 0 <= age < WEEK  # a clock set back also asks again


def _hold(lay: Layout, today: date, version: str, reason: str) -> str:
    """Record ``version`` as held for ``reason`` and say so."""
    current = upd.installed_version(lay.root)
    record = {
        "version": version,
        "installed": current,
        "reason": reason,
        "date": today.isoformat(),
    }
    (lay.data / HELD).write_text(json.dumps(record), encoding="utf-8")
    _record(lay, today, "held", f"update {version} held (see alerts)")
    return f"update {version} held: {reason}; still on {current}"


def check(
    lay: Layout,
    today: date | None = None,
    force: bool = False,
    allow_major: bool = False,
) -> str:
    """One update check; returns a line for the user, or "" when there is
    nothing to say (no feed, checked within 7 days, offline, nothing newer,
    already staged). ``force`` skips the weekly throttle; ``allow_major``
    stages a release that jumps a major version and retries a held one."""
    url = feed_url(lay)
    if url is None:
        return ""
    today = today or date.today()
    if not force and not _due(lay, today):
        return ""
    try:
        offer = latest(url)
    except OSError:
        _record(lay, today, "offline", "update feed not reachable")
        return ""
    except (ValueError, KeyError):
        _record(lay, today, "unreadable", "update feed not readable")
        return ""
    current = upd.installed_version(lay.root)
    if offer is None:
        _record(lay, today, "current", "no installable release is published")
        return ""
    if upd.version_key(offer.version) <= upd.version_key(current):
        _record(lay, today, "current", f"up to date ({current})")
        return ""
    ready = upd.staged(lay.root)
    if ready and ready["version"] == offer.version:
        _record(
            lay, today, "ready", f"update {offer.version} ready; swaps in at next start"
        )
        return ""
    known = held(lay)
    if known and known["version"] == offer.version and not allow_major:
        _record(lay, today, "held", f"update {offer.version} held (see alerts)")
        if force:
            return (
                f"update {known['version']} held: {known['reason']}; still on {current}"
            )
        return ""
    folder = lay.data / "update"
    folder.mkdir(parents=True, exist_ok=True)
    zip_path = folder / Path(urlsplit(offer.zip_url).path).name
    try:
        sha_file = _open(offer.sha_url, TIMEOUT)
        signature = _open(offer.sig_url, TIMEOUT).decode("ascii")
        sha = sha_file.decode("ascii").split()[0]
    except (OSError, ValueError, UnicodeDecodeError, IndexError):
        _record(
            lay, today, "offline", f"update {offer.version} could not be downloaded"
        )
        return ""
    key = public_key(lay)
    if not key:
        return _hold(
            lay,
            today,
            offer.version,
            "config/update.yaml names no publisher key, so the release cannot "
            "be checked",
        )
    if not sign.verify(key, sha_file, signature):
        return _hold(
            lay,
            today,
            offer.version,
            "its .sha256 is not signed by the publisher key in config/update.yaml",
        )
    try:
        zip_path.write_bytes(_open(offer.zip_url, 600))
    except OSError:
        zip_path.unlink(missing_ok=True)
        _record(
            lay, today, "offline", f"update {offer.version} could not be downloaded"
        )
        return ""
    exe = "python.exe" if sys.platform == "win32" else "python"
    try:
        result = upd.stage(
            zip_path,
            sha,
            lay.root,
            python_exe=exe,
            allow_major=allow_major,
            today=today,
        )
    except upd.UpdateError as exc:
        shutil.rmtree(lay.root / upd.CANDIDATE, ignore_errors=True)
        return _hold(lay, today, offer.version, str(exc).splitlines()[0])
    finally:
        zip_path.unlink(missing_ok=True)
    (lay.data / HELD).unlink(missing_ok=True)
    note = result.years_note
    _record(lay, today, "ready", f"update {offer.version} ready; {note}")
    return (
        f"update {offer.version} is ready; it is swapped in the next time you "
        f"start the planner; {note}"
    )
