"""Phase 5b: planner run's local server. Token and host checks, and every
action posted from the page (drop, enter, don't-have, confirm, tax pack)
lands in the ledger and shows on the next render without a restart."""

from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlencode

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.dashboard import serve
from planner.paths import Layout
from tests.pdfgen import make_pdf
from tests.test_forms import SSA
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
