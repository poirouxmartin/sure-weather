/* Sure Weather — standalone on-device provider (APK / offline-backend mode).
   No backend needed: fetches several NWP models straight from Open-Meteo on
   the phone and fuses them with a median consensus. No learned calibration
   (no residual history on device): confidence reflects inter-model agreement
   and items are marked calibrated:false — the UI shows honest "non cal."
   ranges instead of fake "sûr" badges. Payload shape matches GET /weather. */
"use strict";

window.SureLocal = (() => {
  const MODELS = [
    "gfs_seamless",
    "ecmwf_ifs025",
    "ecmwf_aifs025", // AI
    "gfs_graphcast025", // AI
    "icon_seamless",
    "metno_seamless",
    "gem_seamless",
    "ukmo_seamless",
    "jma_seamless",
    "bom_access_global",
    "arpege_seamless",
  ];
  const HOURLY = [
    "temperature_2m", "dew_point_2m", "precipitation_probability",
    "precipitation", "wind_speed_10m", "wind_gusts_10m",
    "wind_direction_10m", "relative_humidity_2m", "cloud_cover",
    "pressure_msl", "visibility", "uv_index",
  ];
  const TOL = {
    temperature_2m: 2.0, dew_point_2m: 3.0, relative_humidity_2m: 10.0,
    precipitation: 0.5, precipitation_probability: 20.0, cloud_cover: 20.0,
    wind_speed_10m: 2.0, wind_gusts_10m: 3.0, wind_direction_10m: 30.0,
    uv_index: 1.0, pressure_msl: 2.0, visibility: 5000.0,
  };
  const BOUNDS = {
    relative_humidity_2m: [0, 100], precipitation: [0, null],
    precipitation_probability: [0, 100], cloud_cover: [0, 100],
    wind_speed_10m: [0, null], wind_gusts_10m: [0, null],
    wind_direction_10m: [0, 360], uv_index: [0, null], visibility: [0, null],
  };
  /* Plausibility guard: a corrupt model value (sensor glitch, unit slip)
     must never enter the consensus. Mirrors the backend's hard validation. */
  const PLAUSIBLE = {
    temperature_2m: [-90, 60], dew_point_2m: [-90, 60],
    relative_humidity_2m: [0, 100], precipitation: [0, 500],
    precipitation_probability: [0, 100], cloud_cover: [0, 100],
    wind_speed_10m: [0, 150], wind_gusts_10m: [0, 150],
    wind_direction_10m: [0, 360], uv_index: [0, 25],
    pressure_msl: [850, 1100], visibility: [0, 100000],
  };
  function plausible(variable, v) {
    const b = PLAUSIBLE[variable];
    return !b || (v >= b[0] && v <= b[1]);
  }
  // Prior rmse per variable (typical inter-model error when all agree):
  // pressure models agree to ~1 hPa, not ±2 — a flat prior capped every
  // agreeing variable at ~70% and dragged the average down with it.
  const PRIOR = {
    temperature_2m: 1.2, dew_point_2m: 1.5, relative_humidity_2m: 5.0,
    precipitation: 0.3, precipitation_probability: 12.0, cloud_cover: 12.0,
    wind_speed_10m: 1.2, wind_gusts_10m: 1.8, wind_direction_10m: 18.0,
    uv_index: 0.6, pressure_msl: 1.0, visibility: 3000.0,
  };
  const SURE_VARS = ["temperature_2m", "wind_speed_10m", "wind_gusts_10m",
    "pressure_msl", "dew_point_2m", "precipitation_probability"];

  function erf(x) {
    const s = x < 0 ? -1 : 1;
    x = Math.abs(x);
    const t = 1 / (1 + 0.3275911 * x);
    const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
    return s * y;
  }
  function median(a) {
    const s = a.slice().sort((x, y) => x - y);
    const m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }
  /* Weighted median: trusted models (low learned rmse) pull the consensus.
     Falls back to the plain median with equal weights. */
  function weightedMedian(vals, weights) {
    const order = vals.map((v, i) => i).sort((a, b) => vals[a] - vals[b]);
    const total = weights.reduce((x, y) => x + y, 0);
    let acc = 0;
    for (const i of order) {
      acc += weights[i];
      if (acc >= total / 2) return vals[i];
    }
    return vals[order[order.length - 1]];
  }
  function mad(a, med) {
    return median(a.map((v) => Math.abs(v - med)));
  }
  function std(a, mean) {
    const m = mean ?? a.reduce((x, y) => x + y, 0) / a.length;
    return Math.sqrt(a.reduce((x, y) => x + (y - m) * (y - m), 0) / a.length);
  }
  function circularMean(degs) {
    let sx = 0, sy = 0;
    for (const d of degs) {
      const r = (d * Math.PI) / 180;
      sx += Math.cos(r); sy += Math.sin(r);
    }
    return ((Math.atan2(sy / degs.length, sx / degs.length) * 180) / Math.PI + 360) % 360;
  }
  function circularSpread(degs, mean) {
    let s = 0;
    for (const d of degs) {
      let diff = Math.abs(d - mean) % 360;
      if (diff > 180) diff = 360 - diff;
      s += diff;
    }
    return s / degs.length;
  }
  function clamp(v, variable) {
    const b = BOUNDS[variable];
    if (!b) return v;
    if (b[0] !== null && b[0] !== undefined) v = Math.max(v, b[0]);
    if (b[1] !== null && b[1] !== undefined) v = Math.min(v, b[1]);
    return v;
  }

  /* One combined request for all models (keys {var}_{model}), exactly like
     the backend collector: 1 request instead of 8 → far fewer 429s.
     wind_speed_unit=ms is MANDATORY: the app displays ×3.6 (km/h). */
  const HIRES_MODELS = ["meteofrance_arome_france", "icon_d2"];
  function forecastUrl(lat, lon, models) {
    return (
      `https://api.open-meteo.com/v1/forecast?latitude=${lat.toFixed(4)}&longitude=${lon.toFixed(4)}` +
      `&hourly=${HOURLY.join(",")}&minutely_15=precipitation&daily=sunrise,sunset&models=${models.join(",")}` +
      `&forecast_days=7&past_days=1&wind_speed_unit=ms&timezone=UTC`
    );
  }
  /* Rain timing at 15-min precision: median precipitation across models per
     quarter hour. Returns {start,end} ISO or nulls. */
  function rainTiming(data, modelsOk) {
    const m15 = data.minutely_15 || {};
    const times = m15.time || [];
    if (!times.length) return { start: null, end: null };
    const nowMs = Date.now();
    const series = modelsOk.map((m) => m15[`precipitation_${m}`] || m15.precipitation || []);
    let start = null, end = null;
    for (let i = 0; i < times.length; i++) {
      const vt = new Date(times[i] + "Z").getTime();
      if (vt < nowMs - 15 * 60e3) continue;
      const vals = series.map((s) => s[i]).filter((v) => v !== null && v !== undefined && Number.isFinite(v));
      if (!vals.length) continue;
      const wet = median(vals) > 0.15;
      if (wet && !start) start = new Date(vt).toISOString();
      if (!wet && start && !end) { end = new Date(vt).toISOString(); break; }
      if (start && vt - new Date(start).getTime() > 12 * 3600e3) break;
    }
    return { start, end };
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function fetchJson(url, signal, what) {
    let lastErr = null;
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        const r = await fetch(url, { signal });
        if (r.status === 429 || (r.status >= 500 && r.status <= 599)) {
          lastErr = new Error(`${what}: serveur saturé (${r.status}), nouvel essai…`);
          await sleep(attempt * 2000);
          continue;
        }
        if (!r.ok) throw new Error(`${what}: erreur ${r.status}`);
        return await r.json();
      } catch (e) {
        // Offline / DNS / CORS: no point retrying the same burst.
        if (e instanceof TypeError || /Failed to fetch|NetworkError|Load failed/i.test(e.message || "")) {
          throw new Error("pas de connexion Internet — vérifie le réseau puis réessaie");
        }
        lastErr = e;
        if (attempt < 3) await sleep(attempt * 2000);
      }
    }
    throw lastErr || new Error(`${what}: échec`);
  }

  function summarize(items) {
    const bands = [[0, 6, "3h"], [6, 24, "12h"], [24, 72, "48h"], [72, 1e9, "J+"]];
    const horizons = {}, byVar = {};
    for (const it of items) {
      if (!SURE_VARS.includes(it.variable)) continue;
      (byVar[it.variable] = byVar[it.variable] || []).push(it);
      for (const [lo, hi, label] of bands) {
        if ((lo === 0 && it.horizon_h <= hi) || (lo < it.horizon_h && it.horizon_h <= hi)) {
          (horizons[label] = horizons[label] || []).push(it);
          break;
        }
      }
    }
    // sure = learned-calibrated AND confident (backend parity). Before any
    // learning the flags stay false and the UI shows honest "non cal."
    // ranges instead of fake "sûr" badges.
    const summary = { horizons: {}, variables: {} };
    for (const [label, bucket] of Object.entries(horizons)) {
      const avg = bucket.reduce((a, b) => a + b.confidence, 0) / bucket.length;
      summary.horizons[label] = {
        avg_confidence: +avg.toFixed(3),
        sure_share: +(bucket.filter((i) => i.sure).length / bucket.length).toFixed(3),
        n: bucket.length,
      };
    }
    for (const [v, bucket] of Object.entries(byVar)) {
      const avg = bucket.reduce((a, b) => a + b.confidence, 0) / bucket.length;
      summary.variables[v] = {
        avg_confidence: +avg.toFixed(3),
        sure: bucket.every((i) => i.sure),
      };
    }
    return summary;
  }

  async function forecast(lat, lon, hours = 168, timeoutMs = 60000) {
    const now = new Date();
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    let sun;
    try {
      const data = await fetchJson(forecastUrl(lat, lon, MODELS), ctrl.signal, "prévisions");
      // High-res regional models (AROME France 1.3km, ICON-D2 2.2km):
      // best-effort, skipped silently outside their domain.
      let hiData = null;
      try {
        hiData = await fetchJson(forecastUrl(lat, lon, HIRES_MODELS), ctrl.signal, "haute-résolution");
      } catch { hiData = null; }
      const hourly = data.hourly || {};
      const modelsOk = MODELS.filter((m) => HOURLY.some((v) => hourly[`${v}_${m}`] || hourly[v]));
      if (!modelsOk.length) throw new Error("réponse météo vide — réessaie dans une minute");
      // timezone=UTC was requested: stamp Z so browsers parse true UTC.
      // NOTE: with several models, daily vars come back suffixed per model
      // (sunrise_gfs_seamless, …) — plain `sunrise` is then absent.
      const stamp = (xs) => (xs || []).map((x) => (/Z|[+-]\d{2}:?\d{2}$/.test(x) ? x : x + "Z"));
      const d0 = data.daily || {};
      const pickDaily = (prefix) =>
        d0[prefix] || d0[`${prefix}_${MODELS[0]}`] ||
        (Object.entries(d0).find(([k]) => k.startsWith(prefix + "_")) || [])[1] || [];
      sun = { sunrise: stamp(pickDaily("sunrise")), sunset: stamp(pickDaily("sunset")) };
      // Per-model series (combined request → {var}_{model} keys; plain {var}
      // fallback when a single model answers).
      const times = (hourly.time || []).map((t) => t + "Z");
      const timeIndex = {};
      times.forEach((t, i) => { timeIndex[t] = i; });
      const seriesOf = (h, varName, model) => h[`${varName}_${model}`] || h[varName] || [];
      const perModel = modelsOk.map((model) => ({ model, hourly }));
      if (hiData && hiData.hourly) {
        for (const m of HIRES_MODELS) {
          if (HOURLY.some((v) => hiData.hourly[`${v}_${m}`] || hiData.hourly[v])) {
            perModel.push({ model: m, hourly: hiData.hourly });
          }
        }
      }
      const lo = now.getTime() - 3600e3;
      const out = [];
      const breakdownByTime = {};
      for (const t of times) {
        const vt = new Date(t).getTime();
        if (vt < lo) continue;
        const horizon_h = +(((vt - now.getTime()) / 3600e3).toFixed(1));
        for (const variable of HOURLY) {
          const vals = [], provs = [];
          const idx = timeIndex[t];
          for (const pm of perModel) {
            const v = seriesOf(pm.hourly, variable, pm.model)[idx];
            if (v === null || v === undefined || !Number.isFinite(v)) continue;
            if (!plausible(variable, v)) continue; // corrupt model value
            vals.push(v); provs.push(pm.model);
          }
          if (!vals.length) continue;
          const isDir = variable === "wind_direction_10m";
          // Learned per-model stats (null until ~5 matched residuals).
          const LEARN = window.SureLearn;
          const stats = provs.map((p) => LEARN ? LEARN.getStat(p, variable, horizon_h, lat, lon) : null);
          const corrs = vals.map((v, i) => {
            const st = stats[i];
            // Directions: never bias-shifted (circular), like the backend.
            if (isDir || !st) return v;
            return v - st.bias;
          });
          const weights = stats.map((st) => (LEARN ? LEARN.weightOf(st) : 1));
          const anyLearned = stats.some((st) => st && st.n >= (LEARN ? LEARN.MIN_N : 1e9));
          const consensus = isDir ? circularMean(corrs) : weightedMedian(corrs, weights);
          const med = isDir ? consensus : median(corrs);
          let spread = isDir ? circularSpread(corrs, med) : mad(corrs, med);
          if (!(spread > 0)) spread = std(corrs, isDir ? undefined : med) || 0.5;
          const tol = TOL[variable] ?? 1.5;
          // Learned component: inverse-variance combination of the models'
          // own rmses (backend formula); otherwise the generic prior.
          let sigmaLearned;
          const inv = stats.filter(Boolean).reduce((a, st) => a + 1 / (st.rmse * st.rmse), 0);
          if (inv > 0) sigmaLearned = 1 / Math.sqrt(inv);
          else sigmaLearned = (PRIOR[variable] ?? 2.0) / Math.sqrt(vals.length);
          const sigmaTotal = Math.hypot(sigmaLearned, spread / 0.8);
          const confidence = +erf(tol / (sigmaTotal * Math.SQRT2)).toFixed(3);
          const low = clamp(consensus - tol, variable);
          const high = clamp(consensus + tol, variable);
          const sure = anyLearned && confidence >= 0.98; // backend parity
          out.push({
            variable, valid_at: new Date(vt).toISOString(),
            value: +clamp(consensus, variable).toFixed(2),
            confidence, calibrated: anyLearned, dispersion: +spread.toFixed(3),
            bias_corrected: anyLearned, tolerance: tol,
            low: +low.toFixed(2), high: +high.toFixed(2),
            sure, horizon_h,
            contributors: provs,
          });
          if (variable === "temperature_2m") {
            const totW = weights.reduce((a, b) => a + b, 0) || 1;
            breakdownByTime[t] = provs.map((p, i) => ({
              provider: p, raw: +vals[i].toFixed(2),
              bias: stats[i] ? +stats[i].bias.toFixed(2) : 0,
              corr: +corrs[i].toFixed(2), weight: +weights[i].toFixed(3),
              share: +(weights[i] / totW * 100).toFixed(1),
            }));
          }
        }
      }
      // Nearest temp hour at/after now for the sources table (shares are
      // the real learned weights, computed above — not equal splits).
      const bTimes = Object.keys(breakdownByTime).sort();
      const b0 = bTimes.find((t) => new Date(t).getTime() >= now.getTime() - 30 * 60e3) || bTimes[0];
      const breakdown = [...(breakdownByTime[b0] || [])].sort((a, b) => b.weight - a.weight);
      const flat = Math.round(lat / 0.1) * 0.1, flon = Math.round(lon / 0.1) * 0.1;
      let rain = { start: null, end: null };
      try {
        rain = rainTiming(data, perModel.map((p) => p.model));
      } catch { /* rain timing is a bonus */ }
      // Feed the learner AFTER render (never on the critical path: the
      // stringify + localStorage write of ~2 MB would ANR slow phones).
      try {
        if (window.SureLearn) {
          const L = window.SureLearn;
          setTimeout(() => {
            try {
              L.record(lat, lon, times, perModel, HOURLY, seriesOf);
              if (L.shouldLearn() && navigator.onLine !== false) {
                L.learnTruth(lat, lon, undefined).catch(() => {});
              }
            } catch { /* learning is best-effort */ }
          }, 2500);
        }
      } catch { /* learning is best-effort */ }
      return {
        location: { lat, lon },
        cell: `${flat.toFixed(4)},${flon.toFixed(4)}`,
        generated_at: now.toISOString(),
        forecast: out,
        summary: summarize(out),
        partial: perModel.length < 3, // fewer than 3 models: degraded consensus
        breakdown,
        breakdown_valid_at: b0 ? new Date(b0).toISOString() : null,
        rain,
        sun: { daily: sun },
        local: true,
        models_ok: perModel.map((p) => p.model),
      };
    } finally {
      clearTimeout(timer);
    }
  }

  async function geocodeNominatim(q) {
    const r = await fetch(
      `https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(q)}&format=json&limit=5&addressdetails=0&accept-language=fr&extratags=0`,
      { headers: { Accept: "application/json" } }
    );
    if (!r.ok) throw new Error(`geocode ${r.status}`);
    return {
      results: (await r.json()).map((item) => ({
        lat: parseFloat(item.lat), lon: parseFloat(item.lon),
        name: item.display_name || q, type: item.type || "",
      })),
    };
  }

  async function radar() {
    const r = await fetch("https://api.rainviewer.com/public/weather-maps.json");
    if (!r.ok) throw new Error(`radar ${r.status}`);
    return r.json();
  }

  /* Model overlay layers (same ranges as the backend renderer): sampled
     on a grid over the viewport from Open-Meteo directly, drawn by the app
     on a canvas imageOverlay. */
  const LAYER_VARS = {
    temp: "temperature_2m", precip: "precipitation", uv: "uv_index",
    humidity: "relative_humidity_2m", cloud: "cloud_cover",
    pressure: "pressure_msl",
  };
  const LUTS = {
    temp: { vmin: -10, vmax: 40, stops: [
      [-10, [69, 117, 180, 200]], [0, [116, 173, 209, 200]],
      [10, [171, 221, 164, 200]], [18, [254, 224, 144, 200]],
      [25, [253, 174, 97, 215]], [33, [244, 109, 67, 225]],
      [40, [165, 0, 38, 230]]] },
    precip: { vmin: 0, vmax: 20, stops: [
      [0, [255, 255, 255, 0]], [0.1, [200, 230, 255, 120]],
      [0.5, [120, 190, 255, 165]], [2, [60, 140, 255, 205]],
      [6, [30, 90, 220, 235]], [12, [20, 40, 160, 245]],
      [20, [10, 10, 90, 250]]] },
    uv: { vmin: 0, vmax: 11, stops: [
      [0, [60, 180, 75, 140]], [2, [120, 200, 80, 165]],
      [4, [255, 220, 60, 180]], [6, [255, 150, 30, 200]],
      [8, [220, 50, 50, 220]], [11, [140, 40, 180, 230]]] },
    humidity: { vmin: 0, vmax: 100, stops: [
      [0, [255, 255, 255, 0]], [30, [200, 220, 255, 110]],
      [60, [100, 160, 255, 175]], [85, [30, 90, 200, 210]],
      [100, [10, 40, 120, 225]]] },
    cloud: { vmin: 0, vmax: 100, stops: [
      [0, [255, 255, 255, 0]], [20, [220, 220, 220, 90]],
      [50, [160, 160, 160, 150]], [80, [90, 90, 90, 190]],
      [100, [40, 40, 40, 215]]] },
    pressure: { vmin: 980, vmax: 1030, stops: [
      [980, [120, 80, 180, 170]], [1000, [100, 150, 220, 175]],
      [1015, [120, 200, 120, 165]], [1030, [220, 180, 80, 185]]] },
  };
  function lutColor(layer, v) {
    const lut = LUTS[layer];
    if (!lut || v == null || !Number.isFinite(v)) return [0, 0, 0, 0];
    const st = lut.stops;
    if (v <= st[0][0]) return st[0][1];
    for (let i = 0; i < st.length - 1; i++) {
      const [v0, c0] = st[i], [v1, c1] = st[i + 1];
      if (v <= v1) {
        const f = (v - v0) / Math.max(1e-9, v1 - v0);
        return [0, 1, 2, 3].map((k) => Math.round(c0[k] + (c1[k] - c0[k]) * f));
      }
    }
    return st[st.length - 1][1];
  }

  async function modelGrid(layer, latN, lonW, latS, lonE, n = 12, signal) {
    const variable = LAYER_VARS[layer];
    if (!variable) throw new Error("unknown layer");
    const lats = [], lons = [];
    for (let i = 0; i < n; i++) {
      lats.push(latN + ((latS - latN) * i) / (n - 1));
      lons.push(lonW + ((lonE - lonW) * i) / (n - 1));
    }
    const params = [];
    for (const la of lats) for (const lo of lons) {
      params.push(`latitude=${la.toFixed(3)}`, `longitude=${lo.toFixed(3)}`);
    }
    params.push(`hourly=${variable}`, "models=gfs_seamless", "forecast_days=2", "timezone=UTC");
    const r = await fetch(`https://api.open-meteo.com/v1/forecast?${params.join("&")}`, { signal });
    if (!r.ok) throw new Error(`grid ${r.status}`);
    const samples = await r.json();
    if (!Array.isArray(samples)) throw new Error("grid shape");
    // Current-hour index from the first location's time axis.
    const t0 = (samples[0]?.hourly?.time || []).map((t) => t + "Z");
    const nowMs = Date.now();
    let idx = 0, best = Infinity;
    t0.forEach((t, i) => {
      const d = Math.abs(new Date(t).getTime() - nowMs);
      if (d < best) { best = d; idx = i; }
    });
    const grid = [];
    samples.forEach((s) => {
      const series = s?.hourly?.[variable] || [];
      grid.push(series[idx] ?? null);
    });
    return { lats, lons, n, grid, at: t0[idx] || null };
  }

  async function windGrid(latN, lonW, latS, lonE, n = 6, signal) {
    const lats = [], lons = [];
    for (let i = 0; i < n; i++) {
      lats.push(latN + ((latS - latN) * i) / (n - 1));
      lons.push(lonW + ((lonE - lonW) * i) / (n - 1));
    }
    const params = [];
    for (const la of lats) for (const lo of lons) {
      params.push(`latitude=${la.toFixed(4)}`, `longitude=${lo.toFixed(4)}`);
    }
    // m/s like everywhere else: the app displays ×3.6 (km/h).
    params.push("current=wind_speed_10m,wind_direction_10m", "wind_speed_unit=ms");
    const r = await fetch(`https://api.open-meteo.com/v1/forecast?${params.join("&")}`, { signal });
    if (!r.ok) throw new Error(`wind ${r.status}`);
    const samples = await r.json();
    if (!Array.isArray(samples)) return { points: [] };
    const points = [];
    samples.forEach((s, i) => {
      const cur = (s && s.current) || {};
      if (cur.wind_speed_10m == null || cur.wind_direction_10m == null) return;
      points.push({
        lat: +lats[Math.floor(i / n)].toFixed(4),
        lon: +lons[i % n].toFixed(4),
        kmh: Math.round(cur.wind_speed_10m * 3.6),
        deg: Math.round(cur.wind_direction_10m),
      });
    });
    return { points };
  }

  return { MODELS, LAYER_VARS, forecast, geocodeNominatim, radar, windGrid, modelGrid, lutColor, rainTiming };
})();
