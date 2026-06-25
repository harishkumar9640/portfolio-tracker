// Portfolio Tracker — minimal vanilla JS
// Handles: mobile nav toggle, refresh button, auto-dismiss toast.

(function () {
  'use strict';

  // ----- Mobile nav toggle -----
  const toggle = document.getElementById('navToggle');
  const nav = document.getElementById('primaryNav');
  if (toggle && nav) {
    toggle.addEventListener('click', () => {
      const open = nav.classList.toggle('is-open');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    // Close on link click (mobile UX)
    nav.addEventListener('click', (e) => {
      if (e.target.tagName === 'A' && nav.classList.contains('is-open')) {
        nav.classList.remove('is-open');
        toggle.setAttribute('aria-expanded', 'false');
      }
    });
  }

  // ----- Refresh button (POST /api/refresh) -----
  const refreshBtn = document.getElementById('refreshBtn');
  const toast = document.getElementById('toast');
  let toastTimer = null;
  function showToast(msg) {
    if (!toast) return;
    toast.textContent = msg;
    toast.classList.add('is-visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('is-visible'), 3500);
  }
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      const kind = refreshBtn.dataset.kind || 'all';
      refreshBtn.disabled = true;
      const orig = refreshBtn.textContent;
      refreshBtn.textContent = 'Refreshing…';
      try {
        const res = await fetch(`/api/refresh?kind=${encodeURIComponent(kind)}`,
                                { method: 'POST' });
        if (!res.ok && res.status !== 202) throw new Error(`HTTP ${res.status}`);
        showToast(`Refresh queued (${kind}). Reload in 30s for fresh data.`);
      } catch (e) {
        showToast(`Refresh failed: ${e.message}`);
      } finally {
        refreshBtn.disabled = false;
        refreshBtn.textContent = orig;
      }
    });
  }

  // ----- Bar fill width check (hide in-bar label if too narrow) -----
  // The My Equity bar shows today's ₹ P&L inside the bar fill. If the
  // fill is narrower than the text, the text overflows and looks bad.
  // We measure the actual pixel width after layout and add .is-narrow
  // when needed; CSS hides the in-bar label and the user can still
  // see the % on the right (where the stacked layout lives).
  function markNarrowBars() {
    document.querySelectorAll('.bar-row-portfolio .bar-fill').forEach((fill) => {
      const w = fill.getBoundingClientRect().width;
      if (w < 90) {
        fill.classList.add('is-narrow');
      } else {
        fill.classList.remove('is-narrow');
      }
    });
  }
  markNarrowBars();
  window.addEventListener('resize', markNarrowBars);
})();