/* Sure Weather PWA — vanilla JS, no build step. */
"use strict";

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js");
}

const VAR_LABELS = {
  temperature_2m: "Température",
  dew_point_2m: "Point de rosée",
  relative_humidity_2m: "Humidité",
  precipitation: "Pluie",
  precipitation_probability: "Probabilité de pluie",
  cloud_cover: "Nébulosité",
  wind_speed_10m: "Vent",
  wind_gusts_10m: "Rafales",
  pressure_msl: "Pression",
  visibility: "Visibilité",
};

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
  return new Date(iso).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
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

/* Rough weather icon + sky key from available variables. */
function weatherKey(row) {
  const cloud = row.cloud_cover?.value ?? 0;
  const rain = row.precipitation?.value ?? 0;
  const prob = row.precipitation_probability?.value ?? 0;
  if (rain > 5) return { icon: "⛈", sky: "storm" };
  if (prob >= 70 || rain > 0.3) return { icon: "🌧", sky: "rain" };
  if (cloud >= 70) return { icon: "☁️", sky: isNight(row.time) ? "night" : "partly" };
  if (cloud >= 30) return { icon: "⛅", sky: isNight(row.time) ? "night" : "partly" };
  return { icon: isNight(row.time) ? "🌙" : "☀️", sky: isNight(row.time) ? "night" : "day" };
}

function confBadge(c, calibrated) {
  if (!calibrated || c === null || c === undefined) {
    return `<span class="hour__conf hour__conf--na">non cal.</span>`;
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
  if (!gd.results || !gd.results.length) throw new Error("Lieu introuvable");
  const hit = gd.results[0];
  return { lat: hit.lat, lon: hit.lon, name: hit.name };
}

function locateMe() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error("Géolocalisation non supportée"));
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude, name: "Ma position" }),
      (err) => reject(new Error(`Géolocalisation refusée (${err.code})`)),
      { timeout: 10000 }
    );
  });
}

/* ---- Load ---- */

async function loadForecast() {
  const token = ++reqToken;
  show(el("loading"));
  hide(el("error"));
  const spinner = el("loading");
  const tip = spinner.querySelector("span");
  const status = spinner.querySelector("#load-status");
  if (tip) tip.textContent = "Calcul de la confiance…";
  if (status) status.textContent = "";
  // Progress feedback: a brand-new zone needs a 3-month backfill on first
  // visit (model analysis + reanalysis), which legitimately takes tens of
  // seconds. Tell the user it's working instead of leaving a silent spinner.
  const progress = [
    "découverte des sources locales…",
    "récupération de 3 mois d'archives (1ère visite)…",
    "calibration de la confiance…",
    "croisement des modèles et des stations…",
    "encore un instant, la calibration est lourde…",
  ];
  let i = 0;
  const tick = setInterval(() => {
    if (status && i < progress.length) status.textContent = progress[i++];
  }, 6000);
  // A stalled connection must never spin forever: hard timeout, and the
  // error card offers a retry.
  const ctrl = new AbortController();
  const abortTimer = setTimeout(() => ctrl.abort(), 45000);
  try {
    syncUrl();
    const r = await fetch(`/weather?lat=${state.lat}&lon=${state.lon}&hours=${state.hours}`, { signal: ctrl.signal });
    clearTimeout(abortTimer);
    if (!r.ok) throw new Error(`API ${r.status}`);
    const data = await r.json();
    clearInterval(tick);
    if (token !== reqToken) return;
    if (!data.forecast || !data.forecast.length) {
      hide(el("loading"));
      hide(el("content"));
      show(el("empty"));
      return;
    }
    render(data);
  } catch (e) {
    clearTimeout(abortTimer);
    clearInterval(tick);
    if (token !== reqToken) return;
    hide(el("loading"));
    hide(el("content"));
    const msg = e.name === "AbortError"
      ? "Le calcul prend trop longtemps (source météo lente ou injoignable)."
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
  btn.textContent = "Réessayer";
  btn.addEventListener("click", loadForecast);
  box.appendChild(span);
  box.appendChild(btn);
  show(box);
}

/* ---- Render ---- */

function render(data) {
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

  const fc = data.forecast;
  const byTime = {};
  for (const item of fc) (byTime[item.valid_at] = byTime[item.valid_at] || {})[item.variable] = item;
  const times = Object.keys(byTime).sort();

  el("loc-name").textContent = state.name;
  el("loc-meta").textContent = `cellule ${data.cell} · mis à jour à ${new Date(data.generated_at).toLocaleTimeString("fr-FR")}`;
  // Union of contributors across the whole window: the first item alone can
  // undercount (its group may lack station reports).
  const contributors = new Set(fc.flatMap((i) => i.contributors ?? []));
  const stationCount = [...contributors].filter((c) => c.startsWith("metar_")).length;
  const modelCount = [...contributors].filter((c) => !c.startsWith("metar_")).length;
  el("footer-note").textContent =
    `Fusion de ${modelCount} modèles` +
    (stationCount ? ` + ${stationCount} stations locales` : "") +
    ` · correction de biais par cellule · source ${data.cell}`;
  centerMapOn(state.lat, state.lon, state.name);
  loadRadar();

  /* Hero: current conditions */
  const nowRow = byTime[times[0]] || {};
  const wk = weatherKey({ ...nowRow, time: times[0] });
  el("sky-icon").textContent = wk.icon;
  document.body.dataset.sky = wk.sky;
  const t = nowRow.temperature_2m;
  el("now-temp").textContent = t ? `${round(t.value)}°` : "—";
  el("now-range").textContent =
    t && t.calibrated && t.confidence < 0.98 ? `fourchette ${round(t.low)}–${round(t.high)}°` : "";
  el("now-feels").textContent = t ? `Ressenti ${rangeText(t)}°` : "";
  const det = [];
  if (nowRow.wind_speed_10m) det.push(`💨 ${kmh(nowRow.wind_speed_10m)}`);
  if (nowRow.relative_humidity_2m) det.push(`💧 ${rangeText(nowRow.relative_humidity_2m, 0)}%`);
  if (nowRow.pressure_msl) det.push(`${rangeText(nowRow.pressure_msl, 0)} hPa`);
  if (nowRow.precipitation_probability) det.push(`☔ ${round(nowRow.precipitation_probability.value, 0)}%`);
  el("now-details").innerHTML = det.map((d) => `<span class="chip">${d}</span>`).join("");
  const rainSum = fc.filter((i) => i.variable === "precipitation").reduce((a, i) => a + (i.value || 0), 0);
  const maxT = fc.filter((i) => i.variable === "temperature_2m").reduce((a, i) => Math.max(a, i.value), -99);
  const minT = fc.filter((i) => i.variable === "temperature_2m").reduce((a, i) => Math.min(a, i.value), 99);
  el("loc-summary").textContent =
    `${wk.icon} ${fc.length > 0 ? `Max ${round(maxT)}° / Min ${round(minT)}°` : ""}` +
    (rainSum > 0.1 ? ` · ${round(rainSum, 1)} mm sur la période` : "");

  /* Timeline */
  renderTimeline(times, byTime);

  /* Hourly cards */
  renderHours(times, byTime);

  /* Confidence per variable */
  renderConfidence(fc);

  /* Global sure badge */
  renderSureBadge(data.summary);

  /* Local station badge */
  renderStationBadge(fc);

  /* Table */
  renderTable(times, byTime);
}

/* How many local stations feed the live consensus. */
function renderStationBadge(fc) {
  const badge = el("station-badge");
  const first = fc.find((i) => i.contributors?.length);
  if (!first) {
    badge.hidden = true;
    return;
  }
  const stations = new Set(first.contributors.filter((c) => c.startsWith("metar_")));
  if (!stations.size) {
    badge.hidden = true;
    return;
  }
  badge.hidden = false;
  badge.className = "station-badge";
  badge.textContent = `📍 ${stations.size} station${stations.size > 1 ? "s" : ""} locale${stations.size > 1 ? "s" : ""}`;
}

/* Verdict banner: how much of the requested window is genuinely sure. */
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
  let cls, label;
  if (share >= 70) {
    cls = "sure--ok";
    label = "✓ météo sûre";
  } else if (share >= 40) {
    cls = "sure--mid";
    label = "fiable";
  } else if (share >= 15) {
    cls = "sure--warn";
    label = "incertitude";
  } else {
    cls = "sure--bad";
    label = "pas fiable";
  }
  badge.hidden = false;
  badge.className = `sure-badge ${cls}`;
  badge.textContent = `${label} · ${share}% des valeurs sûres (conf. moy. ${avg}%)`;
}

function renderTimeline(times, byTime) {
  const chart = el("timeline-chart");
  chart.innerHTML = "";
  const temps = times.map((t) => byTime[t].temperature_2m?.value ?? null).filter((v) => v !== null);
  const min = Math.min(...temps, -99);
  const max = Math.max(...temps, 99);
  const span = Math.max(max - min, 1);
  let avgConf = null;
  const confs = times.map((t) => byTime[t].temperature_2m?.confidence).filter((c) => c !== null);
  if (confs.length) avgConf = confs.reduce((a, b) => a + b, 0) / confs.length;
  // Labels overlap once columns get narrower than the text: thin them out
  // according to the real pixel width per column (mobile has ~15px columns
  // where even 24 labels collide).
  const width = chart.clientWidth || 600;
  const pxPerCol = width / Math.max(times.length, 1);
  const step = Math.max(1, Math.ceil(36 / Math.max(pxPerCol, 1)));

  times.forEach((t, idx) => {
    const row = byTime[t];
    const v = row.temperature_2m?.value;
    if (v === null || v === undefined) return;
    const pct = ((v - min) / span) * 100;
    const cls = v < 10 ? "tl-bar--cold" : v < 25 ? "tl-bar--mild" : "tl-bar--hot";
    const showLabel = idx % step === 0;
    const col = document.createElement("div");
    col.className = "tl-col";
    col.innerHTML = `
      <div class="tl-bar ${cls}" style="height:${Math.max(pct, 3)}%">
        ${showLabel ? `<span class="tl-bar__temp">${round(v)}°</span>` : ""}
      </div>
      ${showLabel ? `<span class="tl-time">${fmtTime(t)}</span>` : ""}`;
    chart.appendChild(col);
  });
  if (avgConf !== null) {
    el("timeline-badge").textContent = `confiance moyenne ${Math.round(avgConf * 100)}%`;
    el("timeline-badge").style.background = `rgba(15,157,88,.12)`;
    el("timeline-badge").style.color = avgConf >= 0.95 ? "#0f9d58" : avgConf >= 0.9 ? "#d97706" : "#dc2626";
  } else {
    el("timeline-badge").textContent = "non calibrée";
  }
}

function renderHours(times, byTime) {
  const wrap = el("hours");
  wrap.innerHTML = "";
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
    const card = document.createElement("div");
    card.className = "hour";
    card.innerHTML = `
      <div class="hour__head">
        <span class="hour__time">${fmtTime(t)}</span>
        ${showDay ? `<span class="hour__day">${day}</span>` : ""}
      </div>
      <div class="hour__main">
        <span class="hour__temp">${temp ? round(temp.value) + "°" : "—"}</span>
        <span class="hour__ic">${wk.icon}</span>
      </div>
      <div class="hour__range">${rng}</div>
      <div class="hour__row">${prob && prob.value > 0 ? `💧 ${round(prob.value, 0)}%` : ""}</div>
      <div class="hour__row">${wind ? `🌬 ${kmh(wind)}${gust ? ` · raf. ${Math.round(gust.value * 3.6)}` : ""}` : ""}</div>
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
        <span class="var__label">${VAR_LABELS[v] || v}</span>
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
  loadForecast();
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
}

async function loadRadar() {
  const status = el("radar-status");
  try {
    const r = await fetch("/radar");
    if (!r.ok) throw new Error(`radar ${r.status}`);
    const d = await r.json();
    const all = [...(d.radar?.past || []), ...(d.radar?.nowcast || [])];
    if (!all.length) {
      status.textContent = "pas de données radar";
      return;
    }
    radarFrames = all.map((f) => ({
      host: d.host,
      path: f.path,
      time: f.time,
      isNowcast: (f.path || "").includes("nowcast"),
    }));
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
    status.textContent = "radar indisponible";
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
    const FrameLayer = L.TileLayer.extend({
      getTileUrl(coords) {
        const z = Math.max(5, Math.min(coords.z, 7));
        const scale = Math.pow(2, coords.z - z);
        return `${f.host}${f.path}/256/${z}/${Math.floor(coords.x / scale)}/${Math.floor(coords.y / scale)}/2/1_1_0.png`;
      },
    });
    layer = new FrameLayer({ opacity: 0.75, maxNativeZoom: 7, noWrap: true });
    radarLayerCache[radarIdx] = layer;
  }
  radarLayer = layer;
  currentRadarIdx = radarIdx;
  layer.addTo(map);
  const t = new Date(f.time * 1000);
  const label = `${t.toLocaleDateString("fr-FR", { day: "numeric", month: "short" })} ${t.toLocaleTimeString("fr-FR")}${f.isNowcast ? " (prévision)" : ""}`;
  el("radar-status").textContent = `radar ${label}`;
  const slider = el("radar-slider");
  if (slider) slider.value = radarIdx;
  const tinfo = el("radar-time");
  if (tinfo) tinfo.textContent = t.toLocaleTimeString("fr-FR");
}

function toggleRadarPlay() {
  if (!radarFrames.length) return;
  if (radarPlaying) {
    radarPlaying = false;
    clearInterval(radarTimer);
    el("radar-play").textContent = "▶ Lecture";
  } else {
    radarPlaying = true;
    el("radar-play").textContent = "⏸ Pause";
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