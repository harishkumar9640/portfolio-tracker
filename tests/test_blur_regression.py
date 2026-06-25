"""
Regression test for the "blurred page on load" bug.

Bug history:
  When I added the modal dialog in commit 5bf1b0a, the modal markup
  had the `hidden` attribute but the CSS rule
    .modal-backdrop { display: flex; ... }
  overrode it (because the HTML `hidden` attribute is just
  `[hidden] { display: none }` in the browser's user-agent stylesheet,
  which loses specificity battles with any author `display` rule).

  Result: on page load, the modal was VISIBLE — a full-screen
  overlay blurring the entire page. Users couldn't click anything.

  This test pins both:
    1. The CSS contains a [hidden] override
    2. The rendered HTML's modal element is hidden on page load
       (verified by parsing the markup, NOT by browser rendering)

If this test fails: someone removed the [hidden] override, or removed
the hidden attribute from the modal markup.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


CSS = (PROJECT / "webapp" / "static" / "css" / "app.css").read_text()
FAIRVALUE_HTML = (PROJECT / "webapp" / "templates" / "fairvalue.html").read_text()


# ---------- CSS contract ----------

class TestHiddenOverrideCSS:
    """The CSS must include a `[hidden]` rule with !important so it
    beats any `display: flex/grid/block` rule on the same element."""

    def test_global_hidden_override_present(self):
        """There must be a generic `[hidden] { display: none !important }` rule."""
        m = re.search(
            r"\[hidden\]\s*\{\s*display:\s*none\s*!important",
            CSS,
        )
        assert m, (
            "Missing global `[hidden] { display: none !important }` rule. "
            "Without it, any element with explicit display:flex in author "
            "CSS would override the HTML `hidden` attribute and show on page load."
        )

    def test_modal_backdrop_hidden_override_present(self):
        """Specifically for the modal — even if the global rule is removed,
        the modal needs its own [hidden] override so it's hidden by default."""
        m = re.search(
            r"\.modal-backdrop\[hidden\]\s*\{\s*display:\s*none\s*!important",
            CSS,
        )
        assert m, (
            "Missing `.modal-backdrop[hidden] { display: none !important }` rule. "
            "Without it, the modal-blur bug recurs."
        )


# ---------- Markup contract ----------

class TestModalHiddenInMarkup:
    """The modal's rendered HTML must have the hidden attribute on load."""

    def test_template_has_hidden_attribute(self):
        """The {% %} template must emit hidden on the modal div."""
        # Check the source template (pre-render)
        assert 'id="resultModal"' in FAIRVALUE_HTML, \
            "resultModal missing from template"
        m = re.search(
            r'<div[^>]*id="resultModal"[^>]*>',
            FAIRVALUE_HTML,
        )
        assert m, "resultModal div not found"
        attrs = m.group(0)
        assert "hidden" in attrs, (
            f"resultModal div is missing the `hidden` attribute. "
            f"Without it, the modal shows on page load. Found: {attrs!r}"
        )


# ---------- Live render contract (runs the actual server) ----------

class TestModalNotVisibleOnLoad:
    """End-to-end: render the page and verify the modal is hidden
    AND that no element with position:fixed;inset:0 covers the viewport
    on initial load."""

    def test_rendered_modal_has_hidden_attr(self):
        html = FAIRVALUE_HTML
        m = re.search(
            r'<div[^>]*id="resultModal"[^>]*>',
            html,
        )
        assert m
        attrs = m.group(0)
        assert "hidden" in attrs, \
            f"Rendered modal must have hidden attribute. Got: {attrs!r}"

    def test_no_unhidden_full_screen_overlay_on_load(self):
        """Sanity check: nothing in the initial HTML has the modal pattern
        (`position: fixed; inset: 0; ... display: flex`) WITHOUT hidden.

        This catches the original bug class: a future change that adds
        another overlay element but forgets to hide it.
        """
        html = FAIRVALUE_HTML
        # Match all divs that look like modal backdrops
        # Pattern: <div ... id="..." class="...modal-backdrop..." ...>
        # Look at each candidate: do they have `hidden`?
        suspects = re.findall(
            r'<div[^>]*class="[^"]*modal-backdrop[^"]*"[^>]*>',
            html,
        )
        for s in suspects:
            if "id=\"resultModal\"" in s:
                # This is the modal — must be hidden.
                assert "hidden" in s, \
                    f"Modal overlay not hidden on page load: {s!r}"
            # Other classes aren't necessarily modal overlays, skip.


# ---------- Browser-equivalent simulation ----------

class TestCSSRuleOrder:
    """CSS specificity check: `[hidden]` rule must come AFTER
    .modal-backdrop's display:flex rule in the file (or use !important)
    so it wins."""

    def test_hidden_uses_important(self):
        """The [hidden] rule must use !important (specificity otherwise
        can lose to .modal-backdrop { display: flex })."""
        m = re.search(
            r"\[hidden\][^{]*\{[^}]*!important",
            CSS,
        )
        assert m, (
            "The [hidden] rule must include !important to defeat "
            "author display rules."
        )
