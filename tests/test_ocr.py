"""OCR with confirm (Phase 2e): scans and photos land pending, never accepted."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest import ingest, ocr
from planner.ingest.confirm import accept, pending, reject
from planner.ledger import db
from planner.paths import Layout
from tests.pdfgen import make_pdf
from tests.test_ingest import DIV_2025, INT_2025

runner = CliRunner()
INT_LINES = [
    "Form 1099-INT Interest Income 2025",
    "PAYER'S name: Example Bank (synthetic)",
    "1 Interest income 1,234.56",
    "4 Federal income tax withheld 0.00",
]


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    return lay


def fake_ocr(path: Path) -> list[str]:
    return ["\n".join(DIV_2025)]


def photo(path: Path, width: int = 40) -> Path:
    """A blank image; ``width`` varies the bytes so two photos are not duplicates."""
    from PIL import Image

    Image.new("RGB", (width, 20), "white").save(path)
    return path


def fake_engine(
    monkeypatch: pytest.MonkeyPatch, lines: list[str], top: int = 20, step: int = 40
) -> None:
    """An engine double: one 20-pixel-tall box per line, ``step`` pixels apart,
    in the pixel space of whatever image it is handed."""

    def engine(_array: object, **_kw: object) -> tuple[list[list[object]], list[float]]:
        return [
            [
                [[10, y], [410, y], [410, y + 20], [10, y + 20]],
                text,
                0.98,
            ]
            for i, text in enumerate(lines)
            for y in [top + i * step]
        ], [0.0]

    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "_engine", lambda: engine)


def ruled_photo(path: Path, lines: int, mark: int | None = None) -> Path:
    """A white image tall enough for ``lines`` engine-double lines, with a black
    bar on line ``mark`` so a crop shows which line it was cut from."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (500, 40 + 40 * lines), "white")
    if mark is not None:
        y = 20 + mark * 40
        ImageDraw.Draw(img).rectangle([10, y + 2, 200, y + 16], fill="black")
    img.save(path)
    return path


def dark(png: Path) -> bool:
    from PIL import Image

    with Image.open(png) as im:
        return (
            im.convert("L").point(lambda v: 255 if v < 60 else 0).getbbox() is not None
        )


def ledger(lay: Layout) -> db.sqlite3.Connection:  # type: ignore[name-defined]
    return db.connect(lay.data / "ledger" / "planner.db")


def test_lines_from_groups_boxes_into_reading_order() -> None:
    def box(x: int, y: int) -> list[list[int]]:
        return [[x, y], [x + 80, y], [x + 80, y + 20], [x, y + 20]]

    result = [
        [box(300, 102), "1,234.56", 0.9],
        [box(40, 100), "1 Interest income", 0.9],
        [box(40, 30), "Form 1099-INT", 0.9],
    ]
    assert ocr.lines_from(result) == "Form 1099-INT\n1 Interest income 1,234.56"
    assert ocr.lines_from(None) == ""


def test_scan_and_photo_land_pending_then_accept_with_a_correction(lay: Layout) -> None:
    make_pdf(lay.data / "inbox" / "scan.pdf", [[]])
    photo(lay.data / "inbox" / "photo.png")
    rep = ingest(lay, ocr=fake_ocr)
    assert sorted(i.file_name for i in rep.pending) == ["photo.png", "scan.pdf"]
    assert rep.imported == [] and rep.unmatched == []
    conn = ledger(lay)
    assert db.facts_for(conn, 2025, "1099-DIV") == []  # nothing counts yet
    pend = pending(conn)
    assert {f.box for f in pend} >= {"1a", "1b"} and {f.status for f in pend} == {
        "pending"
    }
    doc = pend[0].document_id
    facts = accept(conn, doc, {"1a": 999.5})
    by = {f.box: f.value for f in facts}
    assert by["1a"] == 999.5 and all(f.status == "accepted" for f in facts)
    assert {f.document_id for f in pending(conn)} == {doc + 1}
    with pytest.raises(KeyError):
        accept(conn, doc, {})
    with pytest.raises(KeyError):
        accept(conn, doc + 1, {"99z": 1.0})
    # the second copy, accepted, supersedes the first like a corrected form
    accept(conn, doc + 1)
    live = db.facts_for(conn, 2025, "1099-DIV")
    assert {f.document_id for f in live} == {doc + 1}


def test_reject_returns_the_file_and_forgets_the_fingerprint(lay: Layout) -> None:
    photo(lay.data / "inbox" / "photo.png")
    ingest(lay, ocr=fake_ocr)
    conn = ledger(lay)
    doc = pending(conn)[0].document_id
    where = reject(lay, conn, doc)
    assert where == "inbox/UNMATCHED/photo.png"
    assert (lay.data / "inbox" / "UNMATCHED" / "photo.png.reason.txt").exists()
    assert pending(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    (lay.data / "inbox" / "UNMATCHED" / "photo.png").rename(
        lay.data / "inbox" / "again.png"
    )
    rep = ingest(lay, ocr=fake_ocr)
    assert [i.file_name for i in rep.pending] == ["again.png"] and rep.duplicates == []


def test_without_an_engine_scans_go_to_unmatched(
    lay: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr, "available", lambda: False)
    make_pdf(lay.data / "inbox" / "scan.pdf", [[]])
    photo(lay.data / "inbox" / "photo.png")
    rep = ingest(lay)
    reasons = dict(rep.unmatched)
    assert (
        "not installed" in reasons["scan.pdf"]
        and "not installed" in reasons["photo.png"]
    )


def test_cli_confirm_list_set_accept_reject(lay: Layout) -> None:
    photo(lay.data / "inbox" / "a.png")
    photo(lay.data / "inbox" / "b.png", width=50)
    r = runner.invoke(app, ["confirm"])
    assert r.output.strip() == "nothing awaiting confirm"
    ingest(lay, ocr=fake_ocr)
    r = runner.invoke(app, ["confirm"])
    assert "doc 1  a.png  1099-DIV 2025" in r.output and "doc 2  b.png" in r.output
    r = runner.invoke(
        app, ["confirm", "--doc", "1", "--accept", "--set", "1a=1,500.25"]
    )
    assert r.exit_code == 0 and "accepted doc 1" in r.output
    r = runner.invoke(app, ["facts", "--year", "2025", "--form", "1099-DIV"])
    assert "1,500.25" in r.output
    r = runner.invoke(app, ["confirm", "--doc", "2"])
    assert r.exit_code == 2
    r = runner.invoke(app, ["confirm", "--doc", "2", "--reject"])
    assert r.exit_code == 0 and "inbox/UNMATCHED/b.png" in r.output
    r = runner.invoke(app, ["confirm", "--doc", "7", "--accept"])
    assert r.exit_code == 2 and "refused" in r.output


@pytest.mark.skipif(not ocr.available(), reason="OCR engine not installed")
def test_real_engine_reads_a_rendered_1099_int(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1400, 400), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=34)
    for i, line in enumerate(INT_LINES):
        draw.text((40, 30 + i * 80), line, fill="black", font=font)
    path = tmp_path / "int.png"
    img.save(path)
    (text,) = ocr.page_texts(path)
    assert "1099-INT" in text and "1,234.56" in text


def test_a_text_box_read_by_ocr_waits_and_is_corrected_with_text(lay: Layout) -> None:
    from tests.test_forms import F1099R

    make_pdf(lay.data / "inbox" / "scan.pdf", [[]])
    ingest(
        lay, ocr=lambda path: ["\n".join(F1099R[:-1] + ["7 Distribution code(s) 6"])]
    )
    conn = ledger(lay)
    assert db.facts_for(conn, 2025, "1099-R", text=True) == []  # waiting
    shown = {f.box: f for f in pending(conn)}
    assert shown["7"].text == "6" and shown["1"].value == 12000.0
    r = runner.invoke(app, ["confirm"])
    assert "7" in r.output and "6" in r.output
    r = runner.invoke(
        app, ["confirm", "--doc", "1", "--accept", "--set", "7=G", "--set", "1=11,000"]
    )
    assert r.exit_code == 0, r.output
    (code,) = db.facts_for(conn, 2025, "1099-R", text=True)
    assert code.text == "G"
    assert [f.value for f in db.facts_for(conn, 2025, "1099-R") if f.box == "1"] == [
        11000.0
    ]


def mixed_pdf(lay: Layout, name: str = "mixed.pdf") -> Path:
    """Page 1 has a text layer; page 2 is blank, as a scanned page is."""
    return make_pdf(lay.data / "inbox" / name, [DIV_2025, []])


def scan_of_page_two(path: Path) -> list[str]:
    return ["", "\n".join(INT_2025)]


def test_mixed_pdf_ocrs_scanned_pages(lay: Layout) -> None:
    mixed_pdf(lay)
    rep = ingest(lay, ocr=scan_of_page_two)
    assert [i.file_name for i in rep.pending] == ["mixed.pdf"] and rep.imported == []
    assert rep.unmatched == [] and rep.notes == []
    conn = ledger(lay)
    # page 1 came from the text layer and counts; page 2 waits for confirm
    assert {f.box for f in db.facts_for(conn, 2025, "1099-DIV")} >= {"1a", "1b"}
    assert db.facts_for(conn, 2025, "1099-INT") == []
    waiting = pending(conn)
    assert {(f.form, f.page) for f in waiting} == {("1099-INT", 2)}
    accept(conn, waiting[0].document_id)
    assert db.facts_for(conn, 2025, "1099-INT")
    assert db.facts_for(conn, 2025, "1099-DIV")  # accepting did not retire page 1


def test_mixed_pdf_without_an_engine_imports_text_pages_and_says_so(
    lay: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr, "available", lambda: False)
    mixed_pdf(lay)
    rep = ingest(lay)
    assert [i.file_name for i in rep.imported] == ["mixed.pdf"]
    ((name, note),) = rep.notes
    assert name == "mixed.pdf" and "page 2" in note and "not installed" in note


def test_mixed_pdf_without_an_engine_keeps_the_no_template_reason(
    lay: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr, "available", lambda: False)
    old_year = [line.replace("2025", "2019") for line in DIV_2025]
    make_pdf(lay.data / "inbox" / "old.pdf", [old_year, []])
    rep = ingest(lay)
    assert rep.imported == []
    ((name, why),) = rep.unmatched
    assert name == "old.pdf"
    assert "page 1: 1099-DIV 2019 has no template" in why
    assert "page 2 has no text layer" in why


def test_mixed_pdf_whose_scan_reads_nothing_is_noted(lay: Layout) -> None:
    mixed_pdf(lay)
    rep = ingest(lay, ocr=lambda path: ["", ""])
    assert [i.file_name for i in rep.imported] == ["mixed.pdf"]
    ((_, note),) = rep.notes
    assert "page 2" in note and "read nothing" in note


def test_mixed_pdf_scanned_page_disagreeing_with_the_text_page_is_unmatched(
    lay: Layout,
) -> None:
    mixed_pdf(lay)
    other = [line.replace("9,800.00", "9,900.00") for line in DIV_2025]
    rep = ingest(lay, ocr=lambda path: ["", "\n".join(other)])
    ((_, why),) = rep.unmatched
    assert "page 1 says 9800.0 but page 2 says 9900.0" in why


def test_mixed_pdf_scanned_page_that_matches_no_form_is_unmatched_with_why(
    lay: Layout,
) -> None:
    make_pdf(lay.data / "inbox" / "cover.pdf", [["Cover letter, no form"], []])
    rep = ingest(lay, ocr=lambda path: ["", "page two scan"])
    ((_, why),) = rep.unmatched
    assert "no form template matched" in why


def test_mixed_pdf_matching_no_form_says_ocr_read_nothing(lay: Layout) -> None:
    make_pdf(lay.data / "inbox" / "cover.pdf", [["Cover letter, no form"], []])
    rep = ingest(lay, ocr=lambda path: ["", ""])
    ((_, why),) = rep.unmatched
    assert "no form template matched" in why
    assert "page 2" in why and "OCR read nothing" in why


def test_engine_reads_only_the_scanned_pages(
    lay: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    read: list[object] = []

    def fake_read(image: object) -> str:
        read.append(image)
        return "\n".join(INT_2025)

    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "_read", fake_read)
    make_pdf(lay.data / "inbox" / "three.pdf", [DIV_2025, [], DIV_2025])
    rep = ingest(lay, ocr=ocr.page_texts)
    assert [i.file_name for i in rep.pending] == ["three.pdf"]
    assert len(read) == 1  # the two text pages were not rendered or read
    assert {(f.form, f.page) for f in pending(ledger(lay))} == {("1099-INT", 2)}


def test_reject_on_a_mixed_pdf_keeps_the_text_page_values(lay: Layout) -> None:
    """A mixed PDF holds accepted text-page facts beside its pending scan facts;
    rejecting the scan must not take the text pages (or the document) with it."""
    make_pdf(lay.data / "inbox" / "first.pdf", [DIV_2025])
    ingest(lay)
    corrected = [line.replace("9,800.00", "9,900.00") for line in DIV_2025]
    make_pdf(lay.data / "inbox" / "mixed.pdf", [corrected, []])
    ingest(lay, ocr=scan_of_page_two)
    conn = ledger(lay)
    before = {f.box: f.value for f in db.facts_for(conn, 2025, "1099-DIV")}
    assert before["1a"] == 9900.0  # the corrected page replaced the first copy
    ((doc,),) = {(f.document_id,) for f in pending(conn)}
    where = reject(lay, conn, doc)
    assert pending(conn) == []
    after = {f.box: f.value for f in db.facts_for(conn, 2025, "1099-DIV")}
    assert after == before  # the text-page values still count
    assert db.facts_for(conn, 2025, "1099-INT") == []  # the rejected scan does not
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    assert (lay.data / where).is_file() and where.startswith("archive/")
    assert not (lay.data / "inbox" / "UNMATCHED" / "mixed.pdf").exists()
    rep = ingest(lay, ocr=scan_of_page_two)  # same file again: still a duplicate
    assert rep.pending == [] and rep.imported == []


def test_spots_name_the_page_and_line_each_ocr_value_came_from() -> None:
    from planner.ingest.pdf import load_templates, parse_texts
    from tests.test_ingest import TEMPLATES

    (form,) = parse_texts(["\n".join(DIV_2025)], load_templates(TEMPLATES), ocr=True)
    at = {box: DIV_2025[line].split()[0] for box, (_, line) in form.spots.items()}
    assert form.spots["1a"] == (1, 3) and form.spots["1b"] == (1, 4)
    assert at == {box: box for box in form.spots}  # each line starts with its box
    (typed,) = parse_texts(["\n".join(DIV_2025)], load_templates(TEMPLATES))
    assert typed.spots == {}  # a text layer needs no crop


def test_crop_written_per_value(lay: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine(monkeypatch, DIV_2025)
    mark = DIV_2025.index(next(x for x in DIV_2025 if x.startswith("1a ")))
    ruled_photo(lay.data / "inbox" / "photo.png", len(DIV_2025), mark)
    rep = ingest(lay)
    assert [i.file_name for i in rep.pending] == ["photo.png"] and rep.notes == []
    conn = ledger(lay)
    pend = pending(conn)
    assert {f.box for f in pend} >= {"1a", "1b", "2a"}
    for f in pend:  # a PNG under data/ for every box waiting
        png = ocr.crop_path(lay, f.document_id, f.id)
        assert png.is_file() and png.is_relative_to(lay.data)
        assert png.read_bytes().startswith(b"\x89PNG")
    by = {f.box: ocr.crop_path(lay, f.document_id, f.id) for f in pend}
    assert dark(by["1a"]) and not dark(by["1b"]) and not dark(by["2a"])
    from PIL import Image

    with Image.open(by["1a"]) as im:
        assert im.size == (430, 60)  # the line, a line's margin on each side


def test_crops_of_a_scanned_pdf_come_from_the_page_the_value_was_read_on(
    lay: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_engine(monkeypatch, INT_2025)
    make_pdf(lay.data / "inbox" / "mixed.pdf", [DIV_2025, []])
    ingest(lay)
    conn = ledger(lay)
    waiting = pending(conn)
    assert {f.form for f in waiting} == {"1099-INT"}  # page 1 came from its text
    for f in waiting:
        png = ocr.crop_path(lay, f.document_id, f.id)
        assert png.is_file()
    assert sorted(p.name for p in (lay.data / "crops").rglob("*.png")) == sorted(
        f"{f.id}.png" for f in waiting
    )


def test_text_only_readers_make_no_crops_and_no_fuss(lay: Layout) -> None:
    photo(lay.data / "inbox" / "photo.png")
    rep = ingest(lay, ocr=fake_ocr)
    assert rep.notes == [] and not (lay.data / "crops").exists()


def test_a_value_the_engine_cannot_place_is_named_in_the_report(
    lay: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_engine(monkeypatch, DIV_2025)
    ruled_photo(lay.data / "inbox" / "photo.png", len(DIV_2025))
    monkeypatch.setattr(ocr.ScannedPages, "crops", lambda self, spots: {})
    rep = ingest(lay)
    ((name, note),) = rep.notes
    assert name == "photo.png" and "no image crop" in note
    assert pending(ledger(lay))  # still waiting, just without a picture


def test_reject_forgets_the_crops(lay: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine(monkeypatch, DIV_2025)
    ruled_photo(lay.data / "inbox" / "photo.png", len(DIV_2025))
    ingest(lay)
    conn = ledger(lay)
    doc = pending(conn)[0].document_id
    assert list(ocr.crop_dir(lay, doc).glob("*.png"))
    reject(lay, conn, doc)
    assert not ocr.crop_dir(lay, doc).exists()


def test_a_correction_cannot_name_a_box_two_forms_share(
    lay: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_engine(monkeypatch, [*INT_2025, *DIV_2025])
    ruled_photo(lay.data / "inbox" / "both.png", len(INT_2025) + len(DIV_2025))
    ingest(lay)
    conn = ledger(lay)
    doc = pending(conn)[0].document_id
    by_form = {
        form: {f.box for f in pending(conn) if f.form == form}
        for form in ("1099-INT", "1099-DIV")
    }
    assert by_form["1099-INT"] & by_form["1099-DIV"] == {"4"}  # both read line 4
    with pytest.raises(ValueError, match="cannot say which"):
        accept(conn, doc, {"4": 1.0})
    assert len(pending(conn)) > 0  # nothing was taken in
    accept(conn, doc, {"1a": 5.0})
    assert [f.value for f in db.facts_for(conn, 2025, "1099-DIV") if f.box == "1a"] == [
        5.0
    ]


@pytest.mark.skipif(not ocr.available(), reason="OCR engine not installed")
def test_real_engine_crop_shows_the_line_it_was_cut_for(tmp_path: Path) -> None:
    from io import BytesIO

    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1400, 400), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=34)
    for i, line in enumerate(INT_LINES):
        draw.text((40, 30 + i * 80), line, fill="black", font=font)
    path = tmp_path / "int.png"
    img.save(path)
    pages = ocr.page_texts(path)
    assert isinstance(pages, ocr.ScannedPages)
    at = next(n for n, t in enumerate(pages[0].split("\n")) if "1,234.56" in t)
    png = pages.crops([(1, at)])[(1, at)]
    crop_path = tmp_path / "crop.png"
    crop_path.write_bytes(png)
    with Image.open(BytesIO(png)) as crop:
        assert 0 < crop.height < 400 and crop.width <= 1400
    (text,) = ocr.page_texts(crop_path)
    assert "1,234.56" in text and "Interest" in text
    assert "withheld" not in text  # the line, not the whole page
