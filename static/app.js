/* Dashboard: watchlist rail + Lightweight Charts price, ADX and optional MACD panes. */
(() => {
  "use strict";
  const LWC = window.LightweightCharts;
  const $ = (id) => document.getElementById(id);
  // Grouped thousands everywhere a number is shown: 1039.79 reads as
  // 1,039.79. Form fields keep plain values (toFixed) so what is typed back
  // is still a number.
  const fmt = (n, d = 2) => (n == null || Number.isNaN(n) ? "—"
    : Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));

  let states = [];
  let selected = null;
  let priceChart, adxChart, candleSeries, stUp, stDown, smaSeries, adxSeries, adxGuide;
  let volumeSeries, earnMarks = [], earnNext = null;
  let pnlChart = null, pctChart = null, pnlSeries, pctSeries, pctClosedSeries;
  let defaultCommission = 0;
  let equityChart, equitySeries = {};
  let view = "chart";

  // Most filtering first, matching the dropdown and the backend.
  const PROFILE_ORDER = ["full", "adx_rising", "adx_only", "regime", "none"];

  const CURVE = { none: "#7d8f9e", regime: "#5fbf6a", adx_rising: "#b07cd9", adx_only: "#4a90d9", full: "#2bb6a3", bh: "#d2a03c" };

  // One range control drives both views, so the chart and the backtest
  // always cover the same window.
  // US markets trade roughly 252 days a year. Using 260 made every range
  // reach further back than its label claimed - "5 years" was 5.04.
  const RANGES = {
    "6M":  { bars: 126,  years: 0.5 },
    "1Y":  { bars: 252,  years: 1 },
    "2Y":  { bars: 504,  years: 2 },
    "5Y":  { bars: 1260, years: 5 },
    "MAX": { bars: 5000, years: 20 },
  };
  // A walk-forward split needs enough bars on both sides to mean anything.
  const MIN_TUNE_BARS = 400;
  const range = () => RANGES[$("range").value] || RANGES["5Y"];

  // Always fetch everything stored. Range then controls what is shown, so
  // scrolling back in time reveals loaded bars rather than empty space.
  const MAX_BARS = 5000;

  // Track what each view is currently showing so we never redraw needlessly
  // and never leave a stale chart behind a tab.
  let chartFor = null, testFor = null, tuneFor = null;
  let posOpen = false;
  // MACD is display only and off by default; its chart is built on first open.
  let macdOpen = false, macdChart = null, macdLine, macdSignal, macdHist;
  let macdBadges = [];
  let macdFor = null, candleTimes = [];
  // Daily or weekly, remembered for this browser session (nothing is kept
  // beyond the session). Weekly by default: it is the
  // TradingView setup this pane was built to match.
  const MACD_TF_KEY = "moose_macd_tf";
  const MACD_TF_NAME = { D: "daily", W: "weekly, completed weeks" };
  let macdTf = "W";
  try { const saved = sessionStorage.getItem(MACD_TF_KEY); if (saved in MACD_TF_NAME) macdTf = saved; } catch { /* storage blocked */ }
  let tuneChoice = null;

  // Text from the server (tickers, company names, reasons, error messages)
  // goes through esc() before it is put into HTML, so markup in any of it is
  // shown as text rather than run. Numbers from fmt() need no escaping.
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

  // ── api ────────────────────────────────────────────────
  const TOKEN_KEY = "moose_token";
  const token = () => sessionStorage.getItem(TOKEN_KEY) || "";

  async function api(path, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    if (token()) headers["X-Auth-Token"] = token();
    const res = await fetch(path, { ...opts, headers });
    if (res.status === 401) {
      sessionStorage.removeItem(TOKEN_KEY);
      showLogin(true);
      throw new Error("Token rejected");
    }
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch { /* non-JSON body */ }
      throw new Error(detail);
    }
    return res.json();
  }

  function toast(msg, isError = false) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.toggle("err", isError);
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { t.hidden = true; }, 5000);
  }

  // ── token gate ─────────────────────────────────────────
  function showLogin(failed = false) {
    $("login").hidden = false;
    $("loginErr").hidden = !failed;
    $("loginToken").value = "";
    $("loginToken").focus();
  }

  async function tryToken(value) {
    sessionStorage.setItem(TOKEN_KEY, value);
    const res = await fetch("/api/states", { headers: { "X-Auth-Token": value } });
    if (res.status === 401) {
      sessionStorage.removeItem(TOKEN_KEY);
      return false;
    }
    return true;
  }

  async function unlock() {
    const value = $("loginToken").value.trim();
    if (!value) return;
    if (await tryToken(value)) {
      $("login").hidden = true;
      await refreshStates();
      await refreshHealth();
    } else {
      showLogin(true);
    }
  }

  // ── charts ─────────────────────────────────────────────
  const base = {
    layout: {
      background: { color: "#0e1419" }, textColor: "#7d8f9e", fontSize: 11,
      attributionLogo: false,   // credited in the page footer instead
    },
    grid: { vertLines: { color: "#1a242c" }, horzLines: { color: "#1a242c" } },
    rightPriceScale: { borderColor: "#223039" },
    timeScale: { borderColor: "#223039" },
    crosshair: { mode: LWC.CrosshairMode.Normal },
    // Axis labels grouped the same way, so $1,039.79 on the strip and on the
    // chart match.
    localization: { priceFormatter: (v) => fmt(v) },
  };

  function buildCharts() {
    priceChart = LWC.createChart($("priceChart"), {
      ...base,
      timeScale: { ...base.timeScale, timeVisible: false },
    });
    // Volume first so the candles draw over it. It has its own hidden scale,
    // squeezed into the bottom quarter, as on TradingView.
    volumeSeries = priceChart.addHistogramSeries({
      priceScaleId: "vol", priceFormat: { type: "volume" },
      lastValueVisible: false, priceLineVisible: false,
    });
    priceChart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.75, bottom: 0 } });
    priceChart.priceScale("right").applyOptions({ scaleMargins: { top: 0.08, bottom: 0.14 } });
    candleSeries = priceChart.addCandlestickSeries({
      upColor: "#2bb6a3", downColor: "#e5544b",
      borderUpColor: "#2bb6a3", borderDownColor: "#e5544b",
      wickUpColor: "#2bb6a3", wickDownColor: "#e5544b",
    });
    stUp = priceChart.addLineSeries({ color: "#4caf50", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
    stDown = priceChart.addLineSeries({ color: "#e5544b", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
    // The 200-day average is always live, so it always carries a badge.
    smaSeries = priceChart.addLineSeries({ color: "#d2a03c", lineWidth: 1, priceLineVisible: false, lastValueVisible: true });

    adxChart = LWC.createChart($("adxChart"), { ...base, timeScale: { ...base.timeScale, visible: true } });
    adxSeries = adxChart.addLineSeries({ color: "#4a90d9", lineWidth: 2, priceLineVisible: false });
    adxGuide = adxChart.addLineSeries({ color: "#7d8f9e", lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false });

    // Keep every pane on the same x-range
    linkPane(priceChart);
    linkPane(adxChart);

    // Earnings badges follow the chart as it scrolls, zooms and resizes.
    priceChart.timeScale().subscribeVisibleLogicalRangeChange(placeEarnings);
    priceChart.timeScale().subscribeSizeChange(placeEarnings);

    const resize = () => {
      priceChart.applyOptions({ width: $("priceChart").clientWidth, height: $("priceChart").clientHeight });
      adxChart.applyOptions({ width: $("adxChart").clientWidth, height: $("adxChart").clientHeight });
    };
    new ResizeObserver(resize).observe($("priceChart"));
    new ResizeObserver(resize).observe($("adxChart"));
    resize();
  }

  // ── earnings badges ────────────────────────────────────
  // Drawn as HTML over the price pane: the chart's own markers cannot sit on
  // the bottom edge or carry a date on hover.
  function placeEarnings() {
    const layer = $("earnLayer");
    if (!layer || !priceChart) return;
    const ts = priceChart.timeScale();
    const plotW = ts.width();
    const bottom = ts.height() + 3;
    const badges = [];
    const at = new Map(candleTimes.map((t, i) => [t, i]));
    for (const e of earnMarks) {
      const i = at.get(e.time);
      if (i === undefined) continue;
      const x = ts.logicalToCoordinate(i);
      if (x == null || x < 0 || x > plotW) continue;
      badges.push(`<i class="earn-badge" style="left:${x}px;bottom:${bottom}px"`
        + ` title="Earnings ${esc(e.date)}">E</i>`);
    }
    if (earnNext && candleTimes.length) {
      // Past the last bar there are no candles, so place it by trading days
      // and pin it to the right edge when that is off screen.
      let x = ts.logicalToCoordinate(candleTimes.length - 1 + earnNext.trading_days);
      if (x != null && x >= 0) {
        x = Math.min(x, plotW - 9);
        const when = earnNext.trading_days === 1 ? "next trading day"
          : `in ${earnNext.trading_days} trading days`;
        badges.push(`<i class="earn-badge next" style="left:${x}px;bottom:${bottom}px"`
          + ` title="Next earnings ${esc(earnNext.date)} (${when})">E</i>`);
      }
    }
    layer.innerHTML = badges.join("");
  }

  // Scrolling any pane moves all the others. The MACD pane joins the group
  // when it is first opened.
  //
  // The library reports a range change a frame after it happens, so a plain
  // "I am syncing" flag cannot tell our own echo from a real scroll: the echo
  // arrives after the flag is down, bounces between panes and overwrites
  // whatever range was just asked for. Two things prevent that:
  //   pushed    each pane remembers the one range we pushed to it and
  //             swallows that event instead of echoing it back
  //   settling  while data is being loaded, a pane's own default range is
  //             ignored and `want` is re-asserted, so a freshly filled pane
  //             cannot drag the others to its default view
  const linked = [];
  const pushed = new Map();
  const rangeKey = (r) => `${r.from.toFixed(3)}:${r.to.toFixed(3)}`;
  let want = null;      // the window every pane should be showing
  let settling = 0;     // frames left of holding `want` after new data

  function applyRange(r) {
    if (!r) return;
    want = r;
    for (const c of linked) {
      pushed.set(c, rangeKey(r));
      c.timeScale().setVisibleLogicalRange(r);
    }
  }

  // Hold `want` for a few frames, long enough for the range events that new
  // data and any width change produce to arrive and be ignored.
  function holdRange(frames = 3) {
    settling = frames;
    const tick = () => {
      if (settling <= 0) return;
      settling -= 1;
      if (want) applyRange(want);
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  function linkPane(chart) {
    linked.push(chart);
    chart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
      if (!r) return;
      if (pushed.get(chart) === rangeKey(r)) { pushed.delete(chart); return; }
      if (settling > 0 && want) { applyRange(want); return; }
      want = r;
      for (const other of linked) {
        if (other === chart) continue;
        pushed.set(other, rangeKey(r));
        other.timeScale().setVisibleLogicalRange(r);
      }
    });
  }

  // ── MACD (optional, display only) ──────────────────────
  function buildMacdChart() {
    macdChart = LWC.createChart($("macdChart"), {
      ...base,
      timeScale: { ...base.timeScale, visible: true },
      // Headroom so the pane label never sits on the lines.
      rightPriceScale: { ...base.rightPriceScale, scaleMargins: { top: 0.22, bottom: 0.08 } },
    });
    macdHist = macdChart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
    // Axis badges are drawn as price lines (see loadMacd), so they keep their
    // colour when the latest points are hidden.
    macdLine = macdChart.addLineSeries({ color: "#4a90d9", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
    macdSignal = macdChart.addLineSeries({ color: "#d2a03c", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    macdLine.createPriceLine({ price: 0, color: "#7d8f9e", lineWidth: 1,
                               lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: false });
    new ResizeObserver(() => {
      const el = $("macdChart");
      if (el.clientWidth) macdChart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
    }).observe($("macdChart"));
    linkPane(macdChart);
  }

  // Panes are matched by bar index, so the right-hand scales must be the same
  // width or the bars drift apart horizontally. The price chart keeps its own
  // natural width and the smaller panes are widened to match it; measuring by
  // clearing and re-applying widths instead made the chart visibly jump.
  let alignedWidth = null;
  function alignScales() {
    requestAnimationFrame(() => {
      const w = priceChart.priceScale("right").width();
      if (!w || w === alignedWidth) return;
      alignedWidth = w;
      for (const c of [adxChart, macdChart]) {
        if (c) c.priceScale("right").applyOptions({ minimumWidth: w });
      }
    });
  }

  // Colour each bar by side of zero, and a pale tint while the histogram is
  // shrinking, the way TradingView shades it.
  function histColour(v, prev) {
    const growing = prev == null || Math.abs(v) >= Math.abs(prev);
    if (v >= 0) return growing ? "#2bb6a3" : "#a9e0d8";
    return growing ? "#e5544b" : "#f5b8b4";
  }

  async function loadMacd(ticker) {
    if (!macdOpen || !ticker) return;
    const tf = macdTf;
    let m;
    try {
      m = await api(`/api/macd/${ticker}?bars=${MAX_BARS}&timeframe=${tf}`);
    } catch (e) {
      toast(`Could not load MACD for ${ticker}: ${e.message}`, true);
      return;
    }
    // A slower response for a ticker or timeframe we have already moved away from.
    if (ticker !== selected || !macdOpen || tf !== macdTf) return;

    const at = new Map();
    m.time.forEach((t, i) => at.set(t, i));
    // One point per candle, blanks included, so bar indexes line up with the
    // price pane. Any bar the chart has not loaded yet is left out.
    // Weekly plots only the day each week closed on: the lines join across
    // the blank days and the histogram shows one bar per week, as on a
    // TradingView weekly MACD.
    // Latest valued point in a series.
    const lastValue = (pts) => {
      for (let k = pts.length - 1; k >= 0; k--) if (pts[k].value != null) return pts[k].value;
      return null;
    };
    const weekEnd = m.week_end;
    // The days after the latest weekly close still need a point with a value:
    // the chart measures its right edge from the last valued point, and if no
    // series had one there every linked pane would shift left. Only the
    // histogram carries them, as invisible zero bars. The lines stay blank, so
    // they end exactly on the last completed week: a line segment takes the
    // colour of the point it starts from, so hidden line points would still
    // draw a flat stub past that week.
    let tail = candleTimes.length;
    if (weekEnd) {
      while (tail > 0) {
        const i = at.get(candleTimes[tail - 1]);
        if (i !== undefined && weekEnd[i]) break;
        tail--;
      }
    }
    const HIDE = "rgba(0,0,0,0)";
    const line = [], sig = [], hist = [];
    let last = null, prev = null;
    for (let k = 0; k < candleTimes.length; k++) {
      const t = candleTimes[k];
      const i = at.get(t);
      if (weekEnd && k >= tail && tail > 0) {
        line.push({ time: t });
        sig.push({ time: t });
        hist.push({ time: t, value: 0, color: HIDE });
        continue;
      }
      if (weekEnd && (i === undefined || !weekEnd[i])) {
        line.push({ time: t }); sig.push({ time: t }); hist.push({ time: t });
        continue;
      }
      const v = i === undefined ? null : m.macd[i];
      const s = i === undefined ? null : m.signal_line[i];
      const h = i === undefined ? null : m.hist[i];
      line.push(v == null ? { time: t } : { time: t, value: v });
      sig.push(s == null ? { time: t } : { time: t, value: s });
      if (h == null) { hist.push({ time: t }); last = prev = null; continue; }
      if (h !== last) { prev = last; last = h; }
      hist.push({ time: t, value: h, color: histColour(h, prev) });
    }
    const saved = want || priceChart.timeScale().getVisibleLogicalRange();
    macdHist.setData(hist);
    macdLine.setData(line);
    macdSignal.setData(sig);
    applyRange(saved);
    holdRange();
    alignScales();

    const lastSig = lastValue(sig);
    const lastLine = lastValue(line);
    for (const [series, pl] of macdBadges) series.removePriceLine(pl);
    macdBadges = [];
    for (const [series, value, color] of [[macdLine, lastLine, "#4a90d9"], [macdSignal, lastSig, "#d2a03c"]]) {
      if (value == null) continue;
      macdBadges.push([series, series.createPriceLine({
        price: value, color, lineVisible: false, axisLabelVisible: true, title: "",
      })]);
    }
    const week = tf === "W" && m.as_of ? ` · week to ${m.as_of}` : "";
    $("macdNow").textContent = lastLine == null || lastSig == null ? ""
      : `${fmt(lastLine)} vs signal ${fmt(lastSig)} (${lastLine >= lastSig ? "above" : "below"})${week}`;
    macdFor = `${ticker}:${tf}`;
  }

  function setMacd(open) {
    macdOpen = open;
    $("macdToggle").classList.toggle("is-on", open);
    $("macdToggle").setAttribute("aria-pressed", open);
    $("macdWrap").hidden = !open;
    $("macdTf").hidden = !open;
    // Only the lowest pane needs dates along the bottom.
    adxChart.applyOptions({ timeScale: { visible: !open } });
    const saved = want || priceChart.timeScale().getVisibleLogicalRange();
    if (open) {
      if (!macdChart) buildMacdChart();
      requestAnimationFrame(() => {
        resizeCharts();
        applyRange(saved);
        holdRange();
        if (selected && chartFor === selected && macdFor !== `${selected}:${macdTf}`) loadMacd(selected);
        else alignScales();
      });
    } else {
      requestAnimationFrame(() => { resizeCharts(); applyRange(saved); alignScales(); });
    }
  }

  // ── rail ───────────────────────────────────────────────
  const GROUPS = [
    ["signal", "Fired today"],
    ["trending", "In an uptrend · ranked by attention score"],
    ["blocked", "Filtered out"],
    ["standby", "Waiting"],
  ];

  function classify(s) {
    if (s.flip_up && s.passed) return "signal";
    if (s.flip_up && !s.passed) return "blocked";
    if (s.trend === "up") return "trending";
    return "standby";
  }

  function renderRail() {
    const q = $("filterBox").value.trim().toUpperCase();
    const list = $("railList");
    const shown = states.filter((s) => !q || s.ticker.includes(q) || (s.sector || "").toUpperCase().includes(q));

    if (!shown.length) {
      list.innerHTML = `<p class="empty">${states.length ? "Nothing matches that filter." : "No data yet. Run a scan to fetch price history."}</p>`;
      return;
    }

    const buckets = {};
    shown.forEach((s) => { (buckets[classify(s)] ||= []).push(s); });

    let html = "";
    for (const [key, label] of GROUPS) {
      const rows = buckets[key];
      if (!rows || !rows.length) continue;
      html += `<div class="group-label">${label} (${rows.length})</div>`;
      for (const s of rows) {
        // Show the parts that produced the score, so the order can be read
        // rather than taken on trust.
        const age = s.bars_in_trend === 1 ? "new today" : `${s.bars_in_trend}d old`;
        let note, cls = "";
        if (key === "blocked") {
          note = `Rejected · ${esc(s.reason)}`; cls = "warn";
        } else if (key === "standby") {
          note = `Downtrend · ADX ${fmt(s.adx, 0)}`;
        } else {
          note = `${age} · ${fmt(s.risk_pct, 1)}% to stop · ADX ${fmt(s.adx, 0)}`;
          if (s.score == null && s.score_note) note += ` · ${esc(s.score_note)}`;
          if (key === "signal") cls = "pass";
        }
        const badge = s.score == null
          ? `<span class="score none" title="Not scored: ${esc(s.score_note || "not actionable")}">—</span>`
          : `<span class="score" title="Attention score: 40% trend freshness, 30% risk to stop, 30% trend strength">${s.score}</span>`;
        const held = s.position
          ? '<i class="held-dot" title="You hold this"></i>' : "";
        html += `<div class="row ${key}" role="option" tabindex="0"
                      aria-selected="${s.ticker === selected}" data-t="${esc(s.ticker)}">
          <span class="tkr">${esc(s.ticker)}${held}${badge}</span>
          <span class="px">${fmt(s.close)}</span>
          <span class="note ${cls}">${note}</span>
        </div>`;
      }
    }
    list.innerHTML = html;
  }

  // ── chart load ─────────────────────────────────────────
  // Load both views: the hidden one is then already correct when switched to,
  // so the price chart never lags behind the ticker shown in the backtest.
  function select(ticker) {
    // "Buy more" belongs to the ticker it was opened on. Left standing, it
    // showed a purchase form for the next ticker, and hid the Sell controls
    // on the one you actually hold.
    if (ticker !== selected) buyingMore = false;
    selected = ticker;
    renderRail();
    loadChart(ticker);
    if (view === "test") loadBacktest(ticker); else testFor = null;
    if (view === "tune") loadTune(ticker); else tuneFor = null;
  }

  function reloadAll() {
    if (!selected) return;
    chartFor = testFor = tuneFor = null;
    // The selected ticker is redrawn straight away; the chart response carries
    // its own recomputed state, so the readout is correct without waiting.
    loadChart(selected);
    if (view === "test") loadBacktest(selected);
    if (view === "tune") loadTune(selected);
    // The rail ranking needs all 39, which is the expensive part. Let it
    // catch up in the background instead of blocking the redraw.
    refreshStates();
  }

  // Spinner arrows fire a change per click. Without this, holding one down
  // queued a full watchlist recompute per increment.
  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }
  const reloadSoon = debounce(reloadAll, 300);

  async function loadChart(ticker) {
    selected = ticker;
    const params = new URLSearchParams({
      atr_len: $("atrLen").value, atr_mult: $("atrMult").value,
      adx_min: $("adxMin").value, bars: MAX_BARS,
    });
    try {
      const d = await api(`/api/chart/${ticker}?${params}`);
      $("chartName").textContent = d.name || d.ticker;
      $("chartTicker").textContent = d.ticker;
      $("chartSector").textContent = d.sector || "";
      $("profile").value = d.profile;

      candleSeries.setData(d.candles);
      volumeSeries.setData(d.volume || []);
      candleTimes = d.candles.map((c) => c.time);
      earnMarks = d.earnings || [];
      earnNext = d.upcoming_earnings || null;
      stUp.setData(d.supertrend_up);
      stDown.setData(d.supertrend_down);
      smaSeries.setData(d.sma);
      // Both Supertrend legs retain a stale "last value" from whenever that
      // direction last applied, so only the active one gets a badge. Otherwise
      // you would see two stop prices, one of them months out of date.
      const upActive = d.supertrend_up.length
        && d.supertrend_up[d.supertrend_up.length - 1].time === d.candles[d.candles.length - 1].time;
      stUp.applyOptions({ lastValueVisible: upActive, priceLineVisible: false });
      stDown.applyOptions({ lastValueVisible: !upActive, priceLineVisible: false });

      candleSeries.setMarkers(d.markers);
      adxSeries.setData(d.adx);
      adxGuide.setData(d.adx.map((p) => ({ time: p.time, value: d.adx_min })));
      // Show the selected window, with everything else loaded either side.
      // Never start before the indicators exist: those bars carry no
      // Supertrend or 200-day average, and the backtest discards them.
      const total = d.candles.length;
      const show = Math.min(range().bars, total);
      const from = Math.max(total - show, d.warmup_bars || 0);
      applyRange({ from, to: total - 1 });
      holdRange();

      const lastAdx = d.adx.length ? d.adx[d.adx.length - 1].value : null;
      $("adxNow").textContent = lastAdx == null
        ? "" : `${fmt(lastAdx, 1)} (threshold ${fmt(d.adx_min, 0)})`;

      chartFor = ticker;
      requestAnimationFrame(placeEarnings);
      macdFor = null;
      alignScales();
      if (macdOpen) loadMacd(ticker);
      const st = d.state || states.find((s) => s.ticker === ticker);
      renderReadout(st, d);
      renderPosition(st);
    } catch (e) {
      toast(`Could not load ${ticker}: ${e.message}`, true);
    }
  }

  // What the four verdicts mean, on hover.
  const VERDICT_HELP = [
    "Buy signal today — the Supertrend flipped up on the latest bar and the filter allows it.",
    "In trend, filter clear — the Supertrend is up and the filter has no objection, but the flip was on an earlier bar, so there is no fresh entry.",
    "Rejected: … — it flipped up today but the filter blocked it, and the reason says which rule.",
    "Stand aside — the Supertrend is down, so there is nothing to act on.",
  ].join("\n");

  function renderReadout(s, d) {
    if (!s) { $("readout").innerHTML = `<p class="empty">No current state for ${esc(d.ticker)}.</p>`; return; }
    const risk = ((s.close / s.stop - 1) * 100);
    const verdict = s.tradeable
      ? (s.flip_up ? "Buy signal today" : "In trend, filter clear")
      : (s.flip_up ? `Rejected: ${esc(s.reason)}` : "Stand aside");
    const vClass = s.tradeable ? "up" : (s.flip_up ? "warn" : "down");

    const cell = (k, v, c = "") => `<div class="cell"><span class="k">${k}</span><span class="v ${c}">${v}</span></div>`;
    $("readout").innerHTML =
      cell("Close", fmt(s.close)) +
      // Coloured like the Supertrend line on the chart: green while it is
      // trailing below price in an uptrend, red once price is below it.
      cell("Stop (Supertrend)", fmt(s.stop), s.trend === "up" ? "up" : "down") +
      cell("Risk to stop", `${fmt(risk, 1)}%`, risk > 12 ? "warn" : "") +
      cell("Trend age", s.bars_in_trend === 1 ? "new today" : `${s.bars_in_trend} days`) +
      cell("Trend strength (ADX)", fmt(s.adx, 1), s.adx_ok ? "up" : "warn") +
      cell("Volatility (ATR)", `${fmt(s.atr_pct, 1)}%`) +
      cell("vs 200-day avg (SMA)", s.above_sma ? "above" : "below", s.above_sma ? "up" : "down") +
      cell("Next earnings", esc(s.next_earnings || "not set"), s.blackout ? "warn" : "") +
      // No "Your P&L" here: the Position panel shows it, with the dollar
      // figure and what is at risk, so repeating it only adds noise.
      cell("Attention score",
           s.score == null ? `— ${esc(s.score_note || "not actionable")}` : `${s.score}`,
           s.score == null ? "warn" : (s.score >= 60 ? "up" : "")) +
      `<div class="cell verdict" title="${esc(VERDICT_HELP)}">`
      + `<span class="k">Verdict</span><span class="v ${vClass}">${verdict}</span></div>`;
  }


  // ── backtest ───────────────────────────────────────────
  function buildEquityChart() {
    equityChart = LWC.createChart($("equityChart"), {
      ...base,
      rightPriceScale: { ...base.rightPriceScale, scaleMargins: { top: 0.1, bottom: 0.1 } },
    });
    for (const key of [...PROFILE_ORDER, "bh"]) {
      equitySeries[key] = equityChart.addLineSeries({
        color: CURVE[key],
        lineWidth: key === "bh" ? 1 : 2,
        lineStyle: key === "bh" ? LWC.LineStyle.Dashed : LWC.LineStyle.Solid,
        priceLineVisible: false,
        lastValueVisible: true,
        title: "",
      });
    }
    const resize = () => equityChart.applyOptions({
      width: $("equityChart").clientWidth, height: $("equityChart").clientHeight,
    });
    new ResizeObserver(resize).observe($("equityChart"));
    resize();
  }

  const signed = (v, d = 1, suffix = "%") =>
    v == null ? "—" : `<span class="${v >= 0 ? "pos" : "neg"}">${v >= 0 ? "+" : ""}${fmt(v, d)}${suffix}</span>`;

  async function loadBacktest(ticker) {
    const body = $("btStats").querySelector("tbody");
    body.innerHTML = `<tr><td colspan="7" class="empty">Running backtest on ${esc(ticker)}.</td></tr>`;
    $("btVerdict").textContent = "";

    const params = new URLSearchParams({
      bars: range().bars, atr_len: $("atrLen").value,
      atr_mult: $("atrMult").value, adx_min: $("adxMin").value,
    });
    let d;
    try {
      d = await api(`/api/backtest/${ticker}?${params}`);
    } catch (e) {
      body.innerHTML = `<tr><td colspan="7" class="empty">${esc(e.message)}</td></tr>`;
      for (const k in equitySeries) equitySeries[k].setData([]);
      return;
    }

    $("btRange").textContent = `${d.name || d.ticker} · ${d.start} to ${d.end} · ${d.bars} bars`;
    testFor = ticker;

    const order = PROFILE_ORDER;
    const best = d.verdict.profile;
    let html = "";
    for (const key of order) {
      const m = d.profiles[key].metrics;
      const assigned = key === d.current_profile ? '<span class="assigned">in use</span>' : "";
      html += `<tr class="${key === best ? "best" : ""}">
        <td><span class="swatch" style="background:${CURVE[key]}"></span>${esc(d.profiles[key].label)}${assigned}</td>
        <td>${m.trades}</td>
        <td>${m.win_rate == null ? "—" : fmt(m.win_rate, 0) + "%"}</td>
        <td>${signed(m.expectancy, 2)}</td>
        <td>${m.profit_factor == null ? "—" : fmt(m.profit_factor)}</td>
        <td class="neg">${m.max_dd == null ? "—" : fmt(m.max_dd, 0) + "%"}</td>
        <td>${signed(m.total_return, 0)}</td>
      </tr>`;
    }
    const bh = d.buy_hold;
    html += `<tr class="bh">
      <td><span class="swatch" style="background:${CURVE.bh}"></span>${esc(bh.label)}</td>
      <td>1</td><td>—</td><td>—</td><td>—</td>
      <td class="neg">${fmt(bh.max_dd, 0)}%</td>
      <td>${signed(bh.total_return, 0)}</td>
    </tr>`;
    // Your own closed trades on this ticker, for direct comparison.
    try {
      const mine = await api(`/api/positions?ticker=${ticker}`);
      const r = mine.realised;
      if (r && r.trades) {
        html += `<tr class="mine">
          <td>Your trades</td>
          <td>${r.trades}</td>
          <td>${r.win_rate == null ? "—" : fmt(r.win_rate, 0) + "%"}</td>
          <td>${signed(r.expectancy, 2)}</td>
          <td>${r.profit_factor == null ? "—" : fmt(r.profit_factor)}</td>
          <td class="neg">${r.max_dd == null ? "—" : fmt(r.max_dd, 0) + "%"}</td>
          <td>${signed(r.total_return, 0)}</td>
        </tr>`;
      }
    } catch { /* positions are optional context */ }

    body.innerHTML = html;

    for (const key of order) equitySeries[key].setData(d.profiles[key].equity);
    equitySeries.bh.setData(bh.equity);
    resizeCharts();
    equityChart.timeScale().fitContent();

    const v = $("btVerdict");
    v.textContent = d.verdict.text;
    v.className = "verdict-line" + (d.verdict.confidence === "low" ? " low" : "");
  }

  const TABS = { chart: "tabChart", test: "tabTest", tune: "tabTune", earn: "tabEarn" };

  function resizeCharts() {
    const fit = (chart, id) => {
      const el = $(id);
      if (chart && el && el.clientWidth) {
        chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
      }
    };
    fit(priceChart, "priceChart");
    fit(adxChart, "adxChart");
    if (macdOpen) fit(macdChart, "macdChart");
    fit(equityChart, "equityChart");
    fit(pnlChart, "pnlChart");
    fit(pctChart, "pctChart");
  }

  function setView(next) {
    view = next;
    // The chart stays on screen for Backtest so signals and results can be
    // read together; Tune gets the whole pane because its table is wide.
    $("chartView").hidden = next === "tune" || next === "earn";
    $("testView").hidden = next !== "test";
    $("tuneView").hidden = next !== "tune";
    $("earnView").hidden = next !== "earn";
    for (const [key, el] of Object.entries(TABS)) {
      $(el).classList.toggle("is-on", key === next);
      $(el).setAttribute("aria-selected", key === next);
    }
    // Bring whichever view we switched to up to date with the current ticker.
    if (!selected) return;
    if (next === "chart" && chartFor !== selected) loadChart(selected);
    if (next === "test" && testFor !== selected) loadBacktest(selected);
    if (next === "tune" && tuneFor !== selected) loadTune(selected);
    if (next === "earn") loadEarnings();
    // Panes revealed by the switch were zero-height a moment ago.
    requestAnimationFrame(resizeCharts);
  }

  // ── positions ──────────────────────────────────────────
  const today = () => new Date().toISOString().slice(0, 10);
  // Fractional shares are normal now, so show what was actually entered
  // rather than rounding it to a whole number.
  const qty = (v) => Number(v).toLocaleString(undefined, { maximumFractionDigits: 6 });

  let buyingMore = false;          // purchase form while already holding

  // ── position size ──────────────────────────────────────
  // The stop is already known, so the only thing standing between it and a
  // share count is how much you are willing to lose. Kept for the browser
  // session, not stored, because it is a personal figure.
  const SIZER_KEY = "moose_sizer";
  let sizer = { account: "", riskPct: "1", capPct: "33", fractional: true };
  try { Object.assign(sizer, JSON.parse(sessionStorage.getItem(SIZER_KEY) || "{}")); } catch { /* blocked */ }

  function sizeFor(state) {
    const account = Number(sizer.account), riskPct = Number(sizer.riskPct);
    if (!state || !state.stop || !(account > 0) || !(riskPct > 0)) return null;
    const perShare = state.close - state.stop;
    if (!(perShare > 0)) return { locked: true };   // stop above price: no risk to size against

    const budget = account * riskPct / 100;
    const byRisk = budget / perShare;
    // A tight stop asks for a position bigger than the account. The cap is
    // the second limit: whichever allows fewer shares wins, and sizing down
    // only ever risks less.
    const capPct = Number(sizer.capPct) > 0 ? Number(sizer.capPct) : 100;
    const byCap = account * capPct / 100 / state.close;
    const capped = byCap < byRisk;
    let shares = Math.min(byRisk, byCap);
    // Whole shares only when the broker cannot split them; fractional is the
    // default because it can hit the risk figure exactly.
    shares = sizer.fractional ? Math.floor(shares * 10000) / 10000 : Math.floor(shares);

    const cost = shares * state.close;
    const roundTrip = defaultCommission * 2;
    return {
      budget, perShare, shares, cost, capped, capPct,
      risk: shares * perShare,
      riskPct: account ? shares * perShare / account * 100 : 0,
      costPct: account ? cost / account * 100 : 0,
      // Commission is charged per order whatever the size, so a small
      // position pays a large share of itself in fees.
      feeRoundTrip: roundTrip,
      feePct: cost > 0 ? roundTrip / cost * 100 : 0,
    };
  }

  function sizerRow(state) {
    const r = sizeFor(state);
    let answer;
    if (!r) {
      answer = "enter an account size";
    } else if (r.locked) {
      // The Supertrend sits above the price in a downtrend: there is no long
      // entry here to size, whatever the account.
      answer = "the Supertrend is above the price, so there is no entry to size here";
    } else if (r.shares <= 0) {
      answer = `one share risks ${money(r.perShare)}, more than your ${money(r.budget)}`;
    } else {
      const one = r.shares === 1;
      answer = `<b>${qty(r.shares)} share${one ? "" : "s"}</b> risk${one ? "s" : ""} `
        + `${money(r.risk)} (${fmt(r.riskPct)}% of the account), `
        + `costing ${money(r.cost)} (${fmt(r.costPct, 0)}%)`;
      if (r.capped) {
        answer += ` · held to your ${fmt(r.capPct, 0)}% cap, so it risks less than ${fmt(Number(sizer.riskPct))}%`;
      }
      if (r.feePct >= 1) {
        answer += ` · <span class="warn">${money(r.feeRoundTrip)} commission is `
          + `${fmt(r.feePct, 1)}% of it</span>`;
      }
    }
    return `<div class="sizer" title="Shares that put no more than your chosen risk between the price and the Supertrend stop, and no more than your cap into one position">
      <span>Size by risk:</span>
      <label>Account <input id="sizeAccount" type="number" step="any" min="0"
        style="width:100px" value="${esc(sizer.account)}" placeholder="e.g. 1000"></label>
      <label>Risk % <input id="sizeRisk" type="number" step="0.1" min="0" max="100"
        style="width:60px" value="${esc(sizer.riskPct)}"></label>
      <label title="The most of the account to put into one position, whatever the risk figure says">Max %
        <input id="sizeCap" type="number" step="1" min="1" max="100"
        style="width:60px" value="${esc(sizer.capPct)}"></label>
      <label title="Your broker lets you buy part of a share. Off rounds down to whole shares.">Fractional
        <input id="sizeFrac" type="checkbox" ${sizer.fractional ? "checked" : ""}></label>
      <span>${answer}</span>
      ${r && !r.locked && r.shares > 0 ? '<button id="sizeUse" type="button">Use</button>' : ""}
    </div>`;
  }

  function wireSizer(state) {
    for (const [id, key] of [["sizeAccount", "account"], ["sizeRisk", "riskPct"],
                             ["sizeCap", "capPct"]]) {
      if (!$(id)) continue;
      $(id).addEventListener("change", () => {
        sizer[key] = $(id).value;
        saveSizer();
        renderPosition(state);
      });
    }
    if ($("sizeFrac")) {
      $("sizeFrac").addEventListener("change", () => {
        sizer.fractional = $("sizeFrac").checked;
        saveSizer();
        renderPosition(state);
      });
    }
    if ($("sizeUse")) {
      $("sizeUse").addEventListener("click", () => {
        const r = sizeFor(state);
        if (r && r.shares > 0) $("posQty").value = r.shares;
      });
    }
  }

  function saveSizer() {
    try { sessionStorage.setItem(SIZER_KEY, JSON.stringify(sizer)); } catch { /* blocked */ }
  }

  // A split rewrites history: Yahoo divides every older close by the factor,
  // while the price you typed stays as it was, so the holding reads wrong
  // until it is restated.
  function splitWarning(p) {
    const sp = p.split_suspect;
    if (!sp) return "";
    return `<div class="split-warn">
      <span>Looks like a ${esc(sp.note)}: you recorded ${fmt(p.entry_price)},
      and that day now closes at ${fmt(sp.close_on_day)}. Restate as
      ${fmt(sp.suggested_entry)}${sp.suggested_quantity
        ? ` × ${qty(sp.suggested_quantity)} shares` : ""}?</span>
      <button id="posSplit" type="button" data-id="${p.id}"
        data-factor="${sp.factor}" data-reverse="${sp.reverse}">Restate</button>
    </div>`;
  }

  function renderPosition(state) {
    const el = $("posPanel");
    el.hidden = !posOpen || !selected;
    if (el.hidden) return;

    const p = buyingMore ? null : (state && state.position);
    if (p) {
      // Risk from the CURRENT stop: once it trails above entry the trade
      // can no longer lose, which is the thing worth knowing.
      const risk = p.risk_locked_out
        ? '<span class="pos">stop above entry</span>'
        : (p.risk_value != null ? `$${fmt(p.risk_value, 0)}` : "—");
      el.innerHTML = `<div class="held">
          <span><i class="k">Holding since</i><b>${esc(p.entry_date)}</b> (${p.held_days}d)</span>
          <span><i class="k">Entry</i><b>${fmt(p.entry_price)}</b></span>
          ${p.quantity ? `<span><i class="k">Qty</i><b>${qty(p.quantity)}</b></span>` : ""}
          <span><i class="k">P&amp;L</i><b class="${p.pnl_pct >= 0 ? "pos" : "neg"}">${p.pnl_pct >= 0 ? "+" : ""}${fmt(p.pnl_pct)}%${
            p.pnl_value != null ? ` ($${fmt(p.pnl_value, 0)})` : ""}</b></span>
          <span title="What you would lose if price hit the Supertrend stop from here"><i class="k">At risk</i><b>${risk}</b></span>
        </div>
        ${splitWarning(p)}
        <label>Sell date <input id="posExitDate" type="date" value="${today()}"></label>
        <label>Sell price <input id="posExitPrice" type="number" step="0.01"
          value="${state.close.toFixed(2)}"></label>
        <label title="How many shares to sell. The full amount closes the holding; less sells part of it and keeps the rest.">Sell qty
          <input id="posExitQty" type="number" step="any" min="0"
            ${p.quantity ? `max="${p.quantity}" value="${p.quantity}"` : 'placeholder="all" disabled'}></label>
        <label title="Brokerage on this sale. Counted in the Earnings tab.">Commission
          <input id="posExitFee" type="number" step="0.01" min="0" value="${defaultCommission.toFixed(2)}"></label>
        <button id="posClose" type="button">Sell</button>
        <button id="posMore" type="button" title="Record another purchase. It is kept as its own lot and sold oldest first.">Buy more</button>
        <button id="posDelete" class="danger" type="button" data-id="${p.id}">Delete</button>`;

      $("posClose").addEventListener("click", async () => {
        const want = p.quantity ? Number($("posExitQty").value) : null;
        if (p.quantity && (!(want > 0) || want > p.quantity + 1e-9)) {
          toast(`Enter between 0 and ${qty(p.quantity)} shares to sell`, true);
          return;
        }
        const part = Boolean(p.quantity) && want < p.quantity - 1e-9;
        try {
          const r = await api("/api/positions/sell", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ticker: selected, exit_date: $("posExitDate").value,
                                   exit_price: Number($("posExitPrice").value),
                                   exit_fee: Number($("posExitFee").value) || 0,
                                   quantity: part ? want : null }),
          });
          toast(part ? `Sold ${qty(want)} of ${selected}, ${qty(r.still_held)} still held`
                     : `Closed ${selected}`);
          await refreshStates(); reloadAll();
        } catch (e) { toast(`Could not sell: ${e.message}`, true); }
      });
      // Buying more keeps the purchase form, so the new lot is recorded at
      // its own price instead of being averaged in by hand.
      $("posMore").addEventListener("click", () => { buyingMore = true; renderPosition(state); });
      if ($("posSplit")) {
        $("posSplit").addEventListener("click", async () => {
          const b = $("posSplit");
          try {
            await api(`/api/positions/${b.dataset.id}/split`, {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ factor: Number(b.dataset.factor),
                                     reverse: b.dataset.reverse === "true" }),
            });
            toast(`Restated your ${selected} position for the split`);
            await refreshStates(); reloadAll();
          } catch (e) { toast(`Could not restate: ${e.message}`, true); }
        });
      }
      $("posDelete").addEventListener("click", async () => {
        try {
          await api(`/api/positions/${p.id}`, { method: "DELETE" });
          toast(`Removed the ${selected} position record`);
          await refreshStates(); reloadAll();
        } catch (e) { toast(`Could not delete: ${e.message}`, true); }
      });
      return;
    }

    const heldNow = state && state.position;
    el.innerHTML = `<span class="k">${heldNow
        ? `Buying more ${esc(selected)} · kept as its own lot, sold oldest first.`
        : `No position recorded in ${esc(selected)}.`}</span>
      <label>Bought on <input id="posDate" type="date" value="${today()}"></label>
      <label>Price <input id="posPrice" type="number" step="0.01"
        value="${state ? state.close.toFixed(2) : ""}"></label>
      <label title="Number of shares. Needed for dollar figures in the Earnings tab.">Quantity
        <input id="posQty" type="number" step="any" min="0" placeholder="optional"></label>
      <label title="Brokerage on this purchase. Counted in the Earnings tab.">Commission
        <input id="posFee" type="number" step="0.01" min="0" value="${defaultCommission.toFixed(2)}"></label>
      <button id="posAdd" type="button">Record purchase</button>
      ${buyingMore ? '<button id="posCancel" type="button">Cancel</button>' : ""}
      ${sizerRow(state)}`;
    wireSizer(state);
    if (buyingMore) {
      $("posCancel").addEventListener("click", () => { buyingMore = false; renderPosition(state); });
    }

    $("posAdd").addEventListener("click", async () => {
      const qty = Number($("posQty").value);
      try {
        await api("/api/positions", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ticker: selected, entry_date: $("posDate").value,
                                 entry_price: Number($("posPrice").value),
                                 quantity: qty > 0 ? qty : null,
                                 entry_fee: Number($("posFee").value) || 0 }),
        });
        buyingMore = false;
        toast(`Recorded your ${selected} purchase`);
        await refreshStates(); reloadAll();
      } catch (e) { toast(`Could not record: ${e.message}`, true); }
    });
  }

  // ── earnings ───────────────────────────────────────────
  // Built on first use: the tab is usually never opened in a session, and an
  // empty chart would otherwise be created for every visitor.
  function buildEarnCharts() {
    const opts = {
      ...base,
      timeScale: { ...base.timeScale, visible: true },
      rightPriceScale: { ...base.rightPriceScale, scaleMargins: { top: 0.15, bottom: 0.15 } },
    };
    pctChart = LWC.createChart($("pctChart"), opts);
    pnlChart = LWC.createChart($("pnlChart"), opts);
    // Area rather than line: the fill shows at a glance which side of zero
    // the running total is on.
    // Closed-only sits behind, so the live line reads on top of it.
    pctClosedSeries = pctChart.addLineSeries({
      color: "#7d8f9e", lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
    });
    // Green while the trading is in profit, red once it is not. Zero is the
    // dividing line, so a losing stretch is obvious without reading the axis.
    pctSeries = pctChart.addBaselineSeries({
      baseValue: { type: "price", price: 0 },
      topLineColor: "#2bb6a3", topFillColor1: "#2bb6a344", topFillColor2: "#2bb6a305",
      bottomLineColor: "#e5544b", bottomFillColor1: "#e5544b05", bottomFillColor2: "#e5544b44",
      lineWidth: 2, priceLineVisible: false,
    });
    // Plain blue: the money in the account is a level, not a verdict, and the
    // colour keeps this pane distinct from the green-and-red return above.
    pnlSeries = pnlChart.addAreaSeries({
      lineColor: "#4a90d9", topColor: "#4a90d933", bottomColor: "#4a90d905",
      lineWidth: 2, priceLineVisible: false,
    });
    for (const chart of [pctChart, pnlChart]) {
      // No zero line: the axis already shows where zero is, and a dashed rule
      // across the pane only competes with the curve.
      // These are portfolio curves on their own dates, so they are NOT part
      // of the price/ADX/MACD scroll group.
      new ResizeObserver(() => {
        const el = chart === pctChart ? $("pctChart") : $("pnlChart");
        if (el.clientWidth) chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
      }).observe(chart === pctChart ? $("pctChart") : $("pnlChart"));
    }
  }

  // Plain-text money helpers for this tab; `signed` above returns HTML.
  // fitContent centres each point in its own bar, so it leaves half a bar
  // free at each end: a fifth of the pane with five points, and a week-old
  // position looked squeezed into the middle. Framing on the points instead
  // puts the first and last hard against the edges. Two points cannot be
  // framed that way (the range would be narrower than one bar), so they keep
  // the default.
  function frameSeries(chart, points) {
    chart.timeScale().fitContent();
    if (points >= 3) {
      chart.timeScale().setVisibleLogicalRange({ from: 0.5, to: points - 1.5 });
    }
  }

  const money = (v, dp = 2) => (v == null ? "—" : (v < 0 ? "-$" : "$") + fmt(Math.abs(v), dp));
  const moneyPM = (v, dp = 2) => (v == null ? "—" : (v >= 0 ? "+$" : "-$") + fmt(Math.abs(v), dp));
  const pctPM = (v, dp = 2) => (v == null ? "—" : (v >= 0 ? "+" : "") + fmt(v, dp) + "%");

  async function loadEarnings() {
    const body = $("earnOrders").querySelector("tbody");
    let d;
    try {
      d = await api("/api/earnings");
    } catch (e) {
      body.innerHTML = `<tr><td colspan="10" class="empty">${esc(e.message)}</td></tr>`;
      return;
    }
    if (!pnlChart) buildEarnCharts();

    const s = d.summary;
    const m = d.money;
    const open = d.open_positions
      ? `${d.open_positions} open position${d.open_positions > 1 ? "s" : ""} valued at the latest close`
      : "no open positions";
    $("earnRange").textContent = s.trades
      ? `${s.trades} closed trade${s.trades > 1 ? "s" : ""} · ${s.first_sale} to ${s.last_sale} · ${open}`
      : `Nothing closed yet · ${open}`;

    // Every figure is coloured by its sign: green when positive, red when
    // negative, plain when there is nothing to show.
    const sign = (v) => (v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");
    const cell = (label, value, by = null, title = "") =>
      `<div${title ? ` title="${esc(title)}"` : ""}><i>${esc(label)}</i>`
      + `<b class="${sign(by)}">${value}</b></div>`;
    $("earnSummary").innerHTML = s.priced_trades || s.open_trades
      ? cell("Account balance", money(s.balance, 2), null,
             "Uninvested cash plus what you hold, at the latest close")
        + cell("Trading return", pctPM(s.trading_return), s.trading_return,
               "How the trades performed. Money you add or take out does not count")
        + cell("Money in", money(s.capital_in, 0), null,
               s.capital_added ? `Starting balance plus ${money(s.capital_added, 0)} added later`
                               : "What you have put in")
        + cell("Growth on money in", pctPM(s.balance_pct), s.balance_pct,
               "Balance against everything you have put in")
        + cell("Realised", moneyPM(s.net), s.net, "Banked on closed trades, after commission")
        + cell("Unrealised", moneyPM(s.unrealised), s.unrealised,
               "On what you still hold, at the latest close")
        + cell("Gross profit", moneyPM(s.gross), s.gross, "Closed trades, before commission")
        + cell("Commission", money(s.fees), null,
               "Every commission paid, including the purchase of what you still hold"
               + (s.fees_open ? ` (${money(s.fees_open)} of it)` : "")
               + (s.fees_vs_gross_pct != null ? ` · ${s.fees_vs_gross_pct}% of gross profit` : ""))
        + cell("Win rate", s.win_rate == null ? "—" : `${s.win_rate}%`, s.win_rate,
               `${s.wins} of ${s.priced_trades} closed trades`)
        + cell("Average trade", `${moneyPM(s.avg_net)} (${pctPM(s.avg_net_pct)})`, s.avg_net)
        + cell("Best / worst",
               `<span class="${sign(s.best)}">${moneyPM(s.best)}</span> / `
               + `<span class="${sign(s.worst)}">${moneyPM(s.worst)}</span>`, null,
               "Your best and worst closed trades")
        + cell("Average hold", s.avg_hold_days == null ? "—" : `${s.avg_hold_days} days`, null)
      : cell("Account balance", "—", null, "Record a purchase to see this");

    // Daily, so an open position moves the line every day it is held.
    pctSeries.setData(m.pct_total);
    pctClosedSeries.setData(m.pct_closed);
    pnlSeries.setData(m.balance);
    for (const c of [pctChart, pnlChart]) frameSeries(c, m.balance.length);
    const lastPct = m.pct_total.at(-1);
    const lastPnl = m.balance.at(-1);
    $("earnPctNow").textContent = lastPct ? pctPM(lastPct.value) : "";
    $("earnPnlNow").textContent = lastPnl ? money(lastPnl.value) : "";

    if (!d.orders.length) {
      body.innerHTML = `<tr><td colspan="10" class="empty">`
        + `Nothing recorded yet. Use the Position button to record a purchase; `
        + `it appears here straight away, and again when you sell.</td></tr>`;
      return;
    }
    // Buys and sells of one trade sit next to each other, newest trade first.
    let rows = "";
    for (let i = 0; i < d.orders.length; i++) {
      const o = d.orders[i];
      const startsTrade = i === 0 || d.orders[i - 1].trade_id !== o.trade_id;
      const endsTrade = i === d.orders.length - 1 || d.orders[i + 1].trade_id !== o.trade_id;
      const pnl = o.net == null
        ? "—"
        : `<b class="${o.net >= 0 ? "pos" : "neg"}">${moneyPM(o.net)} (${pctPM(o.net_pct)})</b>`
          + (o.open ? '<span class="tag">unrealised</span>' : "");
      rows += `<tr class="trade${startsTrade ? " trade-start" : ""}`
        + `${endsTrade ? " trade-end" : ""}${o.open ? " open" : ""}">
        <td>${esc(o.date)}</td>
        <td>${esc(o.ticker)}</td>
        <td class="${o.side === "BUY" ? "side-buy" : "side-sell"}">${o.side}${
          o.open ? '<span class="tag">still held</span>' : ""}</td>
        <td>${o.quantity ? qty(o.quantity) : "—"}</td>
        <td>${fmt(o.price)}</td>
        <td>${o.value != null ? money(o.value) : "—"}</td>
        <td>${o.commission ? money(o.commission) : "—"}</td>
        <td>${o.hold_days != null ? `${o.hold_days}d` : ""}</td>
        <td>${pnl}</td>
        <td class="act"><button class="del" type="button" data-id="${o.trade_id}"
          data-side="${o.side}" data-ticker="${esc(o.ticker)}" data-date="${esc(o.date)}"
          title="${o.side === "BUY" ? "Remove this purchase and everything recorded against it"
                                    : "Remove this sale; the shares go back to being held"}"
          aria-label="Delete this order">×</button></td>
      </tr>`;
    }
    if (s.without_quantity) {
      rows += `<tr><td colspan="10" class="empty">`
        + `${s.without_quantity} trade${s.without_quantity > 1 ? "s have" : " has"} no quantity `
        + `recorded, so ${s.without_quantity > 1 ? "they are" : "it is"} left out of the dollar `
        + `figures and the charts.</td></tr>`;
    }
    body.innerHTML = rows;

    // Deleting a sale puts the shares back; deleting a purchase removes the
    // whole record, sale included, because a sale cannot outlive its buy.
    for (const btn of body.querySelectorAll(".del")) {
      btn.addEventListener("click", async () => {
        const { id, side, ticker, date } = btn.dataset;
        const undoSale = side === "SELL";
        const ask = undoSale
          ? `Remove the ${ticker} sale of ${date}? The shares go back to being held.`
          : `Remove the ${ticker} purchase of ${date}? Any sale recorded against it goes too.`;
        if (!window.confirm(ask)) return;
        try {
          await api(undoSale ? `/api/positions/${id}/reopen` : `/api/positions/${id}`,
                    { method: undoSale ? "POST" : "DELETE" });
          toast(undoSale ? `${ticker} is held again` : `Removed the ${ticker} purchase`);
          await loadEarnings();
          await refreshStates();
          if (ticker === selected) reloadAll();
        } catch (e) { toast(`Could not remove: ${e.message}`, true); }
      });
    }
  }

  // ── tuning ─────────────────────────────────────────────
  async function loadTune(ticker) {
    const body = $("tuneStats").querySelector("tbody");
    body.innerHTML = `<tr><td colspan="8" class="empty">Searching ${RANGES ? "" : ""}20 settings on ${esc(ticker)}. This takes a few seconds.</td></tr>`;
    $("tuneVerdict").textContent = "";
    $("tuneApply").hidden = true;
    tuneChoice = null;

    const asked = range().bars;
    const used = Math.max(asked, MIN_TUNE_BARS);
    const params = new URLSearchParams({ bars: used });
    let d;
    try {
      d = await api(`/api/tune/${ticker}?${params}`);
    } catch (e) {
      body.innerHTML = `<tr><td colspan="8" class="empty">${esc(e.message)}</td></tr>`;
      return;
    }
    tuneFor = ticker;
    tuneChoice = d.chosen;

    const widened = used > asked
      ? ` · Range too short to split, using the last ${used} bars`
      : "";
    $("tuneRange").textContent =
      `${d.name} · ${d.profile_label} · fit ${d.in_sample} · test ${d.out_sample}${widened}`;
    $("tuneRange").classList.toggle("warn-note", used > asked);

    // How much the fitting window actually told us about the test window.
    const corr = $("tuneCorr");
    corr.textContent = d.fit_test_r === null
      ? `fit → test: ${d.fit_test_note}`
      : `fit → test  r = ${d.fit_test_r >= 0 ? "+" : ""}${fmt(d.fit_test_r, 2)}  ·  ${d.fit_test_note}`;
    // Only a positive relationship earns the green badge. A negative one is a
    // warning, not an endorsement.
    const rv = d.fit_test_r;
    corr.className = "corr " + (rv === null ? ""
      : rv <= -0.2 ? "none"
      : rv < 0.2 ? "none"
      : rv >= 0.5 ? "some" : "");

    const cell = (v, suffix = "%", dp = 2) =>
      v == null ? "—" : `<span class="${v >= 0 ? "pos" : "neg"}">${v >= 0 ? "+" : ""}${fmt(v, dp)}${suffix}</span>`;

    let html = "";
    for (const r of d.results) {
      const isChosen = r.atr_len === d.chosen.atr_len && r.atr_mult === d.chosen.atr_mult;
      const isBase = d.baseline && r.atr_len === d.baseline.atr_len
                     && r.atr_mult === d.baseline.atr_mult;
      html += `<tr class="${isChosen ? "chosen" : (isBase ? "baseline" : "")}">
        <td>${r.atr_len}${isBase && !isChosen ? " <span class=\"assigned\">default</span>" : ""}</td>
        <td>${fmt(r.atr_mult, 1)}</td>
        <td>${r.is_trades}</td>
        <td>${cell(r.is_expectancy)}</td>
        <td>${r.oos_trades}</td>
        <td>${cell(r.oos_expectancy)}</td>
        <td>${cell(r.oos_return, "%", 0)}</td>
        <td class="neg">${r.oos_max_dd == null ? "—" : fmt(r.oos_max_dd, 0) + "%"}</td>
      </tr>`;
    }
    body.innerHTML = html;

    const v = $("tuneVerdict");
    v.textContent = d.verdict;
    v.className = "verdict-line" + (d.held_up ? " held" : " low");

    // Only offer to apply something that actually survived the split.
    $("tuneApply").hidden = !d.held_up;
    $("tuneApply").textContent =
      `Apply ATR ${d.chosen.atr_len} / ${fmt(d.chosen.atr_mult, 1)} to ${ticker}`;
  }

  // ── status + actions ───────────────────────────────────
  // The controls must drive the rail and readout as well as the chart.
  const controlParams = () => new URLSearchParams({
    atr_len: $("atrLen").value, atr_mult: $("atrMult").value, adx_min: $("adxMin").value,
  });

  let statesSeq = 0;

  async function refreshStates() {
    const seq = ++statesSeq;
    try {
      const rows = await api(`/api/states?${controlParams()}`);
      if (seq !== statesSeq) return;   // superseded by a newer request
      states = rows;
      renderRail();
      if (!selected && states.length) select(states[0].ticker);
    } catch (e) { toast(`Could not load watchlist: ${e.message}`, true); }
  }

  async function refreshHealth() {
    try {
      const h = await api("/api/health");
      const through = h.data_through
        ? `data through ${new Date(h.data_through + "T00:00:00").toLocaleDateString()}`
        : "no data yet";
      if (h.scanning) {
        // First run pulls five years for the whole watchlist. Say so, or an
        // empty rail looks like a failure.
        $("lastScan").textContent =
          `${h.watchlist} tickers · first scan running, this takes a few minutes`;
        // Poll faster while it works, then pick up the results automatically.
        clearTimeout(refreshHealth._t);
        refreshHealth._t = setTimeout(() => { refreshHealth(); refreshStates(); }, 5000);
        return;
      }
      const scanned = h.last_scan
        ? `last scan ${new Date(h.last_scan + "Z").toLocaleString()}`
        : "no scan yet";
      // Shown in the browser's own zone; the tooltip gives the rule behind it.
      const next = h.next_scan
        ? ` · next scan ${new Date(h.next_scan).toLocaleString([], {
            weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`
        : "";
      // An inert blackout changes the strategy silently, so it gets said out loud.
      const blackout = h.blackout_active === false
        ? " · no earnings dates — blackout inactive"
        : "";
      // Prices that stop arriving look like a calm market. Say it instead.
      // One missing session is normal on a US holiday.
      const behind = h.sessions_behind >= 2
        ? ` · ${h.sessions_behind} sessions behind — see Diagnose`
        : "";
      const failed = h.last_scan_failed
        ? ` · ${h.last_scan_failed}/${h.last_scan_tickers} tickers failed`
        : "";
      $("lastScan").textContent =
        `${h.watchlist} tickers · ${through} · ${scanned}${next}${behind}${failed}${blackout}`;
      $("lastScan").title = [
        h.scheduled ? `Scans: ${h.scheduled}` : "",
        h.timezone ? `Server time zone: ${h.timezone}` : "",
        h.schedule_warning ? `Warning: ${h.schedule_warning}` : "",
        h.sessions_behind >= 2
          ? `Prices are ${h.sessions_behind} sessions behind. Open /api/diagnose `
            + "or check the container log for fetch failures."
          : "",
      ].filter(Boolean).join("\n");
      defaultCommission = Number(h.default_commission) || 0;
      $("lastScan").classList.toggle("warn-note",
        h.blackout_active === false || Boolean(h.schedule_warning)
        || h.sessions_behind >= 2 || Boolean(h.last_scan_failed));
    } catch { $("lastScan").textContent = "backend unreachable"; }
  }

  // ── wiring ─────────────────────────────────────────────
  function init() {
    buildCharts();
    buildEquityChart();

    $("railList").addEventListener("click", (e) => {
      const row = e.target.closest(".row");
      if (row) select(row.dataset.t);
    });
    $("railList").addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const row = e.target.closest(".row");
      if (row) { e.preventDefault(); select(row.dataset.t); }
    });
    $("filterBox").addEventListener("input", renderRail);

    ["atrLen", "atrMult", "adxMin"].forEach((id) => {
      $(id).addEventListener("change", reloadSoon);
      $(id).addEventListener("input", reloadSoon);
    });
    $("range").addEventListener("change", reloadAll);
    $("tabChart").addEventListener("click", () => setView("chart"));
    $("tabTest").addEventListener("click", () => setView("test"));
    $("tabTune").addEventListener("click", () => setView("tune"));
    $("tabEarn").addEventListener("click", () => setView("earn"));
    $("earnExport").addEventListener("click", async () => {
      const btn = $("earnExport");
      btn.disabled = true;
      try {
        const auth = token() ? { "X-Auth-Token": token() } : {};
        const res = await fetch("/api/export/trades.csv", { headers: auth });
        if (!res.ok) throw new Error(res.statusText);
        const url = URL.createObjectURL(await res.blob());
        const a = document.createElement("a");
        a.href = url;
        a.download = `supertrendmoose-trades-${today()}.csv`;
        a.click();
        URL.revokeObjectURL(url);
        toast("Trades exported");
      } catch (e) {
        toast(`Export failed: ${e.message}`, true);
      } finally {
        btn.disabled = false;
      }
    });

    $("macdToggle").addEventListener("click", () => setMacd(!macdOpen));
    $("macdTf").value = macdTf;
    $("macdTfLabel").textContent = MACD_TF_NAME[macdTf];
    $("macdTf").addEventListener("change", () => {
      macdTf = $("macdTf").value in MACD_TF_NAME ? $("macdTf").value : "D";
      try { sessionStorage.setItem(MACD_TF_KEY, macdTf); } catch { /* storage blocked */ }
      $("macdTfLabel").textContent = MACD_TF_NAME[macdTf];
      macdFor = null;
      if (chartFor === selected) loadMacd(selected);
    });

    $("posToggle").addEventListener("click", () => {
      posOpen = !posOpen;
      $("posToggle").classList.toggle("is-on", posOpen);
      renderPosition(states.find((s) => s.ticker === selected));
    });

    $("gridExport").addEventListener("click", async () => {
      const btn = $("gridExport");
      btn.disabled = true;
      btn.textContent = "Building grid, this takes a moment";
      try {
        // Fetched rather than linked, because the auth token travels in a
        // header and a plain <a download> cannot carry one.
        const headers = token() ? { "X-Auth-Token": token() } : {};
        // Same window the Tune tab is showing, so the two reconcile.
        const want = Math.max(range().bars, MIN_TUNE_BARS);
        const res = await fetch(`/api/export/grid.csv?bars=${want}`, { headers });
        if (!res.ok) throw new Error(res.statusText);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `supertrendmoose-grid-${today()}.csv`;
        a.click();
        URL.revokeObjectURL(url);
        toast("Grid exported");
      } catch (e) {
        toast(`Export failed: ${e.message}`, true);
      } finally {
        btn.disabled = false;
        btn.textContent = "Export full grid";
      }
    });

    $("tuneApply").addEventListener("click", async () => {
      if (!selected || !tuneChoice) return;
      try {
        await api(`/api/tune/${selected}/apply?atr_len=${tuneChoice.atr_len}`
                  + `&atr_mult=${tuneChoice.atr_mult}`, { method: "POST" });
        toast(`${selected} now uses ATR ${tuneChoice.atr_len} / ${tuneChoice.atr_mult}`);
        await refreshStates();
        chartFor = testFor = null;
      } catch (e) { toast(`Could not apply: ${e.message}`, true); }
    });

    $("profile").addEventListener("change", async () => {
      const value = $("profile").value;
      const label = $("profile").selectedOptions[0].text;
      try {
        const r = await api(`/api/watchlist/profile?value=${value}`, { method: "POST" });
        toast(`Filter set to "${label}" for all ${r.updated} tickers`);
        await refreshStates();
        reloadAll();
      } catch (e) { toast(`Could not save filter: ${e.message}`, true); }
    });

    $("scanBtn").addEventListener("click", async () => {
      const btn = $("scanBtn");
      btn.disabled = true; btn.textContent = "Scanning";
      try {
        const r = await api("/api/scan?refresh_prices=true&send_alerts=false", { method: "POST" });
        toast(`Scanned ${r.tickers} tickers · ${r.buys.length} buy, ${r.exits.length} sell`
              + (r.failed.length ? ` · ${r.failed.length} fetch failed` : ""));
        await refreshStates(); await refreshHealth();
        reloadAll();
      } catch (e) { toast(`Scan failed: ${e.message}`, true); }
      finally { btn.disabled = false; btn.textContent = "Scan now"; }
    });

    $("loginGo").addEventListener("click", unlock);
    $("loginToken").addEventListener("keydown", (e) => { if (e.key === "Enter") unlock(); });

    // Probe once: if the server has no token set, or ours still works, go straight in.
    fetch("/api/states", token() ? { headers: { "X-Auth-Token": token() } } : {})
      .then((r) => {
        if (r.status === 401) { showLogin(); return; }
        refreshStates();
        refreshHealth();
        setInterval(refreshHealth, 120000);
      })
      .catch(() => toast("Backend unreachable", true));
  }

  document.addEventListener("DOMContentLoaded", init);
})();
