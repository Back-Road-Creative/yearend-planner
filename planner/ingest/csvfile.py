"""CSV importers driven by ``templates/csv/<source>.yaml``.

A CSV may hold several blocks (Vanguard's download puts holdings and
transactions in one file separated by a blank line). Each block is matched by
its header: the first template whose ``match`` headers are all present wins.
Rows are normalized to :class:`planner.ledger.db.Row`; money becomes cents;
nothing is inferred. A block no template matches unmatches the whole file and
the reason lists the headers found, so the fix is a template, not a guess.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from planner.config import safe_load
from planner.ingest.pdf import Unmatched, parse_amount
from planner.ledger.db import Row, to_cents

__all__ = ["CsvTemplate", "Unmatched", "load_csv_templates", "parse_csv", "read_blocks"]


@dataclass
class CsvTemplate:
    source: str
    kind: str
    match: list[str]
    columns: dict[str, str]
    date_format: str = "%m/%d/%Y"
    term_values: dict[str, str] = field(default_factory=dict)
    skip: dict[str, list[str]] = field(default_factory=dict)
    title: str = ""
    path: Path | None = None

    def matches(self, headers: list[str]) -> bool:
        have = {h.lower() for h in headers}
        return all(m.lower() in have for m in self.match)


def load_csv_templates(folder: Path) -> list[CsvTemplate]:
    out: list[CsvTemplate] = []
    for p in sorted(folder.glob("*.yaml")):
        raw = safe_load(p.read_text(encoding="utf-8"))
        out.append(
            CsvTemplate(
                source=str(raw["source"]),
                kind=str(raw["kind"]),
                match=[str(m) for m in raw["match"]],
                columns={str(k): str(v) for k, v in raw["columns"].items()},
                date_format=str(raw.get("date_format", "%m/%d/%Y")),
                term_values={
                    str(k): str(v) for k, v in (raw.get("term_values") or {}).items()
                },
                skip={
                    str(k): [str(x).lower() for x in v]
                    for k, v in (raw.get("skip") or {}).items()
                },
                title=str(raw.get("title", "")),
                path=p,
            )
        )
    return out


@dataclass
class Block:
    headers: list[str]
    rows: list[tuple[int, list[str]]]  # (1-based line number, cells)
    title: str = ""  # the one-cell line above the header ("Individual ...321")


# A cell only a data row carries: a number, an amount or a date.
DATA_CELL = re.compile(r"[-+($]*\s*(?:\d[\d,]*)?\.?\d+\)?|\d{1,2}/\d{1,2}/\d{2,4}")


def _data_row(cells: list[str]) -> bool:
    return any(DATA_CELL.fullmatch(c.strip()) for c in cells)


def read_blocks(path: Path) -> list[Block]:
    """Split a CSV into header-led blocks; blank lines separate blocks. A data
    row after a blank line carries on the block above (Vanguard's download
    parts each account's holdings with one); a header row starts a new one. A
    line with one filled cell is a title or a note, never a row: the last one
    before a header is that block's title (Schwab names the account so)."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="latin-1")
    blocks: list[Block] = []
    current: Block | None = None
    title: str | None = None
    for line_no, cells in enumerate(csv.reader(io.StringIO(text)), start=1):
        filled = [c.strip() for c in cells if c.strip()]
        if not filled:
            current = None
            continue
        if len(filled) == 1:
            current, title = None, filled[0]
            continue
        if current is None and blocks and title is None and _data_row(cells):
            current = blocks[-1]
        if current is None:
            headers = [c.strip() for c in cells]
            while headers and not headers[-1]:
                headers.pop()
            current = Block(headers=headers, rows=[], title=title or "")
            title = None
            blocks.append(current)
            continue
        current.rows.append((line_no, cells))
    return blocks


def _cell(headers: list[str], cells: list[str], name: str) -> str:
    lowered = [h.lower() for h in headers]
    try:
        i = lowered.index(name.lower())
    except ValueError:
        return ""
    return cells[i].strip() if i < len(cells) else ""


# "01/02/2026 as of 12/31/2025": posted on the first date, counted from the second.
AS_OF = re.compile(r".+? as of (.+)", re.IGNORECASE)
# What an export prints where it has no figure; read as no value, never as 0.
NO_VALUE = {"--", "n/a", "na", "incomplete"}


def _iso(raw: str, fmt: str) -> str | None:
    if not raw:
        return None
    as_of = AS_OF.fullmatch(raw)
    if as_of:
        raw = as_of.group(1).strip()
    try:
        return datetime.strptime(raw, fmt).date().isoformat()
    except ValueError as exc:
        raise Unmatched(f"date {raw!r} does not match {fmt}") from exc


def _money(raw: str) -> int | None:
    if not raw.strip() or raw.strip().lower() in NO_VALUE:
        return None
    return to_cents(parse_amount(raw))


def _number(raw: str) -> float | None:
    cleaned = raw.replace(",", "").strip()
    if not cleaned or cleaned.lower() in NO_VALUE:
        return None
    try:
        return float(cleaned)
    except ValueError as exc:
        raise Unmatched(f"quantity {raw!r} is not a number") from exc


def normalize_block(
    block: Block, tpl: CsvTemplate, file_name: str, today: str
) -> list[Row]:
    col = tpl.columns
    title = block.title
    if tpl.title and (m := re.search(tpl.title, title)):
        title = m.group(1).strip()
    rows: list[Row] = []
    for line_no, cells in block.rows:
        if any(
            _cell(block.headers, cells, name).lower() in values
            for name, values in tpl.skip.items()
        ):
            continue  # a total line ("Account Total"), not a row

        def get(key: str, cells: list[str] = cells) -> str:
            if col.get(key) == "[title]":
                return title
            return _cell(block.headers, cells, col[key]) if key in col else ""

        raw = ",".join(c.strip() for c in cells)
        txn_id = get("txn_id")
        if txn_id:
            key = f"{tpl.source}:{txn_id}"
        else:
            digest = hashlib.sha256(
                f"{file_name}\n{line_no}\n{raw}".encode()
            ).hexdigest()
            key = f"{tpl.source}:{digest[:24]}"
        date = _iso(get("date"), tpl.date_format)
        if date is None and tpl.kind == "holding":
            date = today  # a holdings export is a snapshot as of the import
        amount = _money(get("amount"))
        if amount is None and ("credit" in col or "debit" in col):
            credit = _money(get("credit")) or 0
            debit = _money(get("debit")) or 0
            amount = credit - debit
        term_raw = get("term")
        term = tpl.term_values.get(term_raw.lower(), term_raw.lower()) or None
        rows.append(
            Row(
                source=tpl.source,
                kind=tpl.kind,
                row_key=key,
                account=get("account"),
                date=date,
                tax_year=int(date[:4]) if date else None,
                type=get("type"),
                description=get("description"),
                symbol=get("symbol"),
                quantity=_number(get("quantity")),
                price_cents=_money(get("price")),
                amount_cents=amount,
                basis_cents=_money(get("basis")),
                acquired=_iso(get("acquired"), tpl.date_format),
                term=term,
                line=line_no,
                raw=raw,
            )
        )
    return rows


def parse_csv(path: Path, templates: list[CsvTemplate]) -> list[Row]:
    blocks = [b for b in read_blocks(path) if b.rows]
    if not blocks:
        raise Unmatched("CSV has no data rows")
    today = datetime.now(UTC).date().isoformat()
    rows: list[Row] = []
    for block in blocks:
        # The template naming the most of the block's headers claims it, so a
        # broker's layout wins over a generic bank one it happens to contain.
        claims = [t for t in templates if t.matches(block.headers)]
        tpl = max(claims, key=lambda t: len(t.match), default=None)
        if tpl is None:
            raise Unmatched(
                "no CSV template matches headers: " + ", ".join(block.headers)
            )
        rows.extend(normalize_block(block, tpl, path.name, today))
    return rows
