/**
 * intraday.js — Today's intraday chart on the history page.
 *
 * Three buttons (15m / 5m / 1m) call /api/intraday?interval=... and
 * re-render the Plotly chart with the returned time series.
 *
 * Reality check: the API does real yfinance fetches under the hood
 * (with a 5-minute cache), so the first load for a given interval
 * takes 2-5 seconds. We surface that to the user with a status line
 * and a spinner button state.
 *
 * Errors are shown inline (no alert()). If yfinance returns empty
 * data (market closed, holiday, or 1m cache empty), we fall back to
 * "no data available" instead of crashing.
 *
 * Compatibility: ES5+ (no `??`, no optional chaining). The earlier
 * Safari bug taught us that lesson.
 */
(function () {
  "use strict";

  var COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
  ];

  function $(id) { return document.getElementById(id); }
  var statusEl = $("intraday-status");
  var chartEl = $("intraday-chart");
  if (!chartEl) return;  // history page not in DOM

  function setStatus(msg) {
    if (statusEl) statusEl.textContent = msg;
  }

  function setBusy(busy) {
    var btns = document.querySelectorAll(".intraday-interval");
    for (var i = 0; i < btns.length; i++) {
      btns[i].disabled = busy;
      if (busy) btns[i].setAttribute("aria-busy", "true");
      else btns[i].removeAttribute("aria-busy");
    }
  }

  function setActiveButton(interval) {
    var btns = document.querySelectorAll(".intraday-interval");
    for (var i = 0; i < btns.length; i++) {
      var btn = btns[i];
      var active = btn.getAttribute("data-interval") === interval;
      btn.classList.toggle("btn-primary", active);
      btn.classList.toggle("btn-secondary", !active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    }
  }

  function buildTraces(series) {
    var traces = [];
    var colorIdx = 0;
    // Plot indices first, portfolio last (on top, bold black)
    var portfolioKey = null;
    for (var key in series) {
      if (!series.hasOwnProperty(key)) continue;
      if (key === "My Portfolio") { portfolioKey = key; continue; }
      var pts = series[key] || [];
      if (pts.length === 0) continue;
      traces.push({
        type: "scatter",
        mode: "lines",
        name: key,
        x: pts.map(function (p) { return p.t; }),
        y: pts.map(function (p) { return p.v; }),
        line: { color: COLORS[colorIdx % COLORS.length], width: 2 },
        hovertemplate: "%{x|%H:%M}<br>" + key + ": %{y:.2f}<extra></extra>",
      });
      colorIdx += 1;
    }
    if (portfolioKey && series[portfolioKey] && series[portfolioKey].length > 0) {
      var pp = series[portfolioKey];
      traces.push({
        type: "scatter",
        mode: "lines",
        name: "My Portfolio (equity, mf, sgb)",
        x: pp.map(function (p) { return p.t; }),
        y: pp.map(function (p) { return p.v; }),
        line: { color: "#000000", width: 3.5 },
        hovertemplate: "%{x|%H:%M}<br>My Portfolio: %{y:.2f}<extra></extra>",
      });
    }
    return traces;
  }

  function renderEmpty(message) {
    chartEl.innerHTML =
      '<div class="empty-state" style="padding: 40px; text-align: center;">' +
      '<p class="text-muted">' + message + '</p>' +
      '</div>';
  }

  function renderChart(snap) {
    if (!snap || !snap.series || Object.keys(snap.series).length === 0) {
      renderEmpty("No intraday data available right now. " +
                  "The market may be closed or Yahoo Finance returned no data.");
      return;
    }
    var traces = buildTraces(snap.series);
    if (traces.length === 0) {
      renderEmpty("No intraday data available right now.");
      return;
    }
    var layout = {
      title: "My Portfolio vs World Indices — today (" + snap.interval + " bars, base 100 at open)",
      xaxis: {
        title: "Time (IST, UTC+05:30)",
        type: "date",
        tickformat: "%H:%M",
      },
      yaxis: { title: "Indexed value (open = 100)" },
      hovermode: "x unified",
      hoverlabel: { bgcolor: "#fff" },
      template: "plotly_white",
      legend: { orientation: "h", yanchor: "top", y: -0.15, xanchor: "center", x: 0.5 },
      margin: { l: 60, r: 20, t: 80, b: 120 },
    };
    var config = {
      responsive: true,
      displaylogo: false,
      modeBarButtonsToRemove: ["lasso2d", "select2d"],
    };
    if (typeof Plotly === "undefined") {
      renderEmpty("Chart library failed to load. Refresh the page.");
      return;
    }
    Plotly.react(chartEl, traces, layout, config);
  }

  function loadInterval(interval) {
    setActiveButton(interval);
    setStatus("Loading intraday data for " + interval + " candles…");
    setBusy(true);

    var url = "/api/intraday?interval=" + encodeURIComponent(interval);
    fetch(url, { headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (snap) {
        if (snap.error) throw new Error(snap.error);
        renderChart(snap);
        var asof = snap.asof ? " · as of " + snap.asof : "";
        var nSeries = Object.keys(snap.series || {}).length;
        setStatus("Showing " + nSeries + " series at " + interval + " interval" + asof);
      })
      .catch(function (err) {
        renderEmpty("Failed to load intraday data: " + err.message);
        setStatus("Error: " + err.message);
      })
      .then(function () { setBusy(false); });
  }

  function bindButtons() {
    var btns = document.querySelectorAll(".intraday-interval");
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener("click", function (e) {
        var interval = e.currentTarget.getAttribute("data-interval");
        loadInterval(interval);
      });
    }
  }

  // Auto-load the default interval on page load (5m is marked active)
  bindButtons();
  loadInterval("5m");
})();