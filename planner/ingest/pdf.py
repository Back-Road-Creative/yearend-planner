"""Text PDFs → facts, by template. One template file per form per layout.

A template (``templates/forms/<form>.yaml``) names the form, the tax years
its layout covers, the words that identify a page, and one regex per box with
``AMOUNT`` standing for a dollar figure. A page is accepted only when every
required box parses; otherwise the file is unmatched with the reason. Nothing
is inferred.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

AMOUNT = r"(\(?-?\$?\s*[\d,]*\d(?:\.\d{1,2})?\)?)"
DEFAULT_YEAR = r"(?:tax year|for calendar year|calendar year)\s*:?\s*(20\d\d)"
DEFAULT_ISSUER = r"payer'?s name:?\s*([^\n]+)"


class Unmatched(ValueError):
    """The file could not be read into facts; carries the reason."""


@dataclass(frozen=True)
class Box:
    name: str
    label: str
    pattern: re.Pattern[str]
    required: bool
    group: int


@dataclass(frozen=True)
class Template:
    form: str
    years: tuple[int, ...]
    match: tuple[str, ...]
    year_pattern: re.Pattern[str]
    issuer_pattern: re.Pattern[str]
    boxes: tuple[Box, ...]
    source: str
    issuer: str | None = None  # literal, for the taxpayer's own documents

    def matches(self, text: str) -> bool:
        low = text.lower()
        return all(m.lower() in low for m in self.match)

    def find_year(self, text: str) -> int | None:
        m = self.year_pattern.search(text)
        return int(m.group(1)) if m else None

    def find_issuer(self, text: str) -> str:
        if self.issuer is not None:
            return self.issuer
        m = self.issuer_pattern.search(text)
        return " ".join(m.group(1).split()) if m else "unknown"

    def parse_boxes(self, text: str) -> tuple[dict[str, tuple[str, float]], list[str]]:
        found: dict[str, tuple[str, float]] = {}
        missing: list[str] = []
        for box in self.boxes:
            m = box.pattern.search(text)
            if m is None:
                if box.required:
                    missing.append(box.name)
                continue
            found[box.name] = (box.label, parse_amount(m.group(box.group)))
        return found, missing


@dataclass(frozen=True)
class ParsedForm:
    form: str
    tax_year: int
    issuer: str
    page: int
    boxes: dict[str, tuple[str, float]] = field(default_factory=dict)


def parse_amount(raw: str) -> float:
    s = raw.strip()
    negative = s.startswith("(") or s.startswith("-") or s.startswith("$-")
    digits = re.sub(r"[^\d.]", "", s)
    value = float(digits) if digits else 0.0
    return -value if negative else value


def normalize(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'").replace(" ", " ")


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern.replace("AMOUNT", AMOUNT), re.IGNORECASE)


def load_template(path: Path) -> Template:
    with path.open(encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh)
    boxes = tuple(
        Box(
            name=str(name),
            label=str(spec["label"]),
            pattern=_compile(str(spec["pattern"])),
            required=bool(spec.get("required", False)),
            group=int(spec.get("group", 1)),
        )
        for name, spec in raw["boxes"].items()
    )
    return Template(
        form=str(raw["form"]),
        years=tuple(int(y) for y in raw["years"]),
        match=tuple(str(m) for m in raw["match"]),
        year_pattern=_compile(str(raw.get("year_pattern", DEFAULT_YEAR))),
        issuer_pattern=_compile(str(raw.get("issuer_pattern", DEFAULT_ISSUER))),
        boxes=boxes,
        source=path.name,
        issuer=None if raw.get("issuer") is None else str(raw["issuer"]),
    )


def load_templates(folder: Path) -> list[Template]:
    return [load_template(p) for p in sorted(folder.glob("*.yaml"))]


def page_texts(path: Path) -> list[str]:
    import pdfplumber

    try:
        with pdfplumber.open(path) as pdf:
            return [normalize(page.extract_text() or "") for page in pdf.pages]
    except Exception as exc:  # pdfminer raises many types on a bad file
        raise Unmatched(f"not a readable PDF ({type(exc).__name__})") from exc


def parse_pdf(path: Path, templates: list[Template]) -> list[ParsedForm]:
    """Every form found on every page, or ``Unmatched`` with the reason."""
    texts = page_texts(path)
    if not any(t.strip() for t in texts):
        raise Unmatched(
            "no text layer (scanned); OCR with confirm is not available yet"
        )
    forms: list[ParsedForm] = []
    notes: list[str] = []
    for page_no, text in enumerate(texts, start=1):
        for tpl in templates:
            if not tpl.matches(text):
                continue
            year = tpl.find_year(text)
            if year is None:
                raise Unmatched(f"page {page_no}: {tpl.form} found but no tax year")
            if year not in tpl.years:
                notes.append(f"page {page_no}: {tpl.form} {year} has no template")
                continue
            boxes, missing = tpl.parse_boxes(text)
            if missing:
                raise Unmatched(
                    f"page {page_no}: {tpl.form} {year}: required boxes not found: "
                    + ", ".join(missing)
                )
            _merge(
                forms, ParsedForm(tpl.form, year, tpl.find_issuer(text), page_no, boxes)
            )
    if not forms:
        raise Unmatched("; ".join(notes) if notes else "no form template matched")
    return forms


def _merge(forms: list[ParsedForm], new: ParsedForm) -> None:
    """A copy of the same form on a later page must agree with the first."""
    for old in forms:
        if (old.form, old.tax_year, old.issuer) != (new.form, new.tax_year, new.issuer):
            continue
        for box, (_, value) in new.boxes.items():
            if box in old.boxes and old.boxes[box][1] != value:
                raise Unmatched(
                    f"{new.form} {new.tax_year} box {box}: page {old.page} says "
                    f"{old.boxes[box][1]} but page {new.page} says {value}"
                )
        old.boxes.update(new.boxes)
        return
    forms.append(new)
