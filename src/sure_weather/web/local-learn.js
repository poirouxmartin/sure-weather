/* Sure Weather — on-device learning (APK standalone).
   The backend learns per-provider bias/rmse from residuals; on the phone
   there is no server history, so the app builds its own:
   - every fusion RECORDS each model's raw values per valid hour;
   - the analysis (Open-Meteo's best estimate, past_days) is the GROUND TRUTH;
   - matched pairs become residuals → per-model/variable/horizon bias+rmse
     (Welford, capped with slow forgetting so models stay current);
   - fusion then bias-corrects and weights by 1/(rmse²+0.5), exactly like
     the backend. Learned weights persist in localStorage across sessions,
     so predictions genuinely improve the longer you use the app. */
"use strict";

window.SureLearn = (() => {
  const KEY = "sure-learn-v2"; // v1 poisoned by analysis-hours (rmse 0)
  const MAX_KEYS = 12000; // ~2 MB worst case, shared localStorage budget
  const KEEP_DAYS = 14;
  const MIN_N = 5; // below this: prior weights, calibrated:false
  const MAX_N = 500; // slow forgetting above this

  const bucket = (h) => (h <= 24 ? 24 : h <= 72 ? 72 : 168);
  const cell05 = (lat, lon) =>
    `${(Math.round(lat * 2) / 2).toFixed(1)},${(Math.round(lon * 2) / 2).toFixed(1)}`;

  function load() {
    try {
      const raw = localStorage.getItem(KEY);
      if (!raw) return { preds: {}, stats: {}, lastLearn: 0 };
      const s = JSON.parse(raw);
      if (!s || typeof s !== "object") return { preds: {}, stats: {}, lastLearn: 0 };
      s.preds = s.preds || {}; s.stats = s.stats || {};
      return s;
    } catch { return { preds: {}, stats: {}, lastLearn: 0 }; }
  }
  function save(s) {
    try {
      // Prune oldest predictions first (keys sort chronologically by suffix).
      const keys = Object.keys(s.preds);
      if (keys.length > MAX_KEYS) {
        keys.sort().slice(0, keys.length - MAX_KEYS).forEach((k) => delete s.preds[k]);
      }
      localStorage.setItem(KEY, JSON.stringify(s));
    } catch {
      // Quota: drop half the predictions and retry once.
      try {
        const keys = Object.keys(s.preds).sort();
        keys.slice(0, Math.floor(keys.length / 2)).forEach((k) => delete s.preds[k]);
        localStorage.setItem(KEY, JSON.stringify(s));
      } catch { /* private mode: learning just doesn't persist */ }
    }
  }

  /* Record raw per-model values: seriesOf(hourly,var,model) arrays aligned
     on `times` (ISO Z strings). The record timestamp ≈ issue time, so the
     horizon bucket is recovered later as valid - recorded.
     ONLY genuine forecast hours (valid ≥ now - 30 min): past hours in the
     response are already analysis, and "learning" a model against itself
     yields rmse 0 / weight 2 — fake calibration. */
  function record(lat, lon, times, perModel, vars, seriesOf) {
    const s = load();
    const cell = cell05(lat, lon);
    const cutoff = Date.now() - KEEP_DAYS * 86400e3;
    const at = Date.now();
    const forecastFrom = at - 30 * 60e3;
    let touched = false;
    for (const pm of perModel) {
      for (const variable of vars) {
        const series = seriesOf(pm.hourly, variable, pm.model);
        if (!series || !series.length) continue;
        for (let i = 0; i < times.length; i++) {
          const vt = new Date(times[i]).getTime();
          if (!(vt >= cutoff) || vt < forecastFrom) continue;
          const v = series[i];
          if (v === null || v === undefined || !Number.isFinite(v)) continue;
          const key = `${cell}|${variable}|${times[i]}`;
          const slot = s.preds[key] || (s.preds[key] = {});
          // 1 decimal is plenty for bias learning and halves storage.
          slot[pm.model] = [Math.round(v * 10) / 10, at];
          touched = true;
        }
      }
    }
    // Drop expired predictions.
    for (const k of Object.keys(s.preds)) {
      const vt = new Date(k.split("|")[2]).getTime();
      if (!(vt >= cutoff)) { delete s.preds[k]; touched = true; }
    }
    if (touched) save(s);
  }

  function circDiff(a, b) {
    let d = Math.abs(a - b) % 360;
    return d > 180 ? 360 - d : d;
  }

  function ingest(s, model, variable, hBucket, cell, err) {
    const keys = [`${model}|${variable}|${hBucket}|${cell}`, `${model}|${variable}|${hBucket}|*`];
    for (const k of keys) {
      const st = s.stats[k] || (s.stats[k] = { n: 0, bias: 0, m2: 0 });
      if (st.n >= MAX_N) { st.m2 *= 0.98; st.n = MAX_N - 1; } // slow forgetting
      st.n += 1;
      const delta = err - st.bias;
      st.bias += delta / st.n;
      st.m2 += delta * (err - st.bias);
    }
  }

  /* Fetch analysis truth and match stored predictions → residuals. */
  let running = false; // concurrent runs would ingest the same pairs twice
  async function learnTruth(lat, lon, signal, onProgress) {
    if (running) return { matched: 0, models: [], skipped: true };
    running = true;
    try {
      return await learnTruthInner(lat, lon, signal, onProgress);
    } finally {
      running = false;
    }
  }
  async function learnTruthInner(lat, lon, signal, onProgress) {
    const s = load();
    const cell = cell05(lat, lon);
    const vars = ["temperature_2m", "dew_point_2m", "precipitation_probability",
      "precipitation", "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m",
      "relative_humidity_2m", "cloud_cover", "pressure_msl", "visibility", "uv_index"];
    const url =
      `https://api.open-meteo.com/v1/forecast?latitude=${lat.toFixed(4)}&longitude=${lon.toFixed(4)}` +
      `&hourly=${vars.join(",")}&past_days=14&forecast_days=1&wind_speed_unit=ms&timezone=UTC`;
    const r = await fetch(url, { signal });
    if (!r.ok) throw new Error(`analyse: erreur ${r.status}`);
    const data = await r.json();
    const h = data.hourly || {};
    const truth = {}; // validISO -> {var: value}
    (h.time || []).forEach((t, i) => {
      const iso = t + "Z";
      const row = {};
      for (const v of vars) {
        const val = h[v] ? h[v][i] : null;
        if (val !== null && val !== undefined) row[v] = val;
      }
      truth[iso] = row;
    });
    let matched = 0;
    const models = new Set();
    const prefix = cell + "|";
    // Strictly past hours only: the truth payload also contains forecast
    // hours (forecast_days=1) — matching predictions against future
    // "truth" would learn from another forecast, not reality.
    const pastCutoff = Date.now() - 3600e3;
    const keys = Object.keys(s.preds);
    let done = 0;
    for (const k of keys) {
      if (!k.startsWith(prefix)) continue;
      const [, variable, validISO] = k.split("|");
      if (new Date(validISO).getTime() > pastCutoff) continue;
      const tRow = truth[validISO];
      if (!tRow || tRow[variable] === undefined) continue;
      const truthV = tRow[variable];
      const vt = new Date(validISO).getTime();
      for (const [model, [predV, issuedMs]] of Object.entries(s.preds[k])) {
        // True horizon recovered from the record time (≈ issue time).
        const hb = bucket(Math.max(0, (vt - (issuedMs || vt)) / 3600e3));
        let err;
        if (variable === "wind_direction_10m") {
          // Directions: no linear bias (350° vs 10° ≠ 340°); rmse only.
          err = circDiff(predV, truthV);
          ingestDir(s, model, variable, hb, cell, err);
        } else {
          err = predV - truthV;
          ingest(s, model, variable, hb, cell, err);
        }
        models.add(model);
        matched += 1;
      }
      delete s.preds[k]; // consumed
      done += 1;
      if (onProgress && done % 200 === 0) onProgress(done, keys.length);
    }
    s.lastLearn = Date.now();
    save(s);
    return { matched, models: [...models] };
  }

  /* Direction stats: bias forced to 0, m2 accumulates circular error. */
  function ingestDir(s, model, variable, hBucket, cell, err) {
    for (const k of [`${model}|${variable}|${hBucket}|${cell}`, `${model}|${variable}|${hBucket}|*`]) {
      const st = s.stats[k] || (s.stats[k] = { n: 0, bias: 0, m2: 0 });
      if (st.n >= MAX_N) { st.m2 *= 0.98; st.n = MAX_N - 1; }
      st.n += 1;
      st.m2 += err * err; // bias stays 0
    }
  }

  function rmseOf(st) {
    return st.n > 0 ? Math.sqrt(st.m2 / st.n) : Infinity;
  }

  /* Best stat: own 0.5° cell first, then global pool. */
  function getStat(model, variable, horizon, lat, lon) {
    const s = load();
    const hb = bucket(Math.max(0, horizon));
    const own = s.stats[`${model}|${variable}|${hb}|${cell05(lat, lon)}`];
    if (own && own.n >= MIN_N) return { ...own, rmse: rmseOf(own) };
    // Any bucket at this cell (horizon transfer), then global.
    for (const b of [24, 72, 168]) {
      const c = s.stats[`${model}|${variable}|${b}|${cell05(lat, lon)}`];
      if (c && c.n >= MIN_N) return { ...c, rmse: rmseOf(c) };
    }
    const g = s.stats[`${model}|${variable}|${hb}|*`];
    if (g && g.n >= MIN_N) return { ...g, rmse: rmseOf(g) };
    return null;
  }

  function weightOf(stat) {
    if (!stat) return 1;
    return 1 / (stat.rmse * stat.rmse + 0.5);
  }

  function shouldLearn() {
    try {
      const s = load();
      return Date.now() - (s.lastLearn || 0) > 6 * 3600e3;
    } catch { return false; }
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  /* Deep analysis: full learn + per-model report (temperature primary).
     Waits for any background learn to finish instead of racing it. */
  async function deepAnalyze(lat, lon, onProgress) {
    for (let i = 0; i < 120 && running; i++) await sleep(500);
    const learned = await learnTruth(lat, lon, undefined, onProgress);
    const s = load();
    const seen = new Set();
    for (const k of Object.keys(s.stats)) seen.add(k.split("|")[0]);
    const rows = [];
    for (const model of [...seen].sort()) {
      const t = s.stats[`${model}|temperature_2m|24|*`];
      const w = s.stats[`${model}|wind_speed_10m|24|*`];
      const n = (t?.n || 0) + (w?.n || 0);
      rows.push({
        model,
        n,
        tempBias: t ? +t.bias.toFixed(2) : null,
        tempRmse: t ? +rmseOf(t).toFixed(2) : null,
        weight: t ? +weightOf({ ...t, rmse: rmseOf(t) }).toFixed(3) : 0,
      });
    }
    rows.sort((a, b) => b.weight - a.weight);
    return { ...learned, rows, at: new Date().toISOString() };
  }

  function statsCount() {
    const s = load();
    return { pairs: Object.keys(s.preds).length, stats: Object.keys(s.stats).length };
  }

  return { record, learnTruth, getStat, weightOf, shouldLearn, deepAnalyze, statsCount, MIN_N };
})();
