/* Sure Weather PWA — vanilla JS, no build step. */
"use strict";

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js");
}

const VAR_LABELS_KEYS = {
  temperature_2m: "var_temp",
  dew_point_2m: "var_dew",
  relative_humidity_2m: "var_hum",
  precipitation: "var_rain",
  precipitation_probability: "var_rainprob",
  cloud_cover: "var_cloud",
  wind_speed_10m: "var_wind",
  wind_gusts_10m: "var_gust",
  pressure_msl: "var_press",
  visibility: "var_vis",
};

function varLabel(v) {
  return tr(VAR_LABELS_KEYS[v] || v);
}

const VAR_UNITS = {
  temperature_2m: "°C",
  dew_point_2m: "°C",
  relative_humidity_2m: "%",
  precipitation: "mm",
  precipitation_probability: "%",
  cloud_cover: "%",
  wind_speed_10m: "m/s",
  wind_gusts_10m: "m/s",
  pressure_msl: "hPa",
  visibility: "m",
};

const VAR_ORDER = [
  "temperature_2m",
  "dew_point_2m",
  "relative_humidity_2m",
  "precipitation",
  "precipitation_probability",
  "cloud_cover",
  "wind_speed_10m",
  "wind_gusts_10m",
  "pressure_msl",
  "visibility",
];

let state = { lat: 48.8566, lon: 2.3522, hours: 24, name: "Paris" };
let reqToken = 0;

/* ---- Deep links ----
   ?lat=&lon=&name= pre-selects a place (shared links, home-screen
   shortcuts, widgets); ?geo=1 asks for geolocation on boot. The URL is
   kept in sync so "copy link" always reproduces the current view. */
(function initStateFromUrl() {
  const p = new URLSearchParams(location.search);
  const la = parseFloat(p.get("lat"));
  const lo = parseFloat(p.get("lon"));
  if (Number.isFinite(la) && Number.isFinite(lo)) {
    state.lat = la;
    state.lon = lo;
    state.name = p.get("name") || `${la.toFixed(4)}, ${lo.toFixed(4)}`;
  }
})();

function syncUrl() {
  const q = `?lat=${state.lat}&lon=${state.lon}&name=${encodeURIComponent(state.name)}`;
  history.replaceState(null, "", q);
}

/* ---- Favorites (localStorage) ---- */
const FAV_KEY = "sure-weather-favorites";

function loadFavorites() {
  try {
    const raw = localStorage.getItem(FAV_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}

function saveFavorites(list) {
  try {
    localStorage.setItem(FAV_KEY, JSON.stringify(list));
  } catch {
    /* storage unavailable (private mode): favorites just don't persist */
  }
}

function isFavorite(lat, lon) {
  return loadFavorites().some((f) => Math.abs(f.lat - lat) < 1e-4 && Math.abs(f.lon - lon) < 1e-4);
}

function toggleFavorite() {
  let list = loadFavorites();
  const idx = list.findIndex((f) => Math.abs(f.lat - state.lat) < 1e-4 && Math.abs(f.lon - state.lon) < 1e-4);
  if (idx >= 0) {
    list.splice(idx, 1);
  } else {
    list.unshift({ lat: state.lat, lon: state.lon, name: state.name });
  }
  saveFavorites(list);
  renderFavorites();
}

function renderFavorites() {
  const list = loadFavorites();
  const wrap = el("fav-list");
  const btn = el("fav-btn");
  if (btn) {
    const active = isFavorite(state.lat, state.lon);
    btn.classList.toggle("fav--active", active);
    btn.title = active ? "Retirer des favoris" : "Ajouter aux favoris";
    btn.innerHTML = active ? "★" : "☆";
  }
  if (!wrap) return;
  wrap.innerHTML = "";
  if (!list.length) {
    const empty = document.createElement("li");
    empty.className = "fav__empty";
    empty.textContent = "Aucun favori — cliquez sur ☆ pour enregistrer un lieu";
    wrap.appendChild(empty);
    return;
  }
  for (const f of list) {
    const li = document.createElement("li");
    const a = document.createElement("button");
    a.type = "button";
    a.className = "fav__item";
    a.textContent = f.name;
    a.addEventListener("click", async () => {
      state.lat = f.lat;
      state.lon = f.lon;
      state.name = f.name;
      closeFavorites();
      await loadForecast();
    });
    li.appendChild(a);
    wrap.appendChild(li);
  }
}

function closeFavorites() {
  const menu = el("fav-menu");
  if (menu) menu.hidden = true;
}

const $ = (s) => document.querySelector(s);
const el = (id) => document.getElementById(id);
const hide = (n) => (n.hidden = true);
const show = (n) => (n.hidden = false);

function fmtTime(iso) {
  return new Date(iso).toLocaleTimeString(LANG === "en" ? "en-GB" : "fr-FR", { hour: "2-digit", minute: "2-digit" });
}
function fmtDay(iso) {
  return new Date(iso).toLocaleDateString("fr-FR", { weekday: "short", day: "numeric", month: "short" });
}
function round(v, d = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toFixed(d);
}

function isNight(iso) {
  const h = new Date(iso).getHours();
  return h < 6 || h >= 21;
}

/* Wind is stored in m/s (model unit) but shown in km/h for a FR audience. */
function kmh(item, d = 0) {
  if (!item) return "—";
  const lo = item.low !== undefined ? Math.round(item.low * 3.6) : null;
  const hi = item.high !== undefined ? Math.round(item.high * 3.6) : null;
  if (lo !== null && hi !== null && lo !== hi) return `${lo}–${hi} km/h`;
  return `${Math.round(item.value * 3.6)} km/h`;
}

/* Wind direction: meteorological convention — the sector the wind COMES
   FROM. The arrow shows where it GOES (deg + 180). */
const CARDINALS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO"];
const FLOW_ARROWS = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"];

function windDir(row) {
  const d = row.wind_direction_10m?.value;
  if (d === null || d === undefined) return null;
  const deg = ((d % 360) + 360) % 360;
  return {
    deg,
    from: CARDINALS[Math.round(deg / 22.5) % 16],
    arrow: FLOW_ARROWS[Math.round(((deg + 180) % 360) / 45) % 8],
  };
}

/* Rough weather icon + sky key from available variables. */
function weatherKey(row) {
  const cloud = row.cloud_cover?.value ?? 0;
  const rain = row.precipitation?.value ?? 0;
  const prob = row.precipitation_probability?.value ?? 0;
  if (rain > 5) return { icon: "⛈", sky: "storm" };
  if (prob >= 70 || rain > 0.3) return { sky: "rain" };
  if (cloud >= 70) return { sky: isNight(row.time) ? "night" : "cloud" };
  if (cloud >= 30) return { sky: isNight(row.time) ? "night" : "partly" };
  return { sky: isNight(row.time) ? "night" : "day" };
}


/* ---- Weather icons: inline SVG, consistent across platforms ---- */
const WEATHER_ART = {
  day: '<g stroke="#f0b429" stroke-width="3" stroke-linecap="round">'
     + '<line x1="24" y1="4" x2="24" y2="10"/><line x1="24" y1="38" x2="24" y2="44"/>'
     + '<line x1="4" y1="24" x2="10" y2="24"/><line x1="38" y1="24" x2="44" y2="24"/>'
     + '<line x1="10" y1="10" x2="14.5" y2="14.5"/><line x1="33.5" y1="33.5" x2="38" y2="38"/>'
     + '<line x1="10" y1="38" x2="14.5" y2="33.5"/><line x1="33.5" y1="14.5" x2="38" y2="10"/></g>'
     + '<circle cx="24" cy="24" r="9" fill="#f0b429"/>',
  night: '<path d="M31 6 a17 17 0 1 0 11 29 a14 14 0 0 1 -11 -29 z" fill="#aebdd4"/>',
  cloud: '<path d="M15 36 a8 8 0 0 1 -.6 -16 a10 10 0 0 1 19.4 -1.4 a7.5 7.5 0 0 1 1.2 14.9 z" fill="#c2cfdd"/>',
  partly: '<circle cx="17" cy="16" r="7" fill="#f0b429"/>'
        + '<g stroke="#f0b429" stroke-width="2.4" stroke-linecap="round">'
        + '<line x1="17" y1="3" x2="17" y2="7"/><line x1="5" y1="16" x2="9" y2="16"/>'
        + '<line x1="8.5" y1="7.5" x2="11.5" y2="10.5"/></g>'
        + '<path d="M18 40 a8 8 0 0 1 -.6 -16 a10 10 0 0 1 19.4 -1.4 a7.5 7.5 0 0 1 1.2 14.9 z" fill="#d5deea"/>',
  rain: '<path d="M15 32 a8 8 0 0 1 -.6 -16 a10 10 0 0 1 19.4 -1.4 a7.5 7.5 0 0 1 1.2 14.9 z" fill="#c2cfdd"/>'
      + '<g stroke="#5b9be0" stroke-width="3" stroke-linecap="round">'
      + '<line x1="17" y1="36" x2="15" y2="42"/><line x1="25" y1="36" x2="23" y2="42"/>'
      + '<line x1="33" y1="36" x2="31" y2="42"/></g>',
  storm: '<path d="M15 32 a8 8 0 0 1 -.6 -16 a10 10 0 0 1 19.4 -1.4 a7.5 7.5 0 0 1 1.2 14.9 z" fill="#aeb9c9"/>'
       + '<path d="M24 34 l7 -9 h-5 l4 -8 -9 11 h5 z" fill="#eec33d"/>',
  snow: '<path d="M15 32 a8 8 0 0 1 -.6 -16 a10 10 0 0 1 19.4 -1.4 a7.5 7.5 0 0 1 1.2 14.9 z" fill="#c2cfdd"/>'
      + '<g fill="#7cc4e8"><circle cx="17" cy="39" r="2.2"/><circle cx="25" cy="42" r="2.2"/>'
      + '<circle cx="33" cy="39" r="2.2"/></g>',
  fog: '<path d="M15 30 a8 8 0 0 1 -.6 -16 a10 10 0 0 1 19.4 -1.4 a7.5 7.5 0 0 1 1.2 14.9 z" fill="#c2cfdd"/>'
     + '<g stroke="#9fb0c4" stroke-width="3" stroke-linecap="round">'
     + '<line x1="12" y1="37" x2="36" y2="37"/><line x1="16" y1="43" x2="32" y2="43"/></g>',
};

function weatherSvg(sky, size) {
  return `<svg viewBox="0 0 48 48" width="${size}" height="${size}" aria-hidden="true">${WEATHER_ART[sky] || WEATHER_ART.cloud}</svg>`;
}

function confBadge(c, calibrated) {

  if (!calibrated || c === null || c === undefined) {
    return `<span class="hour__conf hour__conf--na">${tr("conf_na")}</span>`;
  }
  if (c >= 0.98) return `<span class="hour__conf hour__conf--hi">sûr 98%+</span>`;
  if (c >= 0.95) return `<span class="hour__conf hour__conf--hi">95%+</span>`;
  if (c >= 0.9) return `<span class="hour__conf hour__conf--mid">90%+</span>`;
  return `<span class="hour__conf hour__conf--lo"><90%</span>`;
}

/* Honest range display: show the interval instead of a fake-precise point. */
function rangeText(item, d = 1) {
  if (!item) return "—";
  if (item.calibrated && item.confidence >= 0.98) {
    return `${round(item.value, d)}${d === 1 ? "" : ""}`;
  }
  const lo = item.low !== undefined ? round(item.low, d) : null;
  const hi = item.high !== undefined ? round(item.high, d) : null;
  if (lo !== null && hi !== null && lo !== hi) return `${lo}–${hi}`;
  return round(item.value, d);
}

function confColor(c) {
  if (c >= 0.99) return "#0f9d58";
  if (c >= 0.95) return "#2b6df6";
  if (c >= 0.9) return "#d97706";
  return "#dc2626";
}

/* ---- Geo ---- */

async function geocode(query) {
  if (query.includes(",")) {
    const [lat, lon] = query.split(",").map((s) => parseFloat(s.trim()));
    if (Number.isFinite(lat) && Number.isFinite(lon)) {
      return { lat, lon, name: `${lat.toFixed(4)}, ${lon.toFixed(4)}` };
    }
  }
  // Open-Meteo knows cities; fall back to Nominatim (via our /geocode proxy)
  // for exact street addresses when the city search finds nothing.
  const url = `https://geocoding-api.open-meteo.com/v1/search?name=${encodeURIComponent(query)}&count=1&language=fr&format=json`;
  try {
    const r = await fetch(url);
    const d = await r.json();
    if (d.results && d.results.length) {
      const res = d.results[0];
      return { lat: res.latitude, lon: res.longitude, name: res.name + (res.country ? `, ${res.country}` : "") };
    }
  } catch {
    /* fall through to address geocoder */
  }
  const g = await fetch(`/geocode?q=${encodeURIComponent(query)}&limit=1`);
  if (!g.ok) throw new Error(`adresse introuvable`);
  const gd = await g.json();
  if (!gd.results || !gd.results.length) throw new Error(tr("err_notfound"));
  const hit = gd.results[0];
  return { lat: hit.lat, lon: hit.lon, name: hit.name };
}

function locateMe() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error(tr("geoloc_err")));
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude, name: "Ma position" }),
      (err) => reject(new Error(tr("geoloc_err"))),
      { timeout: 10000 }
    );
  });
}

/* ---- Language toggle ---- */
const langBtn = document.getElementById("lang-btn");
if (langBtn) {
  langBtn.textContent = LANG.toUpperCase();
  langBtn.addEventListener("click", () => {
    setLang(LANG === "fr" ? "en" : "fr");
    langBtn.textContent = LANG.toUpperCase();
  });
}

/* ---- Theme ---- */
const themeBtn = document.getElementById("theme-btn");
function applyThemeButton() {
  themeBtn.textContent = document.documentElement.dataset.theme === "dark" ? "☀️" : "🌙";
}
themeBtn.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("sure-weather-theme", next);
  applyThemeButton();
});
applyThemeButton();

/* ---- Load ---- */

/* Progressive, non-blocking load: the place name renders immediately with
   shimmer placeholders, any cached data paints instantly, and the fresh
   forecast (always the full 7 days) replaces the view when it lands. Range
   switches (24h/72h/7j) then slice the cached data client-side: no network,
   no reload. */
const dataCache = new Map(); // "lat,lon" -> full 7-day forecast
let currentData = null;

function cacheKey() {
  return `${state.lat.toFixed(3)},${state.lon.toFixed(3)}`;
}

function showSkeletons(name) {
  el("loc-name").textContent = name;
  el("now-temp").textContent = "—";
  el("now-sub").textContent = "";
  el("verdict").innerHTML =
    `<span class="sk" style="width:130px;height:26px"></span>` +
    `<span class="sk" style="width:96px;height:26px"></span>`;
  el("timeline-chart").innerHTML = `<div class="sk" style="height:200px"></div>`;
  el("hours").innerHTML =
    `<div class="sk" style="height:84px"></div>`.repeat(8);
  el("hours-count").textContent = "";
  show(el("content"));
  hide(el("error"));
  hide(el("empty"));
  document.body.classList.add("is-loading");
}

async function loadForecast() {
  const token = ++reqToken;
  const key = cacheKey();
  const cached = dataCache.get(key);
  showSkeletons(state.name);
  hide(el("loading"));
  if (cached) render(cached, state.hours); // instant paint, then refresh
  else show(el("loading"));
  hide(el("error"));
  const spinner = el("loading");
  const status = spinner.querySelector("#load-status");
  const progress = [
    tr("loading_src"),
    tr("loading_arch"),
    tr("loading_calib"),
    tr("loading_cross"),
    tr("loading_wait"),
  ];
  let i = 0;
  const tick = setInterval(() => {
    if (status && i < progress.length) status.textContent = progress[i++];
  }, 6000);
  const ctrl = new AbortController();
  const abortTimer = setTimeout(() => ctrl.abort(), 45000);
  try {
    syncUrl();
    const r = await fetch(`/weather?lat=${state.lat}&lon=${state.lon}&hours=168`, { signal: ctrl.signal });
    clearTimeout(abortTimer);
    clearInterval(tick);
    if (token !== reqToken) return;
    if (!r.ok) throw new Error(`API ${r.status}`);
    const data = await r.json();
    if (!data.forecast || !data.forecast.length) {
      hide(el("loading"));
      hide(el("content"));
      show(el("empty"));
      return;
    }
    dataCache.set(key, data);
    if (dataCache.size > 24) dataCache.delete(dataCache.keys().next().value);
    document.body.classList.remove("is-loading");
    hide(el("loading"));
    render(data, state.hours);
  } catch (e) {
    clearTimeout(abortTimer);
    clearInterval(tick);
    if (token !== reqToken) return;
    document.body.classList.remove("is-loading");
    hide(el("loading"));
    if (!cached) hide(el("content"));
    const msg = e.name === "AbortError"
      ? tr("err_slow")
      : e.message;
    showError(msg);
  }
}

function showError(message) {
  const box = el("error");
  box.innerHTML = "";
  const span = document.createElement("span");
  span.textContent = message + " ";
  const btn = document.createElement("button");
  btn.className = "btn btn--ghost error__retry";
  btn.type = "button";
  btn.textContent = tr("retry");
  btn.addEventListener("click", loadForecast);
  box.appendChild(span);
  box.appendChild(btn);
  show(box);
}

/* ---- Render ---- */

/* Client-side window summary (mirrors the server's bands) so range
   switches never need a network round-trip. */
function clientSummary(fc) {
  const bands = [[0, 6, "3h"], [6, 24, "12h"], [24, 72, "48h"], [72, Infinity, "J+"]];
  const horizons = {};
  for (const item of fc) {
    for (const [lo, hi, label] of bands) {
      if (lo === 0 ? item.horizon_h <= hi : item.horizon_h > lo && item.horizon_h <= hi) {
        (horizons[label] = horizons[label] || []).push(item.confidence);
        break;
      }
    }
  }
  const out = { horizons: {} };
  for (const [label, confs] of Object.entries(horizons)) {
    out.horizons[label] = {
      avg_confidence: confs.reduce((a, b) => a + b, 0) / confs.length,
      sure_share: confs.filter((c) => c >= 0.98).length / confs.length,
      n: confs.length,
    };
  }
  return out;
}

function render(data, hours = state.hours) {
  currentData = data;
  hide(el("loading"));
  hide(el("empty"));
  show(el("content"));

  // The map must be sized *after* its container is visible: Leaflet
  // initialized while `#content` is hidden gets 0 height and never fetches
  // tiles (gray map). Initialize lazily here and refresh size each render.
  initMap();
  if (map) setTimeout(() => map.invalidateSize(), 50);
  applyMapLayer();

  renderFavorites();

  const fc = data.forecast.filter((i) => i.horizon_h <= hours + 0.5);
  const summary = clientSummary(fc);
  const byTime = {};
  for (const item of fc) (byTime[item.valid_at] = byTime[item.valid_at] || {})[item.variable] = item;
  const times = Object.keys(byTime).sort();

  el("loc-name").textContent = state.name;
  // Union of contributors across the whole window: the first item alone can
  // undercount (its group may lack station reports).
  const contributors = new Set(fc.flatMap((i) => i.contributors ?? []));
  const stationSet = new Set([...contributors].filter((c) => c.startsWith("metar_")).map((c) => c.replace("metar_", "")));
  const modelSet = new Set([...contributors].filter((c) => !c.startsWith("metar_")));
  const stationCount = stationSet.size;
  const modelCount = modelSet.size;
  const modelNames = [...modelSet].sort();
  const stationNames = [...stationSet].sort();
  const shown = modelNames.slice(0, 3).join(", ") + (modelNames.length > 3 ? ` +${modelNames.length - 3}` : "");
  const footerEl = el("footer-note");
  footerEl.textContent = tr("sources", {
    models: shown,
    st: stationCount,
    u: new Date(data.generated_at).toLocaleTimeString(LANG === "en" ? "en-GB" : "fr-FR"),
  });
  footerEl.title = `${tr("sources_full")}: ${modelNames.join(", ")} | ${stationNames.join(", ")}`;
  centerMapOn(state.lat, state.lon, state.name);
  loadRadar();
  clearTimeout(windTimer);
  windTimer = setTimeout(refreshWindArrows, 1200);

  /* Hero: current conditions — one glance, one line of small print */
  const nowRow = byTime[times[0]] || {};
  const wk = weatherKey({ ...nowRow, time: times[0] });
  el("sky-icon").innerHTML = weatherSvg(wk.sky, 56);
  document.body.dataset.sky = wk.sky;
  const t = nowRow.temperature_2m;
  el("now-temp").textContent = t ? `${round(t.value)}°` : "—";
  const sub = [];
  if (t) sub.push(`${tr("now_feels")} ${rangeText(t)}°`);
  'if (nowRow.wind_speed_10m) {
    const wd = windDir(nowRow);
    sub.push(`<span class="hour__windarrow" style="transform:rotate(${wd ? (wd.deg + 180) % 360 : 0}deg)">➤</span> ${kmh(nowRow.wind_speed_10m)}${wd ? ` (${wd.from})` : ""}`);
  }'
  if (nowRow.relative_humidity_2m) sub.push(`💧 ${rangeText(nowRow.relative_humidity_2m, 0)}%`);
  if (nowRow.precipitation_probability && nowRow.precipitation_probability.value > 0)
    sub.push(`☔ ${round(nowRow.precipitation_probability.value, 0)}%`);
  el("now-sub").textContent = sub.join(" · ");

  /* Timeline */
  renderTimeline(times, byTime);

  /* Hourly cards */
  renderHours(times, byTime);

  /* Confidence per variable */
  renderConfidence(fc);

  /* Global sure badge */
  renderSureBadge(summary);

  /* Local station badge */
  renderStationBadge(fc);

  /* Table */
  renderTable(times, byTime);
}

/* Local station badge: a number, the words live in the tooltip. */
function renderStationBadge(fc) {
  const badge = el("station-badge");
  const first = fc.find((i) => i.contributors?.length);
  const stations = new Set(first?.contributors?.filter((c) => c.startsWith("metar_")) ?? []);
  if (!stations.size) {
    badge.hidden = true;
    return;
  }
  badge.hidden = false;
  badge.className = "station-badge";
  badge.innerHTML = `📍 ${stations.size}`;
  badge.title = tr(stations.size > 1 ? "st_many" : "st_one", { n: stations.size });
}

/* Trust pill: a colored check + the number; details on hover. */
function renderSureBadge(summary) {
  const badge = el("sure-badge");
  if (!summary?.horizons) {
    badge.hidden = true;
    return;
  }
  const h = state.hours <= 24 ? "12h" : state.hours <= 72 ? "48h" : "J+";
  const bucket = summary.horizons[h] || summary.horizons["3h"];
  if (!bucket) {
    badge.hidden = true;
    return;
  }
  const share = Math.round(bucket.sure_share * 100);
  const avg = Math.round(bucket.avg_confidence * 100);
  let cls, icon;
  if (share >= 70) {
    cls = "sure--ok";
    icon = "✓";
  } else if (share >= 40) {
    cls = "sure--mid";
    icon = "✓";
  } else if (share >= 15) {
    cls = "sure--warn";
    icon = "!";
  } else {
    cls = "sure--bad";
    icon = "!";
  }
  badge.hidden = false;
  badge.className = `sure-badge ${cls}`;
  badge.innerHTML = `<span class="sure-badge__ic">${icon}</span>${avg}%`;
  badge.title = `${share}% des valeurs sûres · confiance moyenne ${avg}% sur ${h}`;
}

/* ---- Visual verdict: the 24h answer without reading ---- */

function tempColor(v) {
  if (v === null || v === undefined) return "#94a3b8";
  if (v <= 0) return "#2563eb";
  if (v <= 8) return "#0ea5e9";
  if (v <= 16) return "#14b8a6";
  if (v <= 23) return "#f59e0b";
  if (v <= 30) return "#f97316";
  return "#ef4444";
}

function renderVerdict(times, byTime) {
  const wrap = el("verdict");
  const rows = times.map((t) => ({ t, row: byTime[t] || {} }));
  const pills = [];
  // Rain: when does it start, how likely.
  const rainHours = rows.filter(
    (h) => (h.row.precipitation_probability?.value ?? 0) >= 50 || (h.row.precipitation?.value ?? 0) > 0.3
  );
  if (!rainHours.length) {
    pills.push(`<span class="vpill vpill--ok"><span class="vpill__ic">☀️</span>${tr("v_dry")}</span>`);
  } else {
    const first = rainHours[0];
    const peak = Math.max(...rainHours.map((h) => h.row.precipitation_probability?.value ?? 0));
    pills.push(
      `<span class="vpill vpill--rain"><span class="vpill__ic">☔</span>${tr("v_rain", { h: fmtTime(first.t) })}${peak >= 70 ? ` · ${Math.round(peak)}%` : ""}</span>`
    );
  }
  // Temperature: max & min with hour of max.
  const temps = rows.map((h) => h.row.temperature_2m?.value).filter((v) => v !== null && v !== undefined);
  if (temps.length) {
    const maxT = Math.max(...temps);
    const minT = Math.min(...temps);
    const at = rows.find((h) => h.row.temperature_2m?.value === maxT)?.t;
    pills.push(
      `<span class="vpill"><span class="vpill__ic">🌡️</span><b style="color:${tempColor(maxT)}">${round(maxT)}°</b> / ${round(minT)}°${at ? ` <span class="vpill__sub">${tr("v_at", { h: fmtTime(at) })}</span>` : ""}</span>`
    );
  }
  // Wind alert when gusts become unpleasant.
  const gusts = rows.map((h) => (h.row.wind_gusts_10m?.value ?? 0) * 3.6);
  const maxGust = Math.max(...gusts, 0);
  if (maxGust >= 55) {
    const at = rows[gusts.indexOf(maxGust)]?.t;
    pills.push(`<span class="vpill vpill--warn"><span class="vpill__ic">💨</span>${tr("v_gust", { v: Math.round(maxGust) })}${at ? ` ${tr("v_at", { h: fmtTime(at) })}` : ""}</span>`);
  }
  // Heat alert.
  if (temps.length && Math.max(...temps) >= 32) {
    pills.push(`<span class="vpill vpill--warn"><span class="vpill__ic">🥵</span>${tr("v_heat")}</span>`);
  }
  if (rows.length && rows.some((h) => h.row.temperature_2m?.value !== undefined && h.row.temperature_2m.value <= 0)) {
    pills.push(`<span class="vpill vpill--warn"><span class="vpill__ic">❄️</span>${tr("v_frost")}</span>`);
  }
  wrap.innerHTML = pills.join("");
}

/* ---- Meteogram: icons, temp curve, rain bars, night bands ---- */

let lastMeteo = null;

function catmullRomPath(pts) {
  // Smooth curve through points (Catmull-Rom converted to cubic beziers).
  if (pts.length < 2) return "";
  let d = `M ${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(0, i - 1)];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[Math.min(pts.length - 1, i + 2)];
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
    const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    d += ` C ${c1[0]},${c1[1]} ${c2[0]},${c2[1]} ${p2[0]},${p2[1]}`;
  }
  return d;
}

function renderTimeline(times, byTime) {
  lastMeteo = { times, byTime };
  const chart = el("timeline-chart");
  renderVerdict(times, byTime);

  const W = Math.max(chart.clientWidth || 600, 280);
  const H = 200;
  const padL = 6, padR = 6, padT = 40, padB = 22;
  const rainH = 52; // bottom zone for rain bars
  const n = times.length;
  const colW = (W - padL - padR) / Math.max(n, 1);
  const innerW = W - padL - padR;
  const innerH = H - padT - padB - rainH;

  const rows = times.map((t) => byTime[t] || {});
  const temps = rows.map((r) => r.temperature_2m?.value).filter((v) => v !== null && v !== undefined);
  const tMin = Math.min(...temps, 0);
  const tMax = Math.max(...temps, 1);
  const tSpan = Math.max(tMax - tMin, 1);
  const xAt = (i) => padL + colW * (i + 0.5);
  const yTemp = (v) => padT + (1 - (v - tMin) / tSpan) * innerH;
  const yBase = H - padB;

  const parts = [];
  // Night bands + hour labels + rain bars + temp points.
  const labelStep = Math.max(1, Math.ceil(38 / Math.max(colW, 1)));
  const iconStep = Math.max(1, Math.ceil(34 / Math.max(colW, 1)));
  const pts = [];
  rows.forEach((row, i) => {
    const x0 = padL + i * colW;
    const hour = new Date(times[i]).getHours();
    if (hour < 7 || hour >= 20) {
      parts.push(`<rect x="${x0}" y="${padT - 14}" width="${colW}" height="${H - padB - padT + 14}" style="fill:var(--mg-night)"/>`);
    }
    const prob = row.precipitation_probability?.value ?? 0;
    const mm = row.precipitation?.value ?? 0;
    if (prob >= 5 || mm > 0.05) {
      const bh = Math.max((Math.max(prob, Math.min(mm * 20, 100)) / 100) * rainH, 3);
      parts.push(
        `<rect x="${(x0 + colW * 0.18).toFixed(1)}" y="${(yBase - bh).toFixed(1)}" width="${(colW * 0.64).toFixed(1)}" height="${bh.toFixed(1)}" rx="2" fill="#3b82f6" opacity="${(0.25 + Math.min(prob, 100) / 100 * 0.55).toFixed(2)}"><title>pluie ${Math.round(prob)}%</title></rect>`
      );
    }
    const v = row.temperature_2m?.value;
    if (v !== null && v !== undefined) {
      pts.push([xAt(i), yTemp(v), v]);
    }
    if (i % labelStep === 0) {
      parts.push(`<text x="${xAt(i).toFixed(1)}" y="${H - 7}" text-anchor="middle" class="mg-hour">${fmtTime(times[i])}</text>`);
    }
    if (i % iconStep === 0) {
      const wk = weatherKey({ ...row, time: times[i] });
      parts.push(`<svg x="${(xAt(i) - 12).toFixed(1)}" y="${padT - 36}" width="24" height="24" viewBox="0 0 48 48">${WEATHER_ART[wk.sky] || WEATHER_ART.cloud}</svg>`);
    }
  });

  // Temperature curve + gradient area.
  if (pts.length > 1) {
    const line = catmullRomPath(pts.map((p) => [p[0], p[1]]));
    const area = line + ` L ${pts[pts.length - 1][0]},${padT} L ${pts[0][0]},${padT} Z`;
    const stops = pts
      .map((p, i) => `<stop offset="${((p[0] - padL) / innerW * 100).toFixed(1)}%" stop-color="${tempColor(p[2])}"/>`)
      .join("");
    parts.push(
      `<defs><linearGradient id="mg-line" x1="0" y1="0" x2="1" y2="0">${stops}</linearGradient>` +
      `<linearGradient id="mg-area" x1="0" y1="0" x2="0" y2="1">` +
      `<stop offset="0" stop-color="#2f6bff" stop-opacity=".16"/><stop offset="1" stop-color="#2f6bff" stop-opacity="0"/></linearGradient></defs>`
    );
    parts.push(`<path d="${area}" fill="url(#mg-area)"/>`);
    parts.push(`<path d="${line}" fill="none" stroke="url(#mg-line)" stroke-width="2.5" stroke-linecap="round"/>`);
    // Temp labels (thin out to avoid collisions).
    const tStep = Math.max(1, Math.ceil(40 / Math.max(colW, 1)));
    pts.forEach((p, i) => {
      if (i % tStep === 0) {
        parts.push(`<text x="${p[0].toFixed(1)}" y="${(p[1] - 7).toFixed(1)}" text-anchor="middle" class="mg-temp" fill="${tempColor(p[2])}">${round(p[2])}°</text>`);
      }
      parts.push(`<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2.2" fill="${tempColor(p[2])}" style="stroke:var(--dot-stroke)"/>`);
    });
  }

  // "Now" marker on the first column (label below the icon row).
  parts.push(
    `<line x1="${xAt(0).toFixed(1)}" y1="${padT - 8}" x2="${xAt(0).toFixed(1)}" y2="${yBase}" stroke="#2f6bff" stroke-width="1.5" stroke-dasharray="3 3" opacity=".55"/>` +
    `<text x="${(xAt(0) + 5).toFixed(1)}" y="${((padT + yBase) / 2 + 3).toFixed(1)}" class="mg-now">${tr("now_label")}</text>`
  );

  chart.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="Prévision horaire">${parts.join("")}</svg>`;
}

window.addEventListener("resize", (() => {
  let t = null;
  return () => {
    clearTimeout(t);
    t = setTimeout(() => {
      if (lastMeteo && el("timeline-chart")) renderTimeline(lastMeteo.times, lastMeteo.byTime);
    }, 150);
  };
})());

function renderHours(times, byTime) {
  const wrap = el("hours");
  wrap.innerHTML = "";
  const count = el("hours-count");
  if (count) count.textContent = `${times.length} h`;
  let lastDay = null;
  for (const t of times) {
    const row = byTime[t] || {};
    const wk = weatherKey({ ...row, time: t });
    const temp = row.temperature_2m;
    const prob = row.precipitation_probability;
    const wind = row.wind_speed_10m;
    const gust = row.wind_gusts_10m;
    const confs = VAR_ORDER.filter((v) => row[v] && row[v].calibrated).map((v) => row[v].confidence);
    const avgConf = confs.length ? confs.reduce((a, b) => a + b, 0) / confs.length : null;
    const cal = !!(row.temperature_2m && row.temperature_2m.calibrated);
    // The date is shown once per day, not on every card: repeated labels
    // turn the grid into noise.
    const day = fmtDay(t);
    const showDay = day !== lastDay;
    lastDay = day;
    const rng =
      temp && temp.calibrated && temp.confidence < 0.98 && temp.low !== temp.high
        ? `${round(temp.low, 0)}–${round(temp.high, 0)}°`
        : "";
    const probVal = prob?.value ?? 0;
    const wd = windDir(row);
    const card = document.createElement("div");
    card.className = "hour";
    if (temp) card.style.setProperty("--tint", tempColor(temp.value));
    card.innerHTML = `
      <div class="hour__head">
        <span class="hour__time">${fmtTime(t)}</span>
        ${showDay ? `<span class="hour__day">${day}</span>` : ""}
      </div>
      <div class="hour__main">
        <span class="hour__temp" style="color:${temp ? tempColor(temp.value) : "inherit"}">${temp ? round(temp.value) + "°" : "—"}</span>
        <span class="hour__ic">${weatherSvg(wk.sky, 22)}</span>
      </div>
      <div class="hour__range">${rng}</div>
      <div class="hour__row">${probVal > 0 ? `💧 ${round(probVal, 0)}%` : '<span class="hour__dry">' + tr("dry") + '</span>'}</div>
      <div class="hour__row">${wind ? `<span class="hour__windarrow" style="transform:rotate(${wd ? (wd.deg + 180) % 360 : 0}deg)">➤</span> ${kmh(wind)}${gust ? `${" · " + tr("raf") + " "}${Math.round(gust.value * 3.6)}` : ""}${wd ? ` <span class="hour__from">${wd.from}</span>` : ""}` : ""}</div>
      <div class="hour__rainbar"><span style="width:${Math.min(probVal, 100)}%"></span></div>
      ${confBadge(avgConf, cal)}`;
    wrap.appendChild(card);
  }
}

function renderConfidence(fc) {
  const wrap = el("vars");
  wrap.innerHTML = "";
  const overall = [];
  for (const v of VAR_ORDER) {
    const items = fc.filter((i) => i.variable === v && i.calibrated);
    if (!items.length) continue;
    const avg = items.reduce((a, i) => a + i.confidence, 0) / items.length;
    const sureShare = items.filter((i) => i.confidence >= 0.98).length / items.length;
    const pct = Math.round(avg * 100);
    overall.push(pct);
    const color = confColor(avg);
    const div = document.createElement("div");
    div.className = "var";
    div.innerHTML = `
      <div class="var__head">
        <span class="var__label">${varLabel(v)}</span>
        <span class="var__value" style="color:${color}">${pct}% · ${Math.round(sureShare * 100)}% sûres</span>
      </div>
      <div class="var__track">
        <div class="var__bar" style="width:${pct}%; background:${color}"></div>
      </div>`;
    wrap.appendChild(div);
  }
  if (overall.length) {
    const m = Math.round(overall.reduce((a, b) => a + b, 0) / overall.length);
    el("conf-overall").textContent = `moyenne ${m}% (calibrée)`;
  }
}

function renderTable(times, byTime) {
  const tb = el("table-body");
  tb.innerHTML = "";
  // Point values in cells; the honest range moves to a hover tooltip so the
  // table stays scannable. The date appears on the first row of each day.
  const tip = (it, d = 1) =>
    it && it.low !== it.high ? ` title="fourchette ${round(it.low, d)}–${round(it.high, d)}"` : "";
  const val = (it, d = 1, suf = "") => (it ? `${round(it.value, d)}${suf}` : "—");
  let lastDay = null;
  for (const t of times) {
    const row = byTime[t] || {};
    const day = fmtDay(t);
    const showDay = day !== lastDay;
    lastDay = day;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtTime(t)}${showDay ? `<div class="tr__day">${day}</div>` : ""}</td>
      <td${tip(row.temperature_2m)}>${row.temperature_2m ? round(row.temperature_2m.value) + "°" : "—"}</td>
      <td${tip(row.dew_point_2m)}>${val(row.dew_point_2m)}°</td>
      <td${tip(row.precipitation_probability, 0)}>${val(row.precipitation_probability, 0, "%")}</td>
      <td${tip(row.wind_speed_10m, 0)}>${row.wind_speed_10m ? Math.round(row.wind_speed_10m.value * 3.6) + " km/h" : "—"}</td>
      <td${tip(row.wind_gusts_10m, 0)}>${row.wind_gusts_10m ? Math.round(row.wind_gusts_10m.value * 3.6) + " km/h" : "—"}</td>
      <td${tip(row.relative_humidity_2m, 0)}>${val(row.relative_humidity_2m, 0, "%")}</td>
      <td${tip(row.cloud_cover, 0)}>${val(row.cloud_cover, 0, "%")}</td>
      <td${tip(row.pressure_msl, 0)}>${val(row.pressure_msl, 0, " hPa")}</td>
      <td>${row.visibility ? (row.visibility.value >= 10000 ? "≥10 km" : round(row.visibility.value / 1000, 1) + " km") : "—"}</td>`;
    tb.appendChild(tr);
  }
}

/* ---- Events ---- */

$("#search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#search-input").value.trim();
  if (!q) return;
  // Feedback BEFORE the weather request: geocoding (especially the
  // Nominatim address fallback) can take several silent seconds.
  show(el("loading"));
  const status = document.querySelector("#load-status");
  if (status) status.textContent = tr("loading_geocode");
  try {
    const loc = await geocode(q);
    state.lat = loc.lat;
    state.lon = loc.lon;
    state.name = loc.name;
    await loadForecast();
  } catch (err) {
    hide(el("loading"));
    showError(err.message);
  }
});

$("#locate-btn").addEventListener("click", async () => {
  try {
    const loc = await locateMe();
    state.lat = loc.lat;
    state.lon = loc.lon;
    state.name = loc.name;
    await loadForecast();
  } catch (err) {
    hide(el("loading"));
    showError(err.message);
  }
});

$("#fav-btn").addEventListener("click", toggleFavorite);

$("#fav-open").addEventListener("click", () => {
  const menu = el("fav-list");
  menu.hidden = !menu.hidden;
});

$("#range-seg").addEventListener("click", (e) => {
  const btn = e.target.closest(".seg__btn");
  if (!btn) return;
  document.querySelectorAll(".seg__btn").forEach((b) => b.classList.remove("seg__btn--active"));
  btn.classList.add("seg__btn--active");
  state.hours = parseInt(btn.dataset.hours, 10);
  syncUrl();
  // Instant: the full 7 days are already in memory, just slice them.
  if (currentData) render(currentData, state.hours);
});

/* ---- Map & radar ---- */

let map = null;
let radarLayer = null;
let modelLayer = null;
let marker = null;
let radarFrames = [];
let radarPlaying = false;
let radarTimer = null;
let radarIdx = 0;
let mapLayer = "radar";
// One TileLayer instance per frame, created lazily on first display and kept:
// Leaflet caches a layer's tiles, so scrubbing back to an already-seen frame
// is instant instead of refetching every tile (and flickering) like when a
// fresh layer was built for every frame change.
const radarLayerCache = {};
let currentRadarIdx = -1;
/* Wind arrows overlay. */
let windLayer = null;
let windTimer = null;

function initMap() {
  if (map || !window.L) return;
  map = L.map("map", { zoomControl: true }).setView([state.lat, state.lon], 9);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OSM</a>',
  }).addTo(map);
  marker = L.circleMarker([state.lat, state.lon], {
    radius: 8,
    color: "#fff",
    weight: 2,
    fillColor: "#2b6df6",
    fillOpacity: 0.9,
  }).addTo(map);
  marker.bindPopup(`<b>${state.name}</b>`);
  map.on("click", onMapClick);
  map.on("moveend", () => {
    clearTimeout(windTimer);
    windTimer = setTimeout(refreshWindArrows, 600);
  });
}

async function loadRadar() {
  const status = el("radar-status");
  try {
    const r = await fetch("/radar");
    if (!r.ok) throw new Error(`radar ${r.status}`);
    const d = await r.json();
    const all = [...(d.radar?.past || []), ...(d.radar?.nowcast || [])];
    if (!all.length) {
      status.textContent = tr("radar_nodata");
      return;
    }
    radarFrames = all.map((f) => ({
      host: d.host,
      path: f.path,
      time: f.time,
      isNowcast: (f.path || "").includes("nowcast"),
    }));
    // Future frames: the free radar feed stops at the last observation, so
    // the model takes over — one synthesized frame per hour ahead, rendered
    // from our own precipitation tiles (coarser ~10 km, labeled "prévision").
    const lastT = radarFrames[radarFrames.length - 1]?.time ?? Math.floor(Date.now() / 1000);
    for (let h = 1; h <= 6; h++) {
      radarFrames.push({
        model: true,
        hourOffset: h,
        time: lastT + h * 3600,
        isNowcast: true,
      });
    }
    // Fresh frame index: cached layers point at stale tile paths.
    for (const k of Object.keys(radarLayerCache)) {
      const lyr = radarLayerCache[k];
      if (lyr) map.removeLayer(lyr);
      delete radarLayerCache[k];
    }
    currentRadarIdx = -1;
    // Bind the timeline scrubber once frames are known.
    const slider = el("radar-slider");
    if (slider) {
      slider.max = radarFrames.length - 1;
      slider.min = 0;
      slider.step = 1;
      slider.value = radarFrames.length - 1;
    }
    if (!radarPlaying && mapLayer === "radar") showRadarFrame(radarFrames.length - 1);
  } catch (e) {
    status.textContent = tr("radar_err");
  }
}

function showRadarFrame(idx) {
  if (!map || !radarFrames.length) return;
  radarIdx = ((idx % radarFrames.length) + radarFrames.length) % radarFrames.length;
  const f = radarFrames[radarIdx];
  // maxNativeZoom=7 upscales the native z7 tile at any deeper zoom, and
  // noWrap stops Leaflet from tiling the same radar frame repeatedly across
  // the map (the "same thing at different places" bug).
  if (radarLayerCache[currentRadarIdx] && currentRadarIdx !== radarIdx) {
    map.removeLayer(radarLayerCache[currentRadarIdx]);
  }
  let layer = radarLayerCache[radarIdx];
  if (!layer) {
    if (f.model) {
      // Model forecast frame: our own precip tiles at +h hours.
      layer = L.tileLayer(`/tile/precip/{z}/{x}/{y}.png?h=${f.hourOffset}`, {
        opacity: 0.7,
        maxNativeZoom: 9,
        noWrap: true,
        className: "radar-model",
      });
    } else {
      const FrameLayer = L.TileLayer.extend({
        getTileUrl(coords) {
          const z = Math.max(5, Math.min(coords.z, 7));
          const scale = Math.pow(2, coords.z - z);
          return `${f.host}${f.path}/256/${z}/${Math.floor(coords.x / scale)}/${Math.floor(coords.y / scale)}/2/1_1_0.png`;
        },
      });
      layer = new FrameLayer({ opacity: 0.75, maxNativeZoom: 7, noWrap: true });
    }
    radarLayerCache[radarIdx] = layer;
  }
  radarLayer = layer;
  currentRadarIdx = radarIdx;
  layer.addTo(map);
  const t = new Date(f.time * 1000);
  const hh = f.model ? `+${f.hourOffset} h` : t.toLocaleTimeString(LANG === "en" ? "en-GB" : "fr-FR");
  const label = `${t.toLocaleDateString(LANG === "en" ? "en-GB" : "fr-FR", { day: "numeric", month: "short" })} ${hh}${f.isNowcast ? " " + tr("radar_prev") : ""}`;
  el("radar-status").textContent = `${tr("radar_label")} ${label}`;
  const slider = el("radar-slider");
  if (slider) slider.value = radarIdx;
  const tinfo = el("radar-time");
  if (tinfo) tinfo.textContent = hh;
}

function toggleRadarPlay() {
  if (!radarFrames.length) return;
  if (radarPlaying) {
    radarPlaying = false;
    clearInterval(radarTimer);
    el("radar-play").textContent = tr("play");
  } else {
    radarPlaying = true;
    el("radar-play").textContent = tr("pause");
    if (radarIdx >= radarFrames.length - 1) radarIdx = 0; // start from the oldest
    showRadarFrame(radarIdx);
    radarTimer = setInterval(() => {
      radarIdx = (radarIdx + 1) % radarFrames.length;
      showRadarFrame(radarIdx);
    }, 500);
  }
}

/* Clicking anywhere on the map loads the forecast for that precise point:
   a station street corner, a valley, the coast — wherever you point. */
async function onMapClick(e) {
  const lat = +e.latlng.lat.toFixed(4);
  const lon = +e.latlng.lng.toFixed(4);
  state.lat = lat;
  state.lon = lon;
  state.name = `${lat.toFixed(4)}, ${lon.toFixed(4)}`;
  await loadForecast();
}

function centerMapOn(lat, lon, name) {
  if (!map) return;
  map.setView([lat, lon], 9);
  if (marker) {
    marker.setLatLng([lat, lon]);
    marker.setPopupContent(`<b>${name}</b>`);
  }
}

/* Switch the map overlay: live radar, Open-Meteo model tiles (temperature /
   precipitation), or plain streets (no overlay). Model tiles are rendered
   server-side from the model API and cached, so panning is cheap. */
function applyMapLayer() {
  if (radarLayer) {
    map.removeLayer(radarLayer);
    radarLayer = null;
    currentRadarIdx = -1;
  }
  if (modelLayer) {
    map.removeLayer(modelLayer);
    modelLayer = null;
  }
  const isRadar = mapLayer === "radar";
  // The radar timeline + play button only make sense on the live radar layer.
  el("radar-play").hidden = !isRadar;
  el("radar-slider").parentElement.hidden = !isRadar;
  el("radar-status").textContent = "";
  if (isRadar) {
    if (radarFrames.length) showRadarFrame(radarFrames.length - 1);
    return;
  }
  if (mapLayer === "streets") {
    el("radar-status").textContent = "plan (rues)";
    return;
  }
  const label = mapLayer === "temp" ? "température (modèle)" : "précipitation (modèle)";
  el("radar-status").textContent = label;
  modelLayer = L.tileLayer(`/tile/${mapLayer}/{z}/{x}/{y}.png`, {
    opacity: 0.85,
    maxNativeZoom: 9, // model grid ~11 km: beyond z9 the backend upscales
    maxZoom: 18,
  }).addTo(map);
}

$("#map-layer").addEventListener("change", (e) => {
  mapLayer = e.target.value;
  applyMapLayer();
});

/* ---- Wind arrows overlay ---- */
function windColor(kmh) {
  if (kmh < 20) return "#22c55e";
  if (kmh < 45) return "#eab308";
  if (kmh < 70) return "#f97316";
  return "#ef4444";
}

async function refreshWindArrows() {
  if (!map || !el("wind-toggle").checked) return;
  const b = map.getBounds();
  try {
    const r = await fetch(`/wind-grid?lat_n=${b.getNorth().toFixed(3)}&lon_w=${b.getWest().toFixed(3)}&lat_s=${b.getSouth().toFixed(3)}&lon_e=${b.getEast().toFixed(3)}&n=6`);
    if (!r.ok) return;
    const { points } = await r.json();
    if (windLayer) map.removeLayer(windLayer);
    windLayer = L.layerGroup();
    for (const p of points) {
      const color = windColor(p.kmh);
      const icon = L.divIcon({
        className: "",
        html: `<div class="windarrow" style="transform:rotate(${(p.deg + 180) % 360}deg);color:${color}">➤</div>`,
        iconSize: [22, 22],
        iconAnchor: [11, 11],
      });
      const m = L.marker([p.lat, p.lon], { icon, interactive: false, keyboard: false });
      m.bindTooltip(`${p.kmh} km/h`, { permanent: false, direction: "top", offset: [0, -8] });
      windLayer.addLayer(m);
    }
    windLayer.addTo(map);
  } catch { /* wind arrows are decorative: never break the map */ }
}

el("wind-toggle").addEventListener("change", (e) => {
  if (e.target.checked) {
    refreshWindArrows();
  } else if (windLayer) {
    map.removeLayer(windLayer);
    windLayer = null;
  }
});

$("#radar-play").addEventListener("click", toggleRadarPlay);

$("#radar-slider").addEventListener("input", (e) => {
  // Dragging the timeline scrubs to an exact radar frame: pause playback so
  // the animation doesn't fight the user's hand.
  radarPlaying = false;
  clearInterval(radarTimer);
  const btn = el("radar-play");
  if (btn) btn.textContent = "▶ Lecture";
  showRadarFrame(parseInt(e.target.value, 10));
});

/* Boot */
loadForecast();
renderFavorites();
// The map and radar initialize inside render(), once `#content` is visible:
// Leaflet needs a non-zero container to fetch tiles (no gray map).
// Home-screen shortcut "?geo=1": ask for geolocation once on boot.
if (new URLSearchParams(location.search).get("geo") === "1") {
  locateMe()
    .then((loc) => {
      state.lat = loc.lat;
      state.lon = loc.lon;
      state.name = loc.name;
      loadForecast();
    })
    .catch(() => {});
}