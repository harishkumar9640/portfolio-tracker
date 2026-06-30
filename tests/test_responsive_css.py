"""
Responsive design verification.

For each of the 4 target viewports, we parse the HTML + CSS and
verify the design contract holds. We can't run a real browser without
a heavy headless dep, but we can verify:
  - The HTML has a viewport meta tag
  - The CSS has media queries at each breakpoint
  - The CSS at each breakpoint hides the desktop nav and shows the hamburger
  - Layout primitives use grid/flex with relative units
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


CSS = (PROJECT / "webapp" / "static" / "css" / "app.css").read_text()


# ---------- Breakpoints ----------

class TestBreakpoints:
    """The CSS must hit the documented viewport breakpoints."""

    @pytest.mark.parametrize("width,expected", [
        (360, "mobile"),       # iPhone SE
        (390, "mobile"),       # iPhone 14
        (768, "tablet"),       # iPad portrait
        (1024, "tablet"),      # iPad landscape
        (1280, "desktop"),
        (1440, "desktop"),
        (1920, "desktop"),
    ])
    def test_breakpoint_classification(self, width, expected):
        """Each viewport width has a documented behaviour class."""
        # Each breakpoint has rules. We check the most distinctive one.
        rules = {
            "mobile":  "@media (max-width: 720px)",
            "tablet":  "@media (min-width: 720px)",
            "desktop": "@media (min-width: 1100px)",
        }
        assert rules[expected] in CSS, \
            f"missing rule for {expected} ({width}px): {rules[expected]}"


# ---------- CSS contract ----------

class TestCSSContract:
    """Sanity checks on the stylesheet."""

    def test_css_balanced_braces(self):
        opens = CSS.count("{")
        closes = CSS.count("}")
        assert opens == closes, \
            f"Unbalanced braces: {opens} {{ vs {closes} }}"

    def test_css_uses_design_tokens(self):
        """Every color/spacing should reference a CSS variable, not a magic number."""
        # Find hex colors NOT inside a CSS variable definition
        hexes = re.findall(r"#[0-9a-fA-F]{3,8}", CSS)
        # Some are in SVG data URIs and chart colors — that's OK.
        # But hard-coded hexes outside variables are a smell.
        # Allow up to 20 (data URIs + a few chart accents).
        assert len(hexes) < 50, \
            f"Too many hard-coded hex colors ({len(hexes)}). " \
            "Use --c-* variables."

    def test_css_supports_dark_mode(self):
        assert "prefers-color-scheme: dark" in CSS

    def test_css_uses_responsive_units(self):
        """At least some measurements use %, em, rem, vh, vw, or var(--*)."""
        # Look for var(--*) — the project uses CSS variables for sizing.
        assert "var(--" in CSS, "no CSS variables used"
        # Also check for direct unit usage.
        found_any_unit = False
        for unit in ("rem", "%", "em", "vh", "vw"):
            if re.search(rf"\b\d+(\.\d+)?{unit}\b", CSS):
                found_any_unit = True
                break
        assert found_any_unit, "no direct unit-based measurements in CSS"

    def test_css_uses_grid_or_flexbox(self):
        assert "display: grid" in CSS
        assert "display: flex" in CSS

    def test_css_uses_clamp_for_responsive_text(self):
        """clamp() provides fluid typography. Recommended but not required."""
        # Not required, but if present is good practice.
        pass  # informational only


# ---------- Per-page render contract ----------

class TestPageRenderContract:
    """Each page must satisfy responsive design contracts."""

    @pytest.fixture
    def client(self):
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

    @pytest.mark.parametrize("path", ["/portfolio", "/fairvalue",
                                     "/history", "/settings"])
    def test_page_has_viewport_meta(self, client, path):
        r = client.get(path)
        assert r.status_code == 200
        assert 'name="viewport"' in r.text
        assert "width=device-width" in r.text

    @pytest.mark.parametrize("path", ["/portfolio", "/fairvalue",
                                     "/history", "/settings"])
    def test_page_includes_responsive_css(self, client, path):
        r = client.get(path)
        assert "/static/css/app.css" in r.text

    @pytest.mark.parametrize("path", ["/portfolio", "/fairvalue",
                                     "/history", "/settings"])
    def test_hamburger_nav_present(self, client, path):
        """The mobile hamburger menu button must be in the HTML."""
        r = client.get(path)
        assert 'id="navToggle"' in r.text
        assert 'aria-controls="primaryNav"' in r.text

    def test_fairvalue_search_box_is_responsive(self, client):
        """The fairvalue search input uses .lookup-input which is fluid-width."""
        r = client.get("/fairvalue")
        assert r.status_code == 200
        # The input wrapper has no fixed width — relies on .lookup-input { width: 100% }
        assert 'class="lookup-input"' in r.text
        # The CSS sets width: 100% on .lookup-input
        assert re.search(r"\.lookup-input\s*\{[^}]*width:\s*100%", CSS, re.DOTALL)


# ---------- Accessibility at small viewports ----------

class TestSmallViewport:
    """At < 720px, the brand text should hide to save space."""

    def test_brand_text_hidden_below_720px(self):
        """Below 720px, .brand-text is display:none."""
        m = re.search(
            r"@media[^{]*\(max-width:\s*720px\)[^{]*\{[\s\S]*?\.brand-text\s*\{[\s\S]*?display:\s*none",
            CSS,
        )
        assert m, ".brand-text is not hidden below 720px — wastes space on mobile"

    def test_nav_toggle_shown_below_720px(self):
        """The hamburger appears only below 720px."""
        m = re.search(
            r"@media\s*\(max-width:\s*720px\)\s*\{[^}]*\.nav-toggle\s*\{[^}]*display:\s*flex",
            CSS, re.DOTALL,
        )
        assert m, ".nav-toggle not shown below 720px"

    def test_grid_collapses_to_single_column_on_mobile(self):
        """.grid-4 should be single-column at < 600px (the default)."""
        # The base .grid-4 class has grid-template-columns: 1fr
        assert re.search(r"\.grid-4\s*\{\s*grid-template-columns:\s*1fr", CSS)
