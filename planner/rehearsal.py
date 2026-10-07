"""Unit 7c: rehearse a restore. A backup is only as good as the figures it gives
back, so ``planner backup --rehearse`` (or ``planner restore ZIP --rehearse``)
unpacks the zip into a throwaway planner folder, as a clean machine with this
release would have it (this release's ``config/``, the backup's typed limits
carried in), and works out the same figures there and in the live folder:

- the ledger: documents, facts and rows per year, and their totals in cents;
- the Needed list: each item's state and value per year;
- the draft return: every line per year, or what it waits on.

Any difference is listed. The live side is worked out on a copy too (the
ledger copied through sqlite's backup API, as a backup copies it): working out
figures records things (the Needed list's estimates, the portfolio's high), and
the live ``data/`` must not change. Both throwaway folders are deleted. The
result is kept beside the zip (``<zip>.rehearsal.json``) so ``planner health``
can say when the newest backup was last proved."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from planner import backup as bk
from planner.engine.household import MissingInputError
from planner.ingest.needs import needed
from planner.ledger import db
from planner.paths import Layout
from planner.taxprep import draft

SHOWN = 10  # differences printed; the rest are counted


@dataclass
class Rehearsal:
    source: Path
    files: int
    years: list[int]
    compared: int = 0
    differences: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.differences

    @property
    def line(self) -> str:
        span = ", ".join(str(y) for y in self.years) or "no tax year yet"
        if self.ok:
            return (
                f"rehearsal passed: {self.files} files restored on a clean folder; "
                f"{self.compared} figures match ({span})"
            )
        return (
            f"rehearsal FAILED: {len(self.differences)} of {self.compared} figures "
            f"differ after a restore ({span})"
        )


def record_path(src: Path) -> Path:
    return src.with_name(src.name + ".rehearsal.json")


def last(src: Path) -> dict[str, object] | None:
    """The rehearsal kept beside ``src``, or None when it was never rehearsed."""
    path = record_path(src)
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def years(lay: Layout) -> list[int]:
    conn = db.connect_readonly(lay.data / "ledger" / "planner.db")
    if conn is None:
        return []
    try:
        got = conn.execute(
            "SELECT tax_year FROM facts UNION SELECT tax_year FROM rows "
            "WHERE tax_year IS NOT NULL ORDER BY 1"
        ).fetchall()
    finally:
        conn.close()
    return [int(r[0]) for r in got]


def _ledger(lay: Layout) -> dict[str, str]:
    conn = db.connect_readonly(lay.data / "ledger" / "planner.db")
    if conn is None:
        return {"ledger": "none"}
    queries = {
        "documents": "SELECT 'all', COUNT(*), '' FROM documents",
        "facts": "SELECT tax_year || ' ' || form || ' ' || status, COUNT(*), "
        "SUM(value_cents) FROM facts GROUP BY 1",
        "rows": "SELECT COALESCE(tax_year, '-') || ' ' || source || ' ' || kind, "
        "COUNT(*), SUM(COALESCE(amount_cents, 0)) || '/' || "
        "SUM(COALESCE(basis_cents, 0)) FROM rows GROUP BY 1",
        "conversions": "SELECT 'all', COUNT(*), SUM(amount_cents) FROM conversions",
    }
    out: dict[str, str] = {}
    try:
        for table, sql in queries.items():
            for key, n, total in conn.execute(sql):
                out[f"ledger {table} {key}"] = f"{n} ({total})"
    finally:
        conn.close()
    return out


def figures(lay: Layout, year_list: list[int]) -> dict[str, str]:
    """Every figure the rehearsal compares, as text keyed by where it comes from."""
    out = _ledger(lay)
    for year in year_list:
        for s in needed(lay, year).items:
            out[f"needed {year} {s.need.key}"] = f"{s.state} {s.value!r}"
        try:
            d = draft.build(lay, year)
        except MissingInputError as exc:
            out[f"draft {year}"] = f"waits on: {exc}"
            continue
        seen: dict[str, int] = {}
        for ln in d.lines:
            key = f"draft {year} {ln.form} {ln.line}"
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                key += f" #{seen[key]}"
            out[key] = f"{ln.value:.2f}"
    return out


def _up_to_date(clean: Layout) -> Layout:
    clean.ensure()
    ledger = clean.data / "ledger" / "planner.db"
    if ledger.exists():  # an older ledger is brought up to date, as on a first launch
        db.connect(ledger).close()
    return clean


def _mirror(lay: Layout, root: Path) -> Layout:
    """The live data/ copied file by file, what a backup made now would hold."""
    clean = Layout(root)
    root.mkdir()
    shutil.copytree(lay.config, clean.config)
    for path in sorted(p for p in lay.data.rglob("*") if p.is_file()):
        rel = path.relative_to(lay.root).as_posix()
        if bk._skipped(rel) or path.is_symlink():
            continue
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bk._read(path))
    return _up_to_date(clean)


def _clean_folder(lay: Layout, src: Path, password: str, root: Path) -> Layout:
    """The backup restored where nothing else is: this release's config/, the
    zip checked and unpacked, its typed limits carried in as a restore does."""
    from planner.engine.update import carry_thresholds

    clean = Layout(root)
    root.mkdir()
    shutil.copytree(lay.config, clean.config)
    staging = root / bk.STAGING
    bk._unpack(src, password, staging)
    if not (staging / "data").is_dir():
        raise bk.BackupError(f"{src.name}: holds no data/ folder")
    (staging / "data").rename(clean.data)
    carry_thresholds(staging, root)
    return _up_to_date(clean)


def rehearse(
    lay: Layout,
    src: Path,
    password: str = "",
    year_list: list[int] | None = None,
    today: date | None = None,
) -> Rehearsal:
    """Restore ``src`` on a throwaway folder and compare its figures with the
    live folder's. Raises :class:`planner.backup.BackupError` when the zip
    fails its checks, as a real restore would."""
    with tempfile.TemporaryDirectory(prefix="planner-rehearsal-") as tmp:
        clean = _clean_folder(lay, src, password, Path(tmp) / "restored")
        files = sum(1 for p in clean.data.rglob("*") if p.is_file())
        live = _mirror(lay, Path(tmp) / "live")
        year_list = years(live) if year_list is None else year_list
        theirs = figures(clean, year_list)
        ours = figures(live, year_list)
    res = Rehearsal(src, files, year_list, compared=len(ours.keys() | theirs.keys()))
    for key in sorted(ours.keys() | theirs.keys()):
        a, b = ours.get(key, "(none)"), theirs.get(key, "(none)")
        if a != b:
            res.differences.append(f"{key}: live {a}, restored {b}")
    _keep(res, today or date.today())
    return res


def _keep(res: Rehearsal, today: date) -> None:
    record = {
        "date": today.isoformat(),
        "ok": res.ok,
        "compared": res.compared,
        "differences": len(res.differences),
    }
    path = record_path(res.source)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.replace(path)


def render(res: Rehearsal) -> str:
    lines = [res.line]
    lines += [f"  - {d}" for d in res.differences[:SHOWN]]
    if len(res.differences) > SHOWN:
        lines.append(f"  ... and {len(res.differences) - SHOWN} more")
    return "\n".join(lines) + "\n"
