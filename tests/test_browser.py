"""Phase 10, unit 7g: the live page driven in headless Chromium (Playwright),
on a synthetic planner folder. A file dragged onto the page goes through the
page's own drag and drop handlers; every control is reached and used with the
keyboard alone; at 200% zoom nothing needs a sideways scroll; every piece of
text meets WCAG AA contrast in the light and the dark scheme.

CI installs Chromium and sets PLANNER_BROWSER_TESTS=required, so a missing
browser fails there. Elsewhere the tests skip when Chromium is not installed
(`uv run playwright install chromium` installs it)."""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from planner.dashboard import serve
from tests.pdfgen import make_pdf
from tests.test_forms import SSA
from tests.test_serve import live  # noqa: F401

pytestmark = pytest.mark.browser
sync_api = pytest.importorskip("playwright.sync_api")

# WCAG 2.2 AA (1.4.3): 4.5:1, or 3:1 for large text (24px, or 18.66px bold)
NORMAL, LARGE = 4.5, 3.0

CONTRAST_JS = """
() => {
  const rgb = (s) => (s.match(/[\\d.]+/g) || []).map(Number);
  const lum = ([r, g, b]) => {
    const f = (c) => {
      c /= 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const ground = (el) => {
    for (let e = el; e; e = e.parentElement) {
      const c = rgb(getComputedStyle(e).backgroundColor);
      if (c.length === 3 || (c.length === 4 && c[3] > 0)) { return c.slice(0, 3); }
    }
    return rgb(getComputedStyle(document.body).backgroundColor).slice(0, 3);
  };
  const out = [];
  for (const el of document.querySelectorAll("body *")) {
    const own = [...el.childNodes]
      .some((n) => n.nodeType === 3 && n.textContent.trim());
    const box = el.getBoundingClientRect();
    if (!own || !box.width || !box.height) { continue; }
    const st = getComputedStyle(el);
    if (st.visibility === "hidden" || st.display === "none") { continue; }
    const a = lum(rgb(st.color)), b = lum(ground(el));
    const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    const px = parseFloat(st.fontSize), bold = parseInt(st.fontWeight, 10) >= 700;
    out.push({ text: el.textContent.trim().slice(0, 40), tag: el.tagName,
               cls: el.className, ratio, large: px >= 24 || (bold && px >= 18.66) });
  }
  return out;
}
"""

# Every class the template colours, placed on a card so the scheme's own
# colours are measured even when the synthetic page has no such row.
SWATCHES_JS = """
() => {
  const s = document.createElement("section");
  s.id = "swatches";
  s.innerHTML = '<h2><span class="tag actual">actual</span>'
    + ' <span class="tag estimate">estimate</span>'
    + ' <span class="tag unavailable">unavailable</span>'
    + ' <span class="cov">covered</span>'
    + ' <span class="cov not-handled">not handled</span></h2>'
    + '<p class="done">nothing needed</p>'
    + '<div class="need"><b>a need</b><small>why: a reason</small></div>'
    + '<table><tr class="under"><td>under</td></tr></table><p class="stale">stale</p>'
    + '<button>Save</button> <button class="plain">Undo</button>'
    + ' <a href="#x">a link</a>'
    + ' <input value="typed"> <select><option>NC</option></select>';
  document.querySelector("main").prepend(s);
}
"""


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except sync_api.Error as exc:
            if os.environ.get("PLANNER_BROWSER_TESTS") == "required":
                raise
            pytest.skip(f"Chromium is not installed: {str(exc).splitlines()[0]}")
        yield chromium
        chromium.close()


def _open(browser: Any, port: int, **context: Any) -> Any:
    page = browser.new_context(**context).new_page()
    page.set_default_timeout(180_000)  # the first render reads the whole ledger
    page.goto(f"http://127.0.0.1:{port}/?token=tok")
    return page


def test_a_file_dragged_onto_the_page_is_read(
    browser: Any,
    live: tuple[serve.App, int],  # noqa: F811
    tmp_path: Path,
) -> None:
    """The drop goes through the page's own handlers: dragenter lights the
    zone, the drop posts the file and the page reloads with it read."""
    a, port = live
    page = _open(browser, port)
    assert page.locator('input[name="key"][value="ss_estimate_67"]').count() >= 1
    pdf = base64.b64encode(make_pdf(tmp_path / "ssa.pdf", [SSA]).read_bytes())
    page.evaluate(
        """(b64) => {
          const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
          window._dt = new DataTransfer();
          const type = { type: "application/pdf" };
          window._dt.items.add(new File([bytes], "ssa.pdf", type));
          document.body.dispatchEvent(new DragEvent("dragenter",
            { bubbles: true, cancelable: true, dataTransfer: window._dt }));
        }""",
        pdf.decode(),
    )
    assert "over" in (page.locator("#drop").get_attribute("class") or "")
    with page.expect_navigation():
        page.evaluate(
            """() => document.querySelector("main").dispatchEvent(new DragEvent("drop",
                 { bubbles: true, cancelable: true, dataTransfer: window._dt }))"""
        )
    page.wait_for_load_state()
    assert "1 file(s) received: 1 imported" in page.locator("#message").inner_text()
    assert page.locator('input[name="key"][value="ss_estimate_67"]').count() == 0
    assert not list((a.lay.data / "inbox").glob("*.pdf"))


def _tab_order(page: Any) -> list[dict[str, Any]]:
    """Tab from the top until focus comes back round; each stop's name and
    whether the browser draws a focus indicator on it."""
    seen: list[dict[str, Any]] = []
    for _ in range(400):
        page.keyboard.press("Tab")
        stop = page.evaluate(
            """() => {
              const el = document.activeElement;
              if (!el || el === document.body) { return null; }
              const st = getComputedStyle(el);
              return { id: el.outerHTML.slice(0, 80),
                       ring: el.matches(":focus-visible") && st.outlineStyle !== "none"
                             && parseFloat(st.outlineWidth) >= 2 };
            }"""
        )
        if stop is None or (seen and stop["id"] == seen[0]["id"]):
            break
        seen.append(stop)
    return seen


def test_every_control_works_from_the_keyboard_alone(
    browser: Any,
    live: tuple[serve.App, int],  # noqa: F811
    tmp_path: Path,
) -> None:
    a, port = live
    page = _open(browser, port)
    order = _tab_order(page)
    controls = page.evaluate(
        """() => [...document.querySelectorAll(
                   "a[href], input:not([type=hidden]), select, button")]
                 .filter((el) => el.getBoundingClientRect().width > 0).length"""
    )
    assert len(order) == controls, "some control cannot be reached with Tab"
    assert [s["id"] for s in order if not s["ring"]] == [], "focus not visible"
    unlabeled = page.evaluate(
        """() => [...document.querySelectorAll("input:not([type=hidden]), select")]
                 .filter((el) => !el.getAttribute("aria-label") && !el.labels.length)
                 .map((el) => el.outerHTML.slice(0, 80))"""
    )
    assert unlabeled == [], "a field has no name a screen reader can say"
    # answer a question: Tab to its field, type, Enter
    field = page.locator('input[name="key"][value="birth_date"] ~ input[name="value"]')
    page.locator("body").focus()
    for _ in range(400):
        page.keyboard.press("Tab")
        if field.evaluate("(el) => el === document.activeElement"):
            break
    page.keyboard.type("1971-06-15")
    with page.expect_navigation():
        page.keyboard.press("Enter")
    assert page.locator('input[name="key"][value="birth_date"]').count() == 0
    # add a document: the file field opens its chooser on Space, then Add
    picker = page.locator('#drop input[type="file"]')
    picker.focus()
    pdf = make_pdf(tmp_path / "ssa.pdf", [SSA])
    with page.expect_file_chooser() as chooser:
        page.keyboard.press("Space")
    chooser.value.set_files(str(pdf))
    page.keyboard.press("Tab")
    with page.expect_navigation():
        page.keyboard.press("Enter")
    assert "1 file(s) received: 1 imported" in page.locator("#message").inner_text()
    assert a.lay.data.is_dir()


def test_at_200_percent_zoom_nothing_scrolls_sideways(
    browser: Any,
    live: tuple[serve.App, int],  # noqa: F811
) -> None:
    """A 1280-pixel window at 200% zoom lays the page out 640 CSS pixels wide
    (WCAG 1.4.4 and 1.4.10): no sideways page scroll, every control inside the
    window. Wide tables scroll inside their own box."""
    _, port = live
    page = _open(
        browser, port, viewport={"width": 640, "height": 400}, device_scale_factor=2
    )
    page.evaluate(SWATCHES_JS)
    width = page.evaluate("() => document.documentElement.clientWidth")
    assert page.evaluate("() => document.documentElement.scrollWidth") <= width
    outside = page.evaluate(
        """(w) => [...document.querySelectorAll(
                   "input:not([type=hidden]), select, button, a[href]")]
                 .filter((el) => { const r = el.getBoundingClientRect();
                   return r.width > 0 && (r.left < 0 || r.right > w + 0.5); })
                 .map((el) => el.outerHTML.slice(0, 80))""",
        width,
    )
    assert outside == []


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_every_text_meets_aa_contrast(
    browser: Any,
    live: tuple[serve.App, int],  # noqa: F811
    scheme: str,
) -> None:
    _, port = live
    page = _open(browser, port, color_scheme=scheme)
    page.evaluate(SWATCHES_JS)
    texts = page.evaluate(CONTRAST_JS)
    assert len(texts) > 20
    low = [
        f"{t['tag']}.{t['cls']} {t['text']!r}: {t['ratio']:.2f}"
        for t in texts
        if t["ratio"] < (LARGE if t["large"] else NORMAL)
    ]
    assert low == [], "\n".join(low)


def test_ci_installs_the_browser_and_requires_these_tests() -> None:
    import yaml

    ci = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    job = yaml.safe_load(ci.read_text(encoding="utf-8"))["jobs"]["test"]
    runs = [s.get("run", "") for s in job["steps"]]
    install = [i for i, r in enumerate(runs) if "playwright install" in r]
    tests = [i for i, r in enumerate(runs) if r.startswith("uv run pytest")]
    assert install and tests and install[0] < tests[0]
    assert "chromium" in runs[install[0]]
    assert job["steps"][tests[0]]["env"]["PLANNER_BROWSER_TESTS"] == "required"
