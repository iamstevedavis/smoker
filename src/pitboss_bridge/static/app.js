/* Smoker dashboard: read-only live view over /api/live, /api/history and /api/health. */
"use strict";

(() => {
  const HISTORY_PAGE = 10000; // /api/history's maximum limit
  const HISTORY_MAX_PAGES = 20;
  const HEALTH_POLL_MS = 5000;
  const WS_RETRY_MIN_MS = 1000;
  const WS_RETRY_MAX_MS = 30000;
  const OFFLINE_AFTER_MS = 5000; // websocket down this long counts as offline
  const TABLE_ROWS = 60;

  const SERIES = [
    { key: "grill", field: "grillTemp", label: "Grill", short: "Grill", color: "--series-grill" },
    { key: "set", field: "grillSetTemp", label: "Set", short: "Set", color: "--series-set", dashed: true, step: true },
    { key: "p1", field: "p1Temp", label: "Probe 1", short: "P1", color: "--series-p1" },
    { key: "p2", field: "p2Temp", label: "Probe 2", short: "P2", color: "--series-p2" },
  ];

  const ERROR_FLAGS = [
    ["highTempErr", "High temperature"],
    ["fanErr", "Fan fault"],
    ["hotErr", "Igniter fault"],
    ["motorErr", "Auger motor fault"],
    ["noPellets", "Out of pellets"],
    ["erL", "ErL (fire out / low temp)"],
    ["err1", "Error 1"],
    ["err2", "Error 2"],
    ["err3", "Error 3"],
  ];

  const $ = (id) => document.getElementById(id);

  const store = {
    get(key) {
      try { return localStorage.getItem("smoker." + key); } catch (e) { return null; }
    },
    set(key, value) {
      try { localStorage.setItem("smoker." + key, value); } catch (e) { /* storage unavailable */ }
    },
  };

  const state = {
    fields: {},
    source: null,
    lastObserved: null, // server ms of the newest reading
    clockOffset: 0, // server clock minus local clock, ms
    clockKnown: false,
    staleAfter: 30,
    health: null,
    healthOk: null,
    ws: null,
    wsOpen: false,
    everConnected: false,
    wsDownSince: Date.now(),
    retryDelay: WS_RETRY_MIN_MS,
    retryAt: null,
    retryTimer: null,
    hours: [1, 3, 6, 12].includes(Number(store.get("hours"))) ? Number(store.get("hours")) : 3,
    points: [], // {t, grill, set, p1, p2}, sorted by t
    hidden: new Set(),
    historyToken: 0,
    cursorX: null,
    layout: null,
  };

  const serverNow = () => Date.now() + state.clockOffset;

  function num(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  function unitSymbol() {
    return state.fields.isFahrenheit === false ? "°C" : "°F";
  }

  function fmtTemp(value) {
    const v = num(value);
    return v === null ? "--" : String(Math.round(v));
  }

  function fmtAge(seconds) {
    if (seconds === null || seconds === undefined) return "";
    if (seconds < 5) return "just now";
    if (seconds < 90) return Math.round(seconds) + "s ago";
    if (seconds < 5400) return Math.round(seconds / 60) + " min ago";
    return (seconds / 3600).toFixed(1) + " h ago";
  }

  function fmtTime(t) {
    return new Date(t).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function fmtTimeSeconds(t) {
    return new Date(t).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
  }

  // Points ---------------------------------------------------------------

  // Points are stored in Fahrenheit so a unit change mid-cook can't mix
  // units on the chart; they are converted to the display unit when drawn.
  function pointFromValues(t, values) {
    const celsius = values.isFahrenheit === false;
    const point = { t };
    for (const s of SERIES) {
      const v = num(values[s.field]);
      point[s.key] = v !== null && celsius ? v * 9 / 5 + 32 : v;
    }
    return point;
  }

  function toDisplay(point) {
    if (unitSymbol() === "°F") return point;
    const out = { t: point.t };
    for (const s of SERIES) {
      const v = point[s.key];
      out[s.key] = v === null ? null : (v - 32) * 5 / 9;
    }
    return out;
  }

  function addPoint(point) {
    const pts = state.points;
    const last = pts[pts.length - 1];
    if (!last || point.t > last.t) {
      pts.push(point);
    } else if (point.t !== last.t) {
      const i = lowerBound(pts, point.t);
      if (!pts[i] || pts[i].t !== point.t) pts.splice(i, 0, point);
    }
    trimPoints();
  }

  function trimPoints() {
    const cutoff = serverNow() - state.hours * 3600e3 - 60e3;
    const i = lowerBound(state.points, cutoff);
    if (i > 0) state.points.splice(0, i);
  }

  function lowerBound(pts, t) {
    let lo = 0;
    let hi = pts.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (pts[mid].t < t) lo = mid + 1; else hi = mid;
    }
    return lo;
  }

  // Live data ------------------------------------------------------------

  function onSnapshot(msg) {
    if (!msg || msg.available === false || !msg.observed_at) {
      renderStatus();
      return;
    }
    const observed = Date.parse(msg.observed_at);
    if (Number.isNaN(observed)) return;
    // age_seconds is computed by the server at send time, which gives the
    // clock offset without trusting the device clock to match the bridge.
    const age = num(msg.age_seconds) ?? 0;
    state.clockOffset = observed + age * 1000 - Date.now();
    // If history was loaded before the offset was known and the device clock
    // is well off, reload it so the window matches the bridge's time.
    if (!state.clockKnown) {
      state.clockKnown = true;
      if (state.historyToken > 0 && Math.abs(state.clockOffset) > 60e3) loadHistory();
    }
    if (state.lastObserved !== null && observed < state.lastObserved) return;
    const values = {};
    for (const [name, reading] of Object.entries(msg.fields || {})) {
      values[name] = reading && typeof reading === "object" ? reading.value : reading;
    }
    state.fields = values;
    state.source = msg.source || state.source;
    state.lastObserved = observed;
    addPoint(pointFromValues(observed, values));
    renderReadings();
    renderStatus();
    scheduleChart();
    if ($("table-view").open) renderTable();
  }

  function connectLive() {
    clearTimeout(state.retryTimer);
    state.retryAt = null;
    const url = new URL("api/live", location.href);
    url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    let ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      scheduleReconnect();
      return;
    }
    state.ws = ws;
    ws.onopen = () => {
      const reconnected = state.everConnected;
      state.everConnected = true;
      state.wsOpen = true;
      state.retryDelay = WS_RETRY_MIN_MS;
      renderStatus();
      pollHealth();
      // Fill any gap in the chart left while we were disconnected.
      if (reconnected) loadHistory();
    };
    ws.onmessage = (event) => {
      try { onSnapshot(JSON.parse(event.data)); } catch (e) { /* ignore bad frame */ }
    };
    ws.onclose = () => {
      if (state.ws !== ws) return;
      if (state.wsOpen) state.wsDownSince = Date.now();
      state.wsOpen = false;
      state.ws = null;
      scheduleReconnect();
      renderStatus();
    };
    ws.onerror = () => ws.close();
  }

  function scheduleReconnect() {
    const jitter = 0.8 + Math.random() * 0.4;
    const delay = Math.min(state.retryDelay * jitter, WS_RETRY_MAX_MS);
    state.retryDelay = Math.min(state.retryDelay * 2, WS_RETRY_MAX_MS);
    state.retryAt = Date.now() + delay;
    clearTimeout(state.retryTimer);
    state.retryTimer = setTimeout(connectLive, delay);
  }

  async function pollHealth() {
    try {
      const res = await fetch("api/health", { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      state.health = await res.json();
      state.healthOk = true;
      const staleAfter = num(state.health.stale_after_seconds);
      if (staleAfter !== null) state.staleAfter = staleAfter;
      // If the socket is down but the bridge answers, retry straight away.
      if (!state.ws && state.retryAt !== null) connectLive();
    } catch (e) {
      state.healthOk = false;
    }
    renderStatus();
  }

  async function loadInitialState() {
    try {
      const res = await fetch("api/state", { cache: "no-store" });
      if (res.ok) onSnapshot(await res.json());
    } catch (e) { /* the websocket and health poll report failures */ }
  }

  async function loadHistory() {
    const token = ++state.historyToken;
    const wrap = $("chart-wrap");
    wrap.classList.add("is-loading");
    try {
      let since = new Date(serverNow() - state.hours * 3600e3).toISOString().replace("Z", "+00:00");
      const seen = new Set();
      const loaded = [];
      for (let page = 0; page < HISTORY_MAX_PAGES; page++) {
        const query = "limit=" + HISTORY_PAGE + "&since=" + encodeURIComponent(since);
        const res = await fetch("api/history?" + query, { cache: "no-store" });
        if (!res.ok) throw new Error("HTTP " + res.status);
        const body = await res.json();
        const items = Array.isArray(body.items) ? body.items : [];
        for (const item of items) {
          if (seen.has(item.id)) continue;
          seen.add(item.id);
          const t = Date.parse(item.observed_at);
          if (!Number.isNaN(t)) loaded.push(pointFromValues(t, item.fields || {}));
        }
        if (items.length < HISTORY_PAGE) break;
        since = items[items.length - 1].observed_at;
      }
      if (token !== state.historyToken) return;
      loaded.sort((a, b) => a.t - b.t);
      // Keep live points that arrived after the history snapshot.
      const lastLoaded = loaded.length ? loaded[loaded.length - 1].t : -Infinity;
      const live = state.points.filter((p) => p.t > lastLoaded);
      state.points = loaded.concat(live);
      trimPoints();
    } catch (e) {
      // Keep whatever we already have; the banner covers connectivity.
    } finally {
      if (token === state.historyToken) wrap.classList.remove("is-loading");
      scheduleChart();
      if ($("table-view").open) renderTable();
    }
  }

  // Rendering: readings --------------------------------------------------

  function setText(el, text) {
    if (el.textContent !== text) el.textContent = text;
  }

  function renderReadings() {
    const f = state.fields;
    const unit = unitSymbol();
    document.querySelectorAll("[data-unit]").forEach((el) => setText(el, unit));

    const grill = num(f.grillTemp);
    const set = num(f.grillSetTemp);
    setText($("grill-temp"), fmtTemp(grill));
    setText($("set-temp"), fmtTemp(set));

    let delta = "";
    if (grill !== null && set !== null) {
      const d = Math.round(grill - set);
      delta = d === 0 ? "At set" : Math.abs(d) + "° " + (d > 0 ? "above" : "below");
    }
    setText($("delta"), delta);

    // Meter spans 0 to 125% of the set point, so the mark sits at 80%.
    const scale = set !== null && set > 0 ? set * 1.25 : Math.max(grill || 0, 1);
    $("grill-fill").style.width = grill === null ? "0" : Math.min(100, (grill / scale) * 100) + "%";
    $("set-mark").style.display = set === null ? "none" : "";

    const powerTag = $("power-tag");
    powerTag.hidden = f.moduleIsOn !== false;

    renderProbe($("probe-1"), f.p1Temp, f.p1Target);
    renderProbe($("probe-2"), f.p2Temp, f.p2Target);

    renderIndicator($("ind-power"), f.moduleIsOn);
    renderIndicator($("ind-fan"), f.fanState);
    renderIndicator($("ind-igniter"), f.hotState);
    renderIndicator($("ind-auger"), f.motorState);

    renderErrors();
  }

  function renderProbe(el, tempValue, targetValue) {
    const temp = num(tempValue);
    const target = num(targetValue);
    setText(el.querySelector("[data-temp]"), fmtTemp(temp));
    const targetEl = el.querySelector("[data-target]");
    const fill = el.querySelector("[data-fill]");
    let stateName = "idle";
    targetEl.textContent = "";
    if (temp === null) {
      targetEl.textContent = "Not connected";
      fill.style.width = "0";
    } else if (target !== null && target > 0) {
      stateName = temp >= target ? "done" : "cooking";
      if (stateName === "done") {
        targetEl.textContent = "✓ Reached " + Math.round(target) + "°";
      } else {
        const strong = document.createElement("strong");
        strong.textContent = Math.round(target) + "°";
        targetEl.append("Target ", strong);
      }
      fill.style.width = Math.max(0, Math.min(100, (temp / target) * 100)) + "%";
    } else {
      targetEl.textContent = "No target";
      fill.style.width = "0";
    }
    el.dataset.state = stateName;
  }

  function renderIndicator(el, value) {
    const on = value === true ? "true" : value === false ? "false" : "unknown";
    el.dataset.on = on;
    setText(el.querySelector(".ind-state"), on === "true" ? "On" : on === "false" ? "Off" : "--");
  }

  function renderErrors() {
    const f = state.fields;
    const card = $("errors");
    const list = $("errors-list");
    const all = $("flags-all");
    const known = ERROR_FLAGS.filter(([name]) => typeof f[name] === "boolean");
    const active = ERROR_FLAGS.filter(([name]) => f[name] === true);

    list.replaceChildren(...active.map(([, label]) => {
      const li = document.createElement("li");
      li.textContent = label;
      return li;
    }));
    all.replaceChildren(...ERROR_FLAGS.map(([name, label]) => {
      const li = document.createElement("li");
      const a = document.createElement("span");
      const b = document.createElement("span");
      a.textContent = label;
      b.textContent = f[name] === true ? "ON" : f[name] === false ? "off" : "--";
      if (f[name] === true) li.className = "on";
      li.append(a, b);
      return li;
    }));

    if (active.length) {
      card.dataset.state = "alert";
      setText($("errors-title"), active.length === 1 ? "1 error flag on" : active.length + " error flags on");
    } else if (known.length) {
      card.dataset.state = "ok";
      setText($("errors-title"), "No error flags");
    } else {
      card.dataset.state = "unknown";
      setText($("errors-title"), "Error flags unavailable");
    }
  }

  // Rendering: connection status ----------------------------------------

  function computeStatus() {
    const now = serverNow();
    const age = state.lastObserved === null ? null : Math.max(0, (now - state.lastObserved) / 1000);
    const health = state.health;
    const wsDownFor = state.wsOpen ? 0 : Date.now() - state.wsDownSince;

    if (!state.wsOpen && (state.healthOk === false || wsDownFor > OFFLINE_AFTER_MS)) {
      const retryIn = state.retryAt ? Math.max(0, Math.ceil((state.retryAt - Date.now()) / 1000)) : null;
      return {
        kind: "offline",
        pill: "Offline",
        title: "Can't reach the bridge",
        detail: (age !== null ? "Last reading " + fmtAge(age) + ". " : "") +
          (retryIn ? "Retrying in " + retryIn + "s." : "Retrying…"),
        age,
      };
    }
    if (health && health.connected === false && (health.last_error || state.lastObserved !== null)) {
      return {
        kind: "disconnected",
        pill: "Disconnected",
        title: "Grill disconnected",
        detail: [
          age !== null ? "Last reading " + fmtAge(age) + "." : "",
          health.last_error ? "Error: " + health.last_error : "",
          "The bridge is retrying.",
        ].filter(Boolean).join(" "),
        age,
      };
    }
    if (age === null) {
      return {
        kind: "waiting",
        pill: state.wsOpen ? "Waiting" : "Connecting",
        title: "Waiting for the first reading",
        detail: "Make sure the grill is on and in range.",
        age,
      };
    }
    if (age > state.staleAfter) {
      return {
        kind: "stale",
        pill: "Stale",
        title: "Readings are out of date",
        detail: "No new reading for " + fmtAge(age).replace(" ago", "") + ". Values shown may not be current.",
        age,
      };
    }
    if (!state.wsOpen) return { kind: "reconnecting", pill: "Reconnecting", age };
    return { kind: "live", pill: "Live", age };
  }

  function renderStatus() {
    const s = computeStatus();
    const pill = $("status");
    pill.dataset.kind = s.kind;
    setText($("status-text"), s.pill);

    const banner = $("banner");
    if (s.title) {
      banner.hidden = false;
      banner.dataset.kind = s.kind;
      setText($("banner-title"), s.title);
      setText($("banner-detail"), s.detail || "");
    } else {
      banner.hidden = true;
    }

    const main = $("main");
    main.classList.toggle("is-waiting", s.age === null);
    main.classList.toggle("is-stale", s.age !== null && s.kind !== "live" && s.kind !== "reconnecting");

    setText($("foot-updated"), s.age === null ? "No readings yet" : "Updated " + fmtAge(s.age));
    const parts = [];
    const source = state.source || (state.health && state.health.source);
    if (source) parts.push("Source: " + source);
    if (state.health && state.health.reconnect_count) parts.push("Reconnects: " + state.health.reconnect_count);
    setText($("foot-source"), parts.join(" · "));
  }

  // Rendering: chart -----------------------------------------------------

  let chartFrame = 0;
  function scheduleChart() {
    if (!chartFrame) chartFrame = requestAnimationFrame(() => { chartFrame = 0; drawChart(); });
  }

  function niceStep(range, target) {
    const raw = range / Math.max(target, 1);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / mag;
    return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
  }

  // Average points into one bucket per pixel column so long windows stay fast.
  function downsample(pts, start, end, columns) {
    if (pts.length <= columns * 1.5) return pts;
    const span = end - start;
    const out = [];
    let col = -1;
    let acc = null;
    const flush = () => {
      if (!acc) return;
      const p = { t: acc.t / acc.n };
      for (const s of SERIES) p[s.key] = acc[s.key + "n"] ? acc[s.key] / acc[s.key + "n"] : null;
      out.push(p);
    };
    for (const p of pts) {
      const c = Math.floor(((p.t - start) / span) * columns);
      if (c !== col) {
        flush();
        col = c;
        acc = { t: 0, n: 0 };
        for (const s of SERIES) { acc[s.key] = 0; acc[s.key + "n"] = 0; }
      }
      acc.t += p.t;
      acc.n += 1;
      for (const s of SERIES) {
        if (p[s.key] !== null) { acc[s.key] += p[s.key]; acc[s.key + "n"] += 1; }
      }
    }
    flush();
    return out;
  }

  function drawChart() {
    const canvas = $("chart");
    const wrap = $("chart-wrap");
    const width = wrap.clientWidth;
    const height = wrap.clientHeight;
    if (!width || !height) return;
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const css = getComputedStyle(document.documentElement);
    const color = (name) => css.getPropertyValue(name).trim();
    const font = "12px system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";

    const end = serverNow();
    const start = end - state.hours * 3600e3;
    const pad = { l: 40, r: 46, t: 10, b: 24 };
    const pw = Math.max(width - pad.l - pad.r, 10);
    const ph = Math.max(height - pad.t - pad.b, 10);

    const visible = state.points.slice(lowerBound(state.points, start));
    const pts = downsample(visible, start, end, pw).map(toDisplay);
    const series = SERIES.filter((s) => !state.hidden.has(s.key));

    let lo = Infinity;
    let hi = -Infinity;
    for (const p of pts) {
      for (const s of series) {
        const v = p[s.key];
        if (v !== null) { if (v < lo) lo = v; if (v > hi) hi = v; }
      }
    }
    const empty = lo === Infinity;
    $("chart-empty").hidden = !empty;
    if (empty) { lo = 0; hi = 300; }
    if (hi - lo < 20) { const mid = (hi + lo) / 2; lo = mid - 10; hi = mid + 10; }
    const step = niceStep(hi - lo, Math.max(3, Math.floor(ph / 48)));
    const yMin = Math.floor(lo / step) * step;
    const yMax = Math.ceil(hi / step) * step;

    const x = (t) => pad.l + ((t - start) / (end - start)) * pw;
    const y = (v) => pad.t + (1 - (v - yMin) / (yMax - yMin)) * ph;

    // Grid and axes: recessive.
    ctx.font = font;
    ctx.lineWidth = 1;
    ctx.strokeStyle = color("--grid");
    ctx.fillStyle = color("--muted");
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let v = yMin; v <= yMax + step / 2; v += step) {
      const yy = Math.round(y(v)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(pad.l, yy);
      ctx.lineTo(pad.l + pw, yy);
      ctx.stroke();
      ctx.fillText(String(Math.round(v)) + "°", pad.l - 6, yy);
    }

    const minuteSteps = [5, 10, 15, 30, 60, 120, 180, 240];
    const maxLabels = Math.max(2, Math.floor(pw / 72));
    const stepMin = minuteSteps.find((m) => (state.hours * 60) / m <= maxLabels) || 240;
    const stepMs = stepMin * 60e3;
    const tzOffset = new Date().getTimezoneOffset() * 60e3;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (let t = Math.ceil((start - tzOffset) / stepMs) * stepMs + tzOffset; t <= end; t += stepMs) {
      const xx = x(t);
      if (xx < pad.l + 16 || xx > pad.l + pw - 16) continue;
      ctx.fillText(fmtTime(t), xx, pad.t + ph + 7);
    }

    // Break lines across gaps in the data instead of bridging them.
    const colMs = (end - start) / pw;
    const gapMs = Math.max(90e3, state.staleAfter * 2000, colMs * 3);

    const ends = [];
    ctx.save();
    ctx.beginPath();
    ctx.rect(pad.l, 0, pw + 2, height);
    ctx.clip();
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    for (const s of series) {
      ctx.strokeStyle = color(s.color);
      ctx.lineWidth = s.dashed ? 1.5 : 2;
      ctx.setLineDash(s.dashed ? [6, 4] : []);
      ctx.beginPath();
      let prev = null;
      let last = null;
      for (const p of pts) {
        const v = p[s.key];
        if (v === null) { prev = null; continue; }
        const px = x(p.t);
        const py = y(v);
        if (prev === null || p.t - prev.t > gapMs) {
          ctx.moveTo(px, py);
        } else if (s.step) {
          ctx.lineTo(px, y(prev.v));
          ctx.lineTo(px, py);
        } else {
          ctx.lineTo(px, py);
        }
        prev = { t: p.t, v };
        last = { px, py, v };
      }
      ctx.stroke();
      if (last) ends.push({ s, px: last.px, py: last.py, v: last.v });
    }
    ctx.restore();
    ctx.setLineDash([]);

    // Direct labels at the right edge, nudged apart so they don't collide.
    ends.sort((a, b) => a.py - b.py);
    for (let i = 1; i < ends.length; i++) {
      if (ends[i].py - ends[i - 1].py < 14) ends[i].py = ends[i - 1].py + 14;
    }
    ctx.font = "600 " + font;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillStyle = color("--text-2");
    for (const e of ends) {
      ctx.fillText(e.s.short, Math.min(e.px, pad.l + pw) + 6, Math.min(Math.max(e.py, pad.t + 6), pad.t + ph));
    }

    state.layout = { pts, x, y, pad, pw, ph, series, color };
    drawCursor(ctx);
  }

  function nearestIndex(pts, x, targetX) {
    let best = -1;
    let bestDist = Infinity;
    // Points are sorted by t, so binary-search the x position.
    let lo = 0;
    let hi = pts.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const d = x(pts[mid].t) - targetX;
      if (Math.abs(d) < bestDist) { bestDist = Math.abs(d); best = mid; }
      if (d < 0) lo = mid + 1; else hi = mid - 1;
    }
    return best;
  }

  function drawCursor(ctx) {
    const tip = $("tooltip");
    const L = state.layout;
    if (state.cursorX === null || !L || !L.pts.length) { tip.hidden = true; return; }
    const i = nearestIndex(L.pts, L.x, state.cursorX);
    if (i < 0) { tip.hidden = true; return; }
    const p = L.pts[i];
    const px = L.x(p.t);
    if (Math.abs(px - state.cursorX) > 40) { tip.hidden = true; return; }

    ctx.strokeStyle = L.color("--muted");
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(Math.round(px) + 0.5, L.pad.t);
    ctx.lineTo(Math.round(px) + 0.5, L.pad.t + L.ph);
    ctx.stroke();
    for (const s of L.series) {
      const v = p[s.key];
      if (v === null) continue;
      ctx.beginPath();
      ctx.arc(px, L.y(v), 4.5, 0, Math.PI * 2);
      ctx.fillStyle = L.color(s.color);
      ctx.strokeStyle = L.color("--surface");
      ctx.lineWidth = 2;
      ctx.fill();
      ctx.stroke();
    }

    const time = document.createElement("div");
    time.className = "tt-time";
    time.textContent = fmtTimeSeconds(p.t);
    const rows = L.series.map((s) => {
      const row = document.createElement("div");
      row.className = "tt-row";
      const key = document.createElement("span");
      key.className = "key key-" + s.key;
      const value = document.createElement("strong");
      value.textContent = p[s.key] === null ? "--" : Math.round(p[s.key]) + unitSymbol();
      const label = document.createElement("span");
      label.textContent = s.label;
      row.append(key, value, label);
      return row;
    });
    tip.replaceChildren(time, ...rows);
    tip.hidden = false;
    const wrapWidth = $("chart-wrap").clientWidth;
    const tipWidth = tip.offsetWidth;
    const left = px + 14 + tipWidth > wrapWidth ? px - 14 - tipWidth : px + 14;
    tip.style.left = Math.max(0, left) + "px";
  }

  function renderTable() {
    const body = $("table-body");
    const rows = state.points.slice(-TABLE_ROWS).reverse().map(toDisplay).map((p) => {
      const tr = document.createElement("tr");
      const cells = [fmtTimeSeconds(p.t), ...SERIES.map((s) => fmtTemp(p[s.key]))];
      for (const text of cells) {
        const td = document.createElement("td");
        td.textContent = text;
        tr.append(td);
      }
      return tr;
    });
    body.replaceChildren(...rows);
  }

  // Controls ---------------------------------------------------------------

  function setupControls() {
    const seg = document.querySelectorAll(".seg button");
    const markRange = () => seg.forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.hours) === state.hours)));
    markRange();
    seg.forEach((b) => b.addEventListener("click", () => {
      const hours = Number(b.dataset.hours);
      if (hours === state.hours) return;
      state.hours = hours;
      store.set("hours", String(hours));
      markRange();
      scheduleChart();
      loadHistory();
    }));

    document.querySelectorAll("#legend button").forEach((b) => b.addEventListener("click", () => {
      const key = b.dataset.series;
      if (state.hidden.has(key)) state.hidden.delete(key); else state.hidden.add(key);
      b.setAttribute("aria-pressed", String(!state.hidden.has(key)));
      scheduleChart();
    }));

    const canvas = $("chart");
    const toLocalX = (event) => event.clientX - canvas.getBoundingClientRect().left;
    canvas.addEventListener("pointermove", (e) => { state.cursorX = toLocalX(e); scheduleChart(); });
    canvas.addEventListener("pointerdown", (e) => { state.cursorX = toLocalX(e); scheduleChart(); });
    canvas.addEventListener("pointerleave", () => { state.cursorX = null; scheduleChart(); });
    canvas.addEventListener("blur", () => { state.cursorX = null; scheduleChart(); });
    canvas.addEventListener("keydown", (e) => {
      const L = state.layout;
      if (!L || !L.pts.length || (e.key !== "ArrowLeft" && e.key !== "ArrowRight")) return;
      e.preventDefault();
      let i = state.cursorX === null ? L.pts.length - 1 : nearestIndex(L.pts, L.x, state.cursorX);
      const jump = e.shiftKey ? 10 : 1;
      i = Math.max(0, Math.min(L.pts.length - 1, i + (e.key === "ArrowLeft" ? -jump : jump)));
      state.cursorX = L.x(L.pts[i].t);
      scheduleChart();
    });

    $("table-view").addEventListener("toggle", () => { if ($("table-view").open) renderTable(); });

    if ("ResizeObserver" in window) new ResizeObserver(scheduleChart).observe($("chart-wrap"));
    else window.addEventListener("resize", scheduleChart);

    setupTheme();
  }

  function setupTheme() {
    const btn = $("theme");
    const modes = ["auto", "light", "dark"];
    const apply = (mode) => {
      if (mode === "auto") delete document.documentElement.dataset.theme;
      else document.documentElement.dataset.theme = mode;
      btn.textContent = mode[0].toUpperCase() + mode.slice(1);
      btn.setAttribute("aria-label", "Theme: " + mode + ". Click to change.");
      const meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.content = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
      scheduleChart();
    };
    let mode = store.get("theme");
    if (!modes.includes(mode)) mode = "auto";
    apply(mode);
    btn.addEventListener("click", () => {
      mode = modes[(modes.indexOf(mode) + 1) % modes.length];
      store.set("theme", mode);
      apply(mode);
    });
    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => apply(mode));
    }
  }

  // Start ----------------------------------------------------------------

  function start() {
    setupControls();
    renderReadings();
    renderStatus();
    // History's time window is based on the bridge clock, so learn the clock
    // offset from the current state first.
    loadInitialState().finally(loadHistory);
    connectLive();
    pollHealth();
    setInterval(pollHealth, HEALTH_POLL_MS);
    setInterval(renderStatus, 1000);
    // Slide the chart window forward even when no readings arrive.
    setInterval(scheduleChart, 15000);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") return;
      if (!state.ws) connectLive();
      pollHealth();
      scheduleChart();
    });

    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("sw.js").catch(() => { /* needs HTTPS or localhost */ });
    }
  }

  start();
})();
