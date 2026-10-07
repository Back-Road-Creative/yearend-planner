"""Unit 7h: official blank IRS forms carrying synthetic values (written by
scripts/real_forms.py, listed in tests/fixtures/real/forms.yaml) read back
through the ruled-cell reader, and the pattern pieces that reading needs."""

from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from planner.ingest import pdf
from planner.ingest.pdf import Ruled, Unmatched, load_template, parse_pdf, parse_texts
from tests.pdfgen import make_pdf
from tests.test_ingest import INT_2025

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "tests" / "fixtures" / "real"
TEMPLATES = pdf.load_templates(ROOT / "templates" / "forms")
FORMS: list[dict[str, Any]] = yaml.safe_load(
    (REAL / "forms.yaml").read_text(encoding="utf-8")
)["forms"]
_spec = importlib.util.spec_from_file_location(
    "real_forms", ROOT / "scripts" / "real_forms.py"
)
assert _spec is not None and _spec.loader is not None
real_forms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_forms)
TIN = re.compile(r"\b(?:\d{3}-\d{2}-\d{4}|\d{2}-\d{7}|\d{9,17})\b")

# One sale on the official 1099-B, as the ruled reader prints its cells.
SALE = [
    "¦ PAYER'S name, street address, city or town, state or province, country, ZIP",
    "  or foreign postal code, and telephone no.",
    "  Example Brokerage (synthetic)",
    "¦ OMB No. 1545-0715",
    "  2025",
    "  Form 1099-B",
    "¦ 1c Date sold or disposed",
    "  05/06/2025",
    "¦ 1d Proceeds",
    "  $ 900.00",
    "¦ 1e Cost or other basis",
    "  $ 1,000.00",
    "¦ 1g Wash sale loss disallowed",
    "  $",
    "¦ 2 Short-term gain or loss",
    "  X",
    "  Long-term gain or loss",
    "  Ordinary",
    "¦ 4 Federal income tax withheld",
    "  $ 10.00",
]


@pytest.mark.parametrize("entry", FORMS, ids=[e["name"] for e in FORMS])
def test_a_real_form_reads_back_its_synthetic_values(entry: dict[str, Any]) -> None:
    path = REAL / f"{entry['name']}.pdf"
    assert isinstance(pdf.page_texts(path)[0], Ruled)
    (got,) = parse_pdf(path, TEMPLATES)
    want = entry["expect"]
    assert (got.form, got.tax_year, got.issuer) == (
        want["form"],
        want["year"],
        want["issuer"],
    )
    assert {k: v[1] for k, v in got.boxes.items()} == want["boxes"]


def test_the_fixtures_are_irs_blanks_with_synthetic_values_only() -> None:
    assert sorted(p.stem for p in REAL.glob("*.pdf")) == sorted(
        e["name"] for e in FORMS
    )
    for e in FORMS:
        assert e["url"].startswith("https://www.irs.gov/"), e["name"]
        assert "(synthetic)" in e["expect"]["issuer"], e["name"]
        for value in e["fields"].values():
            assert not TIN.search(value), (e["name"], value)


def test_a_literal_space_matches_any_whitespace_outside_a_class() -> None:
    assert pdf._spaces("1 IRA  contributions") == r"1\s+IRA\s+contributions"
    assert pdf._spaces("[^ \\n]") == "[^ \\n]"
    assert pdf._spaces(r"a\ b[ ]c") == r"a\ b[ ]c"


def test_plain_text_pages_and_empty_cells_fall_back_to_plain_reading(
    tmp_path: Path,
) -> None:
    text = pdf.page_texts(make_pdf(tmp_path / "int.pdf", [INT_2025]))[0]
    assert not isinstance(text, Ruled)
    (form,) = parse_texts([Ruled("¦ nothing a template knows", text)], TEMPLATES)
    assert (form.form, form.boxes["1"][1]) == ("1099-INT", 1234.56)


def test_a_sale_files_its_figures_by_term_and_a_blank_box_stays_blank() -> None:
    (form,) = parse_texts(["\n".join(SALE)], TEMPLATES)
    assert form.issuer == "Example Brokerage (synthetic) [sold 05/06/2025]"
    assert {k: v[1] for k, v in form.boxes.items()} == {
        "1c": "05/06/2025",
        "2": "short",
        "st_proceeds": 900.0,
        "st_basis": 1000.0,
        "4": 10.0,
    }
    ordinary = SALE[:15] + ["  Long-term gain or loss", "  Ordinary", "  X"] + SALE[18:]
    with pytest.raises(Unmatched, match="box 2 ordinary is not read"):
        parse_texts(["\n".join(ordinary)], TEMPLATES)


def test_check_options_take_a_mark_under_the_label_and_alternative_labels(
    tmp_path: Path,
) -> None:
    spec = tmp_path / "t.yaml"
    spec.write_text(
        'form: T-1\nyears: [2025]\nmatch: ["Test form"]\n'
        'year_pattern: "Test form (20\\\\d\\\\d)"\nissuer: self\nboxes:\n'
        '  "1": {label: Kind, kind: check, required: true, '
        'options: {a: ["Kind A", "Kind split A"], b: "Kind B"}}\n',
        encoding="utf-8",
    )
    tpl = load_template(spec)

    def kind(*rows: str) -> object:
        (form,) = parse_texts(["\n".join(("Test form 2025",) + rows)], [tpl])
        return form.boxes["1"][1]

    assert kind("¦ Kind A", "  X", "¦ Kind B") == "a"
    assert kind("¦ [X] Kind split A", "¦ Kind B") == "a"
    assert kind("¦ Kind A", "¦ Kind B", "  X") == "b"
    bad = tmp_path / "bad.yaml"
    bad.write_text(spec.read_text().replace("required: true", "by: '2'"))
    with pytest.raises(ValueError, match="by and names go together"):
        load_template(bad)


def test_letters_stacked_down_a_cell_are_dropped() -> None:
    def word(text: str, x0: float, top: float) -> dict[str, Any]:
        return {"text": text, "x0": x0, "top": top, "bottom": top + 6}

    words = [
        word("C", 10, 0),
        word("o", 10.5, 6),
        word("d", 10, 12),
        word("D", 30, 6),
        word("X", 10, 18),
        word("12", 10, 24),
        word("ab", 60, 0),
    ]
    assert pdf._stacked(words) == {id(w) for w in words[:3]}


def test_the_maker_prints_into_named_fields_and_pins_the_blank(tmp_path: Path) -> None:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject
    from pypdf.generic import TextStringObject as Text

    writer = PdfWriter(clone_from=make_pdf(tmp_path / "base.pdf", [["Form T-1"]]))
    annots = ArrayObject()
    for name, rect in (
        ("f1_1[0]", (100, 600, 300, 640)),
        ("c1_1[0]", (50, 500, 58, 508)),
    ):
        widget = DictionaryObject()
        widget[NameObject("/Subtype")] = NameObject("/Widget")
        widget[NameObject("/T")] = Text(name)
        widget[NameObject("/Rect")] = ArrayObject(FloatObject(v) for v in rect)
        annots.append(writer._add_object(widget))
    writer.pages[0][NameObject("/Annots")] = annots
    blank = tmp_path / "blank.pdf"
    with blank.open("wb") as fh:
        writer.write(fh)
    values = {"f1_1[0]": "Example Co (synthetic)\n1 Main St", "c1_1[0]": "X"}
    out = real_forms.fill(blank, 0, values, tmp_path / "out.pdf")
    assert "/Annots" not in PdfReader(out).pages[0]
    text = pdf.page_texts(out)[0]
    assert "Example Co (synthetic)" in text and "1 Main St" in text and "X" in text
    with pytest.raises(ValueError, match="matches"):
        real_forms.fill(blank, 0, {"1[0]": "1"}, tmp_path / "x.pdf")
    url = "https://www.irs.gov/pub/blank.pdf"
    entry = {"name": "t", "url": url, "sha256": "0" * 64}
    with pytest.raises(ValueError, match="the IRS changed the form"):
        real_forms.blank(entry, tmp_path)
    pinned = hashlib.sha256(blank.read_bytes()).hexdigest()
    assert real_forms.blank(dict(entry, sha256=pinned), tmp_path) == blank
    with pytest.raises(ValueError, match="irs.gov only"):
        real_forms.blank(dict(entry, url="https://example.com/blank.pdf"), tmp_path)
