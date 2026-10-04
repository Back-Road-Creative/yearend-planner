"""``planner run``'s page, served live on this computer only.

The server listens on 127.0.0.1 on a port the system picks, and every request
must carry the per-run token that ``planner run`` puts in the address it
opens; a request without it, or addressed to another host name, is refused.
One request at a time (the ledger is a single SQLite file). Each action
(a dropped file, a typed answer, a don't-have, a confirm, the tax pack) runs,
leaves a one-line result on the page, and redirects back to it, so the page
always shows the ledger as it is now."""

from __future__ import annotations

import email.parser
import email.policy
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from planner.dashboard import page, render
from planner.engine.household import MissingInputError
from planner.ingest import confirm, ingest, needs
from planner.ledger import db
from planner.paths import Layout
from planner.plan import rollover
from planner.taxprep import package

HOST = "127.0.0.1"
MAX_BODY = 200 * 1024 * 1024  # one drop of documents
SAFE_NAME = re.compile(r"[^A-Za-z0-9._ ()-]+")


@dataclass
class App:
    lay: Layout
    year: int
    as_of: date | None = None
    token: str = ""
    message: str = ""

    def __post_init__(self) -> None:
        self.token = self.token or secrets.token_urlsafe(24)

    def html(self) -> str:
        msg, self.message = self.message, ""
        pg = page.gather(self.lay, self.year, self.as_of)
        return render.html(pg, self.token, msg)

    def upload(self, files: list[tuple[str, bytes]]) -> str:
        inbox = self.lay.data / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        saved = 0
        for name, data in files:
            clean = SAFE_NAME.sub("_", Path(name.replace("\\", "/")).name).strip(" .")
            if not clean or not data:
                continue
            target = inbox / clean
            n = 1
            while target.exists():
                target = inbox / f"{Path(clean).stem} ({n}){Path(clean).suffix}"
                n += 1
            target.write_bytes(data)
            saved += 1
        if not saved:
            return "no file received"
        rep = ingest(self.lay)
        return (
            f"{saved} file(s) received: {len(rep.imported)} imported, "
            f"{len(rep.pending)} awaiting confirm, {len(rep.duplicates)} already "
            f"in the ledger, {len(rep.unmatched)} not read"
            + "".join(f"\n{name}: {why}" for name, why in rep.unmatched)
            + "".join(f"\n{name}: {note}" for name, note in rep.notes)
        )

    def enter(self, key: str, value: str) -> str:
        try:
            stored = needs.enter(self.lay, self.year, key, value)
        except (KeyError, ValueError) as exc:
            return f"not saved: {exc}"
        return f"saved {key} = {stored}"

    def dont_have(self, key: str) -> str:
        try:
            needs.dont_have(self.lay, self.year, key)
        except KeyError as exc:
            return f"not saved: {exc}"
        return f"marked {key}: don't have"

    def confirm(self, doc: str, action: str) -> str:
        conn = db.connect(self.lay.data / "ledger" / "planner.db")
        try:
            if action == "accept":
                facts = confirm.accept(conn, int(doc))
                return f"accepted {len(facts)} value(s) from document {doc}"
            if action == "reject":
                where = confirm.reject(self.lay, conn, int(doc))
                return f"rejected document {doc}; the file is at {where}"
            return f"unknown action {action!r}"
        except (KeyError, ValueError) as exc:
            return f"not done: {exc}"
        finally:
            conn.close()

    def rollover(self) -> str:
        today = self.as_of or date.today()
        due = rollover.due(self.lay, today)
        if due is None:
            return "nothing to roll over"
        ro = rollover.roll(self.lay, due, today)
        self.year = rollover.active_year(self.lay, today)
        return rollover.render(ro)

    def taxpack(self) -> str:
        try:
            pack = package.build(self.lay, self.year, self.as_of)
        except MissingInputError as exc:
            return f"tax package not built: {exc}"
        return f"tax package written to {pack.folder} ({len(pack.written)} files)"


def _form(body: bytes) -> dict[str, str]:
    q = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {k: v[0] for k, v in q.items()}


def _files(content_type: str, body: bytes) -> list[tuple[str, bytes]]:
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + body)
    out: list[tuple[str, bytes]] = []
    for part in msg.iter_parts():
        name = part.get_filename()
        data = part.get_payload(decode=True)
        if name and isinstance(data, bytes):
            out.append((name, data))
    return out


def handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "planner"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return  # the address carries the token; keep it out of the console

        def _allowed(self) -> bool:
            addr = self.server.server_address
            port = addr[1] if isinstance(addr, tuple) else 0
            host = self.headers.get("Host", "")
            if host not in (f"{HOST}:{port}", f"localhost:{port}"):
                return False
            got = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
            return secrets.compare_digest(got, app.token)

        def _send(self, status: HTTPStatus, body: str, ctype: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'; "
                "form-action 'self'; img-src 'self'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def _refuse(self) -> None:
            self._send(HTTPStatus.FORBIDDEN, "forbidden\n", "text/plain")

        def do_GET(self) -> None:  # noqa: N802
            if urlsplit(self.path).path != "/" or not self._allowed():
                self._refuse()
                return
            self._send(HTTPStatus.OK, app.html(), "text/html; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            route = urlsplit(self.path).path
            if not self._allowed() or route not in ROUTES:
                self._refuse()
                return
            size = int(self.headers.get("Content-Length") or 0)
            if size > MAX_BODY:
                self._send(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too large\n", "text/plain"
                )
                return
            body = self.rfile.read(size)
            app.message = ROUTES[route](app, self.headers.get("Content-Type", ""), body)
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", f"/?token={app.token}")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


ROUTES: dict[str, Callable[[App, str, bytes], str]] = {
    "/upload": lambda a, ct, b: a.upload(_files(ct, b)),
    "/enter": lambda a, ct, b: a.enter(
        _form(b).get("key", ""), _form(b).get("value", "")
    ),
    "/dont-have": lambda a, ct, b: a.dont_have(_form(b).get("key", "")),
    "/confirm": lambda a, ct, b: a.confirm(
        _form(b).get("doc", ""), _form(b).get("action", "")
    ),
    "/taxpack": lambda a, ct, b: a.taxpack(),
    "/rollover": lambda a, ct, b: a.rollover(),
}


def server(app: App, port: int = 0) -> HTTPServer:
    return HTTPServer((HOST, port), handler(app))


def url(app: App, srv: HTTPServer) -> str:
    return f"http://{HOST}:{srv.server_address[1]}/?token={app.token}"
