/* Smokestack dashboard: live view over /api/live, /api/history and /api/health,
   plus the bridge settings panel (/api/settings). Nothing here writes to the grill. */
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

  // Auth (the password can be turned off in Settings) --------------------

  async function clearShellCache() {
    if (!("caches" in window)) return;
    try {
      const keys = await caches.keys();
      await Promise.all(keys.filter((key) => key.startsWith("smoker-shell")).map((key) => caches.delete(key)));
    } catch (e) { /* cache storage unavailable */ }
  }

  async function toLogin() {
    // Drop the cached shell so a signed-out device can't reopen it offline.
    await clearShellCache();
    location.replace("login");
  }

  // fetch() that sends a signed-out user to the login page.
  async function apiFetch(path, options) {
    const res = await fetch(path, options);
    if (res.status === 401) {
      toLogin();
      throw new Error("not authenticated");
    }
    return res;
  }

  // /api/session is open to everyone, so it says whether we're signed in
  // rather than answering 401. Returns null if the bridge is unreachable.
  async function getSession() {
    try {
      const res = await fetch("api/session", { cache: "no-store" });
      return res.ok ? await res.json() : null;
    } catch (e) {
      return null;
    }
  }

  async function checkSession() {
    const session = await getSession();
    if (session && (session.setup_required || (session.auth_enabled && !session.authenticated))) {
      toLogin();
    }
    return session;
  }

  async function setupLogout() {
    const btn = $("logout");
    btn.addEventListener("click", async () => {
      try { await fetch("api/logout", { method: "POST" }); } catch (e) { /* still leave */ }
      toLogin();
    });
    const session = await checkSession();
    if (session && session.auth_enabled) btn.hidden = false;
  }

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
    // Targets are shown in the grill's unit, which may have just changed.
    renderAlertTargets();
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
      opened = true;
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
    let opened = false;
    ws.onclose = () => {
      if (state.ws !== ws) return;
      // Browsers hide why a handshake failed, so ask whether the session ended.
      if (!opened) checkSession();
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
      const res = await apiFetch("api/state", { cache: "no-store" });
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
        const res = await apiFetch("api/history?" + query, { cache: "no-store" });
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
    if (state.health && state.health.version) parts.push("v" + state.health.version);
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

  // Probe alert targets (ntfy) --------------------------------------------

  const alerts = { targets: { 1: null, 2: null }, enabled: null, busy: false };

  function probeAlert(probe) {
    return document.querySelector('.probe-alert[data-probe="' + probe + '"]');
  }

  // A saved target in the display unit, or null when it has no unit (saved
  // before units were recorded) and has to be entered again.
  function targetForDisplay(saved) {
    if (saved === null || saved.unit === null) return null;
    const unit = unitSymbol() === "°C" ? "C" : "F";
    if (saved.unit === unit) return saved.target;
    return unit === "C" ? (saved.target - 32) * 5 / 9 : saved.target * 9 / 5 + 32;
  }

  function renderAlertTargets() {
    for (const probe of [1, 2]) {
      const box = probeAlert(probe);
      const saved = alerts.targets[probe];
      const target = targetForDisplay(saved);
      const stale = saved !== null && target === null;
      box.dataset.set = String(saved !== null);
      box.dataset.stale = String(stale);
      box.dataset.enabled = String(alerts.enabled !== false);
      let label = "Set alert";
      if (stale) label = "Re-enter alert";
      else if (target !== null) label = "Alert at " + Math.round(target * 10) / 10 + unitSymbol();
      if (saved !== null && alerts.enabled === false) label += " (off)";
      setText(box.querySelector("[data-alert-label]"), label);
      const button = box.querySelector("[data-alert-open]");
      button.title = alerts.enabled === false ? "ntfy isn't set up, so no alert will be sent" : "";
    }
  }

  function applyAlertTargets(body) {
    const saved = (probe) => {
      const target = num(body["probe" + probe]);
      const unit = body["probe" + probe + "_unit"];
      return target === null ? null : { target, unit: unit === "F" || unit === "C" ? unit : null };
    };
    alerts.targets = { 1: saved(1), 2: saved(2) };
    alerts.enabled = Boolean(body.alerts_enabled);
    renderAlertTargets();
  }

  async function loadAlertTargets() {
    try {
      const res = await apiFetch("api/probe-targets", { cache: "no-store" });
      if (res.ok) applyAlertTargets(await res.json());
    } catch (e) { /* shown as unset until the bridge answers */ }
  }

  function alertNote(box, text, kind) {
    const note = box.querySelector("[data-alert-note]");
    note.textContent = text;
    note.dataset.kind = kind || "";
  }

  function toggleAlertForm(probe, open) {
    const box = probeAlert(probe);
    const form = box.querySelector("[data-alert-form]");
    const button = box.querySelector("[data-alert-open]");
    form.hidden = !open;
    button.hidden = open;
    button.setAttribute("aria-expanded", String(open));
    if (!open) {
      button.focus();
      return;
    }
    // The value is entered in the unit shown now. It's sent with the target,
    // so a unit change while the form is open can't reinterpret it.
    form.dataset.unit = unitSymbol() === "°C" ? "C" : "F";
    setText(form.querySelector("[data-alert-unit]"), unitSymbol());
    const input = form.querySelector("input");
    const saved = alerts.targets[probe];
    const target = targetForDisplay(saved);
    input.value = target === null ? "" : String(Math.round(target * 10) / 10);
    form.querySelector("[data-alert-clear]").hidden = saved === null;
    let note = alerts.enabled === false ? "ntfy isn't set up, so no alert will be sent. Add a topic URL in Settings." : "";
    if (saved !== null && target === null) {
      note = "This alert was saved before the bridge recorded units, so it's off until you enter it again.";
    }
    alertNote(box, note, "");
    input.focus();
    input.select();
  }

  async function saveAlertTarget(probe, value, unit) {
    if (alerts.busy) return;
    const box = probeAlert(probe);
    alerts.busy = true;
    box.querySelector(".alert-save").disabled = true;
    try {
      const res = await apiFetch("api/probe-targets", {
        method: "PUT",
        cache: "no-store",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(unit ? { ["probe" + probe]: value, unit } : { ["probe" + probe]: value }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        let message = "Save failed (HTTP " + res.status + ").";
        if (res.status === 422) message = "Enter a temperature from 1 to 1000.";
        if (res.status === 409) message = "The grill hasn't reported its temperature unit yet. Try again once it's connected.";
        alertNote(box, message, "error");
        return;
      }
      applyAlertTargets(body);
      toggleAlertForm(probe, false);
    } catch (e) {
      if (e.message !== "not authenticated") alertNote(box, "Can't reach the bridge.", "error");
    } finally {
      alerts.busy = false;
      box.querySelector(".alert-save").disabled = false;
    }
  }

  function setupAlertTargets() {
    for (const probe of [1, 2]) {
      const box = probeAlert(probe);
      const form = box.querySelector("[data-alert-form]");
      box.querySelector("[data-alert-open]").addEventListener("click", () => toggleAlertForm(probe, true));
      box.querySelector("[data-alert-cancel]").addEventListener("click", () => toggleAlertForm(probe, false));
      box.querySelector("[data-alert-clear]").addEventListener("click", () => saveAlertTarget(probe, null));
      form.addEventListener("keydown", (event) => {
        if (event.key === "Escape") toggleAlertForm(probe, false);
      });
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const raw = form.querySelector("input").value.trim();
        if (raw === "") return saveAlertTarget(probe, null);
        const value = Number(raw);
        if (!Number.isFinite(value) || value <= 0 || value > 1000) {
          alertNote(box, "Enter a temperature from 1 to 1000.", "error");
          return;
        }
        saveAlertTarget(probe, value, form.dataset.unit);
      });
    }
    renderAlertTargets();
    loadAlertTargets();
  }

  // Settings -------------------------------------------------------------

  const SETTING_GROUPS = {
    grill: "Grill",
    mqtt: "Home Assistant (MQTT)",
    alerts: "Alerts (ntfy)",
    data: "Data",
    control: "Grill control",
    logging: "Logging",
  };

  const SOURCE_LABELS = { http: "WiFi (http)", ble: "Bluetooth (ble)", fake: "Fake (demo data)" };
  const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
  const ORIGIN_LABELS = { default: "Default", env: ".env", ui: "UI" };
  const APPLY_WATCH_MS = 30000;
  const PASSWORD_MIN_LENGTH = 8;

  // type: text | number | int | bool | secret | source | log
  const SETTING_META = {
    source: { label: "Source", type: "source", hint: "Bluetooth is listed only when the bridge can reach the host's Bluetooth (docker-compose.ble.yml)." },
    model: { label: "Grill model", type: "text" },
    host: { label: "Grill address", type: "text", hint: "WiFi source: IP or host:port." },
    device_name: { label: "Bluetooth name", type: "text", hint: "Bluetooth source only." },
    password: { label: "Grill password", type: "secret" },
    poll_seconds: { label: "Poll every", type: "number", unit: "s", min: 0.1 },
    stale_after_seconds: { label: "Stale after", type: "int", unit: "s", min: 1 },
    mqtt_host: { label: "Broker host", type: "text", hint: "Blank turns MQTT off." },
    mqtt_port: { label: "Broker port", type: "int", min: 1, max: 65535 },
    mqtt_user: { label: "User", type: "text" },
    mqtt_password: { label: "Password", type: "secret" },
    mqtt_prefix: { label: "Topic prefix", type: "text" },
    ntfy_url: { label: "ntfy topic URL", type: "text", inputmode: "url", hint: "Blank turns alerts off. Probe targets are set on each probe's card." },
    ntfy_token: { label: "Access token", type: "secret" },
    alert_drift_degrees: { label: "Drift alert at", type: "number", unit: "°F", min: 0 },
    alert_drift_minutes: { label: "…sustained for", type: "number", unit: "min", min: 0 },
    alert_stale_seconds: { label: "No-data alert after", type: "number", unit: "s", min: 0 },
    alert_cooldown_seconds: { label: "Repeat cooldown", type: "number", unit: "s", min: 0 },
    retention_days: { label: "Keep history", type: "number", unit: "days", min: 0, hint: "0 keeps everything." },
    enable_control: { label: "Grill control", type: "bool", hint: "Lets signed-in users turn the grill off, set its temperature and restart its WiFi module from the dashboard. Needs the login switched on. It can never turn the grill on." },
    log_level: { label: "Log level", type: "log" },
  };

  const settings = {
    payload: null,
    fields: {}, // name -> field from the API
    changing: new Set(), // secrets the user chose to change
    busy: false,
    watchToken: 0,
  };

  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.assign(node, props || {});
    for (const child of children || []) if (child !== null) node.append(child);
    return node;
  }

  function settingStatus(text, kind) {
    const status = $("settings-status");
    status.textContent = text;
    status.dataset.kind = kind || "";
  }

  function settingInput(field, meta) {
    const id = "set-" + field.name;
    const value = field.value;
    if (meta.type === "bool") {
      return el("select", { id, name: field.name }, [["true", "On"], ["false", "Off"]].map(([option, label]) =>
        el("option", { value: option, textContent: label, selected: String(Boolean(value)) === option })));
    }
    if (meta.type === "source" || meta.type === "log") {
      const options = meta.type === "log" ? LOG_LEVELS : settings.payload.sources.slice();
      // Keep a current value the image no longer offers visible.
      if (!options.includes(value)) options.push(value);
      return el("select", { id, name: field.name }, options.map((option) =>
        el("option", { value: option, textContent: SOURCE_LABELS[option] && meta.type === "source" ? SOURCE_LABELS[option] : option, selected: option === value })));
    }
    const numeric = meta.type === "number" || meta.type === "int";
    const input = el("input", {
      id,
      name: field.name,
      type: numeric ? "number" : meta.type === "secret" ? "password" : "text",
      value: value === null || value === undefined ? "" : String(value),
      autocomplete: meta.type === "secret" ? "new-password" : "off",
    });
    if (numeric) {
      input.step = meta.type === "int" ? "1" : "any";
      input.inputMode = meta.type === "int" ? "numeric" : "decimal";
      if (meta.min !== undefined) input.min = String(meta.min);
      if (meta.max !== undefined) input.max = String(meta.max);
    } else {
      input.spellcheck = false;
      input.autocapitalize = "off";
      if (meta.inputmode) input.inputMode = meta.inputmode;
    }
    return input;
  }

  function renderSettingField(field) {
    const meta = SETTING_META[field.name] || { label: field.name, type: field.secret ? "secret" : "text" };
    const row = el("div", { className: "setting" });
    row.dataset.name = field.name;
    const badge = el("span", { className: "origin", textContent: ORIGIN_LABELS[field.origin] || field.origin });
    badge.dataset.origin = field.origin;
    badge.title = field.origin === "ui" ? "Saved from this page" : field.origin === "env" ? "From " + field.env : "Built-in default";
    const head = el("div", { className: "setting-head" }, [
      el("label", { htmlFor: "set-" + field.name, textContent: meta.label }),
      badge,
    ]);
    if (field.origin === "ui") {
      const reset = el("button", { type: "button", className: "link-btn", textContent: "Reset" });
      reset.setAttribute("aria-label", "Reset " + meta.label + " to .env or default");
      reset.addEventListener("click", () => resetSetting(field.name));
      head.append(reset);
    }
    row.append(head);

    let control;
    if (meta.type === "secret" && !settings.changing.has(field.name)) {
      const isSet = field.value && field.value.set;
      control = el("div", { className: "secret" }, [
        el("span", { id: "set-" + field.name, textContent: isSet ? "••• set" : "Not set" }),
      ]);
      const change = el("button", { type: "button", className: "link-btn", textContent: isSet ? "Change" : "Set" });
      change.setAttribute("aria-label", (isSet ? "Change " : "Set ") + meta.label);
      change.addEventListener("click", () => {
        settings.changing.add(field.name);
        row.replaceWith(renderSettingField(field));
        const input = $("set-" + field.name);
        if (input) input.focus();
      });
      control.append(change);
    } else {
      const input = settingInput({ ...field, value: meta.type === "secret" ? "" : field.value }, meta);
      if (meta.type === "secret") input.placeholder = "New value (blank clears it)";
      control = meta.unit
        ? el("div", { className: "with-unit" }, [input, el("span", { className: "unit-label", textContent: meta.unit })])
        : input;
    }
    row.append(control);
    const hint = el("p", { className: "setting-hint", textContent: meta.hint || "" });
    hint.hidden = !meta.hint;
    row.append(hint);
    row.append(el("p", { className: "setting-error", hidden: true }));
    return row;
  }

  function renderSettings(payload) {
    settings.payload = payload;
    settings.fields = {};
    settings.changing.clear();
    const groups = $("settings-groups");
    groups.replaceChildren();
    for (const group of payload.groups) {
      const fieldset = el("fieldset", { className: "setting-group" }, [
        el("legend", { textContent: SETTING_GROUPS[group.name] || group.name }),
      ]);
      for (const field of group.fields) {
        settings.fields[field.name] = field;
        fieldset.append(renderSettingField(field));
      }
      groups.append(fieldset);
    }
    $("settings-envonly").textContent = payload.env_only && payload.env_only.length
      ? "Set only in .env: " + payload.env_only.join(", ") + "."
      : "";
  }

  // The fields whose inputs differ from what the bridge reported.
  function settingChanges() {
    const changes = {};
    const errors = {};
    for (const [name, field] of Object.entries(settings.fields)) {
      const meta = SETTING_META[name] || { type: field.secret ? "secret" : "text" };
      const input = $("set-" + name);
      if (!input || !("value" in input) || input.tagName === "SPAN") continue;
      if (meta.type === "secret") {
        if (settings.changing.has(name)) changes[name] = input.value;
        continue;
      }
      const raw = input.value.trim();
      let value = raw;
      if (meta.type === "number" || meta.type === "int") {
        if (raw === "" || !Number.isFinite(Number(raw))) {
          errors[name] = "Enter a number.";
          continue;
        } else value = Number(raw);
      }
      if (meta.type === "bool") value = raw === "true";
      if (value !== field.value) changes[name] = value;
    }
    return { changes, errors };
  }

  function showSettingErrors(errors) {
    document.querySelectorAll("#settings-groups .setting").forEach((row) => {
      const message = errors[row.dataset.name];
      const box = row.querySelector(".setting-error");
      box.textContent = message || "";
      box.hidden = !message;
      row.classList.toggle("has-error", Boolean(message));
    });
    const first = document.querySelector("#settings-groups .has-error input, #settings-groups .has-error select");
    if (first) first.focus();
  }

  // FastAPI's 422 detail is a list of {loc: ["body", field], msg}, or a string.
  function errorsFrom(detail) {
    const errors = {};
    let general = "";
    if (Array.isArray(detail)) {
      for (const item of detail) {
        const name = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : null;
        const msg = String(item.msg || "Invalid value").replace(/^Value error, /, "");
        if (name && settings.fields[name]) errors[name] = msg;
        else general = general || (name ? name + ": " : "") + msg;
      }
    } else if (detail) {
      general = String(detail);
    }
    return { errors, general };
  }

  async function sendSettings(path, options) {
    if (settings.busy) return;
    settings.busy = true;
    $("settings-save").disabled = true;
    settingStatus("Applying…", "busy");
    try {
      const res = await apiFetch(path, { cache: "no-store", ...options });
      const body = await res.json().catch(() => ({}));
      if (res.status === 422) {
        const { errors, general } = errorsFrom(body.detail);
        showSettingErrors(errors);
        settingStatus(general || "Fix the highlighted fields. Nothing was changed.", "error");
        return;
      }
      if (!res.ok) {
        settingStatus(body.detail ? String(body.detail) : "Save failed (HTTP " + res.status + ").", "error");
        return;
      }
      renderSettings(body);
      watchApply();
    } catch (e) {
      if (e.message !== "not authenticated") settingStatus("Can't reach the bridge.", "error");
    } finally {
      settings.busy = false;
      $("settings-save").disabled = false;
    }
  }

  function saveSettings() {
    const { changes, errors } = settingChanges();
    showSettingErrors(errors);
    if (Object.keys(errors).length) {
      settingStatus("Fix the highlighted fields. Nothing was changed.", "error");
      return;
    }
    if (!Object.keys(changes).length) {
      settingStatus("No changes to save.", "");
      return;
    }
    sendSettings("api/settings", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(changes),
    });
  }

  function resetSetting(name) {
    sendSettings("api/settings/" + encodeURIComponent(name), { method: "DELETE" });
  }

  // After applying, follow /api/health until the new runtime has a reading.
  async function watchApply() {
    const token = ++settings.watchToken;
    const until = Date.now() + APPLY_WATCH_MS;
    settingStatus("Saved. Reconnecting to the grill…", "busy");
    while (token === settings.watchToken) {
      let health = null;
      try {
        const res = await fetch("api/health", { cache: "no-store" });
        if (res.ok) health = await res.json();
      } catch (e) { /* keep waiting */ }
      if (token !== settings.watchToken) return;
      if (health) {
        state.health = health;
        state.healthOk = true;
        renderStatus();
        if (health.connected) {
          settingStatus("Saved. Connected to the grill (" + health.source + ").", "ok");
          return;
        }
      }
      if (Date.now() > until) {
        const error = health && health.last_error ? ": " + health.last_error : "";
        settingStatus("Saved, but no reading from the grill yet" + error, "error");
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  }

  // Login section: turn the password off and on --------------------------

  const authUi = { busy: false };

  function authStatus(text, kind) {
    const status = $("auth-status");
    if (!status) return;
    status.textContent = text;
    status.dataset.kind = kind || "";
  }

  // A small password form inside the settings form; Enter runs `submit`
  // instead of saving the other settings.
  function authForm(inputs, label, submit) {
    const fields = inputs.map(([id, , autocomplete]) => el("input", { id, type: "password", autocomplete }));
    const go = () => submit(fields.map((input) => input.value));
    for (const input of fields) {
      input.addEventListener("keydown", (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        go();
      });
    }
    const button = el("button", { type: "button", className: "login-btn", textContent: label });
    button.addEventListener("click", go);
    const rows = inputs.map(([id, text], i) => el("div", { className: "setting" }, [
      el("label", { htmlFor: id, textContent: text }),
      fields[i],
    ]));
    return el("div", { className: "auth-form", hidden: true }, [...rows, button]);
  }

  async function sendAuth(path, method, body, done) {
    if (authUi.busy) return;
    authUi.busy = true;
    authStatus("Saving…", "");
    try {
      const res = await apiFetch(path, {
        method,
        cache: "no-store",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        await loadAuth();
        authStatus(done, "ok");
        return;
      }
      const data = await res.json().catch(() => ({}));
      const message = res.status === 403 ? "Wrong password."
        : data.detail ? String(data.detail) : "Failed (HTTP " + res.status + ").";
      authStatus(message, "error");
    } catch (e) {
      if (e.message !== "not authenticated") authStatus("Can't reach the bridge.", "error");
    } finally {
      authUi.busy = false;
    }
  }

  function renderAuth(session) {
    const on = Boolean(session.auth_enabled);
    const summary = el("p", {
      className: "auth-state",
      textContent: on
        ? "A password is needed to open the dashboard."
        : "Off: anyone on your network can view the grill and change these settings.",
    });
    summary.dataset.on = String(on);

    // Turning it back on asks for the password, so this browser stays signed in.
    const enableForm = authForm(
      [["auth-enable", "Password", "current-password"]],
      "Turn on",
      ([password]) => sendAuth("api/auth", "PUT", { required: true, password }, "Password turned on."),
    );

    const toggle = el("button", { type: "button", className: "theme-btn", textContent: on ? "Turn off password" : "Turn on password" });
    toggle.addEventListener("click", () => {
      if (!on) {
        enableForm.hidden = !enableForm.hidden;
        authStatus("", "");
        if (!enableForm.hidden) enableForm.querySelector("input").focus();
        return;
      }
      if (confirm("Turn off the password? Anyone on your network will be able to open the dashboard and change settings.")) {
        sendAuth("api/auth", "PUT", { required: false }, "Password turned off.");
      }
    });

    const status = el("p", { id: "auth-status", className: "auth-status" });
    status.setAttribute("role", "status");
    const box = $("settings-auth");
    box.replaceChildren(
      el("legend", { textContent: "Login" }),
      summary,
      el("div", { className: "auth-actions" }, [toggle]),
      el("p", { className: "settings-note", textContent: "Change the password under Profile." }),
      enableForm,
      status,
    );
    box.hidden = false;
    $("logout").hidden = !on;
  }

  async function loadAuth() {
    const session = await checkSession();
    if (session) renderAuth(session);
  }

  async function openSettings() {
    const dialog = $("settings");
    settingStatus("", "");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    loadAuth();
    try {
      const res = await apiFetch("api/settings", { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      renderSettings(await res.json());
    } catch (e) {
      if (e.message !== "not authenticated") {
        $("settings-groups").replaceChildren(el("p", { className: "settings-note", textContent: "Can't load settings from the bridge." }));
      }
    }
  }

  function closeSettings() {
    settings.watchToken++;
    const dialog = $("settings");
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  // Profile: change the password ------------------------------------------

  function profileStatus(text, kind) {
    const status = $("profile-status");
    status.textContent = text;
    status.dataset.kind = kind || "";
  }

  async function changePassword() {
    const form = $("profile-form");
    const current = form.current.value;
    const next = form.new.value;
    if (!current) return profileStatus("Enter your current password.", "error");
    if (next.length < PASSWORD_MIN_LENGTH) return profileStatus("Use at least " + PASSWORD_MIN_LENGTH + " characters.", "error");
    if (next !== form.confirm.value) return profileStatus("The new passwords don't match.", "error");
    const button = $("profile-save");
    button.disabled = true;
    profileStatus("Saving…", "busy");
    try {
      const res = await apiFetch("api/auth/password", {
        method: "POST",
        cache: "no-store",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ current, new: next }),
      });
      if (res.ok) {
        form.reset();
        profileStatus("Password changed. Other devices are signed out.", "ok");
        return;
      }
      const data = await res.json().catch(() => ({}));
      profileStatus(res.status === 403 ? "Current password is wrong." : data.detail ? String(data.detail) : "Failed (HTTP " + res.status + ").", "error");
      if (res.status === 403) form.current.select();
    } catch (e) {
      if (e.message !== "not authenticated") profileStatus("Can't reach the bridge.", "error");
    } finally {
      button.disabled = false;
    }
  }

  function setupProfile() {
    const dialog = $("profile");
    $("profile-open").addEventListener("click", () => {
      $("profile-form").reset();
      profileStatus("", "");
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
      $("profile-current").focus();
    });
    $("profile-close").addEventListener("click", () => {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    });
    $("profile-form").addEventListener("submit", (event) => {
      event.preventDefault();
      changePassword();
    });
  }

  function setupSettings() {
    $("settings-open").addEventListener("click", openSettings);
    $("settings-close").addEventListener("click", closeSettings);
    $("settings").addEventListener("close", () => {
      settings.watchToken++;
      // Saving may have turned ntfy on or off.
      loadAlertTargets();
    });
    $("settings-form").addEventListener("submit", (event) => {
      event.preventDefault();
      saveSettings();
    });
  }

  // Start ----------------------------------------------------------------

  function start() {
    setupControls();
    setupLogout();
    setupSettings();
    setupProfile();
    setupAlertTargets();
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
      loadAlertTargets();
      scheduleChart();
    });

    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("sw.js").catch(() => { /* needs HTTPS or localhost */ });
    }
  }

  start();
})();
