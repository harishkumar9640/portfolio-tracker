// Tax & P&L dashboard — pie + bar chart with toggle, plus trades review

(function () {
  'use strict';

  // ===================================================================
  // CHART DATA (shared between pie and bar)
  // ===================================================================
  var chartData = [];
  var chartMode = 'pie';  // current mode: 'pie' or 'bar'
  var pieRoot = document.getElementById('big-pie');
  if (pieRoot) {
    var raw = pieRoot.getAttribute('data-pie');
    if (raw) {
      try { chartData = JSON.parse(raw); } catch (e) { chartData = []; }
    }
  }

  // ===================================================================
  // PIE CHART
  // ===================================================================
  function renderPieChart(pie, root) {
    if (!pie || pie.length === 0) {
      root.innerHTML = '<p class="text-muted" style="padding:2rem;">No data to display.</p>';
      return;
    }
    var W = 900, H = 520;
    var cx = W / 2, cy = H / 2 - 20;
    var rOuter = 180, rInner = 95;
    var total = 0;
    for (var i = 0; i < pie.length; i++) total += pie[i].value;
    if (total <= 0) return;

    var tooltip = document.createElement('div');
    tooltip.style.cssText = 'position:absolute;pointer-events:none;background:rgba(0,0,0,0.92);color:#fff;padding:8px 12px;border-radius:6px;font-size:13px;line-height:1.45;box-shadow:0 4px 12px rgba(0,0,0,0.3);z-index:1000;display:none;max-width:280px;';
    document.body.appendChild(tooltip);

    var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMidYMid meet" style="width:100%; height:100%; font-family:inherit; font-size:12px;">';

    // Legend
    var legendX = W - 230;
    svg += '<g transform="translate(' + legendX + ',30)">';
    svg += '<text x="0" y="0" font-size="13" font-weight="600" fill="#222">Legend</text>';
    for (var j = 0; j < pie.length; j++) {
      var item = pie[j];
      var ly = 22 + j * 22;
      var pct = (item.value / total * 100).toFixed(1);
      svg += '<rect x="0" y="' + (ly - 10) + '" width="14" height="14" fill="' + item.color + '" rx="2"/>';
      svg += '<text x="22" y="' + ly + '" fill="#333">' + item.label + '</text>';
      svg += '<text x="22" y="' + (ly + 14) + '" fill="#666" font-size="10">₹' + Math.round(item.value).toLocaleString('en-IN') + ' (' + pct + '%)</text>';
    }
    svg += '</g>';

    // Donut slices
    var angle = -Math.PI / 2;
    for (var k = 0; k < pie.length; k++) {
      var it2 = pie[k];
      var sweep = (it2.value / total) * Math.PI * 2;
      var endAngle = angle + sweep;
      var x1 = cx + rOuter * Math.cos(angle);
      var y1 = cy + rOuter * Math.sin(angle);
      var x2 = cx + rOuter * Math.cos(endAngle);
      var y2 = cy + rOuter * Math.sin(endAngle);
      var x3 = cx + rInner * Math.cos(endAngle);
      var y3 = cy + rInner * Math.sin(endAngle);
      var x4 = cx + rInner * Math.cos(angle);
      var y4 = cy + rInner * Math.sin(angle);
      var largeArc = sweep > Math.PI ? 1 : 0;
      var pathD = 'M ' + x1 + ' ' + y1 + ' A ' + rOuter + ' ' + rOuter + ' 0 ' + largeArc + ' 1 ' + x2 + ' ' + y2 + ' L ' + x3 + ' ' + y3 + ' A ' + rInner + ' ' + rInner + ' 0 ' + largeArc + ' 0 ' + x4 + ' ' + y4 + ' Z';
      var midAngle = angle + sweep / 2;
      var labelX = cx + ((rOuter + rInner) / 2) * Math.cos(midAngle);
      var labelY = cy + ((rOuter + rInner) / 2) * Math.sin(midAngle);
      var slicePct = (it2.value / total * 100).toFixed(1);
      svg += '<path d="' + pathD + '" fill="' + it2.color + '" stroke="#fff" stroke-width="2" data-idx="' + k + '" style="cursor:pointer; transition:opacity 0.15s;"/>';
      if (parseFloat(slicePct) > 4) {
        svg += '<text x="' + labelX + '" y="' + labelY + '" text-anchor="middle" dominant-baseline="middle" font-size="11" font-weight="600" fill="#fff" pointer-events="none">' + slicePct + '%</text>';
      }
      angle = endAngle;
    }

    // Centre label
    var netVal = 0;
    for (var m = 0; m < pie.length; m++) {
      var lbl = pie[m].label;
      if (lbl.indexOf('P&L') >= 0 || lbl.indexOf('Unrealised') >= 0) {
        netVal += pie[m].value * (pie[m].detail && pie[m].detail.indexOf('+') === 0 ? 1 : -1);
      }
    }
    var netStr = (netVal >= 0 ? '+' : '-') + '₹' + Math.abs(Math.round(netVal)).toLocaleString('en-IN');
    var netColor = netVal >= 0 ? '#2f7a3d' : '#b3382c';
    svg += '<text x="' + cx + '" y="' + (cy - 5) + '" text-anchor="middle" font-size="11" fill="#666" dominant-baseline="middle">NET</text>';
    svg += '<text x="' + cx + '" y="' + (cy + 12) + '" text-anchor="middle" font-size="20" font-weight="700" fill="' + netColor + '" dominant-baseline="middle">' + netStr + '</text>';
    svg += '</svg>';
    root.innerHTML = svg;

    // Hover
    function showPieTip(idx, evt) {
      var it3 = pie[idx];
      var pct = (it3.value / total * 100).toFixed(1);
      tooltip.innerHTML = '<div style="font-weight:600;margin-bottom:4px;">' + it3.label + '</div><div style="font-size:14px;">₹' + it3.value.toLocaleString('en-IN', {maximumFractionDigits: 0}) + '</div><div style="color:#aaa;font-size:11px;">' + pct + '% of total</div>' + (it3.detail ? '<div style="color:#ccc;margin-top:4px;font-size:11px;">' + it3.detail + '</div>' : '');
      tooltip.style.display = 'block';
      tooltip.style.left = (evt.pageX + 12) + 'px';
      tooltip.style.top = (evt.pageY + 12) + 'px';
    }
    var slices = root.querySelectorAll('path[data-idx]');
    for (var s = 0; s < slices.length; s++) {
      (function (i) {
        slices[i].addEventListener('mouseenter', function (e) {
          var idx = parseInt(this.getAttribute('data-idx'), 10);
          var all = root.querySelectorAll('path[data-idx]');
          for (var j = 0; j < all.length; j++) if (j !== idx) all[j].style.opacity = '0.3';
          showPieTip(idx, e);
        });
        slices[i].addEventListener('mousemove', function (e) {
          showPieTip(parseInt(this.getAttribute('data-idx'), 10), e);
        });
        slices[i].addEventListener('mouseleave', function () {
          var all = root.querySelectorAll('path[data-idx]');
          for (var j = 0; j < all.length; j++) all[j].style.opacity = '1';
          tooltip.style.display = 'none';
        });
      })(s);
    }
  }

  // ===================================================================
  // BAR CHART
  // ===================================================================
  function renderBarChart(pie, root) {
    if (!pie || pie.length === 0) {
      root.innerHTML = '<p class="text-muted" style="padding:2rem;">No data to display.</p>';
      return;
    }
    var W = 1100, H = 520;
    var padL = 200, padR = 250, padT = 30, padB = 50;
    var chartW = W - padL - padR;
    var chartH = H - padT - padB;
    var sorted = pie.slice().sort(function (a, b) { return b.value - a.value; });
    var total = 0;
    for (var i = 0; i < pie.length; i++) total += pie[i].value;
    if (total <= 0) return;

    var maxVal = 0;
    for (var j = 0; j < sorted.length; j++) if (sorted[j].value > maxVal) maxVal = sorted[j].value;
    var niceMax = Math.ceil(maxVal / 100000) * 100000;
    if (niceMax < maxVal) niceMax = maxVal;
    if (niceMax === 0) niceMax = 1;

    var tooltip = document.createElement('div');
    tooltip.style.cssText = 'position:absolute;pointer-events:none;background:rgba(0,0,0,0.92);color:#fff;padding:8px 12px;border-radius:6px;font-size:13px;line-height:1.45;box-shadow:0 4px 12px rgba(0,0,0,0.3);z-index:1000;display:none;max-width:280px;';
    document.body.appendChild(tooltip);

    var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMidYMid meet" style="width:100%; height:100%; font-family:inherit; font-size:12px;">';

    // Grid lines + x-axis labels
    for (var g = 0; g <= 4; g++) {
      var gx = padL + (chartW * g / 4);
      svg += '<line x1="' + gx + '" y1="' + padT + '" x2="' + gx + '" y2="' + (padT + chartH) + '" stroke="#e5e7eb" stroke-width="1" stroke-dasharray="3,3"/>';
      var gv = niceMax * (1 - g / 4);
      svg += '<text x="' + gx + '" y="' + (padT + chartH + 18) + '" text-anchor="middle" fill="#888" font-size="11">₹' + Math.round(gv).toLocaleString('en-IN') + '</text>';
    }

    // Axes
    svg += '<line x1="' + padL + '" y1="' + padT + '" x2="' + padL + '" y2="' + (padT + chartH) + '" stroke="#888" stroke-width="1"/>';
    svg += '<line x1="' + padL + '" y1="' + (padT + chartH) + '" x2="' + (W - padR) + '" y2="' + (padT + chartH) + '" stroke="#888" stroke-width="1"/>';

    // Bars
    var n = sorted.length;
    var barH = Math.min(28, (chartH - 20) / n);
    var gap = 4;
    for (var bi = 0; bi < n; bi++) {
      var item = sorted[bi];
      var by = padT + 10 + bi * (barH + gap);
      var bw = (item.value / niceMax) * chartW;
      svg += '<rect x="' + padL + '" y="' + by + '" width="' + bw + '" height="' + barH + '" fill="' + item.color + '" rx="2" data-idx="' + bi + '" style="cursor:pointer; transition:opacity 0.15s;"/>';
      var pct = (item.value / total * 100).toFixed(1);
      svg += '<text x="' + (padL - 8) + '" y="' + (by + barH / 2 + 4) + '" text-anchor="end" fill="#333" font-size="11" font-weight="600">' + item.label + '</text>';
      svg += '<text x="' + (padL + bw + 6) + '" y="' + (by + barH / 2 + 4) + '" fill="#333" font-size="11">₹' + Math.round(item.value).toLocaleString('en-IN') + ' (' + pct + '%)</text>';
    }

    // Title + axis label
    svg += '<text x="' + (W / 2) + '" y="20" text-anchor="middle" font-size="13" font-weight="600" fill="#222">Total: ₹' + Math.round(total).toLocaleString('en-IN') + '</text>';
    svg += '<text x="20" y="' + (padT + chartH / 2) + '" transform="rotate(-90 20 ' + (padT + chartH / 2) + ')" text-anchor="middle" fill="#666" font-size="11">value (₹)</text>';

    svg += '</svg>';
    root.innerHTML = svg;

    // Hover
    function showBarTip(idx, evt) {
      var it4 = sorted[idx];
      var pct = (it4.value / total * 100).toFixed(1);
      tooltip.innerHTML = '<div style="font-weight:600;margin-bottom:4px;">' + it4.label + '</div><div style="font-size:14px;">₹' + it4.value.toLocaleString('en-IN', {maximumFractionDigits: 0}) + '</div><div style="color:#aaa;font-size:11px;">' + pct + '% of total</div>' + (it4.detail ? '<div style="color:#ccc;margin-top:4px;font-size:11px;">' + it4.detail + '</div>' : '');
      tooltip.style.display = 'block';
      tooltip.style.left = (evt.pageX + 12) + 'px';
      tooltip.style.top = (evt.pageY + 12) + 'px';
    }
    var bars = root.querySelectorAll('rect[data-idx]');
    for (var bk = 0; bk < bars.length; bk++) {
      (function (bi) {
        bars[bi].addEventListener('mouseenter', function (e) {
          var idx = parseInt(this.getAttribute('data-idx'), 10);
          var all = root.querySelectorAll('rect[data-idx]');
          for (var j = 0; j < all.length; j++) if (j !== idx) all[j].style.opacity = '0.3';
          showBarTip(idx, e);
        });
        bars[bi].addEventListener('mousemove', function (e) {
          showBarTip(parseInt(this.getAttribute('data-idx'), 10), e);
        });
        bars[bi].addEventListener('mouseleave', function () {
          var all = root.querySelectorAll('rect[data-idx]');
          for (var j = 0; j < all.length; j++) all[j].style.opacity = '1';
          tooltip.style.display = 'none';
        });
      })(bk);
    }
  }

  // ===================================================================
  // TOGGLE BUTTONS
  // ===================================================================
  function setupChartToggle() {
    var toggleButtons = document.querySelectorAll('#chart-toggles button[data-chart]');
    for (var t = 0; t < toggleButtons.length; t++) {
      toggleButtons[t].addEventListener('click', function () {
        var mode = this.getAttribute('data-chart');
        if (mode === chartMode) return;
        chartMode = mode;
        for (var k = 0; k < toggleButtons.length; k++) {
          if (toggleButtons[k].getAttribute('data-chart') === mode) {
            toggleButtons[k].classList.add('is-active');
          } else {
            toggleButtons[k].classList.remove('is-active');
          }
        }
        if (mode === 'pie') {
          renderPieChart(chartData, pieRoot);
        } else {
          renderBarChart(chartData, pieRoot);
        }
      });
    }
  }

  // Initial render
  if (pieRoot && chartData.length > 0) {
    renderPieChart(chartData, pieRoot);
  }
  setupChartToggle();

  // ===================================================================
  // TRADES REVIEW (separate from the big pie/bar)
  // ===================================================================
  function fmtPct(p) {
    if (p === null || p === undefined) return '—';
    return (p >= 0 ? '+' : '') + p.toFixed(1) + '%';
  }
  function fmtRs(v) {
    if (v === null || v === undefined) return '—';
    return (v < 0 ? '−' : '') + '₹' + Math.abs(Math.round(v)).toLocaleString('en-IN');
  }

  function renderTradesSummary(data, root) {
    var s = data.summary;
    var v = s.verdicts || {};
    var totalPnlClass = s.total_pnl >= 0 ? 'is-positive' : 'is-negative';
    var bestHtml = s.best ? '<span class="is-positive">' + s.best.scrip + ' ' + fmtPct(s.best.pnl_pct) + ' (₹' + Math.round(s.best.pnl).toLocaleString('en-IN') + ')</span>' : '—';
    var worstHtml = s.worst ? '<span class="is-negative">' + s.worst.scrip + ' ' + fmtPct(s.worst.pnl_pct) + ' (₹' + Math.abs(Math.round(s.worst.pnl)).toLocaleString('en-IN') + ')</span>' : '—';
    var html = '<div class="grid grid-2 grid-4">'
      + '<div class="kpi"><div class="kpi-label">Total trades</div><div class="kpi-value">' + s.total_trades + '</div><div class="kpi-sub text-muted">across ' + (s.unique_stocks || 0) + ' unique stocks</div></div>'
      + '<div class="kpi"><div class="kpi-label">Total realised P&amp;L</div><div class="kpi-value ' + totalPnlClass + '">' + (s.total_pnl >= 0 ? '+' : '') + '₹' + Math.abs(Math.round(s.total_pnl)).toLocaleString('en-IN') + '</div><div class="kpi-sub text-muted">all closed positions</div></div>'
      + '<div class="kpi"><div class="kpi-label">Best sell</div><div class="kpi-value is-positive" style="font-size:1.1rem;">' + bestHtml + '</div><div class="kpi-sub text-muted">highest % profit</div></div>'
      + '<div class="kpi"><div class="kpi-label">Worst sell</div><div class="kpi-value is-negative" style="font-size:1.1rem;">' + worstHtml + '</div><div class="kpi-sub text-muted">largest % loss</div></div>'
      + '</div>'
      + '<div style="margin-top: 1rem; display: flex; gap: 0.5rem; flex-wrap: wrap;">'
      + (v['GREAT SELL']   ? '<span class="market-status-badge" style="background:#2f7a3d22;color:#2f7a3d;">GREAT: ' + v['GREAT SELL'] + '</span>' : '')
      + (v['GOOD SELL']    ? '<span class="market-status-badge" style="background:#5cb85c22;color:#2f7a3d;">GOOD: ' + v['GOOD SELL'] + '</span>' : '')
      + (v['OK SELL']      ? '<span class="market-status-badge" style="background:#88888822;color:#666;">OK: ' + v['OK SELL'] + '</span>' : '')
      + (v['BAD TIMING']   ? '<span class="market-status-badge" style="background:#d5851222;color:#d58512;">BAD TIMING: ' + v['BAD TIMING'] + '</span>' : '')
      + (v['POOR SELL']    ? '<span class="market-status-badge" style="background:#b3382c22;color:#b3382c;">POOR: ' + v['POOR SELL'] + '</span>' : '')
      + (v['TERRIBLE SELL']? '<span class="market-status-badge" style="background:#7a131322;color:#7a1313;">TERRIBLE: ' + v['TERRIBLE SELL'] + '</span>' : '')
      + '</div>';
    root.innerHTML = html;
  }

  function renderSideBySide(byStock, container) {
    if (!byStock || byStock.length === 0) {
      container.innerHTML = '<p class="text-muted">No trades to display.</p>';
      return;
    }
    var html = '<table class="data-table" style="margin: 0;">';
    html += '<thead style="position: sticky; top: 0; background: #f8f9fa; z-index: 1;">'
      + '<tr>'
      + '<th style="min-width:130px;">Stock</th>'
      + '<th style="min-width:80px;">Trades</th>'
      + '<th style="min-width:100px;">First buy</th>'
      + '<th style="min-width:100px;">Last sell</th>'
      + '<th class="text-right" style="background:#fce4ec;">Avg Buy ₹</th>'
      + '<th style="width:30px;background:#fce4ec;"></th>'
      + '<th class="text-right" style="background:#e8f5e9;">Avg Sell ₹</th>'
      + '<th style="width:30px;background:#e3f2fd;"></th>'
      + '<th class="text-right" style="background:#e3f2fd;">Now ₹</th>'
      + '<th class="text-right" style="background:#fff8e1;">If held</th>'
      + '<th class="text-right">P&amp;L</th>'
      + '<th>Verdict</th>'
      + '</tr></thead><tbody>';
    for (var i = 0; i < byStock.length; i++) {
      var a = byStock[i];
      var avgBuy = a.wavg_buy || 0;
      var avgSell = a.wavg_sell || 0;
      var curLtp = a.cur_ltp;
      var pnl = a.total_pnl || 0;
      var pnlPct = a.pnl_pct || 0;
      var hypPnl = a.hypothetical_pnl;
      var hypPct = a.hypothetical_pct;
      var heldArrow = '<span class="text-muted">—</span>';
      if (hypPnl !== null && hypPnl !== undefined) {
        if (hypPnl > 0) {
          heldArrow = '<span style="color:#2f7a3d;font-weight:600;">▲ +₹' + Math.round(hypPnl).toLocaleString('en-IN') + (hypPct !== null ? ' (' + (hypPct >= 0 ? '+' : '') + hypPct.toFixed(0) + '%)' : '') + '</span>';
        } else if (hypPnl < 0) {
          heldArrow = '<span style="color:#b3382c;font-weight:600;">▼ −₹' + Math.abs(Math.round(hypPnl)).toLocaleString('en-IN') + (hypPct !== null ? ' (' + hypPct.toFixed(0) + '%)' : '') + '</span>';
        } else {
          heldArrow = '<span style="color:#888;">±₹0</span>';
        }
      }
      var curLtpHtml = curLtp ? '₹' + curLtp.toFixed(2) : '<span class="text-muted">N/A</span>';
      var pnlColor = pnl >= 0 ? '#2f7a3d' : '#b3382c';
      var pnlSign = pnl >= 0 ? '+' : '−';
      var pnlHtml = '<span style="color:' + pnlColor + ';font-weight:600;">' + pnlSign + '₹' + Math.abs(Math.round(pnl)).toLocaleString('en-IN') + ' (' + (pnlPct >= 0 ? '+' : '') + pnlPct.toFixed(0) + '%)</span>';
      var verdictStyle = 'background:' + a.verdict_color + '22;color:' + a.verdict_color + ';font-size:0.7rem;padding:0.15rem 0.4rem;border-radius:4px;font-weight:600;white-space:nowrap;';
      var noteBadge = a.note ? ' <span title="' + a.note + '" style="cursor:help;color:#d58512;">⚠</span>' : '';
      html += '<tr>'
        + '<td><strong>' + a.scrip + '</strong>' + noteBadge + '</td>'
        + '<td style="font-size:0.85rem;text-align:center;">' + a.trade_count + '</td>'
        + '<td style="font-size:0.85rem;">' + a.first_buy_date + '</td>'
        + '<td style="font-size:0.85rem;">' + a.last_sell_date + '</td>'
        + '<td class="text-right" style="background:#fce4ec30;">' + (avgBuy ? '₹' + avgBuy.toFixed(2) : '—') + '</td>'
        + '<td style="text-align:center;background:#fce4ec30;color:#999;">→</td>'
        + '<td class="text-right" style="background:#e8f5e930;font-weight:600;">' + (avgSell ? '₹' + avgSell.toFixed(2) : '—') + '</td>'
        + '<td style="text-align:center;background:#e3f2fd30;color:#999;">→</td>'
        + '<td class="text-right" style="background:#e3f2fd30;font-weight:600;">' + curLtpHtml + '</td>'
        + '<td class="text-right" style="background:#fff8e130;">' + heldArrow + '</td>'
        + '<td class="text-right">' + pnlHtml + '</td>'
        + '<td><span style="' + verdictStyle + '">' + a.verdict + '</span></td>'
        + '</tr>';
    }
    html += '</tbody></table>';
    container.innerHTML = html;
  }

  function renderTradesTable(trades, table) {
    var tbody = table.querySelector('tbody');
    var html = '';
    for (var i = 0; i < trades.length; i++) {
      var t = trades[i];
      var pnlClass = (t.pnl || 0) >= 0 ? 'is-positive' : 'is-negative';
      var curLtp = t.cur_ltp ? '₹' + t.cur_ltp.toFixed(2) : '—';
      var noteBadge = '';
      if (t.scrip === 'TATAMOTORS' || t.scrip === 'ZOMATO') {
        noteBadge = ' <span class="market-status-badge" style="background:#8882;color:#666;font-size:0.7rem;padding:0.1rem 0.3rem;cursor:help;" title="' + (t.scrip === 'TATAMOTORS' ? 'TATAMOTORS DVR demerged in 2024-25. Now trades as Tata Motors Passenger Vehicles Ltd (NSE: TMPV). ₹345 is TMPV LTP.' : 'Zomato Ltd rebranded to Eternal Ltd in Mar 2025. ₹259 is ETERNAL LTP.') + '">⚠</span>';
      }
      var hypo = t.hypothetical_pnl;
      var hypoHtml = '—';
      if (hypo !== null && hypo !== undefined) {
        var hypoClass = hypo >= 0 ? 'is-positive' : 'is-negative';
        hypoHtml = '<span class="' + hypoClass + '">' + (hypo >= 0 ? '+' : '') + '₹' + Math.abs(Math.round(hypo)).toLocaleString('en-IN') + ' (' + fmtPct(t.hypothetical_pct) + ')</span>';
      }
      var verdictBadge = '<span class="market-status-badge" style="background:' + t.verdict_color + '22;color:' + t.verdict_color + ';">' + t.verdict + '</span>';
      html += '<tr>'
        + '<td><strong>' + t.scrip + '</strong>' + noteBadge + '</td>'
        + '<td>' + (t.buy_date || '—') + '</td>'
        + '<td class="text-right">' + (t.avg_buy ? '₹' + t.avg_buy.toFixed(2) : '—') + '</td>'
        + '<td class="text-right">' + t.qty + '</td>'
        + '<td>' + (t.date || '—') + '</td>'
        + '<td class="text-right">' + (t.avg_sell ? '₹' + t.avg_sell.toFixed(2) : '—') + '</td>'
        + '<td class="text-right">₹' + Math.round(t.sell_val).toLocaleString('en-IN') + '</td>'
        + '<td class="text-right">' + curLtp + '</td>'
        + '<td class="text-right ' + pnlClass + '">' + (t.pnl >= 0 ? '+' : '') + '₹' + Math.abs(Math.round(t.pnl)).toLocaleString('en-IN') + ' (' + fmtPct(t.pnl_pct) + ')</td>'
        + '<td>' + verdictBadge + '</td>'
        + '<td class="text-right">' + hypoHtml + '</td>'
        + '</tr>';
    }
    tbody.innerHTML = html;
  }

  // Load trades from API
  var tradesSummary = document.getElementById('trades-summary');
  var tradesTable = document.getElementById('trades-table');
  if (tradesSummary) {
    fetch('/api/tax/trades')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) {
          tradesSummary.innerHTML = '<p class="text-muted">Could not load trades.</p>';
          return;
        }
        renderTradesSummary(data, tradesSummary);
        if (tradesTable) renderTradesTable(data.trades, tradesTable);
        var sxs = document.getElementById('side-by-side');
        if (sxs) renderSideBySide(data.by_stock || [], sxs);
      })
      .catch(function (e) {
        tradesSummary.innerHTML = '<p class="text-muted">Error: ' + e + '</p>';
      });
  }

  // ===================================================================
  // REFRESH BUTTON (re-parse Tax PNL files)
  // ===================================================================
  var refreshBtn = document.getElementById('refreshTaxBtn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', function () {
      refreshBtn.disabled = true;
      var orig = refreshBtn.textContent;
      refreshBtn.textContent = '⏳ Re-parsing…';
      fetch('/api/tax', { method: 'GET' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (d) {
            refreshBtn.textContent = '✓ Done — reloading';
            setTimeout(function () { location.reload(); }, 600);
          } else {
            refreshBtn.disabled = false;
            refreshBtn.textContent = orig;
          }
        })
        .catch(function () {
          refreshBtn.disabled = false;
          refreshBtn.textContent = orig;
        });
    });
  }
})();