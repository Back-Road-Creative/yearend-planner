"""``planner run``'s page, served live on this computer only.

The server listens on 127.0.0.1 on a port the system picks, and every request
must carry the per-run token that ``planner run`` puts in the address it
opens; a request without it, or addressed to another host name, is refused.
One request at a time (the ledger is a single SQLite file). Each action
(a dropped file, a typed answer, a don't-have and its undo, a waived form,
a categorised bank row, a confirm, the tax pack) runs (a filed return
dropped or confirmed closes its year),
leaves a one-line result on the page, and redirects back to it, so the page
always shows the ledger as it is now."""

from __future__ import annotations

import email.parser
import email.policy
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from planner.dashboard import page, render
from planner.engine.household import MissingInputError
from planner.ingest import confirm, ingest, needs, ocr
from planner.ledger import db
from planner.paths import Layout
from planner.plan import rollover
from planner.taxprep import close, expected, package, schedule_c

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
            + self._close_filed()
        )

    def _close_filed(self) -> str:
        """Close any ended year whose filed return just arrived."""
        done = close.on_drop(self.lay, self.as_of or date.today())
        return "".join("\n" + close.render(c).rstrip() for c in done)

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

    def undo_dont_have(self, key: str) -> str:
        try:
            marked = needs.undo_dont_have(self.lay, self.year, key)
        except KeyError as exc:
            return f"not saved: {exc}"
        if not marked:
            return f"{key} was not marked don't have"
        return f"{key} is back on the Needed list"

    def waive(self, form: str, issuer: str, undo: bool = False) -> str:
        if undo:
            if expected.unwaive(self.lay, self.year, form, issuer):
                return f"{form} from {issuer} is back on the Needed list"
            return f"{form} from {issuer} was not waived"
        try:
            expected.waive(self.lay, self.year, form, issuer, self.as_of)
        except ValueError as exc:
            return f"not saved: {exc}"
        return f"waived {form} from {issuer}: it will not come"

    def categorize(self, row: str, rule: str, category: str) -> str:
        """Give one bank row (``row`` is its key) or every row whose
        description holds ``rule`` a Schedule C category, then rebuild the
        schedule so the Needed panel sees it. Never a guess: the category is
        the one typed."""
        if bool(row) == bool(rule.strip()):
            return "not saved: name one bank row or one piece of description text"
        conn = db.connect(self.lay.data / "ledger" / "planner.db")
        try:
            try:
                if row:
                    keys = {
                        r.row_key for r in db.rows_for(conn, self.year, kind="bank")
                    }
                    if row not in keys:
                        return f"not saved: no bank row {row} in {self.year}"
                    schedule_c.assign(self.lay, row, category)
                else:
                    schedule_c.add_rule(self.lay, rule, category)
            except ValueError as exc:
                return f"not saved: {exc}"
            sc = schedule_c.store(conn, self.lay, self.year)
        finally:
            conn.close()
        if row:
            return f"{row} -> {category}; {len(sc.uncategorised)} row(s) left"
        caught = sum(
            1
            for r in sc.rows.get(category, [])
            if rule.strip().lower() in f"{r.type} {r.description}".lower()
        )
        return (
            f"rule {rule.strip()!r} -> {category}: {caught} row(s) this year; "
            f"{len(sc.uncategorised)} left"
        )

    def crop(self, fact: str) -> bytes | None:
        """The scan crop saved for one value still awaiting confirm, or None.
        The id is looked up in the ledger and the file name built from it, so
        nothing the request carries is ever part of a path."""
        if not fact.isdecimal() or len(fact) > 18:
            return None
        conn = db.connect(self.lay.data / "ledger" / "planner.db")
        try:
            row = conn.execute(
                "SELECT document_id FROM facts WHERE id = ? AND status = 'pending'",
                (int(fact),),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        path = ocr.crop_path(self.lay, int(row["document_id"]), int(fact))
        return path.read_bytes() if path.is_file() else None

    def confirm(
        self, doc: str, action: str, edits: dict[str, str] | None = None
    ) -> str:
        conn = db.connect(self.lay.data / "ledger" / "planner.db")
        try:
            if action == "accept":
                typed: dict[str, float | str] = {
                    b: v for b, v in (edits or {}).items() if v.strip()
                }
                changed = _changed(conn, int(doc), typed)
                facts = confirm.accept(conn, int(doc), {b: typed[b] for b in changed})
                fixed = f", {len(changed)} corrected" if changed else ""
                conn.close()  # the closing reads the ledger afresh
                return (
                    f"accepted {len(facts)} value(s) from document {doc}{fixed}"
                    + self._close_filed()
                )
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


def _changed(
    conn: sqlite3.Connection, doc: int, typed: Mapping[str, float | str]
) -> list[str]:
    """The typed boxes whose text differs from what OCR read, so a page posted
    with every field prefilled reports only the real corrections."""
    rows = [f for f in confirm.pending(conn) if f.document_id == doc]
    now = {f.box: f for f in rows}
    shared = confirm.shared_boxes(rows)
    out: list[str] = []
    for box, raw in typed.items():
        f = now.get(box)
        if f is None or box in shared:
            out.append(box)  # unknown or ambiguous box: accept refuses it by name
        elif f.text is not None:
            if " ".join(str(raw).split()) != f.text:
                out.append(box)
        else:
            try:
                same = db.to_cents(confirm.money(box, raw)) == db.to_cents(f.value)
            except ValueError:
                same = False  # accept will refuse it with the reason
            if not same:
                out.append(box)
    return out


def _form(body: bytes) -> dict[str, str]:
    q = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {k: v[0] for k, v in q.items()}


def _edits(form: dict[str, str]) -> dict[str, str]:
    """The corrections typed on the page: ``edit_<box>`` fields, by box."""
    return {k[len("edit_") :]: v for k, v in form.items() if k.startswith("edit_")}


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

        def _send(self, status: HTTPStatus, body: str | bytes, ctype: str) -> None:
            data = body if isinstance(body, bytes) else body.encode("utf-8")
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

        def _drain(self) -> None:
            """Read the request body before answering: a socket closed with
            unread bytes is reset on Windows, and the client sees an abort
            instead of the answer."""
            size = int(self.headers.get("Content-Length") or 0)
            if 0 < size <= MAX_BODY:
                self.rfile.read(size)

        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            if parts.path not in ("/", "/crop") or not self._allowed():
                self._refuse()
                return
            if parts.path == "/crop":
                png = app.crop(parse_qs(parts.query).get("fact", [""])[0])
                if png is None:
                    self._send(HTTPStatus.NOT_FOUND, "no such image\n", "text/plain")
                else:
                    self._send(HTTPStatus.OK, png, "image/png")
                return
            self._send(HTTPStatus.OK, app.html(), "text/html; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            route = urlsplit(self.path).path
            if not self._allowed() or route not in ROUTES:
                self._drain()
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
    "/undo-dont-have": lambda a, ct, b: a.undo_dont_have(_form(b).get("key", "")),
    "/waive": lambda a, ct, b: a.waive(
        _form(b).get("form", ""), _form(b).get("issuer", "")
    ),
    "/undo-waive": lambda a, ct, b: a.waive(
        _form(b).get("form", ""), _form(b).get("issuer", ""), undo=True
    ),
    "/categorize": lambda a, ct, b: a.categorize(
        _form(b).get("row", ""),
        _form(b).get("rule", ""),
        _form(b).get("category", ""),
    ),
    "/confirm": lambda a, ct, b: a.confirm(
        _form(b).get("doc", ""), _form(b).get("action", ""), _edits(_form(b))
    ),
    "/taxpack": lambda a, ct, b: a.taxpack(),
    "/rollover": lambda a, ct, b: a.rollover(),
}


def server(app: App, port: int = 0) -> HTTPServer:
    return HTTPServer((HOST, port), handler(app))


def url(app: App, srv: HTTPServer) -> str:
    return f"http://{HOST}:{srv.server_address[1]}/?token={app.token}"
