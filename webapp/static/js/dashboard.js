// Portfolio Tracker — interactive dashboard enhancements
//
// Adds (on /flows and /concalls):
//   - Auto-refresh of KPI tiles every 30 seconds (poll JSON APIs)
//   - "Re-scan" button: triggers /api/refresh?kind=..., polls for completion,
//     shows progress in a toast, reloads the page when done
//   - Expand/collapse for long bullet lists
//   - "Updated X ago" live timestamps
//
// Vanilla JS, no framework dependency. Loaded by base.html on every page
// but only activates on pages that have the data attributes.

(function () {
  'use strict';

  // ----- Utilities -----

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) {
    return Array.from((root || document).querySelectorAll(sel));
  }

  // Shared toast helper (works regardless of which page loaded first).
  let toastTimer = null;
  function toast(msg, kind) {
    const el = $('#toast');
    if (!el) { console.log('[toast]', msg); return; }
    el.textContent = msg;
    el.className = 'toast is-visible' + (kind ? ' is-' + kind : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('is-visible'), 5000);
  }

  // "5 minutes ago" / "just now" formatter
  function timeAgo(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    const sec = Math.floor((Date.now() - d.getTime()) / 1000);
    if (sec < 10) return 'just now';
    if (sec < 60) return sec + 's ago';
    if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
    if (sec < 86400) return Math.floor(sec / 3600) + 'h ago';
    return Math.floor(sec / 86400) + 'd ago';
  }

  function updateAllTimeAgo() {
    $$('[data-asof]').forEach((el) => {
      const iso = el.getAttribute('data-asof');
      if (iso) el.textContent = timeAgo(iso);
    });
  }

  // ----- Auto-refresh KPI tiles -----

  // Each page registers its endpoints; the poller fetches + updates in place.
  const pageConfig = {
    flows: {
      endpoint: '/api/flows',
      tiles: ['.kpi[data-tile]'],
      map: {
        'fii':     (s, d) => fmtCr(s, d.today_fii && d.today_fii.net_value_cr),
        'dii':     (s, d) => fmtCr(s, d.today_dii && d.today_dii.net_value_cr),
        'net':     (s, d) => {
          const f = d.today_fii ? d.today_fii.net_value_cr : 0;
          const x = d.today_dii ? d.today_dii.net_value_cr : 0;
          fmtCr(s, f + x);
        },
        'my-deals':(s, d) => { s.textContent = d.portfolio_deals.length; },
      },
    },
    concalls: {
      endpoint: '/api/concalls',
      tiles: ['.kpi[data-tile]'],
      map: {
        'total':   (s, d) => { s.textContent = d.summaries.length; },
        'recent':  (s, d) => { s.textContent = d.recent_count; },
        'confident':(s, d) => {
          s.textContent = (d.tone_counts && d.tone_counts.confident) || 0;
        },
        'cautious':(s, d) => {
          s.textContent = (d.tone_counts && d.tone_counts.cautious) || 0;
        },
      },
    },
  };

  function fmtCr(el, value) {
    if (value === null || value === undefined) {
      el.textContent = '—';
      el.classList.remove('is-positive', 'is-negative');
      return;
    }
    const sign = value >= 0 ? '+' : '';
    const formatted = sign + '₹' + Math.round(value).toLocaleString('en-IN') + ' cr';
    el.textContent = formatted;
    el.classList.toggle('is-positive', value >= 0);
    el.classList.toggle('is-negative', value < 0);
  }

  function refreshTiles(page) {
    const cfg = pageConfig[page];
    if (!cfg) return;
    fetch(cfg.endpoint, { cache: 'no-store' })
      .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then((data) => {
        cfg.tiles.forEach((tileSel) => {
          $$(tileSel).forEach((tile) => {
            const key = tile.getAttribute('data-tile');
            const updater = cfg.map[key];
            if (updater) updater(tile, data);
          });
        });
        // Update the asof timestamp at the page level
        const asofEl = $('[data-page-asof]');
        if (asofEl && data.asof) {
          asofEl.setAttribute('data-asof', data.asof);
          asofEl.textContent = 'Updated ' + timeAgo(data.asof);
        }
      })
      .catch((e) => {
        // Silent failure: don't spam the user if the API hiccups.
        console.debug('[dashboard] auto-refresh failed:', e);
      });
  }

  // ----- Re-scan button (POST /api/refresh, then poll, then reload) -----

  function setupRescan() {
    const btn = $('#rescanBtn');
    if (!btn) return;
    const kind = btn.dataset.kind || 'all';
    btn.addEventListener('click', async () => {
      const orig = btn.textContent;
      btn.disabled = true;
      btn.classList.add('is-loading');

      // Step 1: kick off the scan
      btn.textContent = '⏳ Starting scan…';
      try {
        const res = await fetch('/api/refresh?kind=' + encodeURIComponent(kind),
                                { method: 'POST' });
        if (!res.ok && res.status !== 202) {
          throw new Error('HTTP ' + res.status);
        }
      } catch (e) {
        toast('Re-scan failed to start: ' + e.message, 'error');
        btn.disabled = false;
        btn.classList.remove('is-loading');
        btn.textContent = orig;
        return;
      }

      // Step 2: poll the page-asof timestamp until it changes (data refreshed)
      // OR until 60s elapses.
      const startAsOf = $('[data-page-asof]') ?
        $('[data-page-asof]').getAttribute('data-asof') : '';
      const startTime = Date.now();
      const timeoutMs = 120000; // 2 minutes
      const pollMs = 3000;

      btn.textContent = '🔄 Scanning…';

      const tick = async () => {
        const elapsed = Date.now() - startTime;
        if (elapsed > timeoutMs) {
          toast('Re-scan is taking longer than expected — refresh the page manually.',
                'warning');
          btn.disabled = false;
          btn.classList.remove('is-loading');
          btn.textContent = orig;
          return;
        }
        try {
          const cfg = pageConfig[kind] || {};
          if (cfg.endpoint) {
            const r = await fetch(cfg.endpoint, { cache: 'no-store' });
            if (r.ok) {
              const d = await r.json();
              if (d.asof && d.asof !== startAsOf) {
                // Data has refreshed — show success and reload
                btn.textContent = '✓ Done — reloading';
                toast(`Re-scan complete (${kind}). Reloading…`, 'success');
                setTimeout(() => location.reload(), 800);
                return;
              }
            }
          }
        } catch (e) {
          // Continue polling on transient errors
        }
        btn.textContent = '🔄 Scanning… ' + Math.floor(elapsed / 1000) + 's';
        setTimeout(tick, pollMs);
      };
      tick();
    });
  }

  // ----- Expand/collapse for long bullet lists -----

  function setupExpandCollapse() {
    $$('[data-expandable]').forEach((container) => {
      const items = $$('li, .concalls-bullet', container);
      if (items.length <= 3) return;
      const max = parseInt(container.getAttribute('data-expandable') || '3', 10);
      items.forEach((item, idx) => {
        if (idx >= max) item.style.display = 'none';
      });
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-secondary btn-small';
      btn.textContent = `Show all ${items.length} items`;
      btn.style.marginTop = '0.5rem';
      btn.addEventListener('click', () => {
        const expanded = btn.getAttribute('data-expanded') === '1';
        items.forEach((item, idx) => {
          if (idx >= max) item.style.display = expanded ? 'none' : '';
        });
        btn.setAttribute('data-expanded', expanded ? '0' : '1');
        btn.textContent = expanded
          ? `Show all ${items.length} items`
          : 'Show fewer';
      });
      container.appendChild(btn);
    });
  }

  // ----- Init -----

  function init() {
    // Detect which page we're on
    const path = location.pathname;
    let page = null;
    if (path === '/flows' || path.startsWith('/flows')) page = 'flows';
    else if (path === '/concalls' || path.startsWith('/concalls')) page = 'concalls';

    if (page) {
      // Wire the rescan button (replace the old hx-post one)
      const oldBtn = $('#refreshBtn[data-kind="flows"], #refreshBtn[data-kind="concalls"]');
      if (oldBtn) {
        oldBtn.id = 'rescanBtn';
        oldBtn.removeAttribute('hx-post');
        oldBtn.removeAttribute('hx-swap');
        oldBtn.textContent = oldBtn.textContent.replace(/Re-scan/i, '🔄 Re-scan now');
        setupRescan();
      }

      setupExpandCollapse();

      // Auto-refresh tiles every 30s (only on these pages)
      refreshTiles(page);
      setInterval(() => refreshTiles(page), 30000);

      // Live "X ago" timestamps
      updateAllTimeAgo();
      setInterval(updateAllTimeAgo, 10000);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();