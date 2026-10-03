"""Schedule C from bank rows the user has categorised, never from a guess.

A bank row gets a category only from the user: a rule they wrote (a piece of
the description, case ignored, first rule wins) or a category set on that one
row, which beats any rule. Both live in ``profile/categories.yaml`` in the
private data folder. A row with neither is listed as uncategorised and left
out of every line. ``personal`` and ``transfer`` are categories too, so a
personal checking account can be closed out with one catch-all rule.

Line 1 is the categorised receipts, or the 1099-NEC and 1099-K total when the
forms add up to more (a client payment the rows miss), with a note either way.
Meals are half deductible. The result is stored as ``SCH-C`` facts so the
Needed panel's self-employment income is the net profit on line 31; until a
row is categorised the 1099 forms stand in as an estimate. Depreciation (Form
4562) and the home office (Form 8829) are not built from bank rows.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from planner.ledger import db
from planner.paths import Layout

FORM = "SCH-C"
ISSUER = "categorised bank rows"
RECEIPTS = "receipts"
RETURNS = "returns"
MEALS = "meals"
MEALS_SHARE = 0.5
# category -> (Schedule C line, label)
CATEGORIES = {
    RECEIPTS: ("1", "Gross receipts"),
    RETURNS: ("2", "Returns and allowances"),
    "advertising": ("8", "Advertising"),
    "car": ("9", "Car and truck expenses (actual)"),
    "commissions": ("10", "Commissions and fees"),
    "contract_labor": ("11", "Contract labor"),
    "insurance": ("15", "Insurance (other than health)"),
    "interest": ("16b", "Interest (other)"),
    "legal_professional": ("17", "Legal and professional services"),
    "office": ("18", "Office expense"),
    "rent_equipment": ("20a", "Rent: vehicles, machinery, equipment"),
    "rent_property": ("20b", "Rent: other business property"),
    "repairs": ("21", "Repairs and maintenance"),
    "supplies": ("22", "Supplies"),
    "taxes_licenses": ("23", "Taxes and licenses"),
    "travel": ("24a", "Travel"),
    MEALS: ("24b", "Deductible meals (half)"),
    "utilities": ("25", "Utilities"),
    "wages": ("26", "Wages"),
    "other": ("27a", "Other expenses"),
}
EXCLUDED = {"personal": "not business", "transfer": "between your own accounts"}
TOTALS = {
    "7": "Gross income",
    "28": "Total expenses",
    "31": "Net profit or loss",
}
FORM_BOXES = (("1099-NEC", "1"), ("1099-K", "1a"))


def categories_path(lay: Layout) -> Path:
    return lay.data / "profile" / "categories.yaml"


def _check(category: str) -> str:
    if category not in CATEGORIES and category not in EXCLUDED:
        choices = ", ".join([*CATEGORIES, *EXCLUDED])
        raise ValueError(f"unknown category {category}; one of {choices}")
    return category


def load_rules(lay: Layout) -> tuple[list[tuple[str, str]], dict[str, str]]:
    path = categories_path(lay)
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rules = [
        (str(r["match"]), _check(str(r["category"]))) for r in data.get("rules", [])
    ]
    rows = {str(k): _check(str(v)) for k, v in (data.get("rows") or {}).items()}
    return rules, rows


def _save(lay: Layout, rules: list[tuple[str, str]], rows: dict[str, str]) -> None:
    path = categories_path(lay)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "rules": [{"match": m, "category": c} for m, c in rules],
        "rows": dict(sorted(rows.items())),
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def add_rule(lay: Layout, match: str, category: str) -> None:
    """Append a rule; an earlier rule for the same text is replaced in place."""
    if not match.strip():
        raise ValueError("a rule needs some description text to match")
    _check(category)
    rules, rows = load_rules(lay)
    low = match.strip().lower()
    for i, (m, _) in enumerate(rules):
        if m.lower() == low:
            rules[i] = (match.strip(), category)
            break
    else:
        rules.append((match.strip(), category))
    _save(lay, rules, rows)


def assign(lay: Layout, row_key: str, category: str) -> None:
    _check(category)
    rules, rows = load_rules(lay)
    rows[row_key] = category
    _save(lay, rules, rows)


def category_of(
    row: db.LedgerRow, rules: list[tuple[str, str]], rows: dict[str, str]
) -> str | None:
    if row.row_key in rows:
        return rows[row.row_key]
    text = f"{row.type} {row.description}".lower()
    for match, category in rules:
        if match.lower() in text:
            return category
    return None


@dataclass
class ScheduleC:
    year: int
    lines: dict[str, float] = field(default_factory=dict)  # line -> dollars
    rows: dict[str, list[db.LedgerRow]] = field(default_factory=dict)  # by category
    uncategorised: list[db.LedgerRow] = field(default_factory=list)
    receipts_rows: float = 0.0
    receipts_forms: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def categorised(self) -> int:
        return sum(len(v) for v in self.rows.values())

    @property
    def business(self) -> bool:
        """Any row categorised into a Schedule C line (not personal/transfer)."""
        return any(c in CATEGORIES and v for c, v in self.rows.items())


def build(conn: sqlite3.Connection, lay: Layout, year: int) -> ScheduleC:
    rules, overrides = load_rules(lay)
    sc = ScheduleC(year)
    cents: dict[str, int] = defaultdict(int)
    for row in db.rows_for(conn, year, kind="bank"):
        if row.amount_cents is None:
            continue
        category = category_of(row, rules, overrides)
        if category is None:
            sc.uncategorised.append(row)
            continue
        sc.rows.setdefault(category, []).append(row)
        if category in CATEGORIES:
            # deposits are positive: receipts add them, the rest are money out
            sign = 1 if category == RECEIPTS else -1
            cents[category] += sign * row.amount_cents
    for form, box in FORM_BOXES:
        for f in db.facts_for(conn, year, form):
            if f.box == box:
                sc.receipts_forms += f.value
    sc.receipts_forms = round(sc.receipts_forms, 2)
    sc.receipts_rows = db.from_cents(cents[RECEIPTS])
    if not sc.business:
        return sc
    line1 = sc.receipts_rows
    if sc.receipts_forms > sc.receipts_rows:
        line1 = sc.receipts_forms
        sc.notes.append(
            f"1099-NEC/K total {sc.receipts_forms:,.2f} exceeds categorised receipts "
            f"{sc.receipts_rows:,.2f}: line 1 uses the forms; a client payment is "
            "uncategorised or went to another account"
        )
    elif sc.receipts_forms:
        sc.notes.append(
            f"categorised receipts {sc.receipts_rows:,.2f} include the "
            f"{sc.receipts_forms:,.2f} on 1099-NEC/K (every receipt counts, form "
            "or not)"
        )
    sc.lines["1"] = line1
    sc.lines["2"] = db.from_cents(cents[RETURNS])
    sc.lines["7"] = round(line1 - sc.lines["2"], 2)
    total = 0.0
    for category, (line, _) in CATEGORIES.items():
        if category in (RECEIPTS, RETURNS) or category not in sc.rows:
            continue
        value = db.from_cents(cents[category])
        if category == MEALS:
            value = round(value * MEALS_SHARE, 2)
        sc.lines[line] = value
        total += value
    sc.lines["28"] = round(total, 2)
    sc.lines["31"] = round(sc.lines["7"] - total, 2)
    if sc.uncategorised:
        sc.notes.append(
            f"{len(sc.uncategorised)} bank row(s) uncategorised and left out "
            f"(planner categorize --year {year})"
        )
    return sc


def label(line: str) -> str:
    for ln, text in CATEGORIES.values():
        if ln == line:
            return text
    return TOTALS[line]


def store(conn: sqlite3.Connection, lay: Layout, year: int) -> ScheduleC:
    """Rebuild the year and replace its SCH-C facts (none when no row is
    categorised into a Schedule C line)."""
    sc = build(conn, lay, year)
    with conn:
        conn.execute(
            "UPDATE facts SET status = 'superseded' WHERE form = ? AND tax_year = ? "
            "AND status = 'accepted'",
            (FORM, year),
        )
    if sc.lines:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        db.add_document(
            conn,
            fingerprint=f"schedule-c:{year}:{stamp}",
            file_name=f"Schedule C {year}",
            kind="derived",
            pages=0,
            batch=stamp,
            facts=[
                db.Fact(FORM, year, ISSUER, line, label(line), value, 0)
                for line, value in sc.lines.items()
            ],
        )
    return sc


def render(sc: ScheduleC) -> str:
    out = [f"Schedule C for {sc.year} (from categorised bank rows)"]
    if not sc.lines:
        out.append("no bank row is categorised into a Schedule C line yet")
    for line, value in sc.lines.items():
        out.append(f"  line {line:4} {label(line):40} {value:>12,.2f}")
    for category, why in EXCLUDED.items():
        if category in sc.rows:
            out.append(
                f"  {category}: {len(sc.rows[category])} row(s) left out ({why})"
            )
    out.extend(f"note: {n}" for n in sc.notes)
    if sc.uncategorised:
        out.append(f"uncategorised {len(sc.uncategorised)}:")
        for r in sc.uncategorised:
            amount = db.from_cents(r.amount_cents or 0)
            out.append(f"  {r.date}  {amount:>11,.2f}  {r.description}  [{r.row_key}]")
        out.append(
            "categorise with --rule TEXT --as CATEGORY or --row KEY --as CATEGORY; "
            "categories: " + ", ".join([*CATEGORIES, *EXCLUDED])
        )
    return "\n".join(out) + "\n"
