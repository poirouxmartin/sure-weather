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
  wind_direction_10m: "var_wind",
  uv_index: "var_uv",
  pressure_msl: "var_press",
  visibility: "var_vis",
};

function varLabel(v) {
  return tr(VAR_LABELS_KEYS[v] || v);
}

const VAR_ORDER = [
  "temperature_2m",
  "dew_point_2m",
  "relative_humidity_2m",
  "precipitation",
  "precipitation_probability",
  "cloud_cover",
  "wind_speed_10m",
  "wind_gusts_10m",
  "wind_direction_10m",
  "uv_index",
  "pressure_msl",
  "visibility",
];

let state = { lat: 48.8566, lon: 2.3522, hours: 24, name: "Paris", graph: "temperature_2m" };
let reqToken = 0;

/* ---- Deep links ----
   ?lat=&lon=&name= pre-selects a place (shared links, home-screen
   shortcuts, widgets); ?geo=1 asks for geolocation on boot. The URL is
   kept in sync so "copy link" always reproduces the current view. */
const LAST_KEY = "sure-weather-last";

(function initStateFromUrl() {
  const p = new URLSearchParams(location.search);
  const la = parseFloat(p.get("lat"));
  const lo = parseFloat(p.get("lon"));
  // Range + graph tab persist across shares/reloads (validated, else default).
  const h = parseInt(p.get("hours") || "", 10);
  if ([1, 3, 8, 24, 72, 168].includes(h)) state.hours = h;
  const g = p.get("graph");
  if (["temperature_2m", "precipitation", "wind_speed_10m", "pressure_msl", "relative_humidity_2m", "uv_index"].includes(g)) state.graph = g;
  if (Number.isFinite(la) && Number.isFinite(lo)) {
    state.lat = la;
    state.lon = lo;
    state.name = (p.get("name") || `${la.toFixed(4)}, ${lo.toFixed(4)}`).slice(0, 80);
    try { localStorage.setItem(LAST_KEY, JSON.stringify(state)); } catch {}
    return;
  }
  // No URL coords: restore last viewed place, otherwise keep Paris as fallback.
  try {
    const raw = localStorage.getItem(LAST_KEY);
    if (raw) {
      const last = JSON.parse(raw);
      if (Number.isFinite(last.lat) && Number.isFinite(last.lon)) {
        state.lat = last.lat; state.lon = last.lon; state.name = (last.name || state.name).slice(0, 80);
        if ([1, 3, 8, 24, 72, 168].includes(last.hours)) state.hours = last.hours;
        if (typeof last.graph === "string") state.graph = last.graph;
        return;
      }
    }
  } catch {}
  // First visit with no history: try geolocation quietly (non-blocking).
  if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition((pos) => {
      // Only auto-apply if the user never picked a place this session (still at default Paris and no URL).
      if (state.name === "Paris" && !location.search) {
        state.lat = pos.coords.latitude; state.lon = pos.coords.longitude; state.name = tr("my_position");
        try { localStorage.setItem(LAST_KEY, JSON.stringify(state)); } catch {}
        syncUrl(); loadForecast();
      }
    }, () => {}, { timeout: 8000, maximumAge: 600000 });
  }
})();

function saveLast() {
  try { localStorage.setItem(LAST_KEY, JSON.stringify(state)); } catch {}
}
function syncUrl() {
  const q = `?lat=${state.lat}&lon=${state.lon}&name=${encodeURIComponent(state.name)}&hours=${state.hours}&graph=${state.graph}`;
  history.replaceState(null, "", q);
  saveLast();
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
    // Name it now so the place stays reusable after you leave: the prompt
    // defaults to the current display name (city, address, or coords).
    let name = state.name;
    try {
      const answer = prompt(tr("fav_name"), state.name);
      if (answer === null) return; // cancelled: don't add
      name = (answer.trim() || state.name).slice(0, 60);
    } catch { /* prompt blocked: keep display name */ }
    list.unshift({ lat: state.lat, lon: state.lon, name });
  }
  saveFavorites(list);
  renderFavorites();
}

function renameFavorite(i) {
  const list = loadFavorites();
  if (!list[i]) return;
  try {
    const answer = prompt(tr("fav_rename"), list[i].name || "");
    if (answer === null) return;
    const name = answer.trim().slice(0, 60);
    if (!name) return;
    list[i].name = name;
    saveFavorites(list);
    renderFavorites();
  } catch { /* prompt blocked */ }
}

function deleteFavorite(i) {
  const list = loadFavorites();
  list.splice(i, 1);
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
  list.forEach((f, i) => {
    const li = document.createElement("li");
    li.className = "fav__row";
    const a = document.createElement("button");
    a.type = "button";
    a.className = "fav__item";
    a.textContent = f.name;
    a.title = `${Number(f.lat).toFixed(3)}, ${Number(f.lon).toFixed(3)}`;
    a.addEventListener("click", async () => {
      state.lat = f.lat;
      state.lon = f.lon;
      state.name = f.name;
      closeFavorites();
      await loadForecast();
    });
    const rn = document.createElement("button");
    rn.type = "button";
    rn.className = "fav__mini";
    rn.textContent = "✎";
    rn.title = tr("fav_rename");
    rn.setAttribute("aria-label", tr("fav_rename"));
    rn.addEventListener("click", (e) => { e.stopPropagation(); renameFavorite(i); });
    const del = document.createElement("button");
    del.type = "button";
    del.className = "fav__mini";
    del.textContent = "×";
    del.title = tr("fav_remove");
    del.setAttribute("aria-label", tr("fav_remove"));
    del.addEventListener("click", (e) => { e.stopPropagation(); deleteFavorite(i); });
    li.append(a, rn, del);
    wrap.appendChild(li);
  });
}

function closeFavorites() {
  const menu = el("fav-list");
  if (menu) menu.hidden = true;
}

/* Escape user-controlled strings injected into HTML popups/tables. */
function escHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function popupHtml(name, lat, lon) {
  const div = document.createElement("div");
  const b = document.createElement("b");
  b.textContent = name;
  const small = document.createElement("small");
  small.textContent = `${Number(lat).toFixed(4)}, ${Number(lon).toFixed(4)}`;
  div.appendChild(b);
  div.appendChild(document.createElement("br"));
  div.appendChild(small);
  return div;
}

const $ = (s) => document.querySelector(s);
const el = (id) => document.getElementById(id);
const hide = (n) => (n.hidden = true);
const show = (n) => (n.hidden = false);

function fmtTime(iso) {
  // Note: browser-local timezone. Consulting a far-away place shows YOUR
  // local hours, not the place's — the suffix in the table header says so.
  return new Date(iso).toLocaleTimeString(LANG === "en" ? "en-GB" : "fr-FR", { hour: "2-digit", minute: "2-digit" });
}
function fmtDay(iso) {
  return new Date(iso).toLocaleDateString(LANG === "en" ? "en-GB" : "fr-FR", { weekday: "short", day: "numeric", month: "short" });
}
function round(v, d = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toFixed(d);
}

function interpolateTo15Min(times, byTime, hours) {
  if (!times.length) return { times, byTime };
  // Build a dense 15-min grid covering the requested window, interpolating
  // between the two nearest hourly points — so 1h = 5 points, 3h = 13, 8h = 33.
  // Missing values are SKIPPED (never coerced to 0: a null temp must not
  // render a fake 0° dip). Beyond the last hourly point: clamp to it.
  const start = new Date(times[0]).getTime();
  const end = start + hours * 3600000;
  const sorted = times.slice().sort((a, b) => new Date(a).getTime() - new Date(b).getTime());
  const outTimes = [];
  const outByTime = {};
  for (let t = start; t <= end; t += 900000) {
    const iso = new Date(t).toISOString();
    // find bracket
    let lo = sorted[0], hi = sorted[sorted.length - 1];
    for (let i = 0; i < sorted.length - 1; i++) {
      const a = new Date(sorted[i]).getTime(), b = new Date(sorted[i + 1]).getTime();
      if (t >= a && t <= b) { lo = sorted[i]; hi = sorted[i + 1]; break; }
      if (t < a) { hi = lo; break; }
    }
    if (t <= new Date(lo).getTime()) {
      outTimes.push(iso);
      outByTime[iso] = { ...(byTime[lo] || {}), valid_at: iso, time: iso };
      continue;
    }
    if (t >= new Date(hi).getTime()) {
      outTimes.push(iso);
      outByTime[iso] = { ...(byTime[hi] || {}), valid_at: iso, time: iso };
      continue;
    }
    const t0 = new Date(lo).getTime(), t1 = new Date(hi).getTime();
    const frac = (t - t0) / Math.max(1, t1 - t0);
    const row0 = byTime[lo] || {}, row1 = byTime[hi] || {};
    const row = {};
    const vars = new Set([...Object.keys(row0), ...Object.keys(row1)]);
    for (const v of vars) {
      const a = row0[v], b = row1[v];
      if (!a && !b) continue;
      if (!a) { row[v] = { ...b, valid_at: iso }; continue; }
      if (!b) { row[v] = { ...a, valid_at: iso }; continue; }
      if (v === "wind_direction_10m") {
        if (a.value == null || b.value == null) continue;
        const av = a.value, bv = b.value;
        let diff = ((bv - av + 540) % 360) - 180;
        row[v] = { ...a, value: (av + diff * frac + 360) % 360, valid_at: iso };
      } else {
        if (a.value == null || b.value == null) continue;
        const va = a.value, vb = b.value;
        const la = a.low ?? va, ha = a.high ?? va;
        const lb = b.low ?? vb, hb = b.high ?? vb;
        row[v] = { ...a, value: va + (vb - va) * frac, low: la + (lb - la) * frac, high: ha + (hb - ha) * frac, valid_at: iso };
      }
    }
    row.time = iso;
    outTimes.push(iso);
    outByTime[iso] = row;
  }
  return { times: outTimes, byTime: outByTime };
}

/* Day/night from REAL sunrise/sunset (Open-Meteo daily), not a fixed
   6h-21h guess that breaks by ±3h depending on season. Falls back to the
   coarse guess until the sun data lands. */
let sunData = null;



function isNight(iso) {
  const t = new Date(iso).getTime();
  const d = sunData?.daily;
  if (d?.sunrise?.length && d?.sunset?.length) {
    // Before today's sunrise: night, except when still after YESTERDAY's
    // sunset window — compare against the previous day's sunset.
    let prevSs = null;
    for (let i = 0; i < d.sunrise.length; i++) {
      const sr = new Date(d.sunrise[i]).getTime();
      const ss = new Date(d.sunset[i]).getTime();
      if (t < sr) return prevSs === null ? true : t >= prevSs;
      if (t < ss) return false;
      prevSs = ss;
    }
    return true;
  }
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
const CARDINALS_FR = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO"];
const CARDINALS_EN = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
const FLOW_ARROWS = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"];
function cardinals() { return LANG === "en" ? CARDINALS_EN : CARDINALS_FR; }

function windDir(row) {
  const d = row.wind_direction_10m?.value;
  if (d === null || d === undefined) return null;
  const deg = ((d % 360) + 360) % 360;
  return {
    deg,
    from: cardinals()[Math.round(deg / 22.5) % 16],
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
  if (c >= 0.98) return `<span class="hour__conf hour__conf--hi">${LANG === "en" ? "sure 98%+" : "sûr 98%+"}</span>`;
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
  if (c >= 0.97) return "#34a877";
  if (c >= 0.93) return "#5b9be0";
  if (c >= 0.88) return "#e0a63c";
  return "#e07856";
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
  // Standalone (no backend proxy): query Nominatim directly (CORS-open).
  const gd = backendDown
    ? await window.SureLocal.geocodeNominatim(query)
    : await fetch(`/geocode?q=${encodeURIComponent(query)}&limit=1`).then((g) => {
      if (!g.ok) throw new Error(`adresse introuvable`);
      return g.json();
    });
  if (!gd.results || !gd.results.length) throw new Error(tr("err_notfound"));
  const hit = gd.results[0];
  return { lat: hit.lat, lon: hit.lon, name: hit.name };
}

function locateMe() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error(tr("geoloc_err")));
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude, name: tr("my_position") }),
      (err) => reject(new Error(tr("geoloc_err"))),
      { timeout: 10000, maximumAge: 600000 }
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
const THEME_SVGS = {
  dark: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8"/></svg>',
  light: '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M20.7 15.1a8.5 8.5 0 0 1-11.8-11.8 8.5 8.5 0 1 0 11.8 11.8z"/></svg>',
};

function applyThemeButton() {
  const dark = document.documentElement.dataset.theme === "dark";
  themeBtn.innerHTML = dark ? THEME_SVGS.dark : THEME_SVGS.light;
  themeBtn.title = dark ? tr("theme_light") : tr("theme_dark");
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = dark ? "#0c1424" : "#1b2a4a";
}
themeBtn.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("sure-weather-theme", next);
  applyThemeButton();
});
applyThemeButton();

/* ---- PWA install prompt (Android) ---- */
let deferredPrompt = null;
const installBtn = document.getElementById("install-btn");
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredPrompt = e;
  if (installBtn) installBtn.hidden = false;
});
if (installBtn) {
  installBtn.addEventListener("click", async () => {
    if (!deferredPrompt) return;
    deferredPrompt.prompt();
    try { await deferredPrompt.userChoice; } catch {}
    deferredPrompt = null;
    installBtn.hidden = true;
  });
}
window.addEventListener("appinstalled", () => {
  deferredPrompt = null;
  if (installBtn) installBtn.hidden = true;
});

/* ---- Push notifications (alertes pluie / orage) ---- */
const notifBtn = document.getElementById("notif-btn");
function urlB64ToUint8Array(s) {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const b64 = (s + pad).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}
async function setupPush() {
  if (!("Notification" in window) || !("serviceWorker" in navigator) || !("PushManager" in window) || !notifBtn) return;
  if (Notification.permission === "denied") return;
  const reg = await navigator.serviceWorker.ready;
  let sub = null;
  try {
    sub = await reg.pushManager.getSubscription();
  } catch { sub = null; }
  if (sub) {
    notifBtn.textContent = "🔔✓";
    notifBtn.title = "Alertes activées";
    return;
  }
  // granted-but-no-subscription (cleaned/lost) must still offer resubscribe.
  if (Notification.permission === "default" || Notification.permission === "granted") notifBtn.hidden = false;
}
if (notifBtn) {
  notifBtn.addEventListener("click", async () => {
    try {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") return;
      const reg = await navigator.serviceWorker.ready;
      const { publicKey } = await fetch("/push/key").then((r) => r.json());
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64ToUint8Array(publicKey) });
      await fetch("/push/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint: sub.endpoint, keys: { p256dh: btoa(String.fromCharCode(...new Uint8Array(sub.getKey("p256dh")))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""), auth: btoa(String.fromCharCode(...new Uint8Array(sub.getKey("auth")))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "") }, lat: state.lat, lon: state.lon }),
      });
      notifBtn.textContent = "🔔✓";
      notifBtn.title = "Alertes activées";
    } catch (e) {
      notifBtn.hidden = false;
      notifBtn.title = `Échec d'abonnement : ${e?.message || e}`;
    }
  });
  setupPush().catch(() => { if (notifBtn) notifBtn.hidden = false; });
}

/* ---- Load ---- */

/* Progressive, non-blocking load: the place name renders immediately with
   shimmer placeholders, any cached data paints instantly, and the fresh
   forecast (always the full 7 days) replaces the view when it lands. Range
   switches (24h/72h/7j) then slice the cached data client-side: no network,
   no reload. */
const dataCache = new Map(); // "lat,lon" -> full 7-day forecast
const partialTries = new Map(); // "lat,lon" -> upgrade attempts
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
}

/* Standalone mode (APK without the PC server): the on-device provider
   fuses Open-Meteo models directly on the phone. Auto-detected: ?local=1
   forces it, otherwise the first backend failure (or a failed /health
   probe at boot) switches over for the whole session. */
const forceLocal = new URLSearchParams(location.search).get("local") === "1";
let backendDown = forceLocal;

/* App version bundled here — bump on every GitHub release so the in-app
   updater can offer it. Checked against api.github.com (CORS-open). */
const APP_VERSION = "1.2.0";
const APP_REPO = "poirouxmartin/sure-weather";

function cmpVersions(a, b) {
  const pa = String(a).replace(/^v/, "").split(".").map((x) => parseInt(x, 10) || 0);
  const pb = String(b).replace(/^v/, "").split(".").map((x) => parseInt(x, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) > (pb[i] || 0) ? 1 : -1;
  }
  return 0;
}

async function checkAppUpdate() {
  try {
    const last = parseInt(localStorage.getItem("sure-weather-update-check") || "0", 10);
    if (Date.now() - last < 24 * 3600e3) return; // once a day max
    localStorage.setItem("sure-weather-update-check", String(Date.now()));
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch(`https://api.github.com/repos/${APP_REPO}/releases/latest`, { signal: ctrl.signal });
    clearTimeout(t);
    if (!r.ok) return;
    const rel = await r.json();
    const tag = (rel.tag_name || "").replace(/^v/, "");
    if (!tag || cmpVersions(tag, APP_VERSION) <= 0) return;
    const apk = (rel.assets || []).find((a) => /\.apk$/i.test(a.name || ""));
    const banner = document.getElementById("update-banner");
    if (!banner) return;
    banner.href = (apk && apk.browser_download_url) || rel.html_url || `https://github.com/${APP_REPO}/releases`;
    banner.target = "_blank";
    banner.rel = "noopener";
    banner.hidden = false;
    banner.innerHTML = `<span aria-hidden="true">⬆️</span> ${escHtml(tr("update_available", { v: rel.tag_name }))} — ${escHtml(tr("update_download"))}`;
  } catch { /* update check is best-effort */ }
}

/* True when the backend is really absent (not just slow): network errors,
   aborts, non-JSON bodies (Capacitor's SPA fallback serves index.html with
   HTTP 200 for unknown paths — a bare r.ok check would false-positive). */
function isBackendDead(e) {
  if (!e) return false;
  if (e instanceof TypeError) return true; // network failure
  if (e.name === "AbortError") return false; // slow backend, not dead
  const msg = String(e.message || "");
  return /Failed to fetch|NetworkError|Load failed|Unexpected token|is not valid JSON|JSON/i.test(msg);
}

async function probeBackend() {
  if (forceLocal) return;
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 4000);
    const r = await fetch("/health", { signal: ctrl.signal });
    clearTimeout(t);
    // Must be real JSON {"status":"ok"} — HTML with status 200 (SPA
    // fallback, captive portal, proxy login) is NOT a backend.
    const ct = (r.headers.get("content-type") || "").toLowerCase();
    if (!r.ok || !ct.includes("application/json")) {
      backendDown = true;
    } else {
      const body = await r.json().catch(() => null);
      backendDown = !body || body.status !== "ok";
    }
  } catch {
    backendDown = true;
  }
  if (backendDown) initLocalModeUI();
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
  // No backend (APK standalone / server down): fuse on-device.
  if (backendDown) {
    await loadForecastLocal(token, key, cached);
    return;
  }
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
  // A stalled/overloaded upstream must never dead-end: the first visit of a
  // zone backfills months of data server-side, and every completed request
  // is PERSISTED — so an automatic retry resumes where it stopped and
  // usually completes. Only a second consecutive timeout surfaces the error.
  for (let attempt = 1; attempt <= 2; attempt++) {
    const ctrl = new AbortController();
    const abortTimer = setTimeout(() => ctrl.abort(), attempt === 1 ? 45000 : 60000);
    try {
      syncUrl();
      if (attempt === 2 && status) status.textContent = tr("loading_retry");
      const r = await fetch(`/weather?lat=${state.lat}&lon=${state.lon}&hours=168`, { signal: ctrl.signal });
      clearTimeout(abortTimer);
      clearInterval(tick);
      if (token !== reqToken) return;
      if (!r.ok) throw new Error(`API ${r.status}`);
      const ct = (r.headers.get("content-type") || "").toLowerCase();
      if (!ct.includes("application/json")) {
        // HTML with HTTP 200 (SPA fallback / captive portal): no backend.
        throw new TypeError("backend returned HTML instead of JSON");
      }
      const data = await r.json();
      if (!data.forecast || !data.forecast.length) {
        hide(el("loading"));
        hide(el("content"));
        show(el("empty"));
        return;
      }
      dataCache.set(key, data);
      if (dataCache.size > 24) dataCache.delete(dataCache.keys().next().value);
      hide(el("loading"));
      render(data, state.hours);
      // Partial response (background backfill still running): upgrade soon.
      if (data.partial) {
        const tries = (partialTries.get(key) || 0) + 1;
        partialTries.set(key, tries);
        if (tries <= 3) {
          setTimeout(() => {
            if (token === reqToken) loadForecast();
          }, 20000);
        }
      } else {
        partialTries.delete(key);
      }
      return;
    } catch (e) {
      clearTimeout(abortTimer);
      clearInterval(tick);
      if (token !== reqToken) return;
      const timedOut = e.name === "AbortError";
      if (timedOut && attempt === 1) continue; // transparent retry
      // Backend unreachable or impersonated (SPA fallback HTML, proxy
      // login): switch to on-device fusion instead of dead-ending.
      if (!timedOut && isBackendDead(e)) {
        backendDown = true;
        initLocalModeUI();
        await loadForecastLocal(token, key, cached);
        return;
      }
      hide(el("loading"));
      if (!cached) hide(el("content"));
      showError(timedOut ? tr("err_slow") : e.message);
      return;
    }
  }
}

/* On-device forecast: same render path, data fused on the phone. */
async function loadForecastLocal(token, key, cached) {
  const spinner = el("loading");
  const status = spinner.querySelector("#load-status");
  try {
    syncUrl();
    if (status) status.textContent = tr("loading_cross");
    const data = await window.SureLocal.forecast(state.lat, state.lon, 168);
    if (token !== reqToken) return;
    if (!data.forecast || !data.forecast.length) {
      hide(el("loading"));
      if (!cached) {
        hide(el("content"));
        show(el("empty"));
      }
      return;
    }
    dataCache.set(key, data);
    if (dataCache.size > 24) dataCache.delete(dataCache.keys().next().value);
    partialTries.delete(key);
    hide(el("loading"));
    render(data, state.hours);
  } catch (e) {
    if (token !== reqToken) return;
    hide(el("loading"));
    if (!cached) hide(el("content"));
    showError(e.name === "AbortError" ? tr("err_slow") : e.message);
  }
}

/* Standalone UI: model layers render on-device (canvas overlay sampled
   from Open-Meteo over the viewport), so every layer stays available. */
function initLocalModeUI() {
  const sel = document.getElementById("map-layer");
  if (sel) {
    for (const opt of sel.options) opt.disabled = false;
  }
  if (mapLayer && map) applyMapLayer();
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
  let byTime = {};
  for (const item of fc) (byTime[item.valid_at] = byTime[item.valid_at] || {})[item.variable] = item;
  let times = Object.keys(byTime).sort();
  // 1h/3h/8h : interpolate hourly data to 15 min for a truly detailed view
  if (hours <= 8) {
    const interp = interpolateTo15Min(times, byTime, hours);
    byTime = interp.byTime;
    times = interp.times;
  }

  el("loc-name").textContent = state.name;
  // Android widget: publish the current place to native prefs (best-effort,
  // Capacitor only — the widget reads them for its own refresh).
  try {
    window.Capacitor?.Plugins?.Preferences?.set({
      key: "sw_widget_lat", value: String(state.lat),
    }).catch(() => {});
    window.Capacitor?.Plugins?.Preferences?.set({
      key: "sw_widget_lon", value: String(state.lon),
    }).catch(() => {});
    window.Capacitor?.Plugins?.Preferences?.set({
      key: "sw_widget_name", value: state.name.slice(0, 60),
    }).catch(() => {});
  } catch { /* no native bridge (browser/PWA) */ }
  // When the fusion ran: honest staleness indicator next to the place name.
  try {
    el("computed-at").textContent = data.generated_at
      ? tr("computed_at", { t: new Date(data.generated_at).toLocaleTimeString(LANG === "en" ? "en-GB" : "fr-FR", { hour: "2-digit", minute: "2-digit" }) })
      : "";
  } catch { el("computed-at").textContent = ""; }
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
  if (data.sun) sunData = data.sun; // real solar times, server-provided
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
  if (nowRow.wind_speed_10m) {
    const wd = windDir(nowRow);
    sub.push(`<span class="hour__windarrow" style="transform:rotate(${wd ? (wd.deg + 180) % 360 : 0}deg)">➤</span> ${kmh(nowRow.wind_speed_10m)}${wd ? ` (${wd.from})` : ""}`);
  }
  if (nowRow.relative_humidity_2m) sub.push(`💧 ${rangeText(nowRow.relative_humidity_2m, 0)}%`);
  if (nowRow.precipitation_probability && nowRow.precipitation_probability.value > 0)
    sub.push(`☔ ${round(nowRow.precipitation_probability.value, 0)}%`);
  el("now-sub").innerHTML = sub.join(" · ");

  /* Timeline */
  window.__partialData = !!currentData?.partial;
  renderTimeline(times, byTime);
  renderSunCard(byTime, data.sun);
  renderUvCard(byTime);

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
  renderSources(data);
}

function fmtSunHM(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleTimeString(LANG === "en" ? "en-GB" : "fr-FR", { hour: "2-digit", minute: "2-digit" });
}
function minutesBetween(a, b) {
  if (!a || !b) return 0;
  return Math.max(0, Math.round((new Date(b) - new Date(a)) / 60000));
}
/* Pick the sunrise/sunset pair containing "now" (not blindly [0], which is
   yesterday once the day rolls over). After the last sunset, show that pair
   with the dot parked at 100%. */
function pickSunPair(daily, nowMs) {
  const sr = daily?.sunrise || [], ss = daily?.sunset || [];
  const n = Math.max(sr.length, ss.length);
  for (let i = 0; i < n; i++) {
    const e = ss[i] ? new Date(ss[i]).getTime() : NaN;
    if (Number.isFinite(e) && nowMs <= e) return { sunrise: sr[i], sunset: ss[i] };
  }
  const l = n - 1;
  return l >= 0 ? { sunrise: sr[l], sunset: ss[l] } : {};
}
function renderSunCard(byTime, sun) {
  const elSun = document.getElementById("sun-card");
  if (!elSun) return;
  const now = Date.now();
  let { sunrise, sunset } = pickSunPair(sun?.daily, now);
  // fallback: compute from sunData global if payload shape differs
  if (!sunrise && sunData?.daily) ({ sunrise, sunset } = pickSunPair(sunData.daily, now));
  const mins = minutesBetween(sunrise, sunset);
  const h = Math.floor(mins / 60), m = mins % 60;
  const dur = mins ? `${h}h${String(m).padStart(2, "0")}` : "—";
  const sr = sunrise ? new Date(sunrise).getTime() : null;
  const ss = sunset ? new Date(sunset).getTime() : null;
  let pct = 0;
  if (sr && ss && now >= sr && now <= ss) pct = ((now - sr) / (ss - sr)) * 100;
  else if (ss && now > ss) pct = 100;
  pct = Math.max(0, Math.min(100, pct));
  const arc = `M 10 80 A 70 70 0 0 1 150 80`;
  const dotX = 10 + (140 * pct) / 100;
  // rough Y on arc: y = 80 - 70*sin(pi*pct/100)
  const dotY = 80 - 70 * Math.sin((Math.PI * pct) / 100);
  elSun.innerHTML = `
    <div class="sunuv__head"><span class="sunuv__title">${tr("sun_title")}</span><span class="sunuv__dur">${dur} ${tr("sun_daylight")}</span></div>
    <svg viewBox="0 0 160 90" class="sunarc" role="img" aria-label="${escHtml(tr("sun_title"))}"><path d="${arc}" fill="none" stroke="var(--line-strong)" stroke-width="3" stroke-linecap="round"/><circle cx="${dotX.toFixed(1)}" cy="${dotY.toFixed(1)}" r="6" fill="#f59e0b" stroke="#fff" stroke-width="2"/></svg>
    <div class="sunuv__row"><span>🌅 ${fmtSunHM(sunrise)}</span><span>🌇 ${fmtSunHM(sunset)}</span></div>`;
}
function renderUvCard(byTime) {
  const elUv = document.getElementById("uv-card");
  if (!elUv) return;
  const vals = Object.values(byTime).map(r => r.uv_index?.value).filter(v => v != null);
  const cur = vals[0] ?? byTime[Object.keys(byTime)[0]]?.uv_index?.value ?? 0;
  const mx = Math.max(...vals, 0);
  const level = cur <= 2 ? tr("uv_low") : cur <= 5 ? tr("uv_mod") : cur <= 7 ? tr("uv_high") : cur <= 10 ? tr("uv_vhigh") : tr("uv_extreme");
  const color = cur <= 2 ? "#22c55e" : cur <= 5 ? "#eab308" : cur <= 7 ? "#f97316" : cur <= 10 ? "#ef4444" : "#a855f7";
  const pct = Math.max(0, Math.min(100, (cur / 11) * 100));
  // Compact single row: badge + gauge. Peak of the day in the title.
  elUv.innerHTML = `
    <div class="sunuv__head"><span class="sunuv__title">${tr("uv_title")}</span><span class="sunuv__badge" style="background:${color}">${cur?.toFixed(1) ?? "—"} · ${level}</span></div>
    <div class="sunuv__gauge" title="${escHtml(tr("uv_hint"))} · max ${mx.toFixed(1)}"><div class="sunuv__track"><div class="sunuv__fill" style="width:${pct}%;background:${color}"></div></div></div>`;
}
/* Air & pressure card removed (humidity/pressure live in the graph tabs,
   the hourly detail and the table): kept as a no-op so older cached HTML
   calling it never throws. */
function renderAirCard(byTime) {
  const elAir = document.getElementById("air-card");
  if (elAir) elAir.hidden = true;
}

/* Local station badge: a number, the words live in the tooltip. */
function renderStationBadge(fc) {
  const badge = el("station-badge");
  // Union over the whole window (same as the footer): the first item alone
  // undercounts when its hour lacks station reports.
  const stations = new Set(
    fc.flatMap((i) => i.contributors ?? []).filter((c) => c.startsWith("metar_"))
  );
  if (!stations.size) {
    badge.hidden = true;
    return;
  }
  badge.hidden = false;
  badge.className = "station-badge";
  badge.innerHTML = `<span aria-hidden="true">📍</span> ${stations.size}`;
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
  badge.innerHTML = `<span class="sure-badge__ic" aria-hidden="true">${icon}</span>${avg}%`;
  badge.title = tr("sure_tip", { s: share, a: avg, h });
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
    pills.push(`<span class="vpill vpill--ok"><span class="vpill__ic" aria-hidden="true">☀️</span>${tr("v_dry")}</span>`);
  } else {
    const first = rainHours[0];
    const peak = Math.max(...rainHours.map((h) => h.row.precipitation_probability?.value ?? 0));
    pills.push(
      `<span class="vpill vpill--rain"><span class="vpill__ic" aria-hidden="true">☔</span>${tr(first.t === times[0] ? "v_rain_now" : "v_rain", { h: fmtTime(first.t) })}${peak >= 70 ? ` · ${Math.round(peak)}%` : ""}</span>`
    );
  }
  // Precise rain timing (15-min model consensus, standalone payload).
  try {
    const rs = currentData?.rain?.start ? new Date(currentData.rain.start).getTime() : null;
    const re = currentData?.rain?.end ? new Date(currentData.rain.end).getTime() : null;
    const nowMs = Date.now();
    if (rs && rs - nowMs > 5 * 60e3 && rs - nowMs < 12 * 3600e3) {
      const mins = Math.round((rs - nowMs) / 60e3);
      const when = mins < 60 ? tr("v_rain_in_min", { m: mins }) : tr("v_rain_in_h", { h: fmtTime(new Date(rs).toISOString()) });
      const until = re && re > rs ? ` → ${fmtTime(new Date(re).toISOString())}` : "";
      pills.push(`<span class="vpill vpill--rain"><span class="vpill__ic" aria-hidden="true">🌧️</span>${when}${until}</span>`);
    }
  } catch { /* bonus pill */ }
  // Temperature: max & min with hour of max.
  const temps = rows.map((h) => h.row.temperature_2m?.value).filter((v) => v !== null && v !== undefined);
  if (temps.length) {
    const maxT = Math.max(...temps);
    const minT = Math.min(...temps);
    const at = rows.find((h) => h.row.temperature_2m?.value === maxT)?.t;
    pills.push(
      `<span class="vpill"><span class="vpill__ic" aria-hidden="true">🌡️</span><b style="color:${tempColor(maxT)}">${round(maxT)}°</b> / ${round(minT)}°${at ? ` <span class="vpill__sub">${tr("v_at", { h: fmtTime(at) })}</span>` : ""}</span>`
    );
  }
  // Wind alert when gusts become unpleasant.
  const gusts = rows.map((h) => (h.row.wind_gusts_10m?.value ?? 0) * 3.6);
  const maxGust = Math.max(...gusts, 0);
  if (maxGust >= 55) {
    const at = rows[gusts.indexOf(maxGust)]?.t;
    pills.push(`<span class="vpill vpill--warn"><span class="vpill__ic" aria-hidden="true">💨</span>${tr("v_gust", { v: Math.round(maxGust) })}${at ? ` ${tr("v_at", { h: fmtTime(at) })}` : ""}</span>`);
  }
  // Degraded data: stations only, no model run available right now.
  const hasModel = rows.some((h) =>
    Object.values(h.row).some((i) => i?.contributors?.some((c) => !c.startsWith("metar_")))
  );
  if (!hasModel) {
    pills.push(`<span class="vpill vpill--warn" title="${tr("v_limited_tip")}"><span class="vpill__ic" aria-hidden="true">⚠️</span>${tr("v_limited")}</span>`);
  }
  // Single model / partial backfill: the full fusion is being assembled.
  if (window.__partialData) {
    pills.push(`<span class="vpill" title="${tr("v_partial_tip")}"><span class="vpill__ic" aria-hidden="true">⏳</span>${tr("v_partial")}</span>`);
  }
  // Heat alert.
  if (temps.length && Math.max(...temps) >= 32) {
    pills.push(`<span class="vpill vpill--warn"><span class="vpill__ic" aria-hidden="true">🥵</span>${tr("v_heat")}</span>`);
  }
  if (rows.length && rows.some((h) => h.row.temperature_2m?.value !== undefined && h.row.temperature_2m.value <= 0)) {
    pills.push(`<span class="vpill vpill--warn"><span class="vpill__ic" aria-hidden="true">❄️</span>${tr("v_frost")}</span>`);
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

function renderTimeline(times, byTime, graph = state.graph) {
  lastMeteo = { times, byTime, graph };
  const chart = el("timeline-chart");
  renderVerdict(times, byTime);

  const W = Math.max(chart.clientWidth || 600, 280);
  const H = 300;
  const padL = 38, padR = 10, padT = 56, padB = 26;
  const rainH = graph === "precipitation" ? 0 : 42; // rain has its own curve, no bar zone
  const n = times.length;
  const colW = (W - padL - padR) / Math.max(n, 1);
  const innerW = W - padL - padR;
  const innerH = H - padT - padB - rainH;

  const rows = times.map((t) => byTime[t] || {});
  // Select variable for this tab
  const varMap = {
    temperature_2m: { key: "temperature_2m", unit: "°", step: null },
    precipitation: { key: "precipitation", unit: " mm", step: null },
    wind_speed_10m: { key: "wind_speed_10m", unit: " km/h", mult: 3.6, step: null },
    pressure_msl: { key: "pressure_msl", unit: " hPa", step: null },
    relative_humidity_2m: { key: "relative_humidity_2m", unit: "%", step: null },
    uv_index: { key: "uv_index", unit: "", step: null },
  };
  const cfg = varMap[graph] || varMap.temperature_2m;
  const vals = rows.map((r) => {
    const v = r[cfg.key]?.value;
    return v != null ? (cfg.mult ? v * cfg.mult : v) : null;
  }).filter((v) => v !== null);
  let vMin, vMax, vSpan;
  if (!vals.length) { vMin = 0; vMax = 1; vSpan = 1; }
  else if (vals.length === 1) { vMin = vals[0] - 1; vMax = vals[0] + 1; vSpan = 2; }
  else {
    const rawMin = Math.min(...vals), rawMax = Math.max(...vals);
    const rawSpan = rawMax - rawMin;
    let pad = Math.max(rawSpan * 0.18, 1.0);
    if (graph === "precipitation") pad = Math.max(rawSpan * 0.3, 1.0);
    if (graph === "uv_index") pad = 1.0;
    vMin = rawMin - pad / 2;
    vMax = rawMax + pad / 2;
    vSpan = vMax - vMin;
    // Clamp to physical bounds where applicable
    if (graph === "precipitation" || graph === "uv_index" || graph === "relative_humidity_2m") vMin = Math.max(0, vMin);
    if (graph === "relative_humidity_2m") vMax = Math.min(100, vMax);
    if (graph === "uv_index") { vMin = 0; vMax = Math.max(5, vMax); }
    vSpan = vMax - vMin;
    if (vSpan < 1.2) { const mid = (vMin + vMax) / 2; vMin = mid - 0.6; vMax = mid + 0.6; vSpan = 1.2; }
    if (graph === "precipitation" && vMax < 2) { vMax = 2; vSpan = vMax - vMin; }
  }
  const xAt = (i) => padL + colW * (i + 0.5);
  const yBase = H - padB;

  const yFor = (v) => padT + (1 - (v - vMin) / vSpan) * innerH;
  const parts = [];
  // Y grid + labels (adaptive, per variable)
  {
    let step, fmt;
    if (graph === "precipitation") { step = vSpan <= 2 ? 0.5 : vSpan <= 6 ? 1 : 2; fmt = (x) => `${x % 1 === 0 ? x.toFixed(0) : x.toFixed(1)} mm`; }
    else if (graph === "wind_speed_10m") { step = vSpan <= 6 ? 2 : vSpan <= 15 ? 5 : 10; fmt = (x) => `${Math.round(x)} km/h`; }
    else if (graph === "pressure_msl") { step = vSpan <= 6 ? 2 : 5; fmt = (x) => `${Math.round(x)}`; }
    else if (graph === "uv_index") { step = 2; fmt = (x) => x.toFixed(0); }
    else if (graph === "relative_humidity_2m") { step = vSpan <= 20 ? 10 : 20; fmt = (x) => `${Math.round(x)}%`; }
    else { step = vSpan <= 3 ? 0.5 : vSpan <= 6 ? 1 : vSpan <= 12 ? 2 : 5; fmt = (x) => `${x % 1 === 0 ? x.toFixed(0) : x.toFixed(1)}°`; }
    const start = Math.ceil(vMin / step) * step;
    // Integer tick walk avoids 0.1+0.2-style float drift on 0.5 steps.
    const nTicks = Math.max(0, Math.floor((vMax - start) / step + 1e-9));
    for (let k = 0; k <= nTicks; k++) {
      const v = start + k * step;
      const y = yFor(v);
      if (y < padT - 4 || y > H - padB + 4) continue;
      parts.push(`<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" stroke="var(--line)" stroke-width="1" opacity=".45"/>`);
      parts.push(`<text x="${(padL - 6).toFixed(1)}" y="${(y + 3.5).toFixed(1)}" text-anchor="end" class="mg-axis">${fmt(v)}</text>`);
    }
  }
  // Night bands + hour labels + rain bars + curve points.
  const labelStep = Math.max(1, Math.ceil(38 / Math.max(colW, 1)));
  const iconStep = Math.max(1, Math.ceil(34 / Math.max(colW, 1)));
  const pts = [];
  rows.forEach((row, i) => {
    const x0 = padL + i * colW;
    if (isNight(times[i])) {
      parts.push(`<rect x="${x0}" y="${padT - 14}" width="${colW}" height="${H - padB - padT + 14}" style="fill:var(--mg-night)"/>`);
    }
    // Rain bars only on the precipitation tab (otherwise the main curve is the rain itself)
    if (graph !== "precipitation") {
      const prob = row.precipitation_probability?.value ?? 0;
      const mm = row.precipitation?.value ?? 0;
      if (prob >= 5 || mm > 0.05) {
        const bh = Math.max((Math.max(prob, Math.min(mm * 20, 100)) / 100) * rainH, 3);
        parts.push(
          `<rect x="${(x0 + colW * 0.18).toFixed(1)}" y="${(yBase - bh).toFixed(1)}" width="${(colW * 0.64).toFixed(1)}" height="${bh.toFixed(1)}" rx="2" fill="#3b82f6" opacity="${(0.25 + Math.min(prob, 100) / 100 * 0.55).toFixed(2)}"><title>pluie ${Math.round(prob)}%</title></rect>`
        );
      }
    }
    let v = row[cfg.key]?.value;
    if (v != null && cfg.mult) v *= cfg.mult;
    if (v !== null && v !== undefined) {
      pts.push([xAt(i), yFor(v), v]);
    }
    if (i % labelStep === 0) {
      parts.push(`<text x="${xAt(i).toFixed(1)}" y="${H - 7}" text-anchor="middle" class="mg-hour">${fmtTime(times[i])}</text>`);
    }
    if (i % iconStep === 0) {
      const wk = weatherKey({ ...row, time: times[i] });
      parts.push(`<svg x="${(xAt(i) - 12).toFixed(1)}" y="${padT - 36}" width="30" height="30" viewBox="0 0 48 48">${WEATHER_ART[wk.sky] || WEATHER_ART.cloud}</svg>`);
    }
  });

  // Curve + gradient area (color per variable).
  if (pts.length > 1) {
    const palette = {
      temperature_2m: (v) => tempColor(v),
      precipitation: () => "#3b82f6",
      wind_speed_10m: () => "#0ea5e9",
      pressure_msl: () => "#10b981",
      relative_humidity_2m: () => "#6366f1",
      uv_index: (v) => v <= 2 ? "#22c55e" : v <= 5 ? "#eab308" : v <= 7 ? "#f97316" : v <= 10 ? "#ef4444" : "#a855f7",
    };
    const solid = {
      temperature_2m: "#2f6bff", precipitation: "#3b82f6", wind_speed_10m: "#0ea5e9",
      pressure_msl: "#10b981", relative_humidity_2m: "#6366f1", uv_index: "#f59e0b",
    };
    const cFn = palette[graph] || palette.temperature_2m;
    const base = solid[graph] || "#2f6bff";
    const suf = graph === "precipitation" ? " mm" : graph === "wind_speed_10m" ? " km/h" : graph === "pressure_msl" ? " hPa" : graph === "relative_humidity_2m" ? "%" : graph === "uv_index" ? "" : "°";
    const line = catmullRomPath(pts.map((p) => [p[0], p[1]]));
    const area = line + ` L ${pts[pts.length - 1][0]},${padT} L ${pts[0][0]},${padT} Z`;
    if (graph === "temperature_2m") {
      const stops = pts.map((p) => `<stop offset="${((p[0] - padL) / innerW * 100).toFixed(1)}%" stop-color="${cFn(p[2])}"/>`).join("");
      parts.push(`<defs><linearGradient id="mg-line" x1="0" y1="0" x2="1" y2="0">${stops}</linearGradient><linearGradient id="mg-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${base}" stop-opacity=".16"/><stop offset="1" stop-color="${base}" stop-opacity="0"/></linearGradient></defs>`);
      parts.push(`<path d="${area}" fill="url(#mg-area)"/>`);
      parts.push(`<path d="${line}" fill="none" stroke="url(#mg-line)" stroke-width="2.5" stroke-linecap="round"/>`);
    } else {
      parts.push(`<defs><linearGradient id="mg-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${base}" stop-opacity=".18"/><stop offset="1" stop-color="${base}" stop-opacity="0"/></linearGradient></defs>`);
      parts.push(`<path d="${area}" fill="url(#mg-area)"/>`);
      parts.push(`<path d="${line}" fill="none" stroke="${base}" stroke-width="2.5" stroke-linecap="round"/>`);
    }
    const tStep = Math.max(1, Math.ceil(64 / Math.max(colW, 1)));
    // Value labels live BELOW the icon lane (padT-24): near the top of the
    // plot they flip under the point instead of colliding with icons.
    const labelY = (py, below) => (below || py - 7 < padT - 24 ? py + 16 : py - 7);
    // Wind tab: flow arrow beside each labeled point (direction the wind
    // GOES TO = reported direction + 180).
    const windDirs = graph === "wind_speed_10m"
      ? rows.map((r) => r.wind_direction_10m?.value)
      : null;
    pts.forEach((p, i) => {
      const col = graph === "temperature_2m" ? cFn(p[2]) : base;
      if (i % tStep === 0) {
        parts.push(`<text x="${p[0].toFixed(1)}" y="${labelY(p[1]).toFixed(1)}" text-anchor="middle" class="mg-temp" fill="${col}">${round(p[2])}${suf}</text>`);
        if (windDirs && windDirs[i] != null) {
          const a = ((windDirs[i] + 180) % 360).toFixed(0);
          parts.push(`<text x="${p[0].toFixed(1)}" y="${(p[1] + 16).toFixed(1)}" text-anchor="middle" class="mg-windarrow" transform="rotate(${a} ${p[0].toFixed(1)} ${(p[1] + 16).toFixed(1)})">➤</text>`);
        }
      }
      parts.push(`<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2.2" fill="${col}" style="stroke:var(--dot-stroke)"/>`);
    });
    // Global min & max always labeled (max above, min below the curve) so a
    // 7-day view shows every peak, not just sampled points.
    if (pts.length > 2) {
      let iMin = 0, iMax = 0;
      pts.forEach((p, i) => {
        if (p[2] < pts[iMin][2]) iMin = i;
        if (p[2] > pts[iMax][2]) iMax = i;
      });
      const labeled = new Set();
      pts.forEach((p, i) => { if (i % tStep === 0) labeled.add(i); });
      const nearLabeled = (j) => { for (const l of labeled) if (Math.abs(l - j) <= 1) return true; return false; };
      const col = (i) => (graph === "temperature_2m" ? cFn(pts[i][2]) : base);
      if (!nearLabeled(iMax)) {
        parts.push(`<text x="${pts[iMax][0].toFixed(1)}" y="${labelY(yFor(pts[iMax][2])).toFixed(1)}" text-anchor="middle" class="mg-temp" fill="${col(iMax)}">▲ ${round(pts[iMax][2])}${suf}</text>`);
      }
      if (iMin !== iMax && !nearLabeled(iMin)) {
        parts.push(`<text x="${pts[iMin][0].toFixed(1)}" y="${(yFor(pts[iMin][2]) + 16).toFixed(1)}" text-anchor="middle" class="mg-temp" fill="${col(iMin)}">▼ ${round(pts[iMin][2])}${suf}</text>`);
      }
    }
  }

  // "Now" dashed marker on the first column (text label removed: it covered
  // the curve values).
  parts.push(
    `<line x1="${xAt(0).toFixed(1)}" y1="${padT - 8}" x2="${xAt(0).toFixed(1)}" y2="${yBase}" stroke="#2f6bff" stroke-width="1.5" stroke-dasharray="3 3" opacity=".55"/>`
  );

  chart.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="${escHtml(tr("mg_title"))}">${parts.join("")}</svg>`;
}

window.addEventListener("resize", (() => {
  let t = null;
  return () => {
    clearTimeout(t);
    t = setTimeout(() => {
      if (lastMeteo && el("timeline-chart")) renderTimeline(lastMeteo.times, lastMeteo.byTime, lastMeteo.graph || state.graph);
    }, 150);
  };
})());

function renderHours(times, byTime) {
  const wrap = el("hours");
  wrap.innerHTML = "";
  const count = el("hours-count");
  if (count) count.textContent = `${state.hours} h`;
  let lastDay = null;
  for (const t of times) {
    const row = byTime[t] || {};
    const wk = weatherKey({ ...row, time: t });
    const temp = row.temperature_2m;
    const prob = row.precipitation_probability;
    const wind = row.wind_speed_10m;
    const gust = row.wind_gusts_10m;
    const press = row.pressure_msl;
    const uv = row.uv_index;
    // Confidence of this hour: calibrated values when present, otherwise
    // the inter-model agreement (local mode) — never blank.
    const confsAll = VAR_ORDER.filter((v) => row[v] && row[v].confidence != null).map((v) => row[v].confidence);
    const avgConf = confsAll.length ? confsAll.reduce((a, b) => a + b, 0) / confsAll.length : null;
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
      ${press ? `<div class="hour__row">🔵 ${Math.round(press.value)} hPa</div>` : ""}
      ${uv && uv.value != null ? `<div class="hour__row">☀️ UV ${round(uv.value, 0)}</div>` : ""}
      <div class="hour__rainbar"><span style="width:${Math.min(probVal, 100)}%"></span></div>
      ${avgConf != null ? `<div class="hour__row">🎯 ${Math.round(avgConf * 100)}%</div>` : ""}
      ${confBadge(avgConf, cal)}`;
    wrap.appendChild(card);
  }
}

function renderConfidence(fc) {
  const wrap = el("vars");
  wrap.innerHTML = "";
  const overall = [];
  let anyCalibrated = false;
  for (const v of VAR_ORDER) {
    // Standalone (on-device fusion) never calibrates: still show the
    // inter-model agreement bars, suffixed "non cal." instead of hiding all.
    const items = fc.filter((i) => i.variable === v && i.confidence != null);
    if (!items.length) continue;
    const cal = items.filter((i) => i.calibrated);
    const use = cal.length ? cal : items;
    if (cal.length) anyCalibrated = true;
    const avg = use.reduce((a, i) => a + i.confidence, 0) / use.length;
    const sureShare = use.filter((i) => i.sure).length / use.length;
    const pct = Math.round(avg * 100);
    overall.push(pct);
    const color = confColor(avg);
    const div = document.createElement("div");
    div.className = "var";
    div.innerHTML = `
      <div class="var__head">
        <span class="var__label">${varLabel(v)}</span>
        <span class="var__value" style="color:${color}">${cal.length ? tr("conf_sure_share", { p: pct, s: Math.round(sureShare * 100) }) : `${pct}% · ${tr("conf_na")}`}</span>
      </div>
      <div class="var__track">
        <div class="var__bar" style="width:${pct}%; background:${color}"></div>
      </div>`;
    wrap.appendChild(div);
  }
  if (overall.length) {
    const m = Math.round(overall.reduce((a, b) => a + b, 0) / overall.length);
    el("conf-overall").textContent = tr(anyCalibrated ? "conf_avg" : "conf_avg_na", { m });
  }
}

function renderSources(data) {
  const tb = document.getElementById("sources-body");
  const badge = document.getElementById("sources-badge");
  if (!tb) return;
  tb.innerHTML = "";
  const bd = data.breakdown || [];
  if (badge) badge.textContent = bd.length ? `${bd.length} sources · ${fmtTime(data.breakdown_valid_at)}` : "";
  if (!bd.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="6" style="text-align:center;color:var(--ink-faint)">—</td>`;
    tb.appendChild(tr);
    return;
  }
  for (const r of bd) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escHtml(r.provider)}</td><td>${escHtml(r.raw)}°</td><td>${r.bias > 0 ? "+" : ""}${escHtml(r.bias)}°</td><td>${escHtml(r.corr)}°</td><td>${escHtml(r.weight)}</td><td>${escHtml(r.share)}%</td>`;
    tb.appendChild(tr);
  }
  // Consensus row (named `crow`: a local `tr` would shadow the i18n tr()).
  // It MUST be the consensus AT the breakdown hour: raw values above come
  // from that hour, comparing them to another hour's value is nonsense.
  const atHour = data.breakdown_valid_at;
  const first =
    (atHour && data.forecast.find((i) => i.variable === "temperature_2m" && i.valid_at === atHour)) ||
    data.forecast.find((i) => i.variable === "temperature_2m");
  if (first) {
    const crow = document.createElement("tr");
    crow.style.fontWeight = "700";
    crow.style.background = "var(--accent-soft)";
    crow.innerHTML = `<td>${escHtml(tr("consensus_row"))}</td><td></td><td></td><td>${escHtml(first.value)}°</td><td></td><td>100%</td>`;
    tb.appendChild(crow);
  }
}

function renderTable(times, byTime) {
  const tb = el("table-body");
  tb.innerHTML = "";
  // Point value + small honest range underneath (touch-visible, unlike the
  // title tooltip which is hover-only). The date appears once per day.
  const tip = (it, d = 1) =>
    it && it.low !== it.high ? ` title="fourchette ${round(it.low, d)}–${round(it.high, d)}"` : "";
  const val = (it, d = 1, suf = "") => (it ? `${round(it.value, d)}${suf}` : "—");
  const cell = (it, d = 1, suf = "") => {
    if (!it) return "—";
    const base = `${round(it.value, d)}${suf}`;
    if (it.low === undefined || it.high === undefined || it.low === it.high) return base;
    if (it.calibrated && it.confidence >= 0.98) return base;
    return `${base}<div class="tr__day">${round(it.low, d)}–${round(it.high, d)}${suf}</div>`;
  };
  let lastDay = null;
  for (const t of times) {
    const row = byTime[t] || {};
    const day = fmtDay(t);
    const showDay = day !== lastDay;
    lastDay = day;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtTime(t)}${showDay ? `<div class="tr__day">${day}</div>` : ""}</td>
      <td${tip(row.temperature_2m)}>${cell(row.temperature_2m, 0, "°")}</td>
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
    document.getElementById("search-input").value = "";
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

/* Manual refresh: drop the memoized response for this cell and refetch
   (backend reruns the fusion; standalone refetches the models). */
document.getElementById("refresh-btn")?.addEventListener("click", () => {
  dataCache.delete(cacheKey());
  partialTries.delete(cacheKey());
  loadForecast();
});

/* Deep analysis (standalone): match this place's recorded forecasts against
   14 days of analysis truth, learn per-model biases, show the report, then
   refetch so the learned corrections apply immediately. */
document.getElementById("deep-btn")?.addEventListener("click", async () => {
  const btn = document.getElementById("deep-btn");
  const status = document.getElementById("deep-status");
  if (!window.SureLearn) return;
  btn.disabled = true;
  try {
    status.textContent = tr("deep_running", { n: "" });
    const rep = await window.SureLearn.deepAnalyze(state.lat, state.lon, (done, total) => {
      status.textContent = tr("deep_running", { n: `${done}/${total}` });
    });
    renderLearnReport(rep);
    const calibrated = rep.rows.filter((r) => r.n >= window.SureLearn.MIN_N).length;
    let msg = tr("deep_done", { m: rep.matched, k: calibrated });
    if (rep.matched === 0) msg += " " + tr("deep_fresh");
    if (!rep.rows.length) msg += " — " + tr("deep_empty");
    status.textContent = msg;
    dataCache.delete(cacheKey());
    await loadForecast();
  } catch (e) {
    status.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
});

function renderLearnReport(rep) {
  const wrap = document.getElementById("learn-wrap");
  const tb = document.getElementById("learn-body");
  if (!wrap || !tb) return;
  tb.innerHTML = "";
  if (!rep.rows.length) { wrap.hidden = true; return; }
  wrap.hidden = false;
  for (const r of rep.rows) {
    const trEl = document.createElement("tr");
    const bias = r.tempBias == null ? "—" : `${r.tempBias > 0 ? "+" : ""}${r.tempBias}°`;
    trEl.innerHTML = `<td>${escHtml(r.model)}</td><td>${r.n}</td><td>${bias}</td><td>${r.tempRmse == null ? "—" : r.tempRmse + "°"}</td><td>${r.weight}</td>`;
    tb.appendChild(trEl);
  }
}

$("#fav-open").addEventListener("click", () => {
  const menu = el("fav-list");
  const btn = el("fav-open");
  menu.hidden = !menu.hidden;
  btn?.setAttribute("aria-expanded", String(!menu.hidden));
});
document.addEventListener("click", (e) => {
  const menu = el("fav-list");
  if (menu && !menu.hidden && !e.target.closest(".fav")) {
    menu.hidden = true;
    el("fav-open")?.setAttribute("aria-expanded", "false");
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const menu = el("fav-list");
    if (menu && !menu.hidden) {
      menu.hidden = true;
      el("fav-open")?.setAttribute("aria-expanded", "false");
    }
  }
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

document.getElementById("graph-tabs")?.addEventListener("click", (e) => {
  const btn = e.target.closest(".graph-tab");
  if (!btn) return;
  document.querySelectorAll(".graph-tab").forEach((b) => b.classList.remove("graph-tab--active"));
  btn.classList.add("graph-tab--active");
  state.graph = btn.dataset.graph;
  syncUrl();
  if (lastMeteo) renderTimeline(lastMeteo.times, lastMeteo.byTime, state.graph);
});

/* ---- Map & radar ---- */

let map = null;
let radarLayer = null;
let modelLayer = null;
let marker = null;
let radarFrames = [];
let radarPlaying = false;
let radarTimer = null;
let radarRefreshTimer = null;
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
  if (map) return;
  if (!window.L) {
    // Offline/CDN-blocked: say so instead of a silent gray box.
    el("radar-status").textContent = tr("radar_err");
    return;
  }
  map = L.map("map", { zoomControl: true }).setView([state.lat, state.lon], 9);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OSM</a>',
  }).addTo(map);
  marker = L.marker([state.lat, state.lon], {
    draggable: true,
    icon: L.divIcon({
      className: "precise-marker",
      html: `<div style="width:14px;height:14px;border-radius:50%;background:#2b6df6;border:2px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,.35)"></div>`,
      iconSize: [14, 14],
      iconAnchor: [7, 7],
    }),
  }).addTo(map);
  marker.bindPopup(popupHtml(state.name, state.lat, state.lon));
  marker.on("dragend", () => {
    const p = marker.getLatLng();
    state.lat = +p.lat.toFixed(4); state.lon = +p.lng.toFixed(4);
    state.name = `${state.lat.toFixed(4)}, ${state.lon.toFixed(4)}`;
    marker.setPopupContent(popupHtml(state.name, state.lat, state.lon));
    loadForecast();
  });
  map.on("click", onMapClick);
  map.on("moveend", () => {
    clearTimeout(windTimer);
    windTimer = setTimeout(refreshWindArrows, 600);
    scheduleLocalModelRefresh(900);
  });
}

async function loadRadar() {
  const status = el("radar-status");
  const token = reqToken; // stale guard: a fast place switch must not let the old radar win
  try {
    // Standalone: RainViewer directly (CORS-open), no backend proxy.
    const d = backendDown
      ? await window.SureLocal.radar()
      : await fetch("/radar").then(async (r) => {
        if (!r.ok) throw new Error(`radar ${r.status}`);
        return r.json();
      });
    if (token !== reqToken) return;
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
    // Future frames (backend only): the free radar feed stops at the last
    // observation, so the model takes over — one synthesized frame per hour
    // ahead, rendered from our own precipitation tiles. Standalone has no
    // tile renderer: live radar only.
    if (!backendDown) {
      const lastT = radarFrames[radarFrames.length - 1]?.time ?? Math.floor(Date.now() / 1000);
      for (let h = 1; h <= 6; h++) {
        radarFrames.push({
          model: true,
          hourOffset: h,
          time: lastT + h * 3600,
          isNowcast: true,
        });
      }
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
      slider.disabled = false;
    }
    if (!radarPlaying && mapLayer === "radar") showRadarFrame(radarFrames.length - 1);
    // Warm the model tiles (server + browser cache) so the first playback
    // through the +1h..+6h forecast doesn't stall on blank tiles.
    // Standalone: no tile renderer, nothing to warm.
    if (map && mapLayer === "radar" && !backendDown) {
      const z = map.getZoom(), c = map.getCenter();
      const n = 1 << z;
      const tx = Math.floor(((c.lng + 180) / 360) * n);
      const ty = Math.floor(((1 - Math.log(Math.tan((c.lat * Math.PI) / 180)) / Math.PI) / 2) * n);
      for (let h = 1; h <= 6; h++) {
        const img = new Image();
        img.src = `/tile/precip/${z}/${tx}/${ty}.png?h=${h}`;
      }
    }
    // Radar frames age (~10 min): refresh periodically while the tab lives.
    clearTimeout(radarRefreshTimer);
    radarRefreshTimer = setTimeout(() => {
      if (mapLayer === "radar" && !radarPlaying) loadRadar();
    }, 10 * 60 * 1000);
  } catch (e) {
    status.textContent = tr("radar_err");
    // Transient upstream failures retry on their own.
    setTimeout(() => {
      if (map && !radarPlaying) loadRadar();
    }, 30 * 1000);
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
   a station street corner, a valley, the coast — wherever you point.
   The previous toponym is kept in the marker tooltip history (title) so a
   tap never silently destroys the place name; drag stays the precise tool. */
async function onMapClick(e) {
  const lat = +e.latlng.lat.toFixed(4);
  const lon = +e.latlng.lng.toFixed(4);
  const prev = state.name;
  state.lat = lat;
  state.lon = lon;
  state.name = `${lat.toFixed(4)}, ${lon.toFixed(4)}`;
  if (marker) marker.options.prevName = prev;
  await loadForecast();
}

function centerMapOn(lat, lon, name) {
  if (!map) return;
  map.setView([lat, lon], 9);
  if (marker) {
    marker.setLatLng([lat, lon]);
    marker.setPopupContent(popupHtml(name, lat, lon));
  }
}

/* Switch the map overlay: live radar, Open-Meteo model tiles (temperature /
   precipitation), or plain streets (no overlay). Model tiles are rendered
   server-side from the model API and cached, so panning is cheap.
   Standalone (no backend): the same layers render on-device from a viewport
   grid (see refreshLocalModelLayer). */
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
    const lg2 = document.getElementById("map-legend");
    if (lg2) lg2.hidden = true;
    if (radarFrames.length) showRadarFrame(radarFrames.length - 1);
    return;
  }
  if (mapLayer === "streets") {
    el("radar-status").textContent = tr("map_streets");
    const lg = document.getElementById("map-legend");
    if (lg) lg.hidden = true;
    return;
  }
  const layerLabels = {
    temp: `${tr("layer_temp")} (modèle)`,
    precip: `${tr("layer_precip")} (modèle)`,
    uv: `${tr("layer_uv")} (modèle)`,
    humidity: `${tr("layer_humidity")} (modèle)`,
    cloud: `${tr("layer_cloud")} (modèle)`,
    pressure: `${tr("layer_pressure")} (modèle)`,
  };
  const label = layerLabels[mapLayer] || mapLayer;
  el("radar-status").textContent = label;
  // légende détaillée par paramètre (backend comme on-device: mêmes plages)
  const lg = document.getElementById("map-legend");
  if (lg) {
    const legends = {
      temp: '<span class="map-legend__swatch" style="background:linear-gradient(90deg,#4455aa,#f0c040,#d32f2f)"></span> -10°C → 40°C',
      precip: '<span class="map-legend__swatch" style="background:linear-gradient(90deg,rgba(255,255,255,0),#1e40a0)"></span> 0 → 20 mm',
      uv: '<span class="map-legend__swatch" style="background:linear-gradient(90deg,#3cb04a,#eab308,#ef4444,#a855f7)"></span> 0 → 11+',
      humidity: '<span class="map-legend__swatch" style="background:linear-gradient(90deg,#fff,#1e40a0)"></span> 0% → 100%',
      cloud: '<span class="map-legend__swatch" style="background:linear-gradient(90deg,#fff,#404040)"></span> 0% → 100%',
      pressure: '<span class="map-legend__swatch" style="background:linear-gradient(90deg,#7b6fbf,#4fc28a,#e0c35a)"></span> 980 → 1030 hPa',
    };
    lg.innerHTML = legends[mapLayer] || "";
    lg.hidden = !legends[mapLayer];
  }
  // Standalone: same layer, sampled on-device over the viewport.
  if (backendDown) {
    refreshLocalModelLayer();
    return;
  }
  modelLayer = L.tileLayer(`/tile/${mapLayer}/{z}/{x}/{y}.png`, {
    opacity: 0.88,
    maxNativeZoom: 9, // model grid ~11 km: beyond z9 the backend upscales
    maxZoom: 18,
  }).addTo(map);
}

$("#map-layer").addEventListener("change", (e) => {
  mapLayer = e.target.value;
  applyMapLayer();
});

/* On-device model overlay: sample the layer variable on a grid over the
   viewport, color it with the backend's LUTs, and pin it as an imageOverlay
   that pans/zooms with the map. Refreshed (debounced) on every moveend. */
let localGridSeq = 0;
let localGridTimer = null;
function scheduleLocalModelRefresh(ms = 800) {
  clearTimeout(localGridTimer);
  localGridTimer = setTimeout(() => { refreshLocalModelLayer(); }, ms);
}
async function refreshLocalModelLayer() {
  if (!map || backendDown !== true) return;
  if (!window.SureLocal?.modelGrid) return;
  if (!["temp", "precip", "uv", "humidity", "cloud", "pressure"].includes(mapLayer)) return;
  const seq = ++localGridSeq;
  const layer = mapLayer;
  const b = map.getBounds();
  try {
    // 16×16 like the backend's city zoom: fine enough for neighbourhood
    // nuances instead of one big blob.
    const g = await window.SureLocal.modelGrid(
      layer, b.getNorth(), b.getWest(), b.getSouth(), b.getEast(), 16
    );
    if (seq !== localGridSeq || layer !== mapLayer) return;
    const n = g.n;
    const raw = document.createElement("canvas");
    raw.width = n; raw.height = n;
    const rctx = raw.getContext("2d");
    const img = rctx.createImageData(n, n);
    for (let i = 0; i < n * n; i++) {
      const [r, gg, bb, a] = window.SureLocal.lutColor(layer, g.grid[i]);
      img.data[i * 4] = r; img.data[i * 4 + 1] = gg;
      img.data[i * 4 + 2] = bb; img.data[i * 4 + 3] = a;
    }
    rctx.putImageData(img, 0, 0);
    const big = document.createElement("canvas");
    big.width = 256; big.height = 256;
    const bctx = big.getContext("2d");
    bctx.imageSmoothingEnabled = true;
    bctx.drawImage(raw, 0, 0, 256, 256);
    if (seq !== localGridSeq || layer !== mapLayer) return;
    if (modelLayer) map.removeLayer(modelLayer);
    modelLayer = L.imageOverlay(big.toDataURL(), [[b.getSouth(), b.getWest()], [b.getNorth(), b.getEast()]], {
      opacity: 0.62, interactive: false,
    }).addTo(map);
  } catch { /* decorative: never break the map */ }
}

/* ---- Wind arrows overlay ---- */
function windColor(kmh) {
  if (kmh < 20) return "#22c55e";
  if (kmh < 45) return "#eab308";
  if (kmh < 70) return "#f97316";
  return "#ef4444";
}

let windSeq = 0;
async function refreshWindArrows() {
  if (!map || !el("wind-toggle").checked) return;
  const seq = ++windSeq; // last response wins: pans fire overlapping fetches
  const placeToken = reqToken;
  const b = map.getBounds();
  const q = `lat_n=${b.getNorth().toFixed(3)}&lon_w=${b.getWest().toFixed(3)}&lat_s=${b.getSouth().toFixed(3)}&lon_e=${b.getEast().toFixed(3)}&n=6`;
  try {
    // Standalone: Open-Meteo multi-location current wind, direct.
    const { points } = backendDown
      ? await window.SureLocal.windGrid(b.getNorth(), b.getWest(), b.getSouth(), b.getEast(), 6)
      : await fetch(`/wind-grid?${q}`).then(async (r) => {
        if (!r.ok) return { points: [] };
        return r.json();
      });
    if (seq !== windSeq || placeToken !== reqToken) return;
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
(function markRestoredControls() {
  document.querySelectorAll("#range-seg .seg__btn").forEach((b) => {
    b.classList.toggle("seg__btn--active", parseInt(b.dataset.hours, 10) === state.hours);
  });
  document.querySelectorAll("#graph-tabs .graph-tab").forEach((b) => {
    b.classList.toggle("graph-tab--active", b.dataset.graph === state.graph);
  });
})();
// Standalone (?local=1 / APK): skip the backend probe, go on-device now.
if (forceLocal) initLocalModeUI();
else probeBackend().finally(() => { if (backendDown) initLocalModeUI(); });
loadForecast();
renderFavorites();
checkAppUpdate();
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