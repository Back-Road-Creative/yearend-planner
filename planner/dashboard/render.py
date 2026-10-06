"""The dashboard page as HTML. ``live`` pages (served by ``planner run``) carry
the forms that answer the Needed panel; the static export
(``out/index.html``) is the same page without them, for printing and backup.
Autoescape is on: every imported string (issuer names, file names, OCR text)
is escaped."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from planner import NOTICE
from planner.dashboard.page import Page
from planner.paths import Layout

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates" / "dashboard"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(default=True, default_for_string=True),
        keep_trailing_newline=True,
    )
    env.globals["notice"] = NOTICE
    return env


def html(page: Page, token: str = "", message: str = "") -> str:
    """The page; a non-empty ``token`` makes it live (forms post back with it)."""
    tpl = _env().get_template("page.html")
    return tpl.render(p=page, live=bool(token), token=token, message=message)


def write_static(lay: Layout, page: Page) -> Path:
    """``out/index.html`` (personal, gitignored)."""
    lay.out.mkdir(parents=True, exist_ok=True)
    path = lay.out / "index.html"
    path.write_text(html(page), encoding="utf-8")
    return path
