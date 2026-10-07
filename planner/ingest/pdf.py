"""Text PDFs → facts, by template. One template file per form per layout.

A template (``templates/forms/<form>.yaml``) names the form, the tax years
its layout covers, the words that identify a page, and one regex per box with
``AMOUNT`` standing for a dollar figure. A box has a ``kind``: ``amount`` (the
default; the pattern's group is a dollar figure), ``text`` (the group is
kept as words, optionally only when it is one of ``allowed``) or ``check``
(``options`` maps a value to the label printed beside its check box; the value
whose box carries a mark is the answer, and two marks are an error; ``mark:
either`` also takes a mark printed after the label, and ``blank`` is the value
when the label is on the page with no mark beside it). A page is
accepted only when every required box parses; otherwise the file is unmatched
with the reason. Nothing is inferred.

An official form prints each box as a ruled cell with the label and the
figure on separate text lines, and plain extraction runs neighbouring columns
together. A page drawn as a grid of ruled cells is therefore read one cell at
a time: each cell's first line opens with ``CELL``, its further lines are
indented, and the words outside every cell follow. A pattern's ``\\s+AMOUNT``
can then cross from a label to its figure but never into the next box. When
that reading finds no form, the page's plain text is tried instead.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

AMOUNT = r"(\(?-?\$?\s*[\d,]*\d(?:\.\d{1,2})?\)?)"
# The year a form prints after its OMB number, where it has no year box (the
# 1099-R and 1098-T print it there, above the form number).
OMB_YEAR = r"OMB No\.\s*[\d-]+\s+"
DEFAULT_YEAR = (
    rf"(?:(?:tax year|for calendar year|calendar year)\s*:?\s*|{OMB_YEAR})(20\d\d)"
)
# The rest of an issuer's name-and-address label after "name" (an official form
# runs ", street address, ... and telephone no." over two lines of its box),
# so the pattern's group takes the name on the line below; or a colon.
ADDRESS = (
    r"(?:,[^\n:]*(?:\n[ \t]*[^\n:]*?"
    r"(?:telephone (?:no\.|number)|postal code|ZIP code)[^\n:]*)?)?[ \t]*:?\s*"
)
DEFAULT_ISSUER = r"payer'?s nameADDRESS([^\n]+)"
KINDS = ("amount", "text", "check")
# A check box that is marked, as text extraction renders it: a ballot-box glyph,
# a check mark, a bracketed or parenthesised X, or a bare X not inside a word.
MARK = r"(?:\[\s*[xX✓✔]\s*\]|\(\s*[xX]\s*\)|[☒☑✓✔✗]|(?<!\w)[xX](?=[ \t]))"
# The same mark after its label, where it may end the line.
MARK_AFTER = r"(?:\[\s*[xX✓✔]\s*\]|\(\s*[xX]\s*\)|[☒☑✓✔✗]|(?<!\w)[xX](?!\w))"
CELL = "¦"  # opens the first line of each ruled box on a form page
# Onto the next row of the same ruled box (its continuation rows are indented
# two spaces; see _page_text).
CONT = r"[ \t]*\n  [ \t]*"
# The rest of a box label up to its figure, over the rows of its ruled box
# when the label wraps there.
WRAP = rf"[^$\d\n]*(?:{CONT}[^$\d\n]*)*"
# A check box's mark alone on the row under its label, inside the label's box
# (an official form prints the box below or beside a wrapped label).
CELL_MARK = rf"{CONT}{MARK_AFTER}[ \t]*(?=\n|$)"
MIN_CELLS = 6  # fewer ruled cells than this and the page is read as plain text
# A ruled square this small on both sides is a check box, not a box of the
# form: its mark belongs to the box around it.
CHECK_SIDE = 12.0


class Unmatched(ValueError):
    """The file could not be read into facts; carries the reason."""


class Ruled(str):
    """A page's text read one ruled cell at a time, with the page's plain text
    kept as ``plain`` for when the cells give no form."""

    plain: str

    def __new__(cls, text: str, plain: str) -> Ruled:
        made = super().__new__(cls, text)
        made.plain = plain
        return made


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
    blank: str | None = None  # check: the value when a label shows, unmarked
    labels: tuple[re.Pattern[str], ...] = ()  # check: each label, unmarked
    # the check box whose value picks the name this box is filed under, and
    # (value, name, label) for each value (the per-sale 1099-B files proceeds
    # as st_proceeds or lt_proceeds by its box 2 term)
    by: str | None = None
    names: tuple[tuple[str, str, str], ...] = ()

    def key(self, found: dict[str, tuple[str, Value]]) -> tuple[str, str] | None:
        """The (name, label) this box is filed under, given the boxes found;
        None when its ``by`` box was not found."""
        if self.by is None:
            return self.name, self.label
        if self.by not in found:
            return None
        value = found[self.by][1]
        for when, name, label in self.names:
            if when == value:
                return name, label
        raise Unmatched(f"{self.label.lower()}: box {self.by} {value} is not read")

    def read(self, text: str) -> Value | None:
        """The box's value on this page, or None when it is not there."""
        if self.kind == "check":
            marked = [v for v, pat in self.options if pat.search(text)]
            if len(marked) > 1:
                raise Unmatched(
                    f"more than one {self.label.lower()} checked: {', '.join(marked)}"
                )
            if marked:
                return marked[0]
            seen = self.blank is not None and any(p.search(text) for p in self.labels)
            return self.blank if seen else None
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
            for pat in self.labels if self.blank is not None else ():
                if m := pat.search(text):
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
    # (label, pattern) pairs naming one event of a form an issuer files once per
    # event (Form 3921 per exercise): added to the issuer, so a second form is
    # its own and only a corrected copy of the same event replaces it
    event: tuple[tuple[str, re.Pattern[str]], ...] = ()

    def matches(self, text: str) -> bool:
        """Every match phrase is on the page; spacing and line breaks are
        ignored, as a ruled cell or OCR may split or glue the words."""
        page = "".join(text.split()).lower()
        return all("".join(m.split()).lower() in page for m in self.match)

    def find_year(self, text: str) -> int | None:
        m = self.year_pattern.search(text)
        if not m:
            return None
        # A two-digit year is a revision date's (the IL-1040 back page prints
        # only "R-12/25"), 20xx.
        year = int(m.group(1))
        return year + 2000 if year < 100 else year

    def find_issuer(self, text: str) -> str:
        """The issuer's name, with ``event`` in brackets after it."""
        if self.issuer is not None:
            return self.issuer
        m = self.issuer_pattern.search(text)
        name = " ".join(m.group(1).split()) if m else "unknown"
        found = [(label, p.search(text)) for label, p in self.event]
        said = ", ".join(f"{label} {e.group(1)}" for label, e in found if e)
        return f"{name} [{said}]" if said else name

    def parse_boxes(self, text: str) -> tuple[dict[str, tuple[str, Value]], list[str]]:
        found: dict[str, tuple[str, Value]] = {}
        missing: list[str] = []
        # A box filed by another box's value reads after the boxes it names.
        for box in sorted(self.boxes, key=lambda b: b.by is not None):
            value = box.read(text)
            key = box.key(found) if value is not None else None
            if value is None or key is None:
                if box.required:
                    missing.append(box.name)
                continue
            found[key[0]] = (key[1], value)
        return found, missing

    def spots(self, text: str, page: int, found: dict[str, Any]) -> dict[str, Spot]:
        """Where each found box's value sits in ``text``: the page and the line
        (counted from 0) it starts on."""
        out: dict[str, Spot] = {}
        for box in self.boxes:
            key = box.key(found)
            at = box.offset(text) if key and key[0] in found else None
            if key and at is not None:
                out[key[0]] = (page, text.count("\n", 0, at))
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


def base_issuer(issuer: str) -> str:
    """The issuer's name without the bracketed event a per-event form adds."""
    return issuer.split(" [", 1)[0]


def _compile(pattern: str) -> re.Pattern[str]:
    pattern = _spaces(pattern)
    for token, regex in (
        ("AMOUNT", AMOUNT),
        ("ADDRESS", ADDRESS),
        ("OMB_YEAR", OMB_YEAR),
        ("WRAP", WRAP),
        ("CONT", CONT),
    ):
        pattern = pattern.replace(token, regex)
    return re.compile(pattern, re.IGNORECASE)


def _spaces(pattern: str) -> str:
    """``pattern`` with each literal space (outside a character class) matching
    any run of whitespace, line breaks included: an official form wraps a long
    box label over the lines of its box."""
    out: list[str] = []
    in_class = escaped = False
    for ch in pattern:
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == " ":
            if not out or out[-1] != r"\s+":
                out.append(r"\s+")
            continue
        out.append(ch)
    return "".join(out)


def _box(path: Path, name: object, spec: dict[str, Any]) -> Box:
    kind = str(spec.get("kind", "amount"))
    if kind not in KINDS:
        raise ValueError(f"{path.name} box {name}: kind must be one of {KINDS}")
    options: tuple[tuple[str, re.Pattern[str]], ...] = ()
    labels: tuple[re.Pattern[str], ...] = ()
    if kind == "check":
        if not spec.get("options"):
            raise ValueError(f"{path.name} box {name}: a check box needs options")
        either = spec.get("mark", "before") == "either"
        # An option's label, or a list of the ways forms print it.
        words = {
            str(value): "(?:"
            + "|".join(
                r"\s+".join(
                    re.escape(w).replace("/", r"/\s*") for w in str(one).split()
                )
                for one in ([label] if isinstance(label, str) else label)
            )
            + ")"
            for value, label in spec["options"].items()
        }
        options = tuple(
            (
                value,
                re.compile(
                    rf"(?:{MARK}[ \t]*{w}|{w}[ \t]*{MARK_AFTER}|{w}{CELL_MARK})"
                    if either
                    else rf"(?:{MARK}[ \t]*{w}|{w}{CELL_MARK})",
                    re.IGNORECASE,
                ),
            )
            for value, w in words.items()
        )
        labels = tuple(re.compile(w, re.IGNORECASE) for w in words.values())
    elif "pattern" not in spec:
        raise ValueError(f"{path.name} box {name}: a pattern is required")
    if ("by" in spec) != ("names" in spec):
        raise ValueError(f"{path.name} box {name}: by and names go together")
    return Box(
        name=str(name),
        label=str(spec["label"]),
        pattern=_compile(str(spec.get("pattern", ""))),
        required=bool(spec.get("required", False)),
        group=int(spec.get("group", 1)),
        kind=kind,
        options=options,
        allowed=tuple(str(a) for a in spec.get("allowed", ())),
        blank=None if spec.get("blank") is None else str(spec["blank"]),
        labels=labels,
        by=None if spec.get("by") is None else str(spec["by"]),
        names=tuple(
            (str(when), str(pair[0]), str(pair[1]))
            for when, pair in spec.get("names", {}).items()
        ),
    )


def load_template(path: Path) -> Template:
    with path.open(encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh)
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
        event=tuple(
            (str(k), _compile(str(v))) for k, v in raw.get("event", {}).items()
        ),
    )


def load_templates(folder: Path) -> list[Template]:
    return [load_template(p) for p in sorted(folder.glob("*.yaml"))]


def page_texts(path: Path) -> list[str]:
    import pdfplumber

    try:
        with pdfplumber.open(path) as pdf:
            return [_page_text(page) for page in pdf.pages]
    except Exception as exc:  # pdfminer raises many types on a bad file
        raise Unmatched(f"not a readable PDF ({type(exc).__name__})") from exc


def _page_text(page: Any) -> str:
    """The page's plain text, or ``Ruled`` text when it is a grid of cells."""
    plain = normalize(page.extract_text() or "")
    if not plain.strip():
        return plain
    found = page.find_tables(
        {"vertical_strategy": "lines", "horizontal_strategy": "lines"}
    )
    cells = [
        c
        for table in found
        for c in table.cells
        if c and max(c[2] - c[0], c[3] - c[1]) >= CHECK_SIDE
    ]
    if len(cells) < MIN_CELLS:
        return plain
    inside: dict[int, list[dict[str, Any]]] = {}
    outside: list[dict[str, Any]] = []
    # Rotated or stacked words (the W-2's sideways "Code") would split a
    # cell's lines.
    words = [w for w in page.extract_words(extra_attrs=["upright"]) if w["upright"]]
    stacked = _stacked(words)
    for word in words:
        if id(word) in stacked:
            continue
        x = (word["x0"] + word["x1"]) / 2
        y = (word["top"] + word["bottom"]) / 2
        hits = [
            n for n, c in enumerate(cells) if c[0] <= x <= c[2] and c[1] <= y <= c[3]
        ]
        if hits:
            smallest = min(hits, key=lambda n: _area(cells[n]))
            inside.setdefault(smallest, []).append(word)
        else:
            outside.append(word)
    if len(inside) < MIN_CELLS:
        return plain
    lines: list[str] = []
    for n in sorted(inside, key=lambda n: (cells[n][1], cells[n][0])):
        rows = _rows(inside[n])
        lines.append(f"{CELL} {rows[0]}")
        lines.extend(f"  {row}" for row in rows[1:])
    lines.extend(_rows(outside))
    return Ruled(normalize("\n".join(lines)), plain)


def _stacked(words: list[dict[str, Any]]) -> set[int]:
    """The ids of letters printed one above another, as a vertical label: short
    words of letters (not a check mark) sharing a left edge on adjacent rows."""
    short = [
        w
        for w in words
        if len(w["text"]) <= 2 and w["text"].isalpha() and w["text"] not in ("X", "x")
    ]
    out: set[int] = set()
    for a in short:
        for b in short:
            near = abs(a["x0"] - b["x0"]) <= 1.5
            gap = abs(a["top"] - b["top"])
            if a is not b and near and 0 < gap <= 1.5 * (a["bottom"] - a["top"]):
                out.update((id(a), id(b)))
    return out


def _area(cell: Sequence[float]) -> float:
    return (cell[2] - cell[0]) * (cell[3] - cell[1])


def _rows(words: list[dict[str, Any]]) -> list[str]:
    """Words joined into lines: a word whose top is within half its height of
    the line's first word is on that line; each line reads left to right."""
    rows: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        half = (word["bottom"] - word["top"]) / 2
        if rows and abs(word["top"] - rows[-1][0]["top"]) <= half:
            rows[-1].append(word)
        else:
            rows.append([word])
    return [" ".join(w["text"] for w in sorted(r, key=lambda w: w["x0"])) for r in rows]


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
        found, said = _read_ruled(page_no, text, templates, ocr)
        for form in found:
            _merge(forms, form)
        notes.extend(said)
    return forms, notes


def _read_ruled(
    page_no: int, text: str, templates: list[Template], ocr: bool
) -> tuple[list[ParsedForm], list[str]]:
    """A ruled page's forms from its cells, else from its plain text; when
    both fail, the cells' reason."""
    if not isinstance(text, Ruled):
        return _read_page(page_no, text, templates, ocr)
    try:
        found, said = _read_page(page_no, text, templates, ocr)
    except Unmatched as exc:
        try:
            found, said = _read_page(page_no, text.plain, templates, ocr)
        except Unmatched:
            raise exc from None
        if not found:
            raise
        return found, said
    return (found, said) if found else _read_page(page_no, text.plain, templates, ocr)


def _read_page(
    page_no: int, text: str, templates: list[Template], ocr: bool
) -> tuple[list[ParsedForm], list[str]]:
    """The forms on one page, and a note for each form with no template."""
    forms: list[ParsedForm] = []
    notes: list[str] = []
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
