/* Sure Weather PWA — vanilla JS. No build step. */
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

const conf = (x) => document.getElementById(x);

let state = { lat: 48.8566, lon: 2.3522, hours: 24, name: "Paris" };
let reqToken = 0;

const $ = (sel) => document.querySelector(sel);

function fmtTime(iso) {
  const d = new Date(iso);
  return d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

function fmtDay(iso) {
  return new Date(iso).toLocaleDateString("fr-FR", {
    weekday: "short",
    day: "numeric",
    month: "short",
  });
}

function round(v, d = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toFixed(d);
}

function confBadge(c, cal) {
  const val = cal ? c : null;
  let cls = "sn-badge--gray";
  let txt = "non calibrée";
  if (val !== null) {
    if (val >= 0.99) { cls = "sn-badge--green"; txt = "≈99%"; }
    else if (val >= 0.95) { cls = "sn-badge--teal"; txt = "95–99%"; }
    else if (val >= 0.9) { cls = "sn-badge--yellow"; txt = "90–95%"; }
    else { cls = "sn-badge--red"; txt = "<90%"; }
  }
  return `<span class="sn-badge ${cls}" title="Confiance calibrée">${txt}</span>`;
}

/* ---- Geo location / search ---- */

async function geocode(query) {
  if (query.includes(",")) {
    const [lat, lon] = query.split(",").map((s) => parseFloat(s.trim()));
    if (Number.isFinite(lat) && Number.isFinite(lon)) {
      return { lat, lon, name: `${lat.toFixed(4)}, ${lon.toFixed(4)}` };
    }
  }
  const url = `https://geocoding-api.open-meteo.com/v1/search?name=${encodeURIComponent(query)}&count=1&language=fr&format=json`;
  const r = await fetch(url);
  const d = await r.json();
  if (!d.results || !d.results.length) throw new Error("Ville introuvable");
  const res = d.results[0];
  return {
    lat: res.latitude,
    lon: res.longitude,
    name: res.name + (res.country ? `, ${res.country}` : ""),
  };
}

async function locateMe() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error("Géolocalisation non supportée"));
    navigator.geolocation.getCurrentPosition(
      (pos) =>
        resolve({
          lat: pos.coords.latitude,
          lon: pos.coords.longitude,
          name: "Ma position",
        }),
      (err) => reject(new Error(`Géolocalisation refusée (${err.code})`)),
      { timeout: 10000 }
    );
  });
}

/* ---- Data fetching ---- */

async function loadForecast() {
  const token = ++reqToken;
  showLoading(true);
  hide(conf("error"));
  try {
    const url = `/weather?lat=${state.lat}&lon=${state.lon}&hours=${state.hours}`;
    const r = await fetch(url);
    if (!r.ok) throw new Error(`API ${r.status}`);
    const data = await r.json();
    if (token !== reqToken) return; // a newer request superseded this one
    if (!data.forecast || !data.forecast.length) {
      showLoading(false);
      conf("content").hidden = true;
      conf("empty").hidden = false;
      return;
    }
    render(data);
  } catch (e) {
    if (token !== reqToken) return;
    showLoading(false);
    conf("content").hidden = true;
    conf("empty").hidden = true;
    conf("error").hidden = false;
    conf("error-text").textContent = e.message;
  }
}

/* ---- Rendering ---- */

function render(data) {
  showLoading(false);
  conf("empty").hidden = true;
  conf("content").hidden = false;

  const fc = data.forecast;
  const byTime = {};
  for (const item of fc) {
    (byTime[item.valid_at] = byTime[item.valid_at] || {})[item.variable] = item;
  }
  const times = Object.keys(byTime).sort();

  conf("loc-name").textContent = state.name;
  const cell = data.cell;
  conf("loc-meta").textContent = `cellule ${cell} · généré à ${new Date(data.generated_at).toLocaleTimeString("fr-FR")}`;

  /* Horizon cards: first N hours (up to 12) */
  const horizon = conf("horizon");
  horizon.innerHTML = "";
  for (const t of times.slice(0, 12)) {
    const row = byTime[t];
    const temp = row.temperature_2m;
    const precip = row.precipitation;
    const wind = row.wind_speed_10m;
    const confs = VAR_ORDER.filter((v) => row[v]).map((v) => row[v].confidence);
    const avgConf = confs.length ? confs.reduce((a, b) => a + b, 0) / confs.length : null;
    const isDay = new Date(t).getHours() >= 6 && new Date(t).getHours() < 21;
    const card = document.createElement("div");
    card.className = "sw-hour" + (isDay ? "" : " sw-hour--night");
    card.innerHTML = `
      <div class="sw-hour__time">${fmtDay(t)}<br/>${fmtTime(t)}</div>
      <div class="sw-hour__temp">${temp ? round(temp.value) + "°" : "—"}</div>
      <div class="sw-hour__extra">
        ${precip && precip.value > 0.1 ? `<span>💧 ${round(precip.value, 2)}mm</span>` : ""}
        ${wind ? `<span>🌬 ${round(wind.value)}</span>` : ""}
      </div>
      <div class="sw-hour__conf">${confBadge(avgConf, row.temperature_2m?.calibrated)}</div>
    `;
    horizon.appendChild(card);
  }

  /* Per-variable confidence, averaged over the window */
  const varsEl = conf("vars");
  varsEl.innerHTML = "";
  for (const v of VAR_ORDER) {
    const items = fc.filter((i) => i.variable === v && i.calibrated);
    if (!items.length) continue;
    const avg = items.reduce((a, i) => a + i.confidence, 0) / items.length;
    const pct = Math.round(avg * 100);
    const color = pct >= 99 ? "#00875a" : pct >= 95 ? "#00afbf" : pct >= 90 ? "#b86e00" : "#de350b";
    const div = document.createElement("div");
    div.className = "sw-var";
    div.innerHTML = `
      <div class="sw-var__head">
        <span class="sw-var__label">${VAR_LABELS[v] || v}</span>
        <span class="sw-var__value" style="color:${color}">${pct}%</span>
      </div>
      <div class="sn-progress sn-progress--sm sw-var__bar">
        <div class="sn-progress__bar" style="width:${pct}%; background:${color}"></div>
      </div>
    `;
    varsEl.appendChild(div);
  }

  /* Hourly table */
  const tb = conf("table-body");
  tb.innerHTML = "";
  for (const t of times) {
    const row = byTime[t];
    const confs = VAR_ORDER.filter((v) => row[v]).map((v) => row[v].confidence);
    const avgConf = confs.length ? confs.reduce((a, b) => a + b, 0) / confs.length : null;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtTime(t)}</td>
      <td>${row.temperature_2m ? round(row.temperature_2m.value) + "°" : "—"}</td>
      <td>${row.precipitation ? round(row.precipitation.value, 2) + "mm" : "—"}</td>
      <td>${row.wind_speed_10m ? round(row.wind_speed_10m.value) + " m/s" : "—"}</td>
      <td>${row.relative_humidity_2m ? round(row.relative_humidity_2m.value, 0) + "%" : "—"}</td>
      <td>${row.cloud_cover ? round(row.cloud_cover.value, 0) + "%" : "—"}</td>
      <td>${confBadge(avgConf, row.temperature_2m?.calibrated)}</td>
    `;
    tb.appendChild(tr);
  }
}

/* ---- UI helpers ---- */

function showLoading(on) {
  conf("loading").hidden = !on;
}
function hide(el) {
  el.hidden = true;
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
    conf("error").hidden = false;
    conf("error-text").textContent = err.message;
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
    conf("error").hidden = false;
    conf("error-text").textContent = err.message;
  }
});

$("#range-seg").addEventListener("click", (e) => {
  const btn = e.target.closest(".sn-seg__btn");
  if (!btn) return;
  document.querySelectorAll(".sn-seg__btn").forEach((b) => b.classList.remove("sn-seg__btn--active"));
  btn.classList.add("sn-seg__btn--active");
  state.hours = parseInt(btn.dataset.hours, 10);
  loadForecast();
});

/* Boot */
loadForecast();