// Fair-value lookup: debounced autocomplete + on-demand calculation.
// Loaded only on the /fairvalue page.

(function () {
  'use strict';

  const input        = document.getElementById('lookupInput');
  const clearBtn     = document.getElementById('lookupClear');
  const submitBtn    = document.getElementById('lookupSubmit');
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

  // Show a placeholder so the user knows what to do.
  showPlaceholder();

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

  function showPlaceholder() {
    // Inline placeholder shown before the user searches. The modal
    // opens only when a value has actually been computed.
    input.classList.remove("is-loading");
    resultPanel.classList.remove("is-error");
    resultPanel.innerHTML = `
      <p class="text-muted mb-0">
        Enter a ticker, company name, or ISIN above and click
        <strong>Calculate fair value</strong> (or press <kbd>Enter</kbd>).
        Examples: <code>RELIANCE</code>, <code>Infosys Limited</code>,
        <code>INE009A01021</code>.
      </p>`;
  }

  function openModal() {
    const m = document.getElementById("resultModal");
    if (!m) return;
    m.hidden = false;
    document.body.classList.add("modal-open");
    // Move focus to the close button for keyboard users.
    const close = m.querySelector(".modal-close");
    if (close) close.focus();
  }

  function closeModal() {
    const m = document.getElementById("resultModal");
    if (!m) return;
    m.hidden = true;
    document.body.classList.remove("modal-open");
    // Return focus to the search input so the next search is easy.
    if (input) input.focus();
  }

  function showLoading(ticker) {
    input.classList.add("is-loading");
    renderModal({
      ticker,
      isError: false,
      headerHTML: `
        <div class="modal-ticker">${escapeHtml(ticker)}</div>
        <div class="modal-company text-muted">Fetching from screener.in…</div>`,
      priceHTML: `<span class="modal-price text-muted">…</span>`,
      bodyHTML: `
        <p class="text-muted text-sm">Looking up fundamentals from screener.in.
        This usually takes 1–3 seconds.</p>`,
      footnote: "Source: screener.in",
    });
    openModal();
  }

  function showError(msg, ticker) {
    input.classList.remove("is-loading");
    renderModal({
      ticker: ticker || "?",
      isError: true,
      headerHTML: `
        <div class="modal-ticker">${escapeHtml(ticker || "?")}</div>
        <div class="modal-company text-neg">⚠ ${escapeHtml(msg)}</div>`,
      priceHTML: "",
      bodyHTML: `
        <p>If this is a recently listed stock or an unusual ticker,
        screener.in may not have data for it yet. You can also set
        <em>manual_price_per_g</em> for SGBs in <code>sgbs.json</code>.</p>`,
      footnote: "Source: screener.in",
    });
    openModal();
  }

  function renderResult(d) {
    input.classList.remove("is-loading");

    // Only Two-stage DCF is surfaced as the primary recommendation in
    // the UI (per user feedback: most stocks' fair values are closest
    // to this method). Graham and PE-Relative are still computed by
    // the backend and exposed under d.other_methods for callers that
    // want them; the modal shows them in a collapsible "Other methods"
    // panel so the user can sanity-check DCF if they want.
    const params = d.params || {};
    const g1 = (params.dcf_g1 !== undefined ? params.dcf_g1 : 0.10);
    const g2 = (params.dcf_g2 !== undefined ? params.dcf_g2 : 0.03);
    const r  = (params.dcf_r  !== undefined ? params.dcf_r  : 0.10);

    // DCF is the headline tile. The tile shows the fair value, the
    // margin vs market price, and the parameter summary.
    const dcfTile = makeFairValueTile('Two-stage DCF',
      d.dcf, d.price, d.dcf_margin_pct,
      `g₁=${fmtPct(g1 * 100)}, g₂=${fmtPct(g2 * 100)}, r=${fmtPct(r * 100)}`);

    // Build the math breakdown section if the API returned one.
    const mathSection = buildMathSection(d, g1, g2, r);

    // Build the "other methods" collapsible if the API returned them.
    const otherMethodsSection = buildOtherMethodsSection(d);

    const showPe = params.industry_pe !== undefined && params.industry_pe !== null;
    const footnote = [
      d.queried_as && d.queried_as.toUpperCase() !== d.resolved_ticker
        ? `Resolved "${escapeHtml(d.queried_as)}" → ${escapeHtml(d.resolved_ticker)}`
        : '',
      `DCF params: g₁=${fmtPct(g1 * 100)}, g₂=${fmtPct(g2 * 100)}, r=${fmtPct(r * 100)}`,
      showPe ? `PE-Relative uses industry PE = ${fmtNum(params.industry_pe, 1)}` : '',
    ].filter(Boolean).join(' · ');

    renderModal({
      ticker: d.resolved_ticker || d.ticker,
      isError: false,
      headerHTML: `
        <div class="modal-ticker">${escapeHtml(d.resolved_ticker || d.ticker)}</div>
        <div class="modal-company">${escapeHtml(d.resolved_name || "")}</div>`,
      priceHTML: `<div class="modal-price">₹${fmtNum(d.price, 2)}</div>`,
      bodyHTML: `
        <div class="lookup-tabs" role="tablist">
          <button type="button" class="lookup-tab is-active" role="tab"
                  data-tab="summary" aria-selected="true">Summary</button>
          <button type="button" class="lookup-tab" role="tab"
                  data-tab="calc" aria-selected="false">Calculation</button>
          <button type="button" class="lookup-tab" role="tab"
                  data-tab="other" aria-selected="false">Other methods</button>
        </div>

        <div class="lookup-tab-panel is-active" data-panel="summary">
          <div class="lookup-result-grid">
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
          <p class="text-muted text-sm mt-3 mb-0">
            Tap <strong>Calculation</strong> above to see the full Two-stage DCF
            worked example (variables, year-by-year projection, terminal value,
            reality check).
          </p>
        </div>

        <div class="lookup-tab-panel" data-panel="calc">
          ${mathSection}
        </div>

        <div class="lookup-tab-panel" data-panel="other">
          ${otherMethodsSection || '<p class="text-muted text-sm">No other methods available for this ticker.</p>'}
        </div>`,
      footnote,
      closeButtonLabel: "Close",
    });
    openModal();
    bindTabHandlers();
  }

  /**
   * bindTabHandlers() wires the Summary / Calculation / Other methods
   * tabs in the modal. Uses event delegation on .modal so it survives
   * re-renders. ES5+ compatible.
   */
  function bindTabHandlers() {
    var modal = document.getElementById("resultModal");
    if (!modal || modal._tabsBound) return;
    modal._tabsBound = true;
    modal.addEventListener("click", function (e) {
      var btn = e.target.closest && e.target.closest(".lookup-tab");
      if (!btn) return;
      var tabName = btn.getAttribute("data-tab");
      if (!tabName) return;
      // Update button states
      var tabs = modal.querySelectorAll(".lookup-tab");
      for (var i = 0; i < tabs.length; i++) {
        var isActive = tabs[i] === btn;
        tabs[i].classList.toggle("is-active", isActive);
        tabs[i].setAttribute("aria-selected", isActive ? "true" : "false");
      }
      // Update panels
      var panels = modal.querySelectorAll(".lookup-tab-panel");
      for (var j = 0; j < panels.length; j++) {
        var p = panels[j];
        var match = p.getAttribute("data-panel") === tabName;
        p.classList.toggle("is-active", match);
      }
    });
  }

  /**
   * buildMathSection(d, g1, g2, r) renders the worked-example DCF
   * math as an always-visible section. Includes:
   *   - Variable definitions table (FCF, g₁, g₂, r, PV, TV, ...)
   *   - Abbreviations glossary (DCF, FCF, EPS, BVPS, PV, TV, NPV, ...)
   *   - Year-by-year FCF projection with discount factors
   *   - Terminal value derivation
   *   - Step-by-step math write-up
   *   - "Reality check" note showing terminal-value dominance
   */
  function buildMathSection(d, g1, g2, r) {
    const bd = d.dcf_breakdown;
    if (!bd || !bd.years || bd.years.length === 0) {
      return '<p class="text-muted text-sm mt-3">DCF breakdown unavailable (insufficient FCF data).</p>';
    }

    const inputs = bd.inputs || {};
    const totals = bd.totals || {};
    const term   = bd.terminal || {};

    // Variable definitions table: the math symbols used in the steps.
    const variableRows = [
      { sym: 'FCF₀',  name: 'Current FCF per share',
        detail: "Free cash flow the company generated per share in the most recent year (₹" + fmtNum(inputs.fcf_per_share, 2) + " for " + escapeHtml(d.resolved_ticker || d.ticker) + ")." },
      { sym: 'g₁',    name: 'Stage-1 growth rate',
        detail: "Annual growth rate applied to FCF for years 1–" + (inputs.years || 5) + ". Default " + fmtPct((inputs.g1 || 0) * 100) + "." },
      { sym: 'g₂',    name: 'Terminal (perpetual) growth rate',
        detail: "Constant growth rate applied from year " + ((inputs.years || 5) + 1) + " onward, forever. Default " + fmtPct((inputs.g2 || 0) * 100) + " (≈ Indian long-run inflation)." },
      { sym: 'r',     name: 'Discount rate (cost of equity)',
        detail: "Rate used to convert future cash flows to today's value. Default " + fmtPct((inputs.r || 0) * 100) + " (≈ 1-year SBI FD + equity risk premium)." },
      { sym: 'N',     name: 'Stage-1 length (years)',
        detail: "Number of years in the high-growth stage. Default " + (inputs.years || 5) + "." },
      { sym: 'FCFₜ',  name: 'Projected FCF in year t',
        detail: "FCF₀ × (1 + g₁)^t — what FCF would be in year t if growth continues." },
      { sym: 'PV',    name: 'Present value',
        detail: "The value in today's rupees of a cash flow expected in the future, after discounting." },
      { sym: 'TV',    name: 'Terminal value',
        detail: "Value at year " + (inputs.years || 5) + " of all cash flows from year " + ((inputs.years || 5) + 1) + " onward, growing at g₂ forever." },
      { sym: 'DCF',   name: 'Discounted Cash Flow (intrinsic value)',
        detail: "Sum of PV(stage 1) + PV(terminal) — the model's fair value per share." },
    ];

    // Abbreviations glossary
    const abbrevRows = [
      { abbr: 'DCF',  full: 'Discounted Cash Flow',  note: 'A valuation method that converts future cash flows to present value.' },
      { abbr: 'FCF',  full: 'Free Cash Flow',         note: 'Cash a company generates after capital expenditures. "FCF per share" = FCF ÷ shares outstanding.' },
      { abbr: 'EPS',  full: 'Earnings Per Share',     note: "Company's net profit divided by shares outstanding. NOT used directly by Two-stage DCF, but shown for context." },
      { abbr: 'BVPS', full: 'Book Value Per Share',   note: "Net asset value per share from the balance sheet. NOT used by Two-stage DCF." },
      { abbr: 'PV',   full: 'Present Value',          note: "Today's value of a future cash flow after discounting at rate r." },
      { abbr: 'TV',   full: 'Terminal Value',         note: 'Value of all cash flows beyond the explicit forecast horizon.' },
      { abbr: 'NPV',  full: 'Net Present Value',      note: "Sum of PVs of all cash flows (positive and negative). Same as DCF here since FCF > 0." },
      { abbr: 'Cr',   full: 'Crore',                  note: '10 million. Used in Indian financial reporting (1 Cr = 10,000,000).' },
    ];

    // Year-by-year table rows
    const yearRows = bd.years.map(function (yr) {
      return (
        '<tr>' +
          '<td>' + yr.year + '</td>' +
          '<td>₹' + fmtNum(yr.projected_fcf, 2) + '</td>' +
          '<td>' + fmtNum(yr.discount_factor, 4) + '</td>' +
          '<td>₹' + fmtNum(yr.present_value, 2) + '</td>' +
        '</tr>'
      );
    }).join('');

    // Step-by-step math (text version, easy to read)
    const stepText = (bd.step_math || '').split('\n').map(function (line) {
      return line.trim() ? '<li>' + escapeHtml(line) + '</li>' : '';
    }).join('');

    const terminalPct = (totals.terminal_pct || 0) * 100;
    const realityCheckClass = terminalPct > 80 ? 'text-neg' : (terminalPct > 60 ? 'text-warn' : 'text-pos');
    const realityCheckText = terminalPct > 80
      ? 'Most of the DCF value comes from the terminal-value assumption, not from real cash flow growth. Be skeptical: a small change in r or g₂ will swing the DCF by a large amount.'
      : (terminalPct > 60
        ? 'A majority of the DCF value comes from the terminal-value assumption. The model is moderately sensitive to the choice of r and g₂.'
        : 'Most of the DCF value comes from explicit FCF projections. The terminal value is a smaller contribution, so the model is more robust to small changes in r or g₂.');

    // Math section is rendered visible by default (no <details> toggle).
    // Earlier we wrapped it in <details open>, but the user couldn't
    // tell it was a clickable thing — clicking the header actually CLOSED
    // it, which felt like a bug. Now the math is always visible and the
    // user can scroll through it.
    return (
      '<section class="lookup-math mt-4">' +
        '<header class="lookup-math-header">' +
          '<strong>Calculation</strong> ' +
          '<span class="text-muted text-sm">— Two-stage DCF worked example</span>' +
        '</header>' +
        '<div class="lookup-math-body">' +

          // ----- Variable definitions -----
          '<h4 class="lookup-math-h">Variables used</h4>' +
          '<table class="lookup-math-table">' +
            '<thead><tr><th>Symbol</th><th>Meaning</th></tr></thead>' +
            '<tbody>' +
              variableRows.map(function (v) {
                return (
                  '<tr>' +
                    '<td><code>' + escapeHtml(v.sym) + '</code></td>' +
                    '<td><strong>' + escapeHtml(v.name) + '</strong><br>' +
                      '<span class="text-muted text-xs">' + escapeHtml(v.detail) + '</span></td>' +
                  '</tr>'
                );
              }).join('') +
            '</tbody>' +
          '</table>' +

          // ----- Step-by-step math -----
          '<h4 class="lookup-math-h">Step-by-step math</h4>' +
          '<ol class="lookup-math-steps">' + stepText + '</ol>' +

          // ----- Year-by-year breakdown table -----
          '<h4 class="lookup-math-h">Year-by-year projection</h4>' +
          '<table class="lookup-math-table">' +
            '<thead><tr><th>Year (t)</th><th>FCFₜ</th><th>Discount 1/(1+r)^t</th><th>PV</th></tr></thead>' +
            '<tbody>' + yearRows +
              '<tr class="lookup-math-subtotal">' +
                '<td colspan="3"><strong>PV of stage 1 (sum)</strong></td>' +
                '<td><strong>₹' + fmtNum(totals.pv_stage1, 2) + '</strong></td>' +
              '</tr>' +
            '</tbody>' +
          '</table>' +

          // ----- Terminal value -----
          '<h4 class="lookup-math-h">Terminal value</h4>' +
          '<p class="lookup-math-formula">' + escapeHtml(term.formula || '') + '</p>' +
          '<table class="lookup-math-table">' +
            '<tr><td>Terminal value (year ' + (inputs.years || 5) + ')</td>' +
              '<td>₹' + fmtNum(term.terminal_value, 2) + '</td></tr>' +
            '<tr><td>PV of terminal value</td>' +
              '<td><strong>₹' + fmtNum(term.present_value, 2) + '</strong></td></tr>' +
          '</table>' +

          // ----- Reality check -----
          '<div class="lookup-reality-check ' + realityCheckClass + '">' +
            '<strong>Reality check:</strong> terminal value contributes <strong>' +
              fmtPct(terminalPct) + '</strong> of the total DCF (₹' +
              fmtNum(totals.pv_terminal, 2) + ' of ₹' + fmtNum(totals.dcf, 2) + '). ' +
              realityCheckText +
          '</div>' +

          // ----- Abbreviations glossary -----
          '<h4 class="lookup-math-h">Abbreviations</h4>' +
          '<table class="lookup-math-table">' +
            '<thead><tr><th>Abbr.</th><th>Full form</th><th>Note</th></tr></thead>' +
            '<tbody>' +
              abbrevRows.map(function (a) {
                return (
                  '<tr>' +
                    '<td><code>' + escapeHtml(a.abbr) + '</code></td>' +
                    '<td>' + escapeHtml(a.full) + '</td>' +
                    '<td class="text-muted text-xs">' + escapeHtml(a.note) + '</td>' +
                  '</tr>'
                );
              }).join('') +
            '</tbody>' +
          '</table>' +

        '</div>' +
      '</section>'
    );
  }

  /**
   * buildOtherMethodsSection(d) renders a collapsible showing the
   * Graham Number and PE-Relative results, which the API still
   * returns under d.other_methods. Hidden by default so DCF stays
   * the headline.
   */
  function buildOtherMethodsSection(d) {
    const other = d.other_methods || {};
    const rows = [];
    if (other.graham && other.graham.value) {
      rows.push(
        '<tr>' +
          '<td>Graham Number</td>' +
          '<td>₹' + fmtNum(other.graham.value, 2) + '</td>' +
          '<td class="text-muted text-xs">' + escapeHtml(other.graham.formula || '') + '</td>' +
        '</tr>'
      );
    }
    if (other.pe_relative && other.pe_relative.value) {
      rows.push(
        '<tr>' +
          '<td>PE-Relative</td>' +
          '<td>₹' + fmtNum(other.pe_relative.value, 2) + '</td>' +
          '<td class="text-muted text-xs">' + escapeHtml(other.pe_relative.formula || '') + '</td>' +
        '</tr>'
      );
    }
    if (rows.length === 0) return '';

    return (
      '<details class="lookup-other-methods mt-3">' +
        '<summary class="lookup-math-summary">' +
          '<strong>Other methods</strong> (Graham, PE-Relative) — for sanity check' +
        '</summary>' +
        '<table class="lookup-math-table">' +
          '<thead><tr><th>Method</th><th>Fair value</th><th>Formula</th></tr></thead>' +
          '<tbody>' + rows.join('') + '</tbody>' +
        '</table>' +
      '</details>'
    );
  }

  /**
   * renderModal({ticker, isError, headerHTML, priceHTML, bodyHTML,
   *              footnote, closeButtonLabel}) populates the resultModal
   * element with a consistent dialog layout. Centralising this keeps
   * showLoading/showError/renderResult concise.
   */
  function renderModal(opts) {
    const m = document.getElementById("resultModal");
    if (!m) return;
    m.classList.toggle("is-error", !!opts.isError);
    m.innerHTML = `
      <div class="modal-dialog"
           role="document"
           aria-labelledby="modalTitle">
        <div class="modal-header">
          <div class="modal-title-block">
            <h2 id="modalTitle" class="modal-ticker">${escapeHtml(opts.ticker || "")}</h2>
            ${opts.headerHTML || ""}
          </div>
          ${opts.priceHTML || ""}
          <button type="button" class="modal-close"
                  aria-label="${escapeHtml(opts.closeButtonLabel || "Close dialog")}">
            ×
          </button>
        </div>
        <div id="modalDesc" class="modal-body">
          ${opts.bodyHTML || ""}
        </div>
        <div class="modal-footer">
          <span class="text-muted text-xs">${escapeHtml(opts.footnote || "Source: screener.in")}</span>
          <button type="button" class="btn btn-secondary modal-close-btn">
            ${escapeHtml(opts.closeButtonLabel || "Close")}
          </button>
        </div>
      </div>`;
    // Wire the close buttons (re-bound each render — they only listen
    // for the lifetime of this dialog).
    m.querySelectorAll(".modal-close, .modal-close-btn").forEach((btn) => {
      btn.addEventListener("click", closeModal);
    });
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
    // Click on the modal backdrop (but not the dialog itself) closes
    // the modal. The dialog has class .modal-dialog; everything else
    // inside the modal-backdrop is fair game.
    const m = document.getElementById('resultModal');
    if (m && !m.hidden && e.target === m) {
      closeModal();
    }
  });

  // Escape closes the modal
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const m = document.getElementById('resultModal');
      if (m && !m.hidden) {
        e.preventDefault();
        closeModal();
      }
    }
  });

  clearBtn.addEventListener('click', () => {
    input.value = '';
    clearBtn.hidden = true;
    hideSuggestions();
    showPlaceholder();
    input.focus();
  });

  // Submit button: run the lookup on whatever's in the input.
  if (submitBtn) {
    submitBtn.addEventListener('click', () => {
      hideSuggestions();
      const q = input.value.trim();
      if (q) runLookup(q);
    });
  }

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