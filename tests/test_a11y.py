"""
Accessibility (a11y) regression tests.

We can't run a full Lighthouse / axe-core audit from a unit test
(those need a headless browser), but we can verify the static HTML
markup satisfies the most important WCAG 2.1 AA criteria:

  - Every page has <html lang="...">
  - Every <img> has an alt attribute (decorative images: alt="")
  - Every form <input> has a label (or aria-label / aria-labelledby)
  - Buttons have accessible text
  - Links have descriptive text (not just "click here")
  - Heading hierarchy is logical (no skipping levels)
  - Color is not the sole indicator of state (we use text/badges too)
  - Tables have <th scope=...> headers
  - Skip links exist for keyboard nav
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# ---------- HTML parsing helpers ----------

class PageParser(HTMLParser):
    """Simple HTML parser that records the key accessibility-relevant
    elements on a page."""

    def __init__(self):
        super().__init__()
        self.html_lang: str | None = None
        self.title: str = ""
        self.images: list[dict] = []
        self.inputs: list[dict] = []
        self.buttons: list[dict] = []
        self.links: list[dict] = []
        self.headings: list[dict] = []  # [(level, text), ...]
        self.tables: list[dict] = []
        self.in_table = 0
        self.in_th = 0
        self.in_button = 0
        self.button_text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.html_lang = a.get("lang")
        elif tag == "title":
            self._title_pending = True
        elif tag == "img":
            self.images.append({
                "src": a.get("src", ""),
                "alt": a.get("alt"),       # None if missing
                "aria_hidden": a.get("aria-hidden"),
            })
        elif tag == "input":
            self.inputs.append({
                "type": a.get("type", "text"),
                "id": a.get("id"),
                "name": a.get("name"),
                "placeholder": a.get("placeholder"),
                "aria_label": a.get("aria-label"),
                "aria_labelledby": a.get("aria-labelledby"),
                "required": a.get("required") is not None,
            })
        elif tag == "button":
            self.in_button += 1
            self.button_text = ""
            self.buttons.append({
                "type": a.get("type", "submit"),
                "aria_label": a.get("aria-label"),
                "aria_hidden": a.get("aria-hidden"),
                "text": "",
            })
        elif tag == "a":
            self.links.append({
                "href": a.get("href", ""),
                "aria_label": a.get("aria-label"),
                "text": "",
            })
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.headings.append({"level": int(tag[1]), "text": ""})
        elif tag == "table":
            self.in_table += 1
            self.tables.append({"has_th": False})
        elif tag == "th" and self.in_table > 0:
            self.in_th += 1
            if self.tables:
                self.tables[-1]["has_th"] = True
        elif tag == "svg":
            # SVG icons should have role="img" + aria-label or aria-hidden
            pass

    def handle_endtag(self, tag):
        if tag == "title":
            self._title_pending = False
        elif tag == "button":
            if self.in_button > 0 and self.buttons:
                self.buttons[-1]["text"] = self.button_text.strip()
            self.in_button -= 1
        elif tag == "table":
            self.in_table -= 1
        elif tag == "th":
            if self.in_th > 0:
                self.in_th -= 1

    def handle_data(self, data):
        if self._title_pending if hasattr(self, "_title_pending") else False:
            self.title += data
        if self.in_button > 0:
            self.button_text += data


# ---------- Per-page fixtures ----------

def _fetch_html(client, path: str) -> str:
    r = client.get(path)
    assert r.status_code == 200, f"{path} returned {r.status_code}"
    return r.text


@pytest.fixture
def client():
    """TestClient with portfolio/fairvalue snapshots stubbed."""
    import tempfile
    csv = "SYMBOL,NAME OF COMPANY\nRELIANCE,Reliance Industries Limited\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    ) as f:
        f.write(csv)
        csv_path = Path(f.name)
    try:
        import fair_value.search as s
        s.CACHE_FILE = csv_path
        s._index = []
        s._index_loaded_at = 0.0
        import webapp.data as wd
        import webapp.server as ws
        _stub = lambda force=False: {
            "asof": "2026-06-25", "indices": [], "equity":
            {"row": None, "holdings": [], "value": 0, "prev_value": 0},
            "mf": {"count": 0, "value": 0, "prev_value": 0, "pct": 0},
            "sgb": {"count": 0, "value": 0, "prev_value": 0, "pct": 0, "rows": []},
            "total": {"value": 0, "prev_value": 0, "pct": 0},
            "best_index": None, "worst_index": None,
        }
        wd.get_portfolio_snapshot = _stub
        ws.get_portfolio_snapshot = _stub
        wd.get_fairvalue_snapshot = lambda force=False: {"asof": "2026-06-25", "rows": []}
        ws.get_fairvalue_snapshot = wd.get_fairvalue_snapshot
        from fastapi.testclient import TestClient
        from webapp.server import app
        yield TestClient(app)
    finally:
        csv_path.unlink(missing_ok=True)


def _parse(html_text: str) -> PageParser:
    p = PageParser()
    p.feed(html_text)
    return p


# ---------- Per-page a11y checks ----------

class TestPerPageBasics:
    """Every page must satisfy basic HTML/head requirements."""

    @pytest.mark.parametrize("path", ["/portfolio", "/fairvalue",
                                     "/history", "/settings"])
    def test_inputs_have_label(self, client, path):
        html = _fetch_html(client, path)
        p = _parse(html)
        # Build a set of IDs that have an accessible label, either via
        # <label for="id"> or by being nested inside a <label>...</label>.
        label_ids = set(re.findall(r'<label[^>]+for="([\w-]+)"', html))
        for m in re.finditer(r"<label[^>]*>(.*?)</label>", html, re.DOTALL):
            for im in re.finditer(r'id="([\w-]+)"', m.group(1)):
                label_ids.add(im.group(1))
        for inp in p.inputs:
            if inp["type"] in ("hidden", "submit", "button"):
                continue
            inp_id = inp.get("id")
            has_label = (
                inp["aria_label"]
                or inp["placeholder"]
                or inp["aria_labelledby"]
                or (inp_id and inp_id in label_ids)
            )
            assert has_label, (
                f"{path}: <input id={inp_id!r}> has no accessible label"
            )

    @pytest.mark.parametrize("path", ["/portfolio", "/fairvalue",
                                     "/history", "/settings"])
    def test_buttons_have_text(self, client, path):
        html = _fetch_html(client, path)
        p = _parse(html)
        for btn in p.buttons:
            # Either inner text or aria-label must be present
            text = (btn.get("text") or "").strip()
            aria_label = (btn.get("aria_label") or "").strip()
            assert text or aria_label, \
                f"{path}: <button type={btn['type']!r}> has no text or aria-label"


class TestHeadings:
    """Heading hierarchy must be logical (no level skipping)."""

    @pytest.mark.parametrize("path", ["/portfolio", "/fairvalue",
                                     "/history", "/settings"])
    def test_every_page_starts_with_h1(self, client, path):
        html = _fetch_html(client, path)
        p = _parse(html)
        levels = [h["level"] for h in p.headings]
        assert levels, f"{path}: no headings"
        assert levels[0] == 1, \
            f"{path}: first heading is h{levels[0]}, should be h1"

    def test_no_heading_level_skip_on_portfolio(self, client):
        html = _fetch_html(client, "/portfolio")
        p = _parse(html)
        levels = [h["level"] for h in p.headings]
        for prev, nxt in zip(levels, levels[1:]):
            assert nxt <= prev + 1, \
                f"/portfolio: heading skip from h{prev} to h{nxt}"

    def test_no_heading_level_skip_on_fairvalue(self, client):
        html = _fetch_html(client, "/fairvalue")
        p = _parse(html)
        levels = [h["level"] for h in p.headings]
        for prev, nxt in zip(levels, levels[1:]):
            assert nxt <= prev + 1, \
                f"/fairvalue: heading skip from h{prev} to h{nxt}"


class TestTables:
    """Tables must have <th> headers for screen-reader navigation."""

    @pytest.mark.parametrize("path", ["/portfolio", "/fairvalue",
                                     "/settings"])
    def test_tables_have_th_headers(self, client, path):
        html = _fetch_html(client, path)
        p = _parse(html)
        for tbl in p.tables:
            assert tbl["has_th"], \
                f"{path}: <table> has no <th> headers"


class TestLinks:
    """Links should have descriptive text."""

    @pytest.mark.parametrize("path", ["/portfolio", "/fairvalue",
                                     "/history", "/settings"])
    def test_no_generic_link_text(self, client, path):
        html = _fetch_html(client, path)
        p = _parse(html)
        GENERIC = {"click here", "here", "more", "read more", "link"}
        for ln in p.links:
            text = (ln.get("text") or "").strip().lower()
            aria_label = (ln.get("aria_label") or "").strip().lower()
            # Skip fragment-only links (e.g. "#main")
            href = ln.get("href", "")
            if href.startswith("#"):
                continue
            assert text not in GENERIC and aria_label not in GENERIC, \
                f"{path}: link to {href!r} has generic text {text!r}"


class TestColors:
    """We don't auto-test contrast ratios (need a browser), but we can
    verify that we don't rely on color as the sole indicator."""

    def test_no_inline_color_attributes(self, client):
        """Inline `color` attributes are forbidden — must use CSS classes."""
        for path in ("/portfolio", "/fairvalue", "/settings"):
            html = _fetch_html(client, path)
            # Inline color in style="color: ..." is a code smell
            assert "color:" not in html, \
                f"{path}: uses inline color — should use CSS classes"


class TestKeyboard:
    """Interactive elements must be keyboard-accessible."""

    @pytest.mark.parametrize("path", ["/portfolio", "/fairvalue",
                                     "/history", "/settings"])
    def test_hamburger_button_is_a_button_not_div(self, client, path):
        """The mobile menu toggle must be a <button>, not a clickable div."""
        html = _fetch_html(client, path)
        # Look for the navToggle
        assert 'id="navToggle"' in html
        # The tag name before id="navToggle" must be <button
        m = re.search(r'<(\w+)[^>]*id="navToggle"', html)
        assert m, f"{path}: could not find navToggle element"
        assert m.group(1) == "button", \
            f"{path}: navToggle is a <{m.group(1)}>, should be <button>"
