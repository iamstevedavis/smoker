/* Smokestack dashboard: live view over /api/live, /api/history and /api/health,
   plus the bridge settings panel (/api/settings). The only writes to the grill
   are the Controls section's turn-off, set-temperature, unit switch, WiFi
   restart and password change (/api/control), each confirmed in a dialog
   first. */
"use strict";

(() => {
  const HISTORY_PAGE = 10000; // /api/history's maximum limit
  const HISTORY_MAX_PAGES = 20;
  const HEALTH_POLL_MS = 5000;
  const WS_RETRY_MIN_MS = 1000;
  const WS_RETRY_MAX_MS = 30000;
  const OFFLINE_AFTER_MS = 5000; // websocket down this long counts as offline
  const TABLE_ROWS = 60;

  // The user-facing names for the bridge's sources.
  const SOURCE_LABELS = { http: "WiFi", ble: "Bluetooth", fake: "Demo" };

  const SERIES = [
    { key: "grill", field: "grillTemp", label: "Grill", short: "Grill", color: "--series-grill" },
    { key: "set", field: "grillSetTemp", label: "Set", short: "Set", color: "--series-set", dashed: true, step: true },
    { key: "p1", field: "p1Temp", label: "Probe 1", short: "P1", color: "--series-probe-1" },
    { key: "p2", field: "p2Temp", label: "Probe 2", short: "P2", color: "--series-probe-2" },
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
    if (settings.open) {
      updateGrillStatus();
      updateSystem();
      updateNavState();
    }
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
    // The card says so in words, and shows a badge when the target is reached.
    const done = el.querySelector(".probe-done");
    if (done) done.hidden = stateName !== "done";
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
      li.className = "badge badge--error";
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

    renderSource();
    setText($("foot-updated"), s.age === null ? "No readings yet" : "Updated " + fmtAge(s.age));
    const parts = [];
    const source = activeSource();
    if (source) parts.push("Source: " + sourceLabel(source));
    if (state.health && state.health.reconnect_count) parts.push("Reconnects: " + state.health.reconnect_count);
    if (state.health && state.health.version) parts.push("v" + state.health.version);
    setText($("foot-source"), parts.join(" · "));
    // Controls pause while the data is stale.
    if (control.status) renderControls();
    // The settings panel's Grill section follows the same status.
    if (settings.open) updateGrillStatus();
  }

  // The source behind the readings on screen. A connected source has just
  // delivered a reading, so it is the live one; before the first reading, or
  // after Save & apply switches source, fall back to the source of the last
  // reading shown.
  function activeSource() {
    const health = state.health;
    if (health && health.connected && health.source) return health.source;
    return state.source || (health && health.source) || null;
  }

  function sourceLabel(source) {
    return SOURCE_LABELS[source] || source;
  }

  function renderSource() {
    const source = activeSource();
    const badge = $("source-badge");
    badge.hidden = !source;
    if (!source) return;
    const label = sourceLabel(source);
    setText($("source-text"), label);
    const name = "Source: " + label;
    if (badge.title !== name) {
      badge.title = name;
      badge.setAttribute("aria-label", name);
    }
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
    const font = '12px "Geist Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';

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
    ctx.strokeStyle = color("--color-border-subtle");
    ctx.fillStyle = color("--color-label-secondary");
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
    ctx.fillStyle = color("--color-label-secondary");
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

    ctx.strokeStyle = L.color("--color-label-secondary");
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
      ctx.strokeStyle = L.color("--color-surface-1");
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
    // Only the time-range buttons: the unit switch in Controls is a .seg too.
    const seg = document.querySelectorAll(".seg button[data-hours]");
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
      if (meta) meta.content = getComputedStyle(document.documentElement).getPropertyValue("--color-bg").trim();
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
    box.querySelector("[data-alert-save]").disabled = true;
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
      box.querySelector("[data-alert-save]").disabled = false;
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

  // Grill controls (/api/control) ---------------------------------------

  const CONTROL_POLL_MS = 5000;
  const COMMAND_LABELS = {
    "turn-off": "Turn off",
    "set-temperature": "Set temperature",
    reboot: "Restart WiFi",
    "set-unit": "Switch unit",
    "set-password": "Change password",
  };
  const UNIT_NAMES = { F: "Fahrenheit", C: "Celsius" };

  // `notice` is a request the bridge refused, shown until the next command.
  // `asked` and `applied` count the status requests, so a slow answer to an
  // older request cannot undo a newer one.
  const control = { status: null, sending: null, notice: null, asked: 0, applied: 0, live: false, busy: false, tempEdited: false };

  async function loadControl() {
    const asked = ++control.asked;
    try {
      const res = await apiFetch("api/control", { cache: "no-store" });
      if (res.ok) {
        const status = await res.json();
        // A poll asked for earlier can land after a later one, and the older
        // status would put the panel back into a state the bridge has already
        // left: "still sending" after the command is done, or the result
        // before it.
        if (asked > control.applied) {
          control.applied = asked;
          control.status = status;
        }
      }
    } catch (e) { /* keep the last status */ }
    renderControls();
  }

  function controlUnit() {
    const unit = control.status && control.status.unit;
    return unit === "C" ? "°C" : unit === "F" ? "°F" : unitSymbol();
  }

  // Nearest accepted setpoint, ties to the lower one, as the bridge snaps.
  function snapTemp(value) {
    const points = control.status && control.status.setpoints;
    if (!Array.isArray(points) || !points.length) return null;
    return points.reduce((best, p) => (Math.abs(p - value) < Math.abs(best - value) ? p : best), points[0]);
  }

  // The setpoints the grill accepts, in order.
  function tempSetpoints() {
    const points = control.status && control.status.setpoints;
    if (!Array.isArray(points)) return [];
    return points.filter((p) => typeof p === "number" && Number.isFinite(p)).sort((a, b) => a - b);
  }

  // The value the stepper moves from: what is typed, or the grill's set point.
  function tempStepValue(points) {
    const typed = Number($("control-temp").value);
    if (Number.isFinite(typed) && typed > 0) return typed;
    const current = num(state.fields.grillSetTemp);
    return current === null ? points[0] : current;
  }

  function fillTempInput() {
    const input = $("control-temp");
    const current = num(state.fields.grillSetTemp);
    if (!input || current === null || control.tempEdited) return;
    input.value = String(current);
  }

  // The stepper dims the button that has nowhere to go.
  function syncTempStepper() {
    const group = document.querySelector("#control-temp-form .stepper");
    if (!group) return;
    const [down, up] = group.querySelectorAll(".stepper-btn");
    const points = tempSetpoints();
    if (!points.length) {
      down.disabled = true;
      up.disabled = true;
      return;
    }
    const value = tempStepValue(points);
    // The grill can't be turned on from the bridge, so an off grill can't
    // have its temperature set either (#82).
    const blocked = !control.live || control.busy || state.fields.moduleIsOn === false;
    down.disabled = blocked || !points.some((p) => p < value);
    up.disabled = blocked || !points.some((p) => p > value);
  }

  // One step to the next setpoint the grill accepts, so a tap never lands on
  // a temperature it would refuse.
  function stepTemp(direction) {
    const points = tempSetpoints();
    if (!points.length) return false;
    const input = $("control-temp");
    const value = tempStepValue(points);
    const next = direction > 0
      ? points.find((p) => p > value)
      : points.slice().reverse().find((p) => p < value);
    if (next === undefined) return false;
    input.value = String(next);
    control.tempEdited = true;
    syncTempStepper();
    return true;
  }

  function setupTempStepper() {
    const group = document.querySelector("#control-temp-form .stepper");
    if (!group) return;
    const input = group.querySelector("input");
    const [down, up] = group.querySelectorAll(".stepper-btn");
    let wait = null;
    let repeat = null;
    const stop = () => {
      clearTimeout(wait);
      clearInterval(repeat);
      wait = null;
      repeat = null;
    };
    const hold = (direction) => {
      stop();
      if (!stepTemp(direction)) return;
      // Holding repeats, so a long press walks through the setpoints.
      wait = setTimeout(() => {
        repeat = setInterval(() => { if (!stepTemp(direction)) stop(); }, 90);
      }, 400);
    };
    for (const [button, direction] of [[down, -1], [up, 1]]) {
      button.addEventListener("pointerdown", () => hold(direction));
      for (const event of ["pointerup", "pointerleave", "pointercancel"]) {
        button.addEventListener(event, stop);
      }
      // A keyboard click has no pointer, so it steps once.
      button.addEventListener("click", (event) => { if (event.detail === 0) stepTemp(direction); });
    }
    input.addEventListener("input", () => { control.tempEdited = true; });
    // A typed value snaps to the nearest setpoint when the field loses focus.
    input.addEventListener("blur", () => {
      const points = tempSetpoints();
      const typed = Number(input.value);
      if (!points.length || !Number.isFinite(typed) || typed <= 0) return;
      const snapped = snapTemp(typed);
      if (snapped === null) return;
      input.value = String(snapped);
      control.tempEdited = true;
      syncTempStepper();
    });
  }

  function controlLive() {
    const kind = computeStatus().kind;
    return kind === "live" || kind === "reconnecting";
  }

  function describeCommand(result) {
    const label = COMMAND_LABELS[result.command] || result.command;
    const toUnit = result.args && result.args.unit;
    if (result.command === "set-unit" && toUnit) return label + " to °" + toUnit;
    if (result.command !== "set-temperature" || num(result.target) === null) return label;
    return label + " " + result.target + (result.unit ? "°" + result.unit : "");
  }

  // Every result state has its own wording.
  function resultWording(result) {
    const unit = result.unit ? "°" + result.unit : "";
    switch (result.outcome) {
      case "confirmed":
        if (result.command === "turn-off") return ["Done.", "The grill is off. The fan runs for about 10 minutes."];
        if (result.command === "set-temperature") return ["Done.", "The grill is set to " + result.target + unit + "."];
        if (result.command === "set-unit") return ["Done.", "The grill now uses °" + result.args.unit + "."];
        if (result.command === "set-password") return ["Done.", "The grill now uses the new password. The bridge saved it in its settings, so nothing needs changing in .env; use the new password in the Pit Boss app too."];
        return ["Done.", "The WiFi module restarted and the grill is reporting again."];
      case "already_applied":
        if (result.command === "turn-off") return ["Nothing sent.", "The grill was already off."];
        if (result.command === "set-unit") return ["Nothing sent.", "The grill already uses °" + result.args.unit + "."];
        return ["Nothing sent.", "The grill was already set to " + result.target + unit + "."];
      case "unconfirmed":
        if (result.command === "set-password") {
          return ["Not confirmed.", "The bridge can't tell whether the grill took the new password. It keeps both the old and the new one and keeps trying them, so it reconnects on its own if the grill kept one of them. If you want to settle it now, save the password in Settings, or reset it at the grill."];
        }
        return ["Not confirmed.", "The command was sent, but the grill didn't report the change in time. Check the grill."];
      case "failed":
        if (result.command === "set-password") {
          return ["Failed.", "The grill didn't take the new password, so the grill and the bridge still use the old password."];
        }
        return ["Failed.", (result.detail || "The grill didn't take the command.") + " Check the grill."];
      default:
        return [result.outcome || "", result.detail || ""];
    }
  }

  function renderControlResult() {
    const box = $("control-result");
    const s = control.status;
    const inFlight = control.sending || (s && s.in_flight);
    let outcome = null;
    let title = "";
    let detail = "";
    let meta = "";
    if (inFlight) {
      outcome = "pending";
      title = describeCommand({ command: inFlight.command, args: inFlight.args, target: inFlight.args && inFlight.args.temp, unit: s && s.unit }) + ":";
      detail = control.sending ? "sending and waiting for the grill to confirm…" : "another device is sending this command…";
    } else if (control.notice) {
      outcome = "rejected";
      title = "Not sent.";
      detail = control.notice;
    } else if (s && s.last_result) {
      const result = s.last_result;
      outcome = result.outcome;
      [title, detail] = resultWording(result);
      const at = Date.parse(result.finished_at || result.started_at);
      meta = describeCommand(result) + (Number.isNaN(at) ? "" : " · " + fmtTimeSeconds(at));
    }
    box.hidden = outcome === null;
    if (outcome === null) return;
    box.dataset.outcome = outcome;
    setText($("control-result-title"), title);
    setText($("control-result-detail"), detail);
    setText($("control-result-meta"), meta);
  }

  function renderControls() {
    const s = control.status;
    const section = $("controls");
    const live = controlLive();
    const busy = Boolean(control.sending || (s && s.in_flight));
    control.live = live;
    control.busy = busy;
    // Also shown while control is off if there is a log left to look at.
    section.hidden = !s || !(s.enabled || s.log_count > 0);
    if (section.hidden) return;
    setText($("control-log-open"), "Command log" + (s.log_count ? " (" + s.log_count + ")" : ""));
    const reason = $("controls-reason");
    reason.hidden = s.available;
    setText(reason, s.available ? "" : s.reason || "Grill control isn't available.");
    $("controls-actions").hidden = !s.available;
    $("controls-stale").hidden = live;
    // A password change the bridge hasn't settled: it needs the user.
    $("controls-pending").hidden = !s.pending_password_change;
    section.querySelectorAll("[data-control]").forEach((node) => { node.disabled = !live || busy; });
    // The bridge can't turn the grill on, so setting a temperature while it
    // is off would do nothing (#82).
    const off = state.fields.moduleIsOn === false;
    section.querySelectorAll("[data-temp-control]").forEach((node) => { node.disabled = !live || busy || off; });
    $("control-temp-hint").hidden = !off || !live;
    // The unit can only be switched while the grill is off (#58).
    section.querySelectorAll("[data-unit-target]").forEach((node) => {
      const current = node.dataset.unitTarget === s.unit;
      node.setAttribute("aria-pressed", String(current));
      node.disabled = !live || busy || !off || current || !s.unit;
    });
    $("control-unit-hint").hidden = off || !live;
    fillTempInput();
    syncTempStepper();
    renderControlResult();
  }

  // `variant` picks the button: the danger one for turning the grill off and
  // for deleting the log, the primary one for the other commands.
  function confirmControl(title, text, action, variant) {
    const dialog = $("control-confirm");
    if (typeof dialog.showModal !== "function") return Promise.resolve(confirm(text));
    setText($("control-confirm-title"), title);
    setText($("control-confirm-text"), text);
    const ok = $("control-confirm-ok");
    ok.classList.toggle("btn-danger", variant === "danger");
    ok.classList.toggle("btn-primary", variant !== "danger");
    setText(ok, action);
    dialog.returnValue = "";
    dialog.showModal();
    $("control-confirm-ok").focus();
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    });
  }

  // The arguments are shown while the command is in flight, so the password
  // change never shows one.
  function controlArgs(command, body, target) {
    if (command === "set-password") return {};
    if (target !== undefined) return { temp: target };
    return body || {};
  }

  // `target` is the snapped temperature, shown while the command is sent.
  async function sendControl(command, body, target) {
    if (control.sending) return null;
    control.sending = { command, args: controlArgs(command, body, target) };
    control.notice = null;
    renderControls();
    let sent = null;
    try {
      const res = await apiFetch("api/control/" + command, {
        method: "POST",
        cache: "no-store",
        headers: { "content-type": "application/json", "X-Smokestack-Control": "1" },
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        sent = data;
        if (control.status) control.status.last_result = data;
      } else {
        let message = typeof data.detail === "string" ? data.detail : "HTTP " + res.status;
        if (res.status === 409 && !data.detail) message = "Another command is still being confirmed.";
        control.notice = message.charAt(0).toUpperCase() + message.slice(1) + ".";
      }
    } catch (e) {
      if (e.message !== "not authenticated") control.notice = "Can't reach the bridge, so it's unknown whether the command was sent. Check the grill.";
    } finally {
      control.sending = null;
      await loadControl();
    }
    return sent;
  }

  // Changing the grill's own password ----------------------------------------

  function passwordStatus(text, kind) {
    const status = $("password-status");
    status.textContent = text;
    status.dataset.kind = kind || "";
  }

  function openPasswordDialog() {
    const dialog = $("password-dialog");
    $("password-form").reset();
    passwordStatus("", "");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    $("password-new").focus();
  }

  function closePasswordDialog() {
    const dialog = $("password-dialog");
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  async function changeGrillPassword() {
    const form = $("password-form");
    const password = form.password.value;
    const dashboard = form.dashboard_password.value;
    if (!password) return passwordStatus("Enter a new password.", "error");
    if (password !== form.confirm.value) return passwordStatus("The two passwords don't match.", "error");
    if (!dashboard) return passwordStatus("Enter your dashboard password.", "error");
    const ok = await confirmControl(
      "Change the grill's password?",
      "The grill will start using the new password. The bridge saves it in its settings, so nothing needs changing in .env, and the Pit Boss app will need the new password too. If the grill doesn't confirm the change, it may still have the old password; the bridge keeps both and tries them, so it reconnects on its own.",
      "Change password",
    );
    if (!ok) return;
    const button = $("password-save");
    button.disabled = true;
    passwordStatus("Sending…", "busy");
    try {
      const result = await sendControl("set-password", {
        password,
        confirm: password,
        dashboard_password: dashboard,
      });
      if (result) {
        form.reset();
        closePasswordDialog();
      } else if (control.notice) {
        // The dialog is over the result panel, so say it here too.
        passwordStatus(control.notice, "error");
      }
    } finally {
      button.disabled = false;
    }
  }

  // Command log (/api/control-log) ------------------------------------------

  const OUTCOME_LABELS = {
    confirmed: "Confirmed",
    unconfirmed: "Unconfirmed",
    failed: "Failed",
    already_applied: "Already applied",
    rejected: "Refused",
  };
  const commandLog = { items: [], nextBefore: null, busy: false };

  function logStatus(text, kind) {
    const status = $("control-log-status");
    status.textContent = text;
    status.dataset.kind = kind || "";
  }

  function logEntry(item) {
    const args = item.args || {};
    let command = COMMAND_LABELS[item.command] || item.command;
    if (item.command === "set-unit" && args.unit) command += " to °" + args.unit;
    else if (num(args.temp) !== null) command += " " + args.temp + (args.unit ? "°" + args.unit : "");
    if (num(item.target) !== null && item.target !== args.temp) command += " → " + item.target + (item.unit ? "°" + item.unit : "");
    const outcome = el("span", { className: "log-outcome", textContent: OUTCOME_LABELS[item.outcome] || item.outcome || "?" });
    outcome.dataset.outcome = item.outcome || "";
    const at = Date.parse(item.at);
    const when = Number.isNaN(at) ? item.at : new Date(at).toLocaleString([], { dateStyle: "medium", timeStyle: "medium" });
    const detail = item.outcome === "rejected"
      ? (item.status_code ? item.status_code + ": " : "") + (item.detail || "")
      : item.detail || "";
    return el("li", {}, [
      el("div", { className: "log-head" }, [el("strong", { textContent: command }), outcome]),
      detail ? el("div", { textContent: detail }) : null,
      el("div", { className: "log-meta", textContent: when + (item.client ? " · from " + item.client : "") }),
    ]);
  }

  function renderCommandLog() {
    $("control-log-list").replaceChildren(...commandLog.items.map(logEntry));
    $("control-log-empty").hidden = commandLog.items.length > 0;
    $("control-log-more").hidden = commandLog.nextBefore === null;
  }

  async function loadCommandLog(older) {
    if (commandLog.busy) return;
    commandLog.busy = true;
    try {
      const query = older && commandLog.nextBefore !== null ? "?before=" + commandLog.nextBefore : "";
      const res = await apiFetch("api/control-log" + query, { cache: "no-store" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        logStatus(typeof body.detail === "string" ? body.detail : "Can't load the log (HTTP " + res.status + ").", "error");
        return;
      }
      commandLog.items = older ? commandLog.items.concat(body.items) : body.items;
      commandLog.nextBefore = body.next_before;
      renderCommandLog();
    } catch (e) {
      if (e.message !== "not authenticated") logStatus("Can't reach the bridge.", "error");
    } finally {
      commandLog.busy = false;
    }
  }

  async function pruneRequest(body) {
    const res = await apiFetch("api/control-log/prune", {
      method: "POST",
      cache: "no-store",
      headers: { "content-type": "application/json", "X-Smokestack-Control": "1" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "HTTP " + res.status);
    return data;
  }

  async function pruneCommandLog() {
    const all = $("control-log-scope").value === "all";
    const days = Number($("control-log-days").value);
    if (!all && (!Number.isInteger(days) || days < 1)) {
      logStatus("Enter a whole number of days.", "error");
      return;
    }
    const scope = all ? {} : { older_than_days: days };
    const button = $("control-log-prune-btn");
    button.disabled = true;
    logStatus("", "");
    try {
      // The delete is pinned to what the dry run counted.
      const { matching, cutoff, max_id: maxId } = await pruneRequest({ ...scope, dry_run: true });
      if (!matching) {
        logStatus(all ? "The log is already empty." : "Nothing is older than " + days + " days.", "");
        return;
      }
      const what = matching === 1 ? "1 entry" : matching + " entries";
      const ok = await confirmControl(
        "Prune the command log?",
        "Delete " + what + (all ? ", the whole log" : " older than " + days + " days") + ". This can't be undone.",
        "Delete " + what,
        "danger",
      );
      if (!ok) return;
      const { deleted } = await pruneRequest(cutoff ? { cutoff, max_id: maxId } : { max_id: maxId });
      logStatus("Deleted " + (deleted === 1 ? "1 entry." : deleted + " entries."), "ok");
      await loadCommandLog(false);
      loadControl();
    } catch (e) {
      if (e.message !== "not authenticated") logStatus("Prune failed: " + e.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  function setupCommandLog() {
    const dialog = $("control-log");
    $("control-log-open").addEventListener("click", () => {
      logStatus("", "");
      commandLog.items = [];
      commandLog.nextBefore = null;
      renderCommandLog();
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
      loadCommandLog(false);
    });
    $("control-log-close").addEventListener("click", () => {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    });
    $("control-log-more").addEventListener("click", () => loadCommandLog(true));
    $("control-log-scope").addEventListener("change", () => {
      $("control-log-days-wrap").hidden = $("control-log-scope").value === "all";
    });
    $("control-log-prune").addEventListener("submit", (event) => {
      event.preventDefault();
      pruneCommandLog();
    });
  }

  function setupGrillControls() {
    setupCommandLog();
    setupTempStepper();
    $("control-off").addEventListener("click", async () => {
      const ok = await confirmControl("Turn the grill off?", "Turn the grill off. The fan will run for about 10 minutes.", "Turn off", "danger");
      if (ok) sendControl("turn-off");
    });
    document.querySelectorAll("[data-unit-target]").forEach((button) => button.addEventListener("click", async () => {
      const unit = button.dataset.unitTarget;
      const ok = await confirmControl(
        "Switch the grill to " + UNIT_NAMES[unit] + "?",
        "Switch the grill to " + UNIT_NAMES[unit] + ". Its panel and every temperature it reports change to °" + unit + "; probe alerts keep working.",
        "Switch to °" + unit,
      );
      if (ok) sendControl("set-unit", { unit });
    }));
    $("control-reboot").addEventListener("click", async () => {
      const ok = await confirmControl(
        "Restart the WiFi module?",
        "Restart the grill's WiFi module. The grill keeps running; the dashboard goes blank for up to a minute.",
        "Restart",
      );
      if (ok) sendControl("reboot");
    });
    $("control-password").addEventListener("click", openPasswordDialog);
    $("password-close").addEventListener("click", closePasswordDialog);
    $("password-form").addEventListener("submit", (event) => {
      event.preventDefault();
      changeGrillPassword();
    });
    $("control-temp-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const typed = Number($("control-temp").value.trim());
      if (!$("control-temp").value.trim() || !Number.isFinite(typed) || typed <= 0 || typed > 1000) {
        control.notice = "Enter a temperature.";
        renderControls();
        return;
      }
      const snapped = snapTemp(typed);
      if (snapped === null) {
        control.notice = "The grill hasn't reported its temperature unit yet.";
        renderControls();
        return;
      }
      const unit = controlUnit();
      // Sent with the temperature, so a unit change on the grill's panel
      // after this dialog is refused rather than reinterpreting it.
      const unitCode = control.status.unit;
      const current = num(state.fields.grillSetTemp);
      let text = "Set grill to " + snapped + unit + (current === null ? "." : ", currently " + Math.round(current) + unit + ".");
      if (snapped !== typed) text += " " + snapped + unit + " is the nearest setting the grill accepts to " + typed + unit + ".";
      const ok = await confirmControl("Set the grill temperature?", text, "Set " + snapped + unit);
      if (ok) sendControl("set-temperature", { temp: typed, unit: unitCode }, snapped);
    });
    loadControl();
  }

  // Settings -------------------------------------------------------------

  const SOURCE_HINTS = { http: "Local network", ble: "Bluetooth radio", fake: "Sample data" };
  const SOURCE_ORDER = ["http", "ble", "fake"];
  const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
  const APPLY_WATCH_MS = 30000;
  const PASSWORD_MIN_LENGTH = 8;
  const PHONE_QUERY = window.matchMedia("(max-width: 640px)");

  const SECTIONS = [
    { id: "grill", label: "Grill" },
    { id: "alerts", label: "Alerts" },
    { id: "home", label: "Home Assistant" },
    { id: "access", label: "Access & control" },
    { id: "system", label: "System" },
  ];

  const CONNECTION_FIELDS = new Set(["source", "host", "device_name", "ble_adapter", "password"]);

  // type: text | number | int | bool | secret | source | log | adapter
  const SETTING_META = {
    source: { label: "Source", type: "source", hint: "Bluetooth is listed only when the bridge can reach the host's Bluetooth (a Linux host running bluetoothd)." },
    model: { label: "Grill model", type: "text" },
    host: { label: "Grill address", type: "text", hint: "IP or host:port of the grill." },
    device_name: { label: "Bluetooth name", type: "text" },
    ble_adapter: { label: "Bluetooth adapter", type: "adapter", hint: "On a host with more than one adapter, pick the one with the best range. Lists the adapters the bridge can see now." },
    password: { label: "Grill password", type: "secret", hint: "The password the bridge uses to talk to the grill. Saving it here also ends a password change the bridge hasn't settled." },
    poll_seconds: { label: "Poll every", type: "number", unit: "s", min: 0.1, step: 1, snap: false },
    stale_after_seconds: { label: "Stale after", type: "int", unit: "s", min: 1, step: 1 },
    mqtt_enabled: { label: "Publish to Home Assistant", type: "bool", hint: "Off stops publishing and keeps the broker settings." },
    mqtt_host: { label: "Broker host", type: "text", hint: "An empty host leaves publishing off." },
    mqtt_port: { label: "Port", type: "int", min: 1, max: 65535, short: true },
    mqtt_user: { label: "User", type: "text" },
    mqtt_password: { label: "Password", type: "secret" },
    mqtt_prefix: { label: "Topic prefix", type: "text" },
    ntfy_enabled: { label: "Send alerts", type: "bool", hint: "Off stops alerts and keeps the topic URL and token." },
    ntfy_url: { label: "ntfy topic URL", type: "text", inputmode: "url", hint: "An empty URL leaves alerts off. Probe targets are set on each probe's card." },
    ntfy_token: { label: "Access token", type: "secret" },
    alert_drift_degrees: { label: "Drift alert at", type: "number", unit: "°F", min: 0.1, step: 5 },
    alert_drift_minutes: { label: "…sustained for", type: "number", unit: "min", min: 0.1, step: 1 },
    alert_stale_seconds: { label: "No-data alert after", type: "number", unit: "s", min: 0.1, step: 5 },
    alert_cooldown_seconds: { label: "Repeat cooldown", type: "number", unit: "s", min: 0, step: 5 },
    retention_days: { label: "Keep history", type: "number", unit: "days", min: 0, step: 1, hint: "0 keeps everything." },
    enable_control: { label: "Grill control", type: "bool", hint: "Lets signed-in users turn the grill off, set its temperature and restart its WiFi module from the dashboard. It can never turn the grill on." },
    log_level: { label: "Log level", type: "log" },
  };

  // The source each field belongs to. Poll and stale apply to every source.
  const SETTING_SOURCES = {
    host: ["http"],
    model: ["http", "ble"],
    password: ["http", "ble"],
    device_name: ["ble"],
    ble_adapter: ["ble"],
  };

  // The field a source can't be saved without. Choosing one of these sources
  // shows its fields first so they can be filled in before the switch.
  const SOURCE_REQUIRED = { http: "host", ble: "device_name" };

  // The switch whose off state folds these fields away.
  const SETTING_DEPENDS_ON = {
    mqtt_host: "mqtt_enabled",
    mqtt_port: "mqtt_enabled",
    mqtt_user: "mqtt_enabled",
    mqtt_password: "mqtt_enabled",
    mqtt_prefix: "mqtt_enabled",
    ntfy_url: "ntfy_enabled",
    ntfy_token: "ntfy_enabled",
    alert_drift_degrees: "ntfy_enabled",
    alert_drift_minutes: "ntfy_enabled",
    alert_stale_seconds: "ntfy_enabled",
    alert_cooldown_seconds: "ntfy_enabled",
  };

  const settings = {
    payload: null,
    fields: {}, // name -> field from the API
    layouts: {}, // name -> how its row is rendered
    placed: new Set(),
    changing: new Set(), // secrets the user chose to change
    authOn: null, // whether the dashboard login is on; null until known
    section: "grill",
    draftSource: null, // a source chosen before its required field is set
    open: false,
    reconnecting: false,
    watchToken: 0,
    saveChain: Promise.resolve(),
    lastSave: null,
    queued: {}, // name -> value already on its way to the bridge
    savedTimers: {},
    cancelSecret: null, // a secret whose Cancel was pressed before its blur
  };

  const authUi = { busy: false };

  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.assign(node, props || {});
    for (const child of children || []) if (child !== null) node.append(child);
    return node;
  }

  function fieldMeta(field) {
    return SETTING_META[field.name] || { label: field.name, type: field.secret ? "secret" : "text" };
  }

  function fieldValue(name) {
    const field = settings.fields[name];
    return field ? field.value : null;
  }

  function sourceValue() {
    return fieldValue("source") || "";
  }

  // While a source is being set up, show its fields before the switch saves.
  function shownSource() {
    return settings.draftSource || sourceValue();
  }

  function sourceReady(name) {
    const required = SOURCE_REQUIRED[name];
    return !required || Boolean(fieldValue(required));
  }

  function alertsOn() {
    return Boolean(fieldValue("ntfy_enabled") && fieldValue("ntfy_url"));
  }

  function mqttOn() {
    return Boolean(fieldValue("mqtt_enabled") && fieldValue("mqtt_host"));
  }

  // The test button follows what is in the fields, not just the last save, so
  // typing a URL enables it before the blur that saves it.
  function alertTestReady() {
    const toggle = $("set-ntfy_enabled");
    const url = $("set-ntfy_url");
    const on = toggle ? toggle.checked : Boolean(fieldValue("ntfy_enabled"));
    const topic = url ? url.value.trim() : String(fieldValue("ntfy_url") || "");
    return Boolean(on && topic);
  }

  function settingStatus(text, kind) {
    const status = $("settings-status");
    if (!status) return;
    status.textContent = text;
    status.dataset.kind = kind || "";
  }

  function stepWords(meta) {
    const step = meta.step || 1;
    if (meta.unit === "s") return step + " second" + (step === 1 ? "" : "s");
    if (meta.unit === "min") return step + " minute" + (step === 1 ? "" : "s");
    if (meta.unit === "days") return step + " day" + (step === 1 ? "" : "s");
    return meta.unit ? step + meta.unit : String(step);
  }

  // A number input between - and + buttons. Taps step once, holding repeats,
  // and typing stays possible: the value snaps to the step on blur. `commit`
  // runs when a step settles, so a held button saves its last value too.
  function settingStepper(input, meta, commit) {
    const step = meta.step || 1;
    const group = el("div", { className: "stepper", role: "group" });
    group.setAttribute("aria-label", meta.label);
    const down = el("button", { type: "button", className: "stepper-btn", textContent: "−" });
    down.dataset.dir = "-1";
    down.setAttribute("aria-label", "Lower by " + stepWords(meta));
    const up = el("button", { type: "button", className: "stepper-btn", textContent: "+" });
    up.dataset.dir = "1";
    up.setAttribute("aria-label", "Raise by " + stepWords(meta));
    group.append(down, input, up);

    let wait = null;
    let repeat = null;
    let moved = false;
    function stop() {
      clearTimeout(wait);
      clearInterval(repeat);
      if (moved) {
        moved = false;
        if (commit) commit();
      }
    }
    function clamp(value) {
      let next = value;
      if (meta.min !== undefined) next = Math.max(meta.min, next);
      if (meta.max !== undefined) next = Math.min(meta.max, next);
      return next;
    }
    function sync() {
      const raw = input.value.trim();
      const value = Number(raw);
      const known = raw !== "" && Number.isFinite(value);
      down.disabled = known && meta.min !== undefined && value <= meta.min;
      up.disabled = known && meta.max !== undefined && value >= meta.max;
    }
    function set(value) {
      input.value = String(Number(clamp(value).toFixed(6)));
      sync();
    }
    function move(dir) {
      const raw = input.value.trim();
      const current = raw !== "" && Number.isFinite(Number(raw)) ? Number(raw) : (meta.min || 0);
      set(current + dir * step);
      moved = true;
      if ((meta.min !== undefined && Number(input.value) <= meta.min)
        || (meta.max !== undefined && Number(input.value) >= meta.max)) stop();
    }
    [down, up].forEach((button) => {
      const dir = Number(button.dataset.dir);
      button.addEventListener("pointerdown", () => {
        stop();
        move(dir);
        if (button.disabled) return;
        wait = setTimeout(() => { repeat = setInterval(() => move(dir), 90); }, 400);
      });
      ["pointerup", "pointerleave", "pointercancel"].forEach((event) => button.addEventListener(event, stop));
      button.addEventListener("click", (event) => {
        if (event.detail !== 0) return;
        move(dir);
        stop();
      });
    });
    input.addEventListener("input", sync);
    input.addEventListener("blur", () => {
      const raw = input.value.trim();
      if (raw === "" || !Number.isFinite(Number(raw))) {
        set(meta.min || 0);
        return;
      }
      const value = Number(raw);
      set(meta.snap === false ? value : Math.round(value / step) * step);
    });
    sync();
    return group;
  }

  function settingInput(field, meta) {
    const id = "set-" + field.name;
    const value = field.value;
    if (meta.type === "adapter") {
      const options = [["", "System default"]];
      for (const adapter of settings.payload.ble_adapters || []) {
        const details = [adapter.alias, adapter.address, adapter.powered ? "" : "off"].filter(Boolean).join(", ");
        options.push([adapter.name, adapter.name + (details ? " (" + details + ")" : "")]);
      }
      // Keep a saved adapter that isn't plugged in now visible.
      if (value && !options.some(([option]) => option === value)) options.push([value, value + " (not found)"]);
      return el("select", { id, name: field.name, className: "select" }, options.map(([option, label]) =>
        el("option", { value: option, textContent: label, selected: option === (value || "") })));
    }
    if (meta.type === "log") {
      const options = LOG_LEVELS.slice();
      // Keep a current value the image no longer offers visible.
      if (!options.includes(value)) options.push(value);
      return el("select", { id, name: field.name, className: "select" }, options.map((option) =>
        el("option", { value: option, textContent: option, selected: option === value })));
    }
    const numeric = meta.type === "number" || meta.type === "int";
    const classes = ["input"];
    if (numeric) classes.push("input-mono");
    if (meta.short) classes.push("input-short");
    const input = el("input", {
      id,
      name: field.name,
      className: classes.join(" "),
      type: numeric ? "number" : meta.type === "secret" ? "password" : "text",
      value: value === null || value === undefined ? "" : String(value),
      autocomplete: meta.type === "secret" ? "new-password" : "off",
    });
    if (numeric) {
      input.step = meta.step !== undefined ? String(meta.step) : meta.type === "int" ? "1" : "any";
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

  function originNodes(field, meta) {
    const nodes = [];
    if (field.origin === "ui") {
      nodes.push(el("span", { className: "badge", textContent: "Custom" }));
      const reset = el("button", { type: "button", className: "link-btn", textContent: "Reset" });
      reset.setAttribute("aria-label", "Reset " + meta.label + " to .env or default");
      reset.addEventListener("click", () => resetSetting(field.name));
      nodes.push(reset);
    } else if (field.origin === "env") {
      const origin = el("span", { className: "setting-origin", textContent: "from .env" });
      origin.title = "From " + field.env;
      nodes.push(origin);
    }
    return nodes;
  }

  function settingControl(field, meta, hideUnit) {
    if (meta.type === "bool") {
      const input = el("input", { id: "set-" + field.name, name: field.name, type: "checkbox", checked: Boolean(field.value) });
      const label = el("label", { className: "toggle" });
      label.setAttribute("aria-label", meta.label);
      label.append(input, el("span", { className: "toggle-track" }), el("span", { className: "toggle-thumb" }));
      const state = el("span", { className: "toggle-state", textContent: field.value ? "On" : "Off" });
      input.addEventListener("change", () => {
        state.textContent = input.checked ? "On" : "Off";
      });
      return el("div", { className: "toggle-wrap" }, [label, state]);
    }
    if (meta.type === "secret" && !settings.changing.has(field.name)) {
      const isSet = field.value && field.value.set;
      const control = el("div", { className: "secret" }, [
        el("span", { id: "set-" + field.name, textContent: isSet ? "••••••" : "Not set" }),
      ]);
      const change = el("button", { type: "button", className: "link-btn", textContent: isSet ? "Change" : "Set" });
      change.setAttribute("aria-label", (isSet ? "Change " : "Set ") + meta.label);
      change.addEventListener("click", () => {
        settings.changing.add(field.name);
        refreshField(field.name);
        const input = $("set-" + field.name);
        if (input) input.focus();
      });
      control.append(change);
      return control;
    }
    if (meta.type === "secret") {
      const input = settingInput({ ...field, value: "" }, meta);
      input.placeholder = "New value";
      const cancel = el("button", { type: "button", className: "link-btn", textContent: "Cancel" });
      cancel.setAttribute("aria-label", "Cancel changing " + meta.label);
      // The pointer press blurs the input before the click; mark the field so
      // the blur doesn't save the value Cancel is about to discard.
      cancel.addEventListener("pointerdown", () => { settings.cancelSecret = field.name; });
      ["pointerup", "pointercancel", "pointerleave"].forEach((event) => {
        cancel.addEventListener(event, () => {
          if (settings.cancelSecret === field.name) settings.cancelSecret = null;
        });
      });
      cancel.addEventListener("click", () => {
        settings.cancelSecret = null;
        settings.changing.delete(field.name);
        refreshField(field.name);
      });
      const buttons = [cancel];
      if (field.value && field.value.set) {
        const clear = el("button", { type: "button", className: "link-btn", textContent: "Clear" });
        clear.setAttribute("aria-label", "Clear " + meta.label);
        clear.addEventListener("click", () => saveField(field.name, ""));
        buttons.push(clear);
      }
      return el("div", { className: "secret" }, [input, ...buttons]);
    }
    const input = settingInput(field, meta);
    if (!meta.unit) return input;
    const commit = () => commitNumber(field.name, input, meta);
    const parts = [settingStepper(input, meta, commit)];
    if (!hideUnit) parts.push(el("span", { className: "unit-label", textContent: meta.unit }));
    return el("div", { className: "with-unit" }, parts);
  }

  function bindField(field, root) {
    const meta = fieldMeta(field);
    const input = root.querySelector("input, select");
    if (!input) return;
    if (meta.type === "bool") {
      input.addEventListener("change", () => saveField(field.name, input.checked));
      if (field.name === "ntfy_enabled") input.addEventListener("change", updateAlertTest);
      return;
    }
    if (meta.type === "secret") {
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          input.blur();
        }
      });
      input.addEventListener("blur", () => {
        if (settings.cancelSecret === field.name) {
          settings.cancelSecret = null;
          return;
        }
        if (input.value !== "") saveField(field.name, input.value);
      });
      return;
    }
    if (meta.type === "adapter" || meta.type === "log") {
      input.addEventListener("change", () => saveField(field.name, input.value));
      return;
    }
    // Text and number fields save when they lose focus, if the value changed.
    if (field.name === "ntfy_url") input.addEventListener("input", updateAlertTest);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        input.blur();
      }
    });
    input.addEventListener("blur", () => {
      const numeric = meta.type === "number" || meta.type === "int";
      if (numeric) {
        commitNumber(field.name, input, meta);
        return;
      }
      const current = settings.fields[field.name];
      if (current && !inputChanged(input, current.value)) {
        clearFieldError(field.name);
        return;
      }
      saveField(field.name, input.value.trim());
    });
  }

  // Save a number field, keeping a rejected typed value in the input.
  function commitNumber(name, input, meta) {
    const raw = input.value.trim();
    if (raw === "" || !Number.isFinite(Number(raw))) {
      showFieldError(name, "Enter a number.");
      return;
    }
    const value = Number(raw);
    const field = settings.fields[name];
    if (field && Number(value) === Number(field.value)) {
      clearFieldError(name);
      return;
    }
    saveField(name, value);
  }

  function renderField(field, kind) {
    const meta = fieldMeta(field);
    const name = field.name;
    // A rule's sentence already has the unit, e.g. "by [25] °F".
    const control = settingControl(field, meta, kind === "rule");
    const hint = el("p", { className: "setting-hint", textContent: meta.hint || "" });
    hint.hidden = !meta.hint;
    const error = el("p", { className: "setting-error", hidden: true });

    if (kind === "rule") {
      const wrap = el("span", { className: "rule-field" });
      wrap.dataset.name = name;
      wrap.append(control);
      const marks = el("span", { className: "setting-mark" }, originNodes(field, meta));
      if (marks.childNodes.length) wrap.append(marks);
      wrap.append(el("span", { className: "setting-saved", role: "status" }), error);
      bindField(field, wrap);
      return wrap;
    }

    if (kind === "cell") {
      const cell = el("div", { className: "setting-pair-cell" });
      cell.dataset.name = name;
      const head = el("div", { className: "setting-head" }, [
        el("label", { htmlFor: "set-" + name, textContent: meta.label }),
      ]);
      head.append(...originNodes(field, meta), el("span", { className: "setting-saved", role: "status" }));
      cell.append(head, control, hint, error);
      bindField(field, cell);
      return cell;
    }

    const row = el("div", { className: "setting" });
    row.dataset.name = name;
    const head = el("div", { className: "setting-head" }, [
      el("label", { htmlFor: "set-" + name, textContent: meta.label }),
    ]);
    head.append(...originNodes(field, meta), el("span", { className: "setting-saved", role: "status" }));
    row.append(head, control, hint, error);
    if (name === "enable_control") {
      row.append(el("p", {
        className: "setting-hint setting-locked",
        textContent: "Turn on the dashboard password first.",
        hidden: true,
      }));
    }
    bindField(field, row);
    return row;
  }

  function sourceRow() {
    const field = settings.fields.source;
    if (!field) return null;
    const meta = fieldMeta(field);
    settings.placed.add("source");
    settings.layouts.source = "source";
    const row = el("div", { className: "setting setting-source" });
    row.dataset.name = "source";
    const head = el("div", { className: "setting-head" }, [
      el("label", { textContent: meta.label }),
    ]);
    head.append(...originNodes(field, meta), el("span", { className: "setting-saved", role: "status" }));

    const cards = el("div", { className: "source-cards" });
    cards.setAttribute("role", "radiogroup");
    cards.setAttribute("aria-label", "Source");
    const available = settings.payload.sources || ["fake"];
    const options = SOURCE_ORDER.filter((name) => available.includes(name))
      .concat(available.filter((name) => !SOURCE_ORDER.includes(name)));
    for (const name of options) {
      const card = el("button", { type: "button", className: "source-card", role: "radio" });
      card.dataset.source = name;
      card.setAttribute("aria-checked", String(name === shownSource()));
      card.append(
        el("span", { className: "source-card-label", textContent: SOURCE_LABELS[name] || name }),
        el("span", { className: "source-card-hint", textContent: SOURCE_HINTS[name] || "" }),
      );
      card.addEventListener("click", () => chooseSource(name));
      cards.append(card);
    }

    row.append(head, cards, el("p", { className: "setting-hint", textContent: meta.hint || "" }),
      el("p", { className: "setting-error", hidden: true }));
    return row;
  }

  function syncSourceSelection() {
    document.querySelectorAll("#settings .source-card").forEach((card) => {
      card.setAttribute("aria-checked", String(card.dataset.source === shownSource()));
    });
    applySettingsVisibility();
  }

  // A source that needs a host or device name can't be saved yet. Show its
  // fields, then save the switch once the required field is saved.
  function chooseSource(name) {
    if (name === sourceValue()) {
      settings.draftSource = null;
      clearFieldError("source");
      syncSourceSelection();
      return;
    }
    settings.draftSource = name;
    syncSourceSelection();
    if (!sourceReady(name)) {
      const required = SOURCE_REQUIRED[name];
      const field = required && settings.fields[required];
      const label = field ? fieldMeta(field).label.toLowerCase() : "connection details";
      showFieldError("source", "Set the " + label + " first.");
      const input = required && $("set-" + required);
      if (input) input.focus();
      return;
    }
    clearFieldError("source");
    saveField("source", name, { connection: true });
  }

  function refreshField(name) {
    const field = settings.fields[name];
    const old = document.querySelector('#settings [data-name="' + name + '"]');
    if (!field || !old) return;
    const kind = settings.layouts[name] || "row";
    if (kind === "source") old.replaceWith(sourceRow());
    else old.replaceWith(renderField(field, kind));
    applySettingsVisibility();
    syncControlToggle();
  }

  function fieldNode(name) {
    return document.querySelector('#settings [data-name="' + name + '"]');
  }

  function fieldInput(name) {
    const node = fieldNode(name);
    return node ? node.querySelector("input, select") : null;
  }

  // Whether the control now holds a different value from the one saved.
  function inputChanged(input, value) {
    if (!input) return false;
    if (input.type === "checkbox") return input.checked !== Boolean(value);
    const submitted = value === null || value === undefined ? "" : String(value);
    if (input.tagName === "SELECT") return input.value !== submitted;
    const raw = input.value.trim();
    if (input.type === "number") {
      if (raw === "") return submitted !== "";
      return !Number.isFinite(Number(raw)) || Number(raw) !== Number(submitted);
    }
    return raw !== submitted;
  }

  function hasQueuedEdit(name, value) {
    return settings.queued[name] !== undefined && settings.queued[name] !== value;
  }

  function hasNewerEdit(name, value) {
    if (hasQueuedEdit(name, value)) return true;
    return inputChanged(fieldInput(name), value);
  }

  // Don't replace an input the user has returned to or edited while its save
  // was in flight.
  function pendingEdit(name, value) {
    const input = fieldInput(name);
    return hasNewerEdit(name, value) || Boolean(input && input === document.activeElement);
  }

  function setFieldStatus(name, text) {
    const node = fieldNode(name);
    if (!node) return;
    const status = node.querySelector(".setting-saved");
    if (status) status.textContent = text;
  }

  function flashSaved(name) {
    const node = fieldNode(name);
    if (!node) return;
    const status = node.querySelector(".setting-saved");
    if (!status) return;
    status.textContent = "Saved";
    clearTimeout(settings.savedTimers[name]);
    settings.savedTimers[name] = setTimeout(() => {
      if (status.isConnected) status.textContent = "";
    }, 2000);
  }

  function showFieldError(name, message) {
    const node = fieldNode(name);
    if (!node) return;
    const box = node.querySelector(".setting-error");
    if (!box) return;
    box.textContent = message || "";
    box.hidden = !message;
    node.classList.toggle("has-error", Boolean(message));
  }

  function clearFieldError(name) {
    showFieldError(name, "");
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

  function fieldsFromPayload(payload) {
    const fields = {};
    for (const group of payload.groups || []) {
      for (const field of group.fields) fields[field.name] = field;
    }
    return fields;
  }

  function applyPayload(payload) {
    settings.payload = payload;
    settings.fields = fieldsFromPayload(payload);
    updateNavState();
    applySettingsVisibility();
    syncControlToggle();
    updateAlertTest();
    updateSystem();
    updateGrillStatus();
  }

  async function performSave(name, value, options) {
    const meta = fieldMeta(settings.fields[name]);
    const save = {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ [name]: value }),
    };
    try {
      const res = await apiFetch("api/settings", { cache: "no-store", ...save });
      const body = await res.json().catch(() => ({}));
      if (res.status === 422) {
        const { errors, general } = errorsFrom(body.detail);
        const message = errors[name] || general || "That value was rejected.";
        if (!hasNewerEdit(name, value)) {
          if (!["text", "number", "int", "secret"].includes(meta.type)) refreshField(name);
          setFieldStatus(name, "");
          showFieldError(name, message);
        }
        return false;
      }
      if (!res.ok) {
        if (!hasNewerEdit(name, value)) {
          if (!["text", "number", "int", "secret"].includes(meta.type)) refreshField(name);
          setFieldStatus(name, "");
          showFieldError(name, body.detail ? String(body.detail) : "Save failed (HTTP " + res.status + ").");
        }
        return false;
      }
      if (meta.type === "secret" && !hasNewerEdit(name, value)) settings.changing.delete(name);
      if (name === "source" && settings.draftSource === value) settings.draftSource = null;
      applyPayload(body);
      if (hasQueuedEdit(name, value)) {
        // A newer save is already on its way and shows "Saving…"; leave it to
        // refresh the field when it finishes.
      } else if (hasNewerEdit(name, value)) {
        setFieldStatus(name, "");
      } else {
        if (!pendingEdit(name, value)) refreshField(name);
        flashSaved(name);
      }
      if (settings.open && (options.connection || CONNECTION_FIELDS.has(name))) watchApply();
      if (name === "enable_control") loadControl();
      if (name.startsWith("ntfy_") || name.startsWith("alert_")) loadAlertTargets();
      // A source chosen before its required field was set: the field just
      // saved, so finish the switch.
      const draft = settings.draftSource;
      if (draft && draft !== sourceValue() && sourceReady(draft)) {
        saveField("source", draft, { connection: true });
      }
      return true;
    } catch (e) {
      if (e.message !== "not authenticated" && !hasNewerEdit(name, value)) {
        if (!["text", "number", "int", "secret"].includes(meta.type)) refreshField(name);
        setFieldStatus(name, "");
        showFieldError(name, "Can't reach the bridge.");
      }
      return false;
    }
  }

  function saveField(name, value, options) {
    // A stepper can commit the same value twice (the input's blur and the
    // button's pointerup), and only the first needs to go to the bridge.
    if (name in settings.queued && settings.queued[name] === value) return settings.lastSave;
    settings.queued[name] = value;
    clearFieldError(name);
    setFieldStatus(name, "Saving…");
    const task = settings.saveChain.then(() => performSave(name, value, options || {}));
    settings.saveChain = task.catch(() => {});
    settings.lastSave = task;
    const clearQueued = () => {
      if (settings.queued[name] === value) delete settings.queued[name];
    };
    task.then(clearQueued, clearQueued);
    return task;
  }

  async function performReset(name) {
    try {
      const res = await apiFetch("api/settings/" + encodeURIComponent(name), { method: "DELETE", cache: "no-store" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        showFieldError(name, body.detail ? String(body.detail) : "Reset failed (HTTP " + res.status + ").");
        return false;
      }
      settings.changing.delete(name);
      applyPayload(body);
      refreshField(name);
      flashSaved(name);
      if (settings.open && CONNECTION_FIELDS.has(name)) watchApply();
      return true;
    } catch (e) {
      if (e.message !== "not authenticated") showFieldError(name, "Can't reach the bridge.");
      return false;
    }
  }

  function resetSetting(name) {
    const task = settings.saveChain.then(() => performReset(name));
    settings.saveChain = task.catch(() => {});
    settings.lastSave = task;
    return task;
  }

  function settingToggleOn(name) {
    const input = $("set-" + name);
    if (input) return input.checked;
    const field = settings.fields[name];
    return Boolean(field && field.value);
  }

  function settingAppliesToSource(name) {
    const sources = SETTING_SOURCES[name];
    if (!sources) return true;
    return sources.includes(shownSource());
  }

  // Show only the fields the chosen source uses, and fold away an
  // integration's fields while its switch is off.
  function applySettingsVisibility() {
    document.querySelectorAll("#settings .setting, #settings .settings-rules").forEach((row) => {
      const name = row.dataset.name;
      const parent = (name && SETTING_DEPENDS_ON[name]) || row.dataset.parent;
      row.hidden = Boolean(name && !settingAppliesToSource(name))
        || Boolean(parent && !settingToggleOn(parent));
    });
    document.querySelectorAll("#settings [data-sources]").forEach((block) => {
      block.hidden = !block.dataset.sources.split(" ").includes(shownSource());
    });
  }

  function syncControlToggle() {
    const input = $("set-enable_control");
    if (!input) return;
    const row = input.closest(".setting");
    const locked = settings.authOn !== true;
    input.disabled = locked;
    row.classList.toggle("is-locked", locked);
    row.querySelector(".setting-locked").hidden = !locked;
  }

  function updateNavState() {
    document.querySelectorAll(".settings-nav-item").forEach((item) => {
      const section = item.dataset.section;
      let summary = "";
      if (section === "grill") summary = SOURCE_LABELS[sourceValue()] || sourceValue();
      else if (section === "alerts") summary = alertsOn() ? "On" : "Off";
      else if (section === "home") summary = mqttOn() ? "On" : "Off";
      else if (section === "access") summary = settings.authOn === null ? "" : (settings.authOn ? "Password" : "Open");
      else if (section === "system" && state.health && state.health.version) summary = "v" + state.health.version;
      const label = item.querySelector(".settings-nav-state");
      if (label) label.textContent = summary;
    });
  }

  function shortAge(seconds) {
    if (seconds < 5) return "just now";
    if (seconds < 90) return Math.round(seconds) + " s ago";
    if (seconds < 5400) return Math.round(seconds / 60) + " min ago";
    return (seconds / 3600).toFixed(1) + " h ago";
  }

  function updateGrillStatus() {
    const box = $("settings-grill-status");
    if (!box) return;
    const health = state.health;
    let kind = "queued";
    let text = "Checking the connection…";
    if (settings.reconnecting) {
      kind = "building";
      text = "Reconnecting to the grill…";
    } else if (health) {
      const source = SOURCE_LABELS[health.source] || health.source || "grill";
      const age = num(health.last_update_age_seconds);
      if (health.connected) {
        kind = health.stale ? "building" : "ready";
        text = "Connected over " + source;
        if (age !== null) text += " · last reading " + shortAge(age);
      } else {
        kind = "error";
        text = "Not connected over " + source;
        if (health.last_error) text += " · " + health.last_error;
      }
    }
    box.replaceChildren(
      el("span", { className: "deployment-dot deployment-dot--" + kind, "aria-hidden": "true" }),
      el("span", { className: "settings-conn-text", textContent: text }),
    );
  }

  function updateSystem() {
    const version = state.health && state.health.version;
    const box = $("settings-version");
    if (box) box.textContent = version ? "v" + version : "—";
  }

  function updateAlertTest() {
    const button = $("alert-test");
    if (!button) return;
    const ready = alertTestReady();
    button.disabled = !ready;
    const hint = $("alert-test-hint");
    if (hint) hint.hidden = ready;
  }

  function updateAuthSummary() {
    updateNavState();
    syncControlToggle();
  }

  function renderSettings(payload) {
    settings.payload = payload;
    settings.fields = fieldsFromPayload(payload);
    settings.layouts = {};
    settings.placed = new Set();
    settings.changing.clear();
    settings.draftSource = null;
    const nav = $("settings-nav");
    const sections = $("settings-sections");
    nav.replaceChildren();
    sections.replaceChildren();

    for (const section of SECTIONS) {
      const item = el("button", { type: "button", className: "nav-docs-item settings-nav-item" });
      item.dataset.section = section.id;
      item.append(
        el("span", { className: "settings-nav-label", textContent: section.label }),
        el("span", { className: "settings-nav-state", "aria-hidden": "true" }),
      );
      item.addEventListener("click", () => showSection(section.id));
      nav.append(item);

      const panel = el("section", { className: "settings-section", id: "settings-" + section.id });
      panel.dataset.section = section.id;
      panel.setAttribute("aria-labelledby", "settings-" + section.id + "-title");
      const title = el("h3", { className: "settings-section-title", textContent: section.label });
      title.id = "settings-" + section.id + "-title";
      panel.append(title);
      sections.append(panel);
      if (section.id === "grill") renderGrillSection(panel);
      else if (section.id === "alerts") renderAlertsSection(panel);
      else if (section.id === "home") renderHomeSection(panel);
      else if (section.id === "access") renderAccessSection(panel);
      else if (section.id === "system") renderSystemSection(panel);
    }

    // A field the API knows but this panel doesn't: keep it reachable.
    const unplaced = Object.keys(settings.fields).filter((name) => !settings.placed.has(name));
    if (unplaced.length) {
      const panel = $("settings-system");
      const card = el("div", { className: "settings-card" });
      for (const name of unplaced) {
        settings.layouts[name] = "row";
        card.append(renderField(settings.fields[name], "row"));
      }
      panel.append(card);
    }

    showSection("grill");
    if (PHONE_QUERY.matches) showSectionList();
    applySettingsVisibility();
    updateNavState();
    syncControlToggle();
    updateAlertTest();
    updateSystem();
    updateGrillStatus();
    loadAuth();
  }

  function cardFor(names) {
    const card = el("div", { className: "settings-card" });
    for (const name of names) {
      const field = settings.fields[name];
      if (!field) continue;
      settings.placed.add(name);
      settings.layouts[name] = "row";
      card.append(renderField(field, "row"));
    }
    return card;
  }

  function advancedFor(names) {
    const details = el("details", { className: "settings-advanced" });
    details.append(el("summary", { textContent: "Advanced" }));
    details.append(cardFor(names));
    return details;
  }

  function renderGrillSection(panel) {
    panel.append(el("div", {
      id: "settings-grill-status",
      className: "settings-conn",
      role: "status",
      "aria-live": "polite",
    }));
    panel.append(el("span", { className: "section-label", textContent: "Connection" }));
    const connection = el("div", { className: "settings-card" }, [sourceRow()]);
    for (const name of ["host", "device_name", "ble_adapter"]) {
      const field = settings.fields[name];
      if (!field) continue;
      settings.placed.add(name);
      settings.layouts[name] = "row";
      connection.append(renderField(field, "row"));
    }
    panel.append(connection);
    // The model and grill password only apply to a real source, not Demo.
    const grillBlock = el("div", { className: "settings-block" });
    grillBlock.dataset.sources = "http ble";
    grillBlock.append(
      el("span", { className: "section-label", textContent: "Grill" }),
      cardFor(["model", "password"]),
    );
    panel.append(grillBlock);
    panel.append(advancedFor(["poll_seconds", "stale_after_seconds"]));
    panel.append(el("p", {
      className: "settings-note",
      textContent: "Changing the source, address, Bluetooth name or adapter, or grill password reconnects the bridge.",
    }));
  }

  function renderAlertsSection(panel) {
    panel.append(el("span", { className: "section-label", textContent: "Notifications" }));
    panel.append(cardFor(["ntfy_enabled", "ntfy_url", "ntfy_token"]));
    const testButton = el("button", { id: "alert-test", className: "btn btn-secondary btn-sm", type: "button", textContent: "Send test alert" });
    testButton.addEventListener("click", sendTestAlert);
    panel.append(el("div", { className: "settings-test" }, [
      testButton,
      el("p", { id: "alert-test-hint", className: "settings-note", textContent: "Add a topic URL and turn alerts on to send a test." }),
      el("p", { id: "alert-test-status", className: "settings-status", role: "status", "aria-live": "polite" }),
    ]));
    const rulesBlock = el("div", { className: "settings-rules" });
    rulesBlock.dataset.parent = "ntfy_enabled";
    rulesBlock.append(el("span", { className: "section-label", textContent: "Rules" }));
    const rules = el("div", { className: "rules" });
    rules.append(
      renderRule([
        "Alert me when the grill is off its set temperature by ",
        "alert_drift_degrees",
        " °F for ",
        "alert_drift_minutes",
        " minutes.",
      ]),
      renderRule(["Alert me when no reading arrives for ", "alert_stale_seconds", " seconds."]),
      renderRule(["Wait ", "alert_cooldown_seconds", " seconds before repeating one."]),
    );
    rulesBlock.append(rules);
    panel.append(rulesBlock);
    panel.append(el("p", { className: "settings-note", textContent: "Probe targets are set on each probe's card on the dashboard. The drift threshold is in Fahrenheit degrees and scales for °C readings." }));
  }

  function renderRule(parts) {
    const line = el("p", { className: "rule" });
    for (const part of parts) {
      const field = settings.fields[part];
      if (!field) {
        line.append(document.createTextNode(part));
        continue;
      }
      settings.placed.add(part);
      settings.layouts[part] = "rule";
      line.append(renderField(field, "rule"));
    }
    return line;
  }

  function renderPair(left, right) {
    const row = el("div", { className: "setting setting-pair" });
    if (SETTING_DEPENDS_ON[left]) row.dataset.parent = SETTING_DEPENDS_ON[left];
    for (const name of [left, right]) {
      const field = settings.fields[name];
      if (!field) continue;
      settings.placed.add(name);
      settings.layouts[name] = "cell";
      row.append(renderField(field, "cell"));
    }
    return row;
  }

  function renderHomeSection(panel) {
    panel.append(el("span", { className: "section-label", textContent: "Publishing" }));
    const card = cardFor(["mqtt_enabled"]);
    card.append(renderPair("mqtt_host", "mqtt_port"));
    for (const name of ["mqtt_user", "mqtt_password"]) {
      const field = settings.fields[name];
      if (!field) continue;
      settings.placed.add(name);
      settings.layouts[name] = "row";
      card.append(renderField(field, "row"));
    }
    panel.append(card);
    panel.append(advancedFor(["mqtt_prefix"]));
  }

  function renderAccessSection(panel) {
    panel.append(el("span", { className: "section-label", textContent: "Dashboard password" }));
    panel.append(el("div", { id: "settings-auth", className: "settings-card" }, [
      el("p", { className: "settings-note", textContent: "Loading…" }),
    ]));
    panel.append(el("span", { className: "section-label", textContent: "Grill control" }));
    const card = cardFor(["enable_control"]);
    const log = el("button", { id: "settings-log-open", type: "button", className: "link-btn", textContent: "Command log" });
    log.addEventListener("click", () => $("control-log-open").click());
    card.append(el("div", { className: "settings-link-row" }, [log]));
    panel.append(card);
  }

  function renderSystemSection(panel) {
    panel.append(el("span", { className: "section-label", textContent: "Data and logs" }));
    panel.append(cardFor(["retention_days", "log_level"]));
    const version = el("div", { className: "setting" });
    version.append(el("div", { className: "setting-head" }, [el("span", { textContent: "Version" })]));
    version.append(el("span", { id: "settings-version", className: "mono" }, []));
    panel.append(version);
    const envOnly = settings.payload.env_only || [];
    const dev = el("details", { id: "settings-dev", className: "settings-dev" });
    dev.append(el("summary", { textContent: "Set only in .env" }));
    dev.append(el("p", {
      className: "settings-intro",
      textContent: "Saved values live in the bridge's data volume and override .env. A field saved from this page shows Custom; Reset drops it and falls back to .env or the default.",
    }));
    dev.append(el("p", { id: "settings-envonly", className: "settings-note", textContent: envOnly.join(", ") }));
    dev.hidden = !envOnly.length;
    panel.append(dev);
  }

  function showSection(id) {
    settings.section = id;
    document.querySelectorAll(".settings-section").forEach((section) => {
      section.classList.toggle("is-active", section.dataset.section === id);
    });
    document.querySelectorAll(".settings-nav-item").forEach((item) => {
      const active = item.dataset.section === id;
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    settings.view = "section";
    const dialog = $("settings");
    if (dialog) dialog.dataset.view = "section";
    const body = $("settings-sections");
    if (body) body.scrollTop = 0;
  }

  function showSectionList() {
    settings.view = "list";
    const dialog = $("settings");
    if (dialog) dialog.dataset.view = "list";
    const nav = $("settings-nav");
    if (nav) nav.scrollTop = 0;
  }

  // After applying a connection change, follow /api/health until the new
  // runtime has a reading; the Grill section's status line shows it.
  async function watchApply() {
    const token = ++settings.watchToken;
    const until = Date.now() + APPLY_WATCH_MS;
    settings.reconnecting = true;
    updateGrillStatus();
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
        updateGrillStatus();
        updateSystem();
        if (health.connected) {
          settings.reconnecting = false;
          settingStatus("", "");
          updateGrillStatus();
          return;
        }
      }
      if (Date.now() > until) {
        settings.reconnecting = false;
        updateGrillStatus();
        const error = health && health.last_error ? ": " + health.last_error : "";
        settingStatus("Saved, but no reading from the grill yet" + error, "error");
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  }

  // Send one test alert with the saved ntfy URL and token -----------------

  async function sendTestAlert() {
    const button = $("alert-test");
    const status = $("alert-test-status");
    if (!button || !status) return;
    button.disabled = true;
    status.dataset.kind = "";
    status.textContent = "Sending…";
    try {
      // A field that just lost focus is saving; wait for it, then don't test
      // a rejected value in this section.
      await settings.saveChain;
      const section = button.closest(".settings-section");
      if (section && section.querySelector(".setting.has-error")) {
        status.textContent = "Fix the highlighted field first.";
        status.dataset.kind = "error";
        return;
      }
      const res = await apiFetch("api/alerts/test", { method: "POST", cache: "no-store" });
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        status.textContent = "Test alert sent.";
        status.dataset.kind = "ok";
      } else {
        status.textContent = data.detail ? String(data.detail) : "Failed (HTTP " + res.status + ").";
        status.dataset.kind = "error";
      }
    } catch (e) {
      if (e.message !== "not authenticated") {
        status.textContent = "Can't reach the bridge.";
        status.dataset.kind = "error";
      }
    } finally {
      updateAlertTest();
    }
  }

  // Login section: turn the password off and on, and change it ------------

  function authStatus(text, kind) {
    const status = $("auth-status");
    if (!status) return;
    status.textContent = text;
    status.dataset.kind = kind || "";
  }

  // A small password form; Enter runs `submit` instead of leaving the panel.
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
    const button = el("button", { type: "button", className: "btn btn-primary", textContent: label });
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

  function passwordForm() {
    const form = el("form", { id: "settings-password-form", className: "auth-form", hidden: true, novalidate: true });
    form.append(
      el("div", { className: "setting" }, [
        el("label", { htmlFor: "settings-password-current", textContent: "Current password" }),
        el("input", { id: "settings-password-current", name: "current", className: "input", type: "password", autocomplete: "current-password", required: true }),
      ]),
      el("div", { className: "setting" }, [
        el("label", { htmlFor: "settings-password-new", textContent: "New password" }),
        el("input", { id: "settings-password-new", name: "new", className: "input", type: "password", autocomplete: "new-password", minlength: "8", required: true }),
      ]),
      el("p", { className: "setting-hint", textContent: "At least 8 characters. Other devices are signed out." }),
      el("div", { className: "setting" }, [
        el("label", { htmlFor: "settings-password-confirm", textContent: "Confirm new password" }),
        el("input", { id: "settings-password-confirm", name: "confirm", className: "input", type: "password", autocomplete: "new-password", required: true }),
      ]),
      el("button", { id: "settings-password-save", className: "btn btn-primary", type: "submit", textContent: "Change password" }),
    );
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      changePassword();
    });
    return form;
  }

  function renderAuth(session) {
    const box = $("settings-auth");
    if (!box) return;
    const on = Boolean(session.auth_enabled);
    settings.authOn = on;

    const input = el("input", { id: "settings-auth-toggle", type: "checkbox", checked: on });
    const toggle = el("label", { className: "toggle" });
    toggle.setAttribute("aria-label", "Dashboard password");
    toggle.append(input, el("span", { className: "toggle-track" }), el("span", { className: "toggle-thumb" }));
    const stateLabel = el("span", { className: "toggle-state", textContent: on ? "On" : "Off" });

    const summary = el("p", {
      className: "setting-hint",
      textContent: on
        ? "A password is needed to open the dashboard."
        : "Off: anyone on your network can view the grill and change these settings.",
    });
    summary.dataset.on = String(on);

    const change = el("button", { id: "settings-password-open", type: "button", className: "btn btn-secondary btn-sm", textContent: "Change password" });
    const enableForm = authForm(
      [["settings-auth-password", "Password", "current-password"]],
      "Turn on",
      ([password]) => sendAuth("api/auth", "PUT", { required: true, password }, "Password turned on."),
    );
    const changeForm = passwordForm();

    input.addEventListener("change", () => {
      stateLabel.textContent = input.checked ? "On" : "Off";
      if (!input.checked) {
        if (!confirm("Turn off the password? Anyone on your network will be able to open the dashboard and change settings.")) {
          input.checked = true;
          stateLabel.textContent = "On";
          return;
        }
        sendAuth("api/auth", "PUT", { required: false }, "Password turned off.");
      } else {
        // Turning it back on asks for the password, so this browser stays signed in.
        input.checked = false;
        stateLabel.textContent = "Off";
        enableForm.hidden = false;
        authStatus("", "");
        enableForm.querySelector("input").focus();
      }
    });

    change.addEventListener("click", () => {
      changeForm.hidden = !changeForm.hidden;
      authStatus("", "");
      if (!changeForm.hidden) $("settings-password-current").focus();
    });

    box.replaceChildren(
      el("div", { className: "setting" }, [
        el("div", { className: "setting-head" }, [
          el("label", { htmlFor: "settings-auth-toggle", textContent: "Dashboard password" }),
        ]),
        el("div", { className: "toggle-wrap" }, [toggle, stateLabel]),
      ]),
      summary,
      el("div", { className: "auth-actions" }, [change]),
      enableForm,
      changeForm,
      el("p", { id: "auth-status", className: "auth-status", role: "status", "aria-live": "polite" }),
    );
    updateAuthSummary();
    $("logout").hidden = !on;
  }

  async function loadAuth() {
    const session = await checkSession();
    if (session) renderAuth(session);
  }

  async function changePassword() {
    const form = $("settings-password-form");
    const current = form.current.value;
    const next = form.new.value;
    if (!current) return authStatus("Enter your current password.", "error");
    if (next.length < PASSWORD_MIN_LENGTH) return authStatus("Use at least " + PASSWORD_MIN_LENGTH + " characters.", "error");
    if (next !== form.confirm.value) return authStatus("The new passwords don't match.", "error");
    const button = $("settings-password-save");
    button.disabled = true;
    authStatus("Saving…", "busy");
    try {
      const res = await apiFetch("api/auth/password", {
        method: "POST",
        cache: "no-store",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ current, new: next }),
      });
      if (res.ok) {
        form.reset();
        form.hidden = true;
        authStatus("Password changed. Other devices are signed out.", "ok");
        return;
      }
      const data = await res.json().catch(() => ({}));
      authStatus(res.status === 403 ? "Current password is wrong." : data.detail ? String(data.detail) : "Failed (HTTP " + res.status + ").", "error");
      if (res.status === 403) form.current.select();
    } catch (e) {
      if (e.message !== "not authenticated") authStatus("Can't reach the bridge.", "error");
    } finally {
      button.disabled = false;
    }
  }

  async function openSettings() {
    const dialog = $("settings");
    settingStatus("", "");
    settings.open = true;
    settings.reconnecting = false;
    settings.view = PHONE_QUERY.matches ? "list" : "section";
    dialog.dataset.view = settings.view;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    try {
      const res = await apiFetch("api/settings", { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const payload = await res.json();
      if (!settings.open) return;
      renderSettings(payload);
    } catch (e) {
      if (e.message !== "not authenticated") {
        $("settings-sections").replaceChildren(el("p", { className: "settings-note", textContent: "Can't load settings from the bridge." }));
      }
    }
  }

  function closeSettings() {
    settings.watchToken++;
    settings.open = false;
    settings.reconnecting = false;
    for (const timer of Object.values(settings.savedTimers)) clearTimeout(timer);
    const dialog = $("settings");
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  function setupSettings() {
    $("settings-open").addEventListener("click", openSettings);
    $("settings-close").addEventListener("click", closeSettings);
    $("settings-back").addEventListener("click", showSectionList);
    $("settings").addEventListener("close", () => {
      settings.open = false;
      settings.watchToken++;
      // Saving may have turned ntfy or grill control on or off.
      loadAlertTargets();
      loadControl();
    });
    PHONE_QUERY.addEventListener("change", (event) => {
      if (!settings.open) return;
      if (event.matches) showSectionList();
      else showSection(settings.section);
    });
  }

  // Start ----------------------------------------------------------------

  function start() {
    setupControls();
    setupLogout();
    setupSettings();
    setupAlertTargets();
    setupGrillControls();
    renderReadings();
    renderStatus();
    // History's time window is based on the bridge clock, so learn the clock
    // offset from the current state first.
    loadInitialState().finally(loadHistory);
    connectLive();
    pollHealth();
    setInterval(pollHealth, HEALTH_POLL_MS);
    setInterval(() => { if (!control.sending) loadControl(); }, CONTROL_POLL_MS);
    setInterval(renderStatus, 1000);
    // Slide the chart window forward even when no readings arrive.
    setInterval(scheduleChart, 15000);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") return;
      if (!state.ws) connectLive();
      pollHealth();
      loadAlertTargets();
      loadControl();
      scheduleChart();
    });

    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("sw.js").catch(() => { /* needs HTTPS or localhost */ });
    }
  }

  start();
})();
