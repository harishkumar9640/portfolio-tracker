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

  // ----- Refresh button (POST /api/refresh, then poll, then reload) -----
  const refreshBtn = document.getElementById('refreshBtn');
  const toast = document.getElementById('toast');
  let toastTimer = null;
  function showToast(msg, kind) {
    if (!toast) return;
    toast.textContent = msg;
    toast.className = 'toast is-visible' + (kind ? ' is-' + kind : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('is-visible'), 5000);
  }
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      const kind = refreshBtn.dataset.kind || 'all';
      const orig = refreshBtn.textContent;
      refreshBtn.disabled = true;
      refreshBtn.classList.add('is-loading');
      refreshBtn.textContent = '🔄 Starting…';

      try {
        // 1) Kick off the rebuild
        const res = await fetch('/api/refresh?kind=' + encodeURIComponent(kind),
                                { method: 'POST' });
        if (!res.ok && res.status !== 202) {
          throw new Error('HTTP ' + res.status);
        }

        // 2) Poll /api/refresh/status until done (or 2-minute cap)
        refreshBtn.textContent = '🔄 Refreshing…';
        // Use cache_ts (Unix timestamp of last rebuild) for completion
        // detection — asof date string is too coarse (same day = same
        // string even after a fresh rebuild).
        const startCacheTs = parseFloat(
          document.querySelector('[data-portfolio-asof]')?.getAttribute('data-cache-ts') || '0'
        );
        const t0 = Date.now();
        const timeoutMs = 120000;
        const pollMs = 2000;

        while (Date.now() - t0 < timeoutMs) {
          await new Promise(r => setTimeout(r, pollMs));
          try {
            const sr = await fetch('/api/refresh/status', { cache: 'no-store' });
            if (sr.ok) {
              const s = await sr.json();
              const p = s.portfolio || {};
              const elapsed = Math.floor((Date.now() - t0) / 1000);
              const newCacheTs = p.cache_ts || 0;
              // Show live progress with seconds elapsed
              if (p.in_progress) {
                refreshBtn.textContent = '🔄 Refreshing… ' + elapsed + 's';
              } else {
                refreshBtn.textContent = '🔄 Finishing up… ' + elapsed + 's';
              }
              // Done when: not in progress, AND cache_ts is newer than
              // the value we recorded before clicking refresh
              if (!p.in_progress && newCacheTs > startCacheTs) {
                refreshBtn.textContent = '✓ Done — reloading';
                showToast('Refresh complete. Reloading…', 'success');
                setTimeout(() => location.reload(), 600);
                return;
              }
            }
          } catch (e) {
            // ignore transient network errors
          }
        }
        // Timed out — tell user to reload manually
        showToast('Refresh is taking longer than expected. Reload manually.', 'warning');
      } catch (e) {
        showToast('Refresh failed: ' + e.message, 'error');
      } finally {
        refreshBtn.disabled = false;
        refreshBtn.classList.remove('is-loading');
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