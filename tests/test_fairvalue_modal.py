"""
Tests for the fairvalue result modal dialog.

The modal opens when a lookup completes (success or error) and can be
closed via:
  - The × button (top-right)
  - The "Close" button (footer)
  - Clicking on the backdrop (but not the dialog itself)
  - Pressing Escape
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


CSS = (PROJECT / "webapp" / "static" / "css" / "app.css").read_text()
JS = (PROJECT / "webapp" / "static" / "js" / "fairvalue.js").read_text()


@pytest.fixture
def client():
    import tempfile
    csv = "SYMBOL,NAME OF COMPANY\nRELIANCE,Reliance Industries Limited\n"
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    )
    f.write(csv); f.close()
    try:
        import pipeline.fair_value.search as s
        s.CACHE_FILE = Path(f.name); s._index = []; s._index_loaded_at = 0.0
        import webapp.data as wd, webapp.server as ws
        _stub = lambda force=False: {
            "asof": "2026-06-25", "indices": [],
            "equity": {"row": None, "holdings": [], "value": 0, "prev_value": 0},
            "mf": {"count": 0, "value": 0, "prev_value": 0, "pct": 0},
            "sgb": {"count": 0, "value": 0, "prev_value": 0, "pct": 0, "rows": []},
            "total": {"value": 0, "prev_value": 0, "pct": 0},
            "best_index": None, "worst_index": None,
        }
        wd.get_portfolio_snapshot = _stub; ws.get_portfolio_snapshot = _stub
        wd.get_fairvalue_snapshot = lambda force=False: {"asof": "2026-06-25", "rows": []}
        ws.get_fairvalue_snapshot = wd.get_fairvalue_snapshot
        from fastapi.testclient import TestClient
        from webapp.server import app
        yield TestClient(app)
    finally:
        Path(f.name).unlink(missing_ok=True)


def _page(client, path="/fairvalue"):
    r = client.get(path)
    assert r.status_code == 200
    return r.text


# ---------- Markup ----------

class TestModalMarkup:
    def test_modal_container_present(self, client):
        html = _page(client)
        assert 'id="resultModal"' in html, "resultModal container missing"
        assert 'class="modal-backdrop"' in html, "modal-backdrop class missing"
        assert "hidden" in html, "modal must start with hidden attr"

    def test_modal_is_a_proper_dialog(self, client):
        """role=dialog + aria-modal=true + labelledby are required for a11y."""
        html = _page(client)
        # Find the modal div
        m = re.search(r'<div[^>]*id="resultModal"[^>]*>', html)
        assert m, "resultModal not found"
        attrs = m.group(0)
        assert 'role="dialog"' in attrs, "missing role=dialog"
        assert 'aria-modal="true"' in attrs, "missing aria-modal"
        assert 'aria-labelledby="modalTitle"' in attrs, "missing aria-labelledby"
        assert 'aria-describedby="modalDesc"' in attrs, "missing aria-describedby"

    def test_modal_starts_hidden(self, client):
        """The modal must start with the hidden attribute so it's not
        visible until the user clicks Calculate fair value."""
        html = _page(client)
        # The hidden attribute is present in the initial markup.
        m = re.search(r'<div[^>]*id="resultModal"[^>]*>', html)
        assert m, "resultModal not found"
        assert "hidden" in m.group(0), \
            "modal must have hidden attribute on page load"


# ---------- CSS contract ----------

class TestModalCSS:
    def test_modal_backdrop_covers_viewport(self):
        """The backdrop must use position:fixed with inset:0 to cover the viewport."""
        # Find the .modal-backdrop block
        m = re.search(
            r"\.modal-backdrop\s*\{([^}]*)\}",
            CSS,
        )
        assert m, ".modal-backdrop CSS block not found"
        body = m.group(1)
        assert "position:" in body and "fixed" in body, \
            ".modal-backdrop must use position:fixed"
        assert "inset:" in body, ".modal-backdrop must use inset:0"
        assert "z-index:" in body, ".modal-backdrop must have a z-index"

    def test_modal_dialog_has_animation(self):
        """The dialog opens with an animation (fade-in + scale)."""
        assert "@keyframes modal-pop-in" in CSS
        assert "@keyframes modal-fade-in" in CSS

    def test_modal_responsive_at_small_widths(self):
        """At < 600px the modal must shrink padding/font."""
        # Find the @media (max-width: 600px) block by reading lines.
        # The substring 'max-width: 600px' is unambiguous (vs min-width).
        in_block = False
        body_lines: list[str] = []
        for line in CSS.splitlines():
            stripped = line.strip()
            if not in_block and "max-width" in line and "600px" in line:
                in_block = True
                continue
            if in_block:
                if stripped == "}":
                    break
                body_lines.append(stripped)
        body = "\n".join(body_lines)
        assert ".modal-ticker" in body, \
            f"no .modal-ticker override in mobile breakpoint (body={body[:200]!r})"
        assert ".modal-backdrop" in body, \
            "no .modal-backdrop override in mobile breakpoint"
        assert ".modal-price" in body, \
            "no .modal-price override in mobile breakpoint"

    def test_body_scroll_locked_when_modal_open(self):
        """body.modal-open { overflow: hidden } prevents background scroll."""
        assert re.search(
            r"body\.modal-open\s*\{[^}]*overflow:\s*hidden",
            CSS, re.DOTALL,
        ), "no body.modal-open { overflow: hidden } rule"

    def test_close_button_styled(self):
        """The .modal-close button must be visible and have hover state."""
        assert ".modal-close {" in CSS
        assert ".modal-close:hover" in CSS, \
            ".modal-close needs a :hover style"
        assert ".modal-close:focus-visible" in CSS, \
            ".modal-close needs :focus-visible outline for a11y"


# ---------- JS behavior ----------

class TestModalJS:
    def test_openModal_function_defined(self):
        assert "function openModal" in JS, "openModal not defined"

    def test_closeModal_function_defined(self):
        assert "function closeModal" in JS, "closeModal not defined"

    def test_openModal_sets_body_class(self):
        """openModal must add modal-open to body to lock scroll."""
        # Find openModal function body
        m = re.search(
            r"function\s+openModal\s*\(\)\s*\{(.*?)\n\s*\}\n",
            JS,
            re.DOTALL,
        )
        assert m, "openModal not found"
        body = m.group(1)
        assert 'modal-open' in body, \
            "openModal must add 'modal-open' class to body"

    def test_closeModal_unlocks_body(self):
        """closeModal must remove modal-open from body."""
        m = re.search(
            r"function\s+closeModal\s*\(\)\s*\{(.*?)\n\s*\}\n",
            JS,
            re.DOTALL,
        )
        assert m, "closeModal not found"
        body = m.group(1)
        assert 'modal-open' in body, \
            "closeModal must remove 'modal-open' class from body"

    def test_openModal_shows_element(self):
        """openModal sets hidden=false on the modal element."""
        m = re.search(
            r"function\s+openModal\s*\(\)\s*\{(.*?)\n\s*\}\n",
            JS,
            re.DOTALL,
        )
        body = m.group(1)
        assert re.search(r"\.hidden\s*=\s*false", body), \
            "openModal must set hidden=false"

    def test_openModal_focuses_close_button(self):
        """For a11y, focus should move to the close button when modal opens."""
        m = re.search(
            r"function\s+openModal\s*\(\)\s*\{(.*?)\n\s*\}\n",
            JS,
            re.DOTALL,
        )
        body = m.group(1)
        assert ".focus()" in body, \
            "openModal must .focus() an element for keyboard users"
        assert "modal-close" in body, \
            "the focused element must be the close button"

    def test_escape_closes_modal(self):
        """Pressing Escape must close the modal."""
        assert re.search(
            r"e\.key\s*===\s*[\"']Escape[\"']",
            JS,
        ), "no Escape key handler"

    def test_click_on_backdrop_closes_modal(self):
        """Clicking the backdrop (but not the dialog) closes the modal."""
        # The handler checks e.target === m (the backdrop itself)
        assert re.search(
            r"e\.target\s*===\s*m\b",
            JS,
        ), "no click-on-backdrop-close handler"

    def test_renderModal_creates_close_buttons(self):
        """renderModal must bind click handlers to the close buttons."""
        m = re.search(
            r"function\s+renderModal\s*\([^)]*\)\s*\{(.*?)\n\s*\}\n",
            JS,
            re.DOTALL,
        )
        assert m, "renderModal not found"
        body = m.group(1)
        assert "modal-close" in body, \
            "renderModal must render .modal-close elements"
        assert "closeModal" in body, \
            "renderModal must bind closeModal handlers"
        assert "addEventListener" in body, \
            "renderModal must addEventListener for close"

    def test_close_button_has_accessible_label(self):
        """The close button must have aria-label."""
        m = re.search(
            r"function\s+renderModal\s*\([^)]*\)\s*\{(.*?)\n\s*\}\n",
            JS,
            re.DOTALL,
        )
        body = m.group(1)
        assert 'aria-label=' in body, \
            "close button must have aria-label for screen readers"


# ---------- JS smoke tests via the live route ----------

class TestModalRoutes:
    """The /api/fairvalue/lookup endpoint returns valid JSON that the
    modal can render. (We don't actually render the modal in JS — we
    just confirm the data shape matches what renderModal expects.)"""

    def test_lookup_returns_valid_modal_payload(self, client):
        """The lookup response has all fields renderModal needs."""
        # Stub the fetcher so we don't hit screener.in
        import pipeline.fair_value.fetcher as fetcher
        import pipeline.fair_value.valuation as fv_val
        fake = {
            "RELIANCE": {"ticker": "RELIANCE", "current_price": 1327.0,
                         "eps": 14.26, "book_value": 668.0,
                         "market_cap": 1700000.0,
                         "operating_cash_flow_per_share": 141.97,
                         "source_url": "", "fetched_at": ""},
        }
        fetcher.fetch = lambda t: fake.get(t, {"ticker": t,
                                                          "error": "no mock"})
        fv_val.fetch = fetcher.fetch
        try:
            r = client.post("/api/fairvalue/lookup",
                            json={"ticker": "RELIANCE", "industry_pe": 25})
            assert r.status_code == 200
            data = r.json()
            # Every field renderResult consumes must be present
            for field in ("ticker", "price", "eps", "book_value",
                          "fcf_per_share", "graham", "dcf", "resolved_ticker",
                          "resolved_name", "params"):
                assert field in data, f"missing field: {field}"
            # All numeric fields must be numeric (or None)
            for field in ("price", "eps", "book_value", "fcf_per_share",
                          "graham", "dcf"):
                assert data[field] is None or isinstance(data[field], (int, float)), \
                    f"{field} should be numeric, got {data[field]!r}"
        finally:
            fetcher.fetch = lambda t: {"ticker": t,
                                                   "error": "unmocked"}
            fv_val.fetch = fetcher.fetch
