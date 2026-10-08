"""Text PDFs → facts, by template. One template file per form per layout.

A template (``templates/forms/<form>.yaml``) names the form, the tax years
its layout covers, the words that identify a page, and one regex per box with
``AMOUNT`` standing for a dollar figure. A box has a ``kind``: ``amount`` (the
default; the pattern's group is a dollar figure), ``text`` (the group is
kept as words, optionally only when it is one of ``allowed``) or ``check``
(``options`` maps a value to the label printed beside its check box; the value
whose box carries a mark is the answer, and two marks are an error). A page is
accepted only when every required box parses; otherwise the file is unmatched
with the reason. Nothing is inferred.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from planner.config import safe_load

AMOUNT = r"(\(?-?\$?\s*[\d,]*\d(?:\.\d{1,2})?\)?)"
DEFAULT_YEAR = r"(?:tax year|for calendar year|calendar year)\s*:?\s*(20\d\d)"
DEFAULT_ISSUER = r"payer'?s name:?\s*([^\n]+)"
KINDS = ("amount", "text", "check")
# A check box that is marked, as text extraction renders it: a ballot-box glyph,
# a check mark, a bracketed or parenthesised X, or a bare X not inside a word.
MARK = r"(?:\[\s*[xX✓✔]\s*\]|\(\s*[xX]\s*\)|[☒☑✓✔✗]|(?<!\w)[xX](?=[ \t]))"


class Unmatched(ValueError):
    """The file could not be read into facts; carries the reason."""


Value = float | str  # dollars, or the words of a text or check box
Spot = tuple[int, int]  # (page from 1, line from 0) of a value in a page's text


@dataclass(frozen=True)
class Box:
    name: str
    label: str
    pattern: re.Pattern[str]
    required: bool
    group: int
    kind: str = "amount"
    options: tuple[tuple[str, re.Pattern[str]], ...] = ()  # check: value -> label
    allowed: tuple[str, ...] = ()  # text: the only words accepted

    def read(self, text: str) -> Value | None:
        """The box's value on this page, or None when it is not there."""
        if self.kind == "check":
            marked = [v for v, pat in self.options if pat.search(text)]
            if len(marked) > 1:
                raise Unmatched(
                    f"more than one {self.label.lower()} checked: {', '.join(marked)}"
                )
            return marked[0] if marked else None
        m = self.pattern.search(text)
        if m is None:
            return None
        if self.kind == "amount":
            return parse_amount(m.group(self.group))
        word = " ".join(m.group(self.group).split())
        if not word or (self.allowed and word not in self.allowed):
            return None
        return word

    def offset(self, text: str) -> int | None:
        """Where in ``text`` the value ``read`` takes starts (for a check box,
        where its marked label starts), or None when it is not there."""
        if self.kind == "check":
            for _, pat in self.options:
                m = pat.search(text)
                if m:
                    return m.start()
            return None
        m = self.pattern.search(text)
        if m is None:
            return None
        return m.start(self.group) if m.start(self.group) >= 0 else m.start()


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

    def parse_boxes(self, text: str) -> tuple[dict[str, tuple[str, Value]], list[str]]:
        found: dict[str, tuple[str, Value]] = {}
        missing: list[str] = []
        for box in self.boxes:
            value = box.read(text)
            if value is None:
                if box.required:
                    missing.append(box.name)
                continue
            found[box.name] = (box.label, value)
        return found, missing

    def spots(self, text: str, page: int, found: dict[str, Any]) -> dict[str, Spot]:
        """Where each found box's value sits in ``text``: the page and the line
        (counted from 0) it starts on."""
        out: dict[str, Spot] = {}
        for box in self.boxes:
            at = box.offset(text) if box.name in found else None
            if at is not None:
                out[box.name] = (page, text.count("\n", 0, at))
        return out


@dataclass(frozen=True)
class ParsedForm:
    form: str
    tax_year: int
    issuer: str
    page: int
    boxes: dict[str, tuple[str, Value]] = field(default_factory=dict)
    ocr: bool = False  # read by OCR: lands pending, not accepted
    # box -> (page, line) its value was read from; kept for OCR, where the line
    # is cut from the scan and shown beside the value at confirm
    spots: dict[str, Spot] = field(default_factory=dict, compare=False)


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


def _box(path: Path, name: object, spec: dict[str, Any]) -> Box:
    kind = str(spec.get("kind", "amount"))
    if kind not in KINDS:
        raise ValueError(f"{path.name} box {name}: kind must be one of {KINDS}")
    options: tuple[tuple[str, re.Pattern[str]], ...] = ()
    if kind == "check":
        if not spec.get("options"):
            raise ValueError(f"{path.name} box {name}: a check box needs options")
        options = tuple(
            (
                str(value),
                re.compile(
                    MARK + r"[ \t]*" + r"\s+".join(map(re.escape, str(label).split())),
                    re.IGNORECASE,
                ),
            )
            for value, label in spec["options"].items()
        )
    elif "pattern" not in spec:
        raise ValueError(f"{path.name} box {name}: a pattern is required")
    return Box(
        name=str(name),
        label=str(spec["label"]),
        pattern=_compile(str(spec.get("pattern", ""))),
        required=bool(spec.get("required", False)),
        group=int(spec.get("group", 1)),
        kind=kind,
        options=options,
        allowed=tuple(str(a) for a in spec.get("allowed", ())),
    )


def load_template(path: Path) -> Template:
    with path.open(encoding="utf-8") as fh:
        raw: dict[str, Any] = safe_load(fh)
    boxes = tuple(_box(path, name, spec) for name, spec in raw["boxes"].items())
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


def parse_pdf(
    path: Path,
    templates: list[Template],
    ocr: Callable[[Path], Sequence[str]] | None = None,
    notes: list[str] | None = None,
) -> list[ParsedForm]:
    """Every form found on every page, or ``Unmatched`` with the reason. A page
    with no text layer goes through ``ocr`` when one is given (the whole file
    when none of its pages has text, only the bare pages when some do); the
    forms read that way are marked so their facts wait for confirm. ``ocr``
    returns one text per page of the file. A page that stays unread is added to
    ``notes`` when the rest of the file is accepted."""
    texts = page_texts(path)
    bare = [n for n, t in enumerate(texts, start=1) if not t.strip()]
    if not bare:
        return parse_texts(texts, templates)
    if len(bare) == len(texts):
        if ocr is None:
            raise Unmatched("no text layer (scanned); OCR engine is not installed")
        texts = list(ocr(path))
        if not any(t.strip() for t in texts):
            raise Unmatched("no text layer (scanned) and OCR read nothing from it")
        return parse_texts(texts, templates, ocr=True)
    return _parse_mixed(
        path, templates, texts, bare, ocr, [] if notes is None else notes
    )


def _pages(numbers: Sequence[int]) -> str:
    """``page 2 has`` / ``pages 2, 3 have``, ready to precede a predicate."""
    one = len(numbers) == 1
    return (
        ("page " if one else "pages ")
        + ", ".join(map(str, numbers))
        + (" has" if one else " have")
    )


def _parse_mixed(
    path: Path,
    templates: list[Template],
    texts: list[str],
    bare: list[int],
    ocr: Callable[[Path], Sequence[str]] | None,
    notes: list[str],
) -> list[ParsedForm]:
    """Text pages from the text layer, bare pages (a scan inside a text PDF)
    from ``ocr``; each page's forms keep the trust of how they were read."""
    typed = [(n, t) for n, t in enumerate(texts, start=1) if n not in bare]
    if ocr is None:
        skipped = f"{_pages(bare)} no text layer and the OCR engine is not installed"
        try:
            forms, template_notes = _parse_pages(typed, templates, False)
        except Unmatched as exc:
            raise Unmatched(f"{exc}; {skipped}") from exc
        if not forms:
            why = "; ".join(template_notes) or "no form template matched"
            raise Unmatched(f"{why}; {skipped}")
        notes.append(f"{skipped}; not read")
        return forms
    scans = ocr(path)
    if len(scans) != len(texts):
        raise Unmatched(f"OCR read {len(scans)} pages of a {len(texts)}-page file")
    read = [(n, scans[n - 1]) for n in bare]
    empty = [n for n, t in read if not t.strip()]
    forms, template_notes = _parse_pages(typed, templates, False)
    scanned, scan_notes = _parse_pages(
        [(n, t) for n, t in read if t.strip()], templates, True
    )
    unread = f"{_pages(empty)} no text layer and OCR read nothing; not read"
    if not forms and not scanned:
        why = "; ".join(template_notes + scan_notes) or "no form template matched"
        raise Unmatched(f"{why}; {unread}" if empty else why)
    if empty:
        notes.append(unread)
    return forms + _scan_only(forms, scanned)


def _scan_only(typed: list[ParsedForm], scanned: list[ParsedForm]) -> list[ParsedForm]:
    """The scanned forms, minus any box a text page already supplied. One
    document holds a box once, and a text page outranks a scan, but the two
    must not disagree."""
    kept: list[ParsedForm] = []
    for new in scanned:
        boxes = dict(new.boxes)
        for old in typed:
            if (old.form, old.tax_year, old.issuer) != (
                new.form,
                new.tax_year,
                new.issuer,
            ):
                continue
            for box in [b for b in boxes if b in old.boxes]:
                if old.boxes[box][1] != boxes[box][1]:
                    raise Unmatched(
                        f"{new.form} {new.tax_year} box {box}: page {old.page} says "
                        f"{old.boxes[box][1]} but page {new.page} says {boxes[box][1]}"
                    )
                del boxes[box]
        if boxes:
            spots = {b: s for b, s in new.spots.items() if b in boxes}
            kept.append(replace(new, boxes=boxes, spots=spots))
    return kept


def parse_texts(
    texts: list[str], templates: list[Template], ocr: bool = False
) -> list[ParsedForm]:
    forms, notes = _parse_pages(list(enumerate(texts, start=1)), templates, ocr)
    if not forms:
        raise Unmatched("; ".join(notes) if notes else "no form template matched")
    return forms


def _parse_pages(
    pages: list[tuple[int, str]], templates: list[Template], ocr: bool
) -> tuple[list[ParsedForm], list[str]]:
    """Forms on the numbered pages, and a note for each form with no template."""
    forms: list[ParsedForm] = []
    notes: list[str] = []
    for page_no, text in pages:
        for tpl in templates:
            if not tpl.matches(text):
                continue
            year = tpl.find_year(text)
            if year is None:
                raise Unmatched(f"page {page_no}: {tpl.form} found but no tax year")
            if year not in tpl.years:
                notes.append(f"page {page_no}: {tpl.form} {year} has no template")
                continue
            try:
                boxes, missing = tpl.parse_boxes(text)
            except Unmatched as exc:
                raise Unmatched(f"page {page_no}: {tpl.form} {year}: {exc}") from exc
            if missing:
                raise Unmatched(
                    f"page {page_no}: {tpl.form} {year}: required boxes not found: "
                    + ", ".join(missing)
                )
            spots = tpl.spots(text, page_no, boxes) if ocr else {}
            _merge(
                forms,
                ParsedForm(
                    tpl.form, year, tpl.find_issuer(text), page_no, boxes, ocr, spots
                ),
            )
    return forms, notes


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
        for box, spot in new.spots.items():
            old.spots.setdefault(box, spot)
        return
    forms.append(new)
