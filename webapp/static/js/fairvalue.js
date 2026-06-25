// Fair-value lookup: debounced autocomplete + on-demand calculation.
// Loaded only on the /fairvalue page.

(function () {
  'use strict';

  const input        = document.getElementById('lookupInput');
  const clearBtn     = document.getElementById('lookupClear');
  const suggestions  = document.getElementById('lookupSuggestions');
  const resultPanel  = document.getElementById('lookupResult');
  const paramIndustry = document.getElementById('paramIndustryPe');
  const paramG1       = document.getElementById('paramDcfG1');
  const paramG2       = document.getElementById('paramDcfG2');
  const paramR        = document.getElementById('paramDcfR');

  if (!input || !suggestions || !resultPanel) return;

  let debounceTimer = null;
  let activeFetch = null;          // AbortController for in-flight search
  let activeLookup = null;         // AbortController for in-flight valuation
  let highlightIdx = -1;           // -1 = no highlight
  let lastResults = [];

  // ---------- helpers ----------
  function debounce(fn, ms) {
    return function () {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => fn.apply(null, arguments), ms);
    };
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function fmtNum(v, n) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    return Number(v).toLocaleString('en-IN', {
      minimumFractionDigits: n, maximumFractionDigits: n,
    });
  }

  function fmtPct(v) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    return (v >= 0 ? '+' : '') + Number(v).toFixed(2) + '%';
  }

  // ---------- suggestions (autocomplete) ----------
  async function fetchSuggestions(q) {
    if (activeFetch) activeFetch.abort();
    activeFetch = new AbortController();
    try {
      const url = '/api/fairvalue/search?q=' + encodeURIComponent(q) + '&limit=10';
      const res = await fetch(url, { signal: activeFetch.signal });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      lastResults = data.results || [];
      renderSuggestions(lastResults);
    } catch (e) {
      if (e.name === 'AbortError') return;
      console.warn('autocomplete failed:', e);
      lastResults = [];
      renderSuggestions([]);
    }
  }

  function renderSuggestions(items) {
    highlightIdx = -1;
    if (!items.length) {
      suggestions.innerHTML = '<li class="lookup-empty">No matches</li>';
      suggestions.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      return;
    }
    suggestions.innerHTML = items.map((it, i) => `
      <li class="lookup-suggestion" role="option" data-idx="${i}"
          data-symbol="${escapeHtml(it.symbol)}"
          data-name="${escapeHtml(it.name)}"
          data-isin="${escapeHtml(it.isin)}">
        <span class="lookup-suggestion-sym">${escapeHtml(it.symbol)}</span>
        <span class="lookup-suggestion-name">${escapeHtml(it.name)}</span>
        <span class="lookup-suggestion-isin">${escapeHtml(it.isin)}</span>
      </li>`).join('');
    suggestions.hidden = false;
    input.setAttribute('aria-expanded', 'true');
  }

  function hideSuggestions() {
    suggestions.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    highlightIdx = -1;
  }

  function highlightSuggestion(idx) {
    const items = suggestions.querySelectorAll('.lookup-suggestion');
    items.forEach((el, i) => el.classList.toggle('is-highlighted', i === idx));
    highlightIdx = idx;
    if (idx >= 0 && items[idx]) {
      items[idx].scrollIntoView({ block: 'nearest' });
    }
  }

  function pickSuggestion(idx) {
    const it = lastResults[idx];
    if (!it) return;
    input.value = it.symbol;
    hideSuggestions();
    runLookup(it.symbol);
  }

  // ---------- lookup (fair-value calculation) ----------
  async function runLookup(ticker) {
    if (!ticker) return;
    if (activeLookup) activeLookup.abort();
    activeLookup = new AbortController();
    showLoading(ticker);
    try {
      const body = {
        ticker: ticker,
        dcf_g1: numOrUndef(paramG1),
        dcf_g2: numOrUndef(paramG2),
        dcf_r:  numOrUndef(paramR),
        industry_pe: numOrUndef(paramIndustry),
      };
      const res = await fetch('/api/fairvalue/lookup', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
        signal: activeLookup.signal,
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showError(data.error || ('HTTP ' + res.status), ticker);
      } else {
        renderResult(data);
      }
    } catch (e) {
      if (e.name === 'AbortError') return;
      console.warn('lookup failed:', e);
      showError(String(e), ticker);
    }
  }

  function numOrUndef(el) {
    if (!el) return undefined;
    const v = parseFloat(el.value);
    return Number.isFinite(v) ? v : undefined;
  }

  function showLoading(ticker) {
    input.classList.add('is-loading');
    resultPanel.hidden = false;
    resultPanel.classList.remove('is-error');
    resultPanel.innerHTML = `
      <div class="lookup-result-header">
        <div class="lookup-result-title">
          <span class="lookup-result-ticker">${escapeHtml(ticker)}</span>
        </div>
        <span class="text-muted">Fetching from screener.in…</span>
      </div>
      <p class="text-muted text-sm">This usually takes 1–3 seconds.</p>`;
  }

  function showError(msg, ticker) {
    input.classList.remove('is-loading');
    resultPanel.hidden = false;
    resultPanel.classList.add('is-error');
    resultPanel.innerHTML = `
      <div class="lookup-result-header">
        <div class="lookup-result-title">
          <span class="lookup-result-ticker">${escapeHtml(ticker || '?')}</span>
        </div>
      </div>
      <p class="text-neg">⚠ ${escapeHtml(msg)}</p>
      <p class="text-sm text-muted">If this is a recently listed stock or an unusual
      ticker, screener.in may not have data for it yet.</p>`;
  }

  function renderResult(d) {
    input.classList.remove('is-loading');
    resultPanel.hidden = false;
    resultPanel.classList.remove('is-error');

    const grahamTile = makeFairValueTile('Graham Number',
      d.graham, d.price, d.graham_margin_pct,
      `√(22.5 × ${fmtNum(d.eps, 2)} × ${fmtNum(d.book_value, 2)})`);
    const peTile = (d.pe_relative !== undefined && d.pe_relative !== null)
      ? makeFairValueTile('PE-Relative',
          d.pe_relative, d.price, d.pe_margin_pct,
          `EPS ${fmtNum(d.eps, 2)} × industry PE ${fmtNum(d.params && d.params.industry_pe, 2)}`)
      : makeFairValueTile('PE-Relative', null, null, null,
          'Set an industry PE in Advanced parameters');
    const dcfTile = makeFairValueTile('Two-stage DCF',
      d.dcf, d.price, d.dcf_margin_pct,
      `g₁=${fmtPct((d.params && d.params.dcf_g1 ?? 0.10) * 100)}, ` +
      `g₂=${fmtPct((d.params && d.params.dcf_g2 ?? 0.03) * 100)}, ` +
      `r=${fmtPct((d.params && d.params.dcf_r ?? 0.10) * 100)}`);

    const params = d.params || {};
    const showPe = params.industry_pe !== undefined && params.industry_pe !== null;
    const footnote = [
      d.queried_as && d.queried_as.toUpperCase() !== d.resolved_ticker
        ? `Resolved "${escapeHtml(d.queried_as)}" → ${escapeHtml(d.resolved_ticker)}`
        : '',
      showPe ? '' : 'PE-Relative hidden: no industry PE set',
      `DCF params: g₁=${fmtPct((params.dcf_g1 ?? 0.10) * 100)}, g₂=${fmtPct((params.dcf_g2 ?? 0.03) * 100)}, r=${fmtPct((params.dcf_r ?? 0.10) * 100)}`,
    ].filter(Boolean).join(' · ');

    resultPanel.innerHTML = `
      <div class="lookup-result-header">
        <div class="lookup-result-title">
          <span class="lookup-result-ticker">${escapeHtml(d.resolved_ticker || d.ticker)}</span>
          <span class="lookup-result-name">${escapeHtml(d.resolved_name || '')}</span>
        </div>
        <span class="lookup-result-price">₹${fmtNum(d.price, 2)}</span>
      </div>

      <div class="lookup-result-grid">
        ${grahamTile}
        ${peTile}
        ${dcfTile}
        <div class="lookup-tile">
          <div class="lookup-tile-label">Underlying</div>
          <div class="lookup-tile-meta">
            <div class="lookup-tile-meta-row"><span>EPS</span><span>₹${fmtNum(d.eps, 2)}</span></div>
            <div class="lookup-tile-meta-row"><span>BVPS</span><span>₹${fmtNum(d.book_value, 2)}</span></div>
            <div class="lookup-tile-meta-row"><span>FCF/Share</span><span>₹${fmtNum(d.fcf_per_share, 2)}</span></div>
            <div class="lookup-tile-meta-row"><span>Mkt Cap</span><span>₹${fmtNum(d.market_cap, 2)} Cr</span></div>
          </div>
        </div>
      </div>

      <div class="lookup-result-footnote">
        <span>${footnote}</span>
        <span class="text-muted">Source: screener.in</span>
      </div>`;
  }

  function makeFairValueTile(label, value, price, margin, sub) {
    const hasValue = value !== null && value !== undefined;
    const cls = hasValue ? '' : 'is-na';
    const display = hasValue ? '₹' + fmtNum(value, 2) : '—';
    let marginBadge = '';
    if (hasValue && price && margin !== undefined && margin !== null) {
      const positive = margin >= 0;
      marginBadge = `<div class="lookup-tile-sub">
        <span class="badge ${positive ? 'badge-positive' : 'badge-negative'}">
          ${fmtPct(margin)} vs market
        </span>
      </div>`;
    }
    return `<div class="lookup-tile ${cls}">
      <div class="lookup-tile-label">${escapeHtml(label)}</div>
      <div class="lookup-tile-value">${display}</div>
      ${marginBadge}
      <div class="lookup-tile-sub">${escapeHtml(sub || '')}</div>
    </div>`;
  }

  // ---------- event wiring ----------
  const onInput = debounce(() => {
    const q = input.value.trim();
    clearBtn.hidden = q.length === 0;
    if (q.length === 0) {
      hideSuggestions();
      return;
    }
    fetchSuggestions(q);
  }, 180);

  input.addEventListener('input', onInput);

  input.addEventListener('focus', () => {
    if (input.value.trim() && lastResults.length) {
      suggestions.hidden = false;
      input.setAttribute('aria-expanded', 'true');
    }
  });

  input.addEventListener('keydown', (e) => {
    const n = lastResults.length;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      highlightIdx = Math.min(highlightIdx + 1, n - 1);
      highlightSuggestion(highlightIdx);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      highlightIdx = Math.max(highlightIdx - 1, -1);
      highlightSuggestion(highlightIdx);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (highlightIdx >= 0) {
        pickSuggestion(highlightIdx);
      } else {
        hideSuggestions();
        runLookup(input.value.trim());
      }
    } else if (e.key === 'Escape') {
      hideSuggestions();
    }
  });

  // Click on suggestion
  suggestions.addEventListener('mousedown', (e) => {
    // mousedown so the input's blur doesn't close the list first
    const li = e.target.closest('.lookup-suggestion');
    if (!li) return;
    const idx = parseInt(li.getAttribute('data-idx'), 10);
    if (Number.isFinite(idx)) pickSuggestion(idx);
  });

  // Hover updates highlight
  suggestions.addEventListener('mousemove', (e) => {
    const li = e.target.closest('.lookup-suggestion');
    if (!li) return;
    const idx = parseInt(li.getAttribute('data-idx'), 10);
    if (Number.isFinite(idx) && idx !== highlightIdx) {
      highlightSuggestion(idx);
    }
  });

  // Click outside closes the dropdown
  document.addEventListener('click', (e) => {
    if (!suggestions.contains(e.target) && e.target !== input) {
      hideSuggestions();
    }
  });

  clearBtn.addEventListener('click', () => {
    input.value = '';
    clearBtn.hidden = true;
    hideSuggestions();
    resultPanel.hidden = true;
    resultPanel.innerHTML = '';
    input.focus();
  });

  // Re-run lookup when any advanced param changes (only if we already have a ticker)
  [paramIndustry, paramG1, paramG2, paramR].forEach((el) => {
    if (!el) return;
    el.addEventListener('change', () => {
      if (!resultPanel.hidden) {
        const tickerMatch = resultPanel.querySelector('.lookup-result-ticker');
        if (tickerMatch) runLookup(tickerMatch.textContent.trim());
      }
    });
  });
})();