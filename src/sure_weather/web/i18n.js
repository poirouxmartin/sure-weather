/* ---- i18n: FR (default) / EN ---- */
const I18N = {
  fr: {
    tagline: "la météo sûre",
    search_ph: "Ville, adresse, ou lat, lon…",
    search: "Rechercher",
    fav: "Favoris",
    fav_add: "Ajouter aux favoris",
    fav_remove: "Retirer des favoris",
    locate: "Me localiser",
    theme: "Thème clair/sombre",
    hours_title: "Détail heure par heure",
    mg_title: "Prochaines heures",
    now_title: "Maintenant",
    map_title: "Radar & carte",
    map_hint: "Précipitations en temps réel autour du point — cliquez la carte pour un lieu précis, déplacez le curseur pour remonter le temps. Les frames « +Nh » sont la prévision du modèle.",
    layer_radar: "Radar pluie (live)",
    layer_precip: "Préci. (modèle)",
    layer_temp: "Température",
    layer_streets: "Rue",
    wind_arrows: "💨",
    wind_arrows_title: "Flèches de vent (vitesse + direction)",
    play: "▶ Lecture",
    pause: "⏸ Pause",
    conf_title: "Confiance de la prévision",
    conf_hint: "Probabilité calibrée que la valeur reste sous une tolérance (ex. ±2 °C), vérifiée hors échantillon contre la réanalyse sur les 3 derniers mois. Une valeur « sûre » est affichée telle quelle ; sinon une fourchette honnête est montrée à la place d'un chiffre faux-précis.",
    table_title: "Tableau détaillé",
    th_hour: "Heure", th_temp: "Temp", th_dew: "Pt rosée", th_rain: "Pluie (prob.)",
    th_wind: "Vent", th_gust: "Rafales", th_hum: "Humid", th_cloud: "Nébulosité",
    th_press: "Pression", th_vis: "Visibilité",
    footer: "fusion de {m} modèles + {s} stations · correction de biais par cellule · maj {u}",
    now_feels: "Ressenti",
    dry: "sec",
    v_dry: "Sec",
    v_rain: "Pluie dès {h}",
    v_rain_now: "Pluie en cours",
    v_temp: "🌡️ {max}° / {min}°",
    v_at: "à {h}",
    v_gust: "Rafales {v} km/h",
    v_heat: "Forte chaleur",
    v_frost: "Gel",
    radar_nodata: "pas de données radar",
    radar_err: "radar indisponible",
    radar_label: "radar",
    radar_prev: "(prévision)",
    now_label: "maintenant",
    legend_rain: "pluie",
    err_notfound: "Lieu introuvable",
    err_slow: "Le calcul prend trop longtemps (source météo lente ou injoignable).",
    retry: "Réessayer",
    geoloc_err: "Géolocalisation refusée ou indisponible",
    loading_geocode: "recherche du lieu…",
    loading_calib: "Calcul de la confiance…",
    loading_src: "découverte des sources locales…",
    loading_arch: "récupération de 3 mois d'archives (1ère visite)…",
    loading_cross: "croisement des modèles et des stations…",
    loading_wait: "encore un instant, la calibration est lourde…",
    empty: "Pas de données pour cette zone.",
    raf: "raf.",
    st_one: "1 station météo locale en direct",
    st_many: "{n} stations météo locales en direct",
    conf_na: "non cal.",
    sources: "Sources : Open-Meteo ({models}) · {st} stations METAR · radar RainViewer · © OpenStreetMap · maj {u}",
    sources_full: "Modèles et stations",
    var_temp: "Température", var_dew: "Point de rosée", var_hum: "Humidité",
    var_rain: "Pluie", var_rainprob: "Probabilité de pluie", var_cloud: "Nébulosité",
    var_wind: "Vent", var_gust: "Rafales", var_press: "Pression", var_vis: "Visibilité",
    wind_tip: "{v} km/h · secteur {d}",
  },
  en: {
    tagline: "sure weather",
    search_ph: "City, address, or lat, lon…",
    search: "Search",
    fav: "Favorites",
    fav_add: "Add to favorites",
    fav_remove: "Remove from favorites",
    locate: "Locate me",
    theme: "Light/dark theme",
    hours_title: "Hour-by-hour detail",
    mg_title: "Next hours",
    now_title: "Now",
    map_title: "Radar & map",
    map_hint: "Real-time precipitation around the point — click the map for an exact place, scrub the timeline to go back in time. “+Nh” frames are model forecast.",
    layer_radar: "Live rain radar",
    layer_precip: "Precip (model)",
    layer_temp: "Temperature",
    layer_streets: "Streets",
    wind_arrows: "💨",
    wind_arrows_title: "Wind arrows (speed + direction)",
    play: "▶ Play",
    pause: "⏸ Pause",
    conf_title: "Forecast confidence",
    conf_hint: "Calibrated probability that the value stays within a tolerance (e.g. ±2 °C), verified out-of-sample against reanalysis over the last 3 months. A “sure” value is shown as is; otherwise an honest range is shown instead of a fake-precise number.",
    table_title: "Detailed table",
    th_hour: "Hour", th_temp: "Temp", th_dew: "Dew pt", th_rain: "Rain (prob.)",
    th_wind: "Wind", th_gust: "Gusts", th_hum: "Humid", th_cloud: "Cloud",
    th_press: "Pressure", th_vis: "Visibility",
    footer: "fusion of {m} models + {s} stations · per-cell bias correction · updated {u}",
    now_feels: "Feels",
    dry: "dry",
    v_dry: "Dry",
    v_rain: "Rain from {h}",
    v_rain_now: "Rain falling now",
    v_temp: "🌡️ {max}° / {min}°",
    v_at: "at {h}",
    v_gust: "Gusts {v} km/h",
    v_heat: "Heat wave",
    v_frost: "Frost",
    radar_nodata: "no radar data",
    radar_err: "radar unavailable",
    radar_label: "radar",
    radar_prev: "(forecast)",
    now_label: "now",
    legend_rain: "rain",
    err_notfound: "Place not found",
    err_slow: "Taking too long (weather source slow or unreachable).",
    retry: "Retry",
    geoloc_err: "Geolocation denied or unavailable",
    loading_geocode: "searching for place…",
    loading_calib: "Computing confidence…",
    loading_src: "discovering local sources…",
    loading_arch: "fetching 3 months of archives (first visit)…",
    loading_cross: "cross-referencing models and stations…",
    loading_wait: "one more moment, calibration is heavy…",
    empty: "No data for this area.",
    raf: "gusts",
    st_one: "1 live local weather station",
    st_many: "{n} live local weather stations",
    conf_na: "uncal.",
    sources: "Sources: Open-Meteo ({models}) · {st} METAR stations · radar RainViewer · © OpenStreetMap · updated {u}",
    sources_full: "Models and stations",
    var_temp: "Temperature", var_dew: "Dew point", var_hum: "Humidity",
    var_rain: "Rain", var_rainprob: "Rain probability", var_cloud: "Cloud cover",
    var_wind: "Wind", var_gust: "Gusts", var_press: "Pressure", var_vis: "Visibility",
    wind_tip: "{v} km/h · from {d}",
  },
};

let LANG = localStorage.getItem("sure-weather-lang")
  || (navigator.language || "fr").slice(0, 2);
if (!I18N[LANG]) LANG = "fr";

function tr(key, vars) {
  let s = I18N[LANG][key] ?? I18N.fr[key] ?? key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, v);
  return s;
}

function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = tr(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => {
    el.placeholder = tr(el.dataset.i18nPh);
  });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => {
    el.title = tr(el.dataset.i18nTitle);
  });
  document.documentElement.lang = LANG;
}

function setLang(lang) {
  LANG = I18N[lang] ? lang : "fr";
  localStorage.setItem("sure-weather-lang", LANG);
  applyI18n();
  if (lastMeteo) renderTimeline(lastMeteo.times, lastMeteo.byTime);
}

window.addEventListener("DOMContentLoaded", applyI18n);