"""Phase 5b: planner run's local server. Token and host checks, and every
action posted from the page (drop, enter, don't-have, confirm, tax pack)
lands in the ledger and shows on the next render without a restart."""

from __future__ import annotations

import html
import http.client
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlencode

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.dashboard import serve
from planner.ingest import ingest, ocr
from planner.ingest.confirm import pending
from planner.ledger import db
from planner.paths import Layout
from tests.pdfgen import make_pdf
from tests.test_forms import SSA
from tests.test_ingest import DIV_2025, INT_2025
from tests.test_ocr import fake_engine, ruled_photo
from tests.test_spending import AS_OF

runner = CliRunner()
BOUNDARY = "plannerTestBoundary"


@pytest.fixture
def live(planner_home: Path) -> Iterator[tuple[serve.App, int]]:
    lay = Layout(planner_home)
    lay.ensure()
    a = serve.App(lay, 2026, AS_OF, token="tok")  # noqa: S106
    srv = serve.server(a)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield a, srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def call(
    port: int,
    method: str,
    path: str,
    body: bytes = b"",
    ctype: str = "application/x-www-form-urlencoded",
    host: str | None = None,
) -> tuple[int, dict[str, str], str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    headers = {"Content-Type": ctype}
    if host:
        headers["Host"] = host
    conn.request(method, path, body=body, headers=headers)
    r = conn.getresponse()
    text = r.read().decode("utf-8")
    conn.close()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, text


def post(port: int, route: str, **fields: str) -> tuple[int, dict[str, str], str]:
    return call(port, "POST", f"{route}?token=tok", urlencode(fields).encode())


def multipart(files: list[tuple[str, bytes]]) -> bytes:
    out = b""
    for name, data in files:
        out += (
            (
                f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
            + data
            + b"\r\n"
        )
    return out + f"--{BOUNDARY}--\r\n".encode()


@pytest.mark.engine
def test_token_and_host_are_required(live: tuple[serve.App, int]) -> None:
    _, port = live
    assert call(port, "GET", "/")[0] == 403
    assert call(port, "GET", "/?token=wrong")[0] == 403
    assert call(port, "GET", "/?token=tok", host=f"evil.example:{port}")[0] == 403
    assert call(port, "POST", "/enter?token=nope", b"key=state&value=NC")[0] == 403
    assert call(port, "POST", "/nowhere?token=tok")[0] == 403
    status, headers, text = call(port, "GET", "/?token=tok")
    assert status == 200 and "Needed" in text
    assert "default-src 'none'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store"
    assert 'action="/upload?token=tok"' in text


@pytest.mark.engine
def test_enter_and_dont_have_shrink_the_needed_panel(
    live: tuple[serve.App, int],
) -> None:
    a, port = live
    before = call(port, "GET", "/?token=tok")[2]
    assert 'name="key" value="state"' in before
    status, headers, _ = post(port, "/enter", key="state", value="NC")
    assert status == 303 and headers["location"] == "/?token=tok"
    after = call(port, "GET", "/?token=tok")[2]
    assert "saved state = NC" in after and 'name="key" value="state"' not in after
    # the message shows once
    assert "saved state = NC" not in call(port, "GET", "/?token=tok")[2]
    post(port, "/enter", key="birth_date", value="not a date")
    assert "not saved" in a.message
    post(port, "/dont-have", key="ss_estimate_62")
    assert a.message == "marked ss_estimate_62: don't have"
    post(port, "/dont-have", key="no_such_key")
    assert a.message.startswith("not saved")


@pytest.mark.engine
def test_dropped_file_is_read_and_the_panel_shrinks(
    live: tuple[serve.App, int], tmp_path: Path
) -> None:
    a, port = live
    assert 'name="key" value="ss_estimate_67"' in call(port, "GET", "/?token=tok")[2]
    pdf = make_pdf(tmp_path / "ssa.pdf", [SSA]).read_bytes()
    status, _, _ = call(
        port,
        "POST",
        "/upload?token=tok",
        multipart([("..\\..\\my ssa<1>.pdf", pdf), ("empty.csv", b"")]),
        f"multipart/form-data; boundary={BOUNDARY}",
    )
    assert status == 303
    assert a.message.startswith("1 file(s) received: 1 imported")
    page = call(port, "GET", "/?token=tok")[2]
    assert 'name="key" value="ss_estimate_67"' not in page
    # the name was cleaned to the inbox (no path), then archived by the intake
    assert not list((a.lay.data / "inbox").glob("*.pdf"))
    assert not (a.lay.root.parent / "my ssa_1_.pdf").exists()
    # the same file again is a duplicate, not a second import
    call(
        port,
        "POST",
        "/upload?token=tok",
        multipart([("ssa.pdf", pdf)]),
        f"multipart/form-data; boundary={BOUNDARY}",
    )
    assert "1 already in the ledger" in a.message


@pytest.mark.engine
def test_confirm_and_taxpack_report_what_blocks_them(
    live: tuple[serve.App, int],
) -> None:
    a, port = live
    post(port, "/confirm", doc="99", action="accept")
    assert a.message.startswith("not done") and "99" in a.message
    post(port, "/confirm", doc="x", action="accept")
    assert a.message.startswith("not done")
    post(port, "/taxpack")
    assert a.message.startswith("tax package not built: ")
    assert not (a.lay.out / "tax-2026").exists()


def scanned(a: serve.App, monkeypatch: pytest.MonkeyPatch) -> int:
    """Drop a photo of a 1099-DIV through the fake engine; return its document."""
    fake_engine(monkeypatch, DIV_2025)
    ruled_photo(a.lay.data / "inbox" / "photo.png", len(DIV_2025))
    ingest(a.lay)
    conn = db.connect(a.lay.data / "ledger" / "planner.db")
    try:
        return pending(conn)[0].document_id
    finally:
        conn.close()


def box_values(a: serve.App) -> dict[str, tuple[str, float | str]]:
    conn = db.connect(a.lay.data / "ledger" / "planner.db")
    try:
        return {
            f.box: (f.status, f.value if f.text is None else f.text)
            for f in db.facts_for(conn, status="pending", text=None)
            + db.facts_for(conn, text=None)
        }
    finally:
        conn.close()


@pytest.mark.engine
def test_confirm_with_edit_from_page(
    live: tuple[serve.App, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, port = live
    doc = scanned(a, monkeypatch)
    page = call(port, "GET", "/?token=tok")[2]
    assert 'name="edit_1a" value="9,800.00"' in page  # prefilled with what OCR read
    assert page.count('<img src="/crop?token=tok&amp;fact=') == len(box_values(a))
    status, _, _ = post(
        port, "/confirm", doc=str(doc), action="accept", edit_1a="9,750.25", edit_1b=""
    )
    assert status == 303
    assert a.message == f"accepted 4 value(s) from document {doc}, 1 corrected"
    got = box_values(a)
    assert got["1a"] == ("accepted", 9750.25) and got["1b"] == ("accepted", 8100.0)
    assert "pending" not in {s for s, _ in got.values()}
    assert 'name="edit_1a"' not in a.html()  # nothing left awaiting confirm


def page_form(page: str) -> dict[str, str]:
    """What a browser would post for the page's confirm form: every named
    text input with the value the page put in it."""
    found = re.findall(r'<input type="text" name="(edit_[^"]+)" value="([^"]*)"', page)
    assert len({n for n, _ in found}) == len(found)  # no field name twice
    return {n: html.unescape(v) for n, v in found}


@pytest.mark.engine
def test_a_scan_of_two_forms_sharing_a_box_accepts_from_the_unmodified_page(
    live: tuple[serve.App, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, port = live
    fake_engine(monkeypatch, [*INT_2025, *DIV_2025])
    ruled_photo(a.lay.data / "inbox" / "both.png", len(INT_2025) + len(DIV_2025))
    ingest(a.lay)
    conn = db.connect(a.lay.data / "ledger" / "planner.db")
    try:
        rows = pending(conn)
        doc = rows[0].document_id
        by_form = {
            form: {f.box for f in rows if f.form == form}
            for form in ("1099-INT", "1099-DIV")
        }
    finally:
        conn.close()
    assert by_form["1099-INT"] & by_form["1099-DIV"] == {"4"}
    fields = page_form(call(port, "GET", "/?token=tok")[2])
    assert (
        "edit_4" not in fields
    )  # a shared box cannot be retyped, so it is not a field
    assert "edit_1a" in fields
    post(port, "/confirm", doc=str(doc), action="accept", **fields)
    assert a.message == f"accepted {len(rows)} value(s) from document {doc}"
    assert "pending" not in {s for s, _ in box_values(a).values()}


@pytest.mark.engine
def test_a_scan_of_two_forms_sharing_a_box_still_takes_a_correction_elsewhere(
    live: tuple[serve.App, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, port = live
    fake_engine(monkeypatch, [*INT_2025, *DIV_2025])
    ruled_photo(a.lay.data / "inbox" / "both.png", len(INT_2025) + len(DIV_2025))
    ingest(a.lay)
    conn = db.connect(a.lay.data / "ledger" / "planner.db")
    try:
        doc = pending(conn)[0].document_id
    finally:
        conn.close()
    fields = page_form(call(port, "GET", "/?token=tok")[2])
    fields["edit_1a"] = "5.00"
    post(port, "/confirm", doc=str(doc), action="accept", **fields)
    assert a.message.endswith(", 1 corrected")
    conn = db.connect(a.lay.data / "ledger" / "planner.db")
    try:
        assert [
            f.value for f in db.facts_for(conn, 2025, "1099-DIV") if f.box == "1a"
        ] == [5.0]
    finally:
        conn.close()


@pytest.mark.engine
def test_two_payers_of_one_form_keep_their_own_values_from_the_unmodified_page(
    live: tuple[serve.App, int],
) -> None:
    a, port = live
    other = [
        "PAYER'S name: Other Bank (synthetic)" if ln.startswith("PAYER'S") else ln
        for ln in INT_2025
    ]
    other[3] = "1 Interest income $ 500.00"
    make_pdf(a.lay.data / "inbox" / "banks.pdf", [[], []])  # two scanned pages
    ingest(a.lay, ocr=lambda path: ["\n".join(INT_2025), "\n".join(other)])
    conn = db.connect(a.lay.data / "ledger" / "planner.db")
    try:
        rows = pending(conn)
        doc = rows[0].document_id
    finally:
        conn.close()
    assert {f.issuer for f in rows} == {
        "Example Bank (synthetic)",
        "Other Bank (synthetic)",
    }
    fields = page_form(call(port, "GET", "/?token=tok")[2])
    assert "edit_1" not in fields  # box 1 is two payers' figures: not one field
    post(port, "/confirm", doc=str(doc), action="accept", **fields)
    assert a.message == f"accepted {len(rows)} value(s) from document {doc}"
    conn = db.connect(a.lay.data / "ledger" / "planner.db")
    try:
        got = {
            f.issuer: f.value
            for f in db.facts_for(conn, 2025, "1099-INT")
            if f.box == "1"
        }
    finally:
        conn.close()
    assert got == {"Example Bank (synthetic)": 1234.56, "Other Bank (synthetic)": 500.0}


@pytest.mark.engine
def test_a_correction_for_a_box_two_payers_share_is_refused(
    live: tuple[serve.App, int],
) -> None:
    a, port = live
    other = [
        "PAYER'S name: Other Bank (synthetic)" if ln.startswith("PAYER'S") else ln
        for ln in INT_2025
    ]
    other[3] = "1 Interest income $ 500.00"
    make_pdf(a.lay.data / "inbox" / "banks.pdf", [[], []])  # two scanned pages
    ingest(a.lay, ocr=lambda path: ["\n".join(INT_2025), "\n".join(other)])
    conn = db.connect(a.lay.data / "ledger" / "planner.db")
    try:
        doc = pending(conn)[0].document_id
    finally:
        conn.close()
    post(port, "/confirm", doc=str(doc), action="accept", edit_1="1,234.56")
    assert a.message.startswith("not done") and "cannot say which one" in a.message
    assert {s for s, _ in box_values(a).values()} == {"pending"}


@pytest.mark.engine
def test_an_unchanged_page_posts_back_as_no_corrections(
    live: tuple[serve.App, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, port = live
    doc = scanned(a, monkeypatch)
    post(port, "/confirm", doc=str(doc), action="accept", edit_1a="9,800.00")
    assert a.message == f"accepted 4 value(s) from document {doc}"


@pytest.mark.engine
def test_a_bad_edit_takes_nothing_in(
    live: tuple[serve.App, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, port = live
    doc = scanned(a, monkeypatch)
    post(port, "/confirm", doc=str(doc), action="accept", edit_1a="lots", edit_1b="5")
    assert a.message.startswith("not done: box 1a: a dollar amount")
    assert {s for s, _ in box_values(a).values()} == {"pending"}
    post(port, "/confirm", doc=str(doc), action="accept", edit_9z="5")
    assert a.message.startswith("not done") and "9z" in a.message
    assert {s for s, _ in box_values(a).values()} == {"pending"}


@pytest.mark.engine
def test_reject_ignores_typed_edits(
    live: tuple[serve.App, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, port = live
    doc = scanned(a, monkeypatch)
    post(port, "/confirm", doc=str(doc), action="reject", edit_1a="1")
    assert a.message.startswith(f"rejected document {doc}")
    assert box_values(a) == {}
    assert not ocr.crop_dir(a.lay, doc).exists()


@pytest.mark.engine
def test_crop_is_served_only_with_the_token_and_only_while_pending(
    live: tuple[serve.App, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, port = live
    doc = scanned(a, monkeypatch)
    conn = db.connect(a.lay.data / "ledger" / "planner.db")
    fact = pending(conn)[0].id
    conn.close()
    path = f"/crop?fact={fact}"
    assert call(port, "GET", path)[0] == 403
    assert call(port, "GET", f"{path}&token=wrong")[0] == 403
    assert call(port, "GET", f"{path}&token=tok", host=f"evil.example:{port}")[0] == 403
    conn2 = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn2.request("GET", f"{path}&token=tok")
    r = conn2.getresponse()
    body = r.read()
    headers = {k.lower(): v for k, v in r.getheaders()}
    conn2.close()
    assert r.status == 200 and headers["content-type"] == "image/png"
    assert body.startswith(b"\x89PNG") and headers["cache-control"] == "no-store"
    assert "img-src 'self'" in headers["content-security-policy"]
    for bad in ("9999", "x", "../../ledger/planner.db", "-1", ""):
        assert call(port, "GET", f"/crop?token=tok&fact={bad}")[0] == 404
    assert call(port, "GET", "/crop?token=tok")[0] == 404
    post(port, "/confirm", doc=str(doc), action="accept")
    assert call(port, "GET", f"{path}&token=tok")[0] == 404  # accepted: no longer shown


@pytest.mark.engine
def test_cli_run_quiet_reads_the_inbox_and_writes_the_page(planner_home: Path) -> None:
    lay = Layout(planner_home)
    lay.ensure()
    make_pdf(lay.data / "inbox" / "ssa.pdf", [SSA])
    r = runner.invoke(
        app, ["run", "--year", "2026", "--as-of", "2026-07-10", "--quiet"]
    )
    assert r.exit_code == 0, r.output
    assert "inbox: 1 imported" in r.output and "serving" not in r.output
    assert (lay.out / "index.html").exists()
