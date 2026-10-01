package com.sureweather.app;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.widget.RemoteViews;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.view.View;

/**
 * Home-screen widget: current temperature + condition for the last place
 * viewed in the app (saved by the web UI via Capacitor Preferences into the
 * "CapacitorStorage" prefs file). Refreshes every 30 min (updatePeriodMillis)
 * and on every tap. Fully offline-tolerant: keeps the last values on error.
 */
public class WeatherWidget extends AppWidgetProvider {

    private static final ExecutorService POOL = Executors.newSingleThreadExecutor();
    static final String ACTION_REFRESH = "com.sureweather.app.WIDGET_REFRESH";
    static final String TAG = "SureWidget";

    /** System HTTP proxy (emulator -http-proxy, corporate proxies): plain
     * HttpURLConnection does NOT always follow the APN proxy, so read the
     * global setting explicitly. Returns NO_PROXY when unset/unparseable. */
    static java.net.Proxy systemProxy(Context ctx) {
        try {
            String hp = android.provider.Settings.Global.getString(
                    ctx.getContentResolver(), android.provider.Settings.Global.HTTP_PROXY);
            if (hp == null || hp.isEmpty()) return java.net.Proxy.NO_PROXY;
            String host = hp;
            int port = 8080;
            int colon = hp.lastIndexOf(':');
            if (colon >= 0) {
                host = hp.substring(0, colon);
                try { port = Integer.parseInt(hp.substring(colon + 1)); } catch (NumberFormatException e) { port = 8080; }
            }
            if (host.isEmpty()) return java.net.Proxy.NO_PROXY;
            android.util.Log.d(TAG, "using proxy " + host + ":" + port);
            return new java.net.Proxy(java.net.Proxy.Type.HTTP,
                    new java.net.InetSocketAddress(host, port));
        } catch (Exception e) {
            return java.net.Proxy.NO_PROXY;
        }
    }

    static HttpURLConnection openConn(Context ctx, String url) throws Exception {
        java.net.Proxy proxy = systemProxy(ctx);
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection(proxy);
        c.setConnectTimeout(12000);
        c.setReadTimeout(12000);
        return c;
    }

    @Override
    public void onUpdate(Context context, AppWidgetManager manager, int[] ids) {
        for (int id : ids) {
            refresh(context, manager, id);
        }
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        super.onReceive(context, intent);
        // Tap on ⟳ : refresh every instance now (no 30-min wait).
        if (ACTION_REFRESH.equals(intent != null ? intent.getAction() : null)) {
            try {
                AppWidgetManager manager = AppWidgetManager.getInstance(context);
                android.content.ComponentName self =
                        new android.content.ComponentName(context, WeatherWidget.class);
                for (int id : manager.getAppWidgetIds(self)) {
                    refresh(context, manager, id);
                }
            } catch (Exception ignored) {}
        }
    }

    static void refresh(Context context, AppWidgetManager manager, int appWidgetId) {
        SharedPreferences prefs = context.getSharedPreferences("CapacitorStorage", Context.MODE_PRIVATE);
        double lat, lon;
        String name;
        try {
            lat = Double.parseDouble(prefs.getString("sw_widget_lat", "48.8566"));
            lon = Double.parseDouble(prefs.getString("sw_widget_lon", "2.3522"));
            name = prefs.getString("sw_widget_name", "Paris");
        } catch (Exception e) {
            lat = 48.8566; lon = 2.3522; name = "Paris";
        }
        final double fLat = lat, fLon = lon;
        final String fName = name;
        // Tap = open the app (and trigger a refresh of this widget).
        Intent open = new Intent(context, MainActivity.class);
        open.setAction("com.sureweather.app.OPEN_FROM_WIDGET");
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
        PendingIntent tap = PendingIntent.getActivity(context, appWidgetId, open, flags);
        Intent doRefresh = new Intent(context, WeatherWidget.class);
        doRefresh.setAction(ACTION_REFRESH);
        PendingIntent tapRefresh = PendingIntent.getBroadcast(context, appWidgetId, doRefresh, flags);
        RemoteViews views = new RemoteViews(context.getPackageName(), R.layout.widget_weather);
        views.setOnClickPendingIntent(R.id.widget_root, tap);
        views.setOnClickPendingIntent(R.id.widget_refresh, tapRefresh);
        views.setTextViewText(R.id.widget_city, fName);
        manager.updateAppWidget(appWidgetId, views);

        POOL.execute(() -> {
            String temp = "--", icon = "\u2601\uFE0F", desc = "";
            String minStart = null; // "14:35" of next rain at 15-min precision
            String[] hTime = new String[0];
            double[] hTemp = new double[0];
            int[] hCode = new int[0];
            double[] hProb = new double[0];
            double[] hWind = new double[0];
            int[] hWdir = new int[0];
            double[] hPress = new double[0];
            try {
                android.util.Log.d(TAG, "refresh lat=" + fLat + " lon=" + fLon);
                HttpURLConnection c = openConn(context,
                        "https://api.open-meteo.com/v1/forecast?latitude=" + fLat
                        + "&longitude=" + fLon
                        + "&current=temperature_2m,weather_code&hourly=temperature_2m,weather_code,precipitation_probability,wind_speed_10m,wind_direction_10m,pressure_msl"
                        + "&minutely_15=precipitation&wind_speed_unit=ms&timezone=auto&forecast_days=2");
                try {
                    if (c.getResponseCode() == 200) {
                        BufferedReader br = new BufferedReader(new InputStreamReader(c.getInputStream()));
                        StringBuilder sb = new StringBuilder();
                        String line;
                        while ((line = br.readLine()) != null) sb.append(line);
                        br.close();
                        JSONObject root = new JSONObject(sb.toString());
                        JSONObject cur = root.getJSONObject("current");
                        double t = cur.getDouble("temperature_2m");
                        int code = cur.optInt("weather_code", 3);
                        temp = String.valueOf(Math.round(t)) + "\u00B0";
                        String[] cond = conditionFor(code);
                        icon = cond[0];
                        desc = cond[1];
                        // 15-min rain start (best single-model series available).
                        try {
                            JSONObject m15 = root.optJSONObject("minutely_15");
                            if (m15 != null) {
                                JSONArray mt = m15.optJSONArray("time");
                                JSONArray mp = null;
                                if (m15.has("precipitation")) mp = m15.optJSONArray("precipitation");
                                else {
                                    java.util.Iterator<String> ks = m15.keys();
                                    while (ks.hasNext()) {
                                        String k = ks.next();
                                        if (k.startsWith("precipitation_")) { mp = m15.optJSONArray(k); break; }
                                    }
                                }
                                if (mt != null && mp != null) {
                                    long nowMs = System.currentTimeMillis();
                                    // timezone=auto returns location-local ISO (no
                                    // suffix): parse/display in device tz (exact
                                    // when both match, the widget's case).
                                    java.text.SimpleDateFormat inFmt =
                                            new java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm", Locale.US);
                                    for (int i = 0; i < Math.min(mt.length(), 48); i++) {
                                        try {
                                            long qt = inFmt.parse(mt.optString(i, "")).getTime();
                                            if (qt < nowMs - 15 * 60 * 1000) continue;
                                            if (mp.optDouble(i, 0) > 0.15) {
                                                java.text.SimpleDateFormat hf =
                                                        new java.text.SimpleDateFormat("HH:mm", Locale.getDefault());
                                                minStart = hf.format(new Date(qt));
                                                break;
                                            }
                                        } catch (Exception ignored) {}
                                    }
                                }
                            }
                        } catch (Exception ignored) {}
                        JSONObject hourly = root.optJSONObject("hourly");
                        if (hourly != null) {
                            JSONArray times = hourly.optJSONArray("time");
                            JSONArray temps = hourly.optJSONArray("temperature_2m");
                            JSONArray codes = hourly.optJSONArray("weather_code");
                            JSONArray probs = hourly.optJSONArray("precipitation_probability");
                            JSONArray winds = hourly.optJSONArray("wind_speed_10m");
                            JSONArray wdirs = hourly.optJSONArray("wind_direction_10m");
                            JSONArray press = hourly.optJSONArray("pressure_msl");
                            String nowPrefix = cur.optString("time", "").substring(0, Math.min(13, cur.optString("time", "").length()));
                            int start = 0;
                            if (times != null) {
                                for (int i = 0; i < times.length(); i++) {
                                    if (times.optString(i, "").startsWith(nowPrefix)) { start = i; break; }
                                    start = i;
                                }
                                int count = Math.min(12, times.length() - start);
                                hTime = new String[count];
                                hTemp = new double[count];
                                hCode = new int[count];
                                hProb = new double[count];
                                hWind = new double[count];
                                hWdir = new int[count];
                                hPress = new double[count];
                                for (int i = 0; i < count; i++) {
                                    hTime[i] = times.optString(start + i, "");
                                    hTemp[i] = temps != null ? temps.optDouble(start + i, Double.NaN) : Double.NaN;
                                    hCode[i] = codes != null ? codes.optInt(start + i, 3) : 3;
                                    hProb[i] = probs != null ? probs.optDouble(start + i, 0) : 0;
                                    hWind[i] = winds != null ? winds.optDouble(start + i, Double.NaN) : Double.NaN;
                                    hWdir[i] = wdirs != null ? wdirs.optInt(start + i, -1) : -1;
                                    hPress[i] = press != null ? press.optDouble(start + i, Double.NaN) : Double.NaN;
                                }
                            }
                        }
                    }
                } finally {
                    c.disconnect();
                }
            } catch (Exception e) {
                android.util.Log.w(TAG, "forecast fetch failed: " + e);
                // Keep previous values (or placeholders on first run).
            }
            String when;
            try {
                Date now = new Date();
                String hm = new SimpleDateFormat("HH:mm", Locale.getDefault()).format(now);
                String day = new SimpleDateFormat("EEE d MMM", Locale.FRENCH).format(now);
                when = day + " · " + hm;
            } catch (Exception e) {
                when = "";
            }
            RemoteViews v = new RemoteViews(context.getPackageName(), R.layout.widget_weather);
            v.setOnClickPendingIntent(R.id.widget_root, tap);
            v.setOnClickPendingIntent(R.id.widget_refresh, tapRefresh);
            v.setTextViewText(R.id.widget_city, fName);
            v.setTextViewText(R.id.widget_temp, temp);
            v.setTextViewText(R.id.widget_icon, icon);
            v.setTextViewText(R.id.widget_desc, desc);
            v.setTextViewText(R.id.widget_when, when);
            // +1h … +6h strip (fixed slots, hidden when missing).
            int[] idsTime = {R.id.h0_time, R.id.h1_time, R.id.h2_time, R.id.h3_time, R.id.h4_time, R.id.h5_time};
            int[] idsIcon = {R.id.h0_icon, R.id.h1_icon, R.id.h2_icon, R.id.h3_icon, R.id.h4_icon, R.id.h5_icon};
            int[] idsTemp = {R.id.h0_temp, R.id.h1_temp, R.id.h2_temp, R.id.h3_temp, R.id.h4_temp, R.id.h5_temp};
            int[] idsRain = {R.id.h0_rain, R.id.h1_rain, R.id.h2_rain, R.id.h3_rain, R.id.h4_rain, R.id.h5_rain};
            int[] idsWind = {R.id.h0_wind, R.id.h1_wind, R.id.h2_wind, R.id.h3_wind, R.id.h4_wind, R.id.h5_wind};
            int[] idsPress = {R.id.h0_press, R.id.h1_press, R.id.h2_press, R.id.h3_press, R.id.h4_press, R.id.h5_press};
            int[] idsBox = {R.id.h0, R.id.h1, R.id.h2, R.id.h3, R.id.h4, R.id.h5};
            String[] arrows = {"\u2191", "\u2197", "\u2192", "\u2198", "\u2193", "\u2199", "\u2190", "\u2196"};
            for (int i = 0; i < 6; i++) {
                int h = i + 1;
                if (h < hTime.length && hTime[h] != null && hTime[h].length() >= 13 && !Double.isNaN(hTemp[h])) {
                    v.setViewVisibility(idsBox[i], View.VISIBLE);
                    v.setTextViewText(idsTime[i], hTime[h].substring(11, 13) + "h");
                    v.setTextViewText(idsIcon[i], conditionFor(hCode[h])[0]);
                    v.setTextViewText(idsTemp[i], String.valueOf(Math.round(hTemp[h])) + "\u00B0");
                    v.setTextViewText(idsRain[i], h < hProb.length ? "\uD83D\uDCA7" + Math.round(hProb[h]) + "%" : "");
                    String wTxt = "";
                    if (h < hWind.length && !Double.isNaN(hWind[h])) {
                        String ar = "";
                        if (h < hWdir.length && hWdir[h] >= 0) {
                            ar = arrows[((int) Math.round((((hWdir[h] + 180) % 360) / 45.0))) % 8];
                        }
                        wTxt = ar + Math.round(hWind[h] * 3.6);
                    }
                    v.setTextViewText(idsWind[i], wTxt);
                    v.setTextViewText(idsPress[i], h < hPress.length && !Double.isNaN(hPress[h]) ? String.valueOf(Math.round(hPress[h])) : "");
                } else {
                    v.setViewVisibility(idsBox[i], View.GONE);
                }
            }
            // Next rain sentence: 15-min precision when available
            // ("Pluie 14:35"), else hourly fallback ("Pluie ~15h" / "Sec 12h").
            try {
                String rainTxt = "";
                if (minStart != null) {
                    rainTxt = "Pluie " + minStart;
                } else if (hTime.length > 1) {
                    rainTxt = "Sec 12h";
                    for (int i = 1; i < Math.min(hTime.length, 12); i++) {
                        if (i < hProb.length && hProb[i] >= 50 && hTime[i] != null && hTime[i].length() >= 13) {
                            rainTxt = "Pluie ~" + hTime[i].substring(11, 13) + "h";
                            break;
                        }
                    }
                }
                v.setTextViewText(R.id.widget_rain_when, rainTxt);
            } catch (Exception ignored) {}
            // Mini rain MAP (RainViewer tiles around the place). Falls back
            // to the probability bars when the tile service is unreachable.
            try {
                Bitmap map = rainMap(context, fLat, fLon);
                if (map != null) v.setImageViewBitmap(R.id.widget_rain, map);
                else v.setImageViewBitmap(R.id.widget_rain, rainBitmap(hProb, hTime));
            } catch (Exception e) {
                try { v.setImageViewBitmap(R.id.widget_rain, rainBitmap(hProb, hTime)); } catch (Exception ignored) {}
            }
            try {
                manager.updateAppWidget(appWidgetId, v);
                android.util.Log.d(TAG, "updated id=" + appWidgetId + " temp=" + temp);
            } catch (Exception e) {
                android.util.Log.w(TAG, "updateAppWidget failed: " + e);
            }
        });
    }

    /** Mini rain map: 2×2 RainViewer tiles around the place, cropped square
     * centered on it. Zooms to z8 and cycles through recent frames (one per
     * refresh slot) with the frame time stamped on the bitmap — a slow
     * animation across updates. Returns null when unreachable. */
    static Bitmap rainMap(Context ctx, double lat, double lon) {
        HttpURLConnection c = null;
        try {
            // Latest radar frame index.
            c = openConn(ctx, "https://api.rainviewer.com/public/weather-maps.json");
            String idx = readAll(c);
            if (idx == null) return null;
            JSONObject root = new JSONObject(idx);
            String host = root.optString("host", "");
            JSONArray past = root.optJSONObject("radar") != null
                    ? root.optJSONObject("radar").optJSONArray("past") : null;
            if (host.isEmpty() || past == null || past.length() == 0) return null;
            // Animation: a different recent frame every 30-min refresh slot.
            int slot = (int) ((System.currentTimeMillis() / 1800000) % past.length());
            JSONObject frame = past.getJSONObject(slot);
            String path = frame.optString("path", "");
            long frameTime = frame.optLong("time", 0) * 1000;
            if (path.isEmpty()) return null;
            // Slippy tiles at z9 around the place (2×2 stitched ≈ 100 km,
            // center crop ≈ 60 km). RainViewer only serves radar up to z7:
            // those are upsampled ×4 over the sharp z9 street map, exactly
            // like the web app does (the radar's true resolution is coarse).
            int z = 9;
            double n = Math.pow(2, z);
            double fx = (lon + 180.0) / 360.0 * n;
            double latR = Math.toRadians(lat);
            double fy = (1.0 - Math.log(Math.tan(latR) + 1.0 / Math.cos(latR)) / Math.PI) / 2.0 * n;
            int x0 = (int) Math.floor(fx - 0.5);
            int y0 = (int) Math.floor(fy - 0.5);
            Bitmap stitched = Bitmap.createBitmap(512, 512, Bitmap.Config.ARGB_8888);
            Canvas cv = new Canvas(stitched);
            Paint paint = new Paint();
            for (int dx = 0; dx < 2; dx++) {
                for (int dy = 0; dy < 2; dy++) {
                    // Base map first (streets/coastlines at full z9 detail).
                    Bitmap base = fetchBitmap(ctx,
                            "https://tile.openstreetmap.org/" + z + "/" + (x0 + dx) + "/" + (y0 + dy) + ".png",
                            "SureWeatherWidget/1.0 (contact: widget)");
                    if (base != null) {
                        cv.drawBitmap(base, dx * 256, dy * 256, paint);
                        base.recycle();
                    }
                }
            }
            // Radar echoes upsampled from z7 over the sharp map. RainViewer
            // tiles are transparent outside precipitation.
            Bitmap radar = rainRadarUpsampled(ctx, host, path, x0, y0);
            if (radar != null) {
                Paint smooth = new Paint();
                smooth.setFilterBitmap(true);
                cv.drawBitmap(radar, 0, 0, smooth);
                radar.recycle();
            }
            // Centered 320×320 crop on the exact place.
            int px = (int) ((fx - x0) * 256);
            int py = (int) ((fy - y0) * 256);
            int left = Math.max(0, Math.min(512 - 320, px - 160));
            int top = Math.max(0, Math.min(512 - 320, py - 160));
            Bitmap crop = Bitmap.createBitmap(stitched, left, top, 320, 320);
            stitched.recycle();
            // Live frame timestamp stamped on the map ("Radar 14:35").
            if (frameTime > 0) {
                try {
                    Canvas cc = new Canvas(crop);
                    Paint tp = new Paint();
                    tp.setColor(0xFFFFFFFF);
                    tp.setTextSize(30f);
                    tp.setShadowLayer(4f, 2f, 2f, 0xCC000000);
                    String label;
                    try {
                        java.text.SimpleDateFormat hf =
                                new java.text.SimpleDateFormat("HH:mm", java.util.Locale.getDefault());
                        label = "Radar " + hf.format(new Date(frameTime));
                    } catch (Exception e) {
                        label = "";
                    }
                    cc.drawText(label, 12f, 34f, tp);
                } catch (Exception ignored) {}
            }
            return crop;
        } catch (Exception e) {
            return null;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** Radar layer for a z9 2×2 zone, sampled at z7 (the finest the radar
     * serves) and upscaled ×4 with bilinear filtering. 1 px at z7 = 4 px
     * at z9; the wanted 512 px zone = a 128 px window in z7 pixels. */
    static Bitmap rainRadarUpsampled(Context ctx, String host, String path, int x0, int y0) {
        try {
            int xa = x0 / 4, xb = (x0 + 1) / 4;
            int ya = y0 / 4, yb = (y0 + 1) / 4;
            int nx = xb - xa + 1, ny = yb - ya + 1;
            Bitmap sheet = Bitmap.createBitmap(nx * 256, ny * 256, Bitmap.Config.ARGB_8888);
            Canvas cv = new Canvas(sheet);
            Paint paint = new Paint();
            for (int tx = xa; tx <= xb; tx++) {
                for (int ty = ya; ty <= yb; ty++) {
                    Bitmap tile = fetchBitmap(ctx,
                            host + path + "/256/7/" + tx + "/" + ty + "/2/1_1_0.png");
                    if (tile == null) { sheet.recycle(); return null; }
                    cv.drawBitmap(tile, (tx - xa) * 256, (ty - ya) * 256, paint);
                    tile.recycle();
                }
            }
            int cx = x0 * 64 - xa * 256;
            int cy = y0 * 64 - ya * 256;
            cx = Math.max(0, Math.min(sheet.getWidth() - 128, cx));
            cy = Math.max(0, Math.min(sheet.getHeight() - 128, cy));
            Bitmap window = Bitmap.createBitmap(sheet, cx, cy, 128, 128);
            sheet.recycle();
            Bitmap up = Bitmap.createScaledBitmap(window, 512, 512, true);
            window.recycle();
            return up;
        } catch (Exception e) {
            return null;
        }
    }

    static String readAll(HttpURLConnection c) {
        try {
            if (c.getResponseCode() != 200) return null;
            BufferedReader br = new BufferedReader(new InputStreamReader(c.getInputStream()));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = br.readLine()) != null) sb.append(line);
            br.close();
            return sb.toString();
        } catch (Exception e) {
            return null;
        }
    }

    static Bitmap fetchBitmap(Context ctx, String url) {
        return fetchBitmap(ctx, url, "SureWeatherWidget/1.0");
    }

    static Bitmap fetchBitmap(Context ctx, String url, String userAgent) {
        HttpURLConnection c = null;
        try {
            c = openConn(ctx, url);
            // OpenStreetMap tiles require a valid User-Agent (usage policy).
            c.setRequestProperty("User-Agent", userAgent);
            if (c.getResponseCode() != 200) return null;
            return android.graphics.BitmapFactory.decodeStream(c.getInputStream());
        } catch (Exception e) {
            return null;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** 12-bar precipitation-probability chart drawn on a Bitmap. */
    static Bitmap rainBitmap(double[] probs, String[] times) {
        int W = 360, H = 92;
        Bitmap bmp = Bitmap.createBitmap(W, H, Bitmap.Config.ARGB_8888);
        Canvas c = new Canvas(bmp);
        Paint bar = new Paint();
        bar.setColor(0xFF60A5FA);
        Paint barHi = new Paint();
        barHi.setColor(0xFF2563EB);
        Paint axis = new Paint();
        axis.setColor(0x44FFFFFF);
        Paint txt = new Paint();
        txt.setColor(0xFF9FB3CC);
        txt.setTextSize(20f);
        int n = Math.max(probs != null ? probs.length : 0, 1);
        float slot = (float) W / 12;
        for (int i = 0; i < 12; i++) {
            double p = (i < probs.length) ? Math.max(0, Math.min(100, probs[i])) : 0;
            float h = (float) (p / 100.0 * 58);
            float left = i * slot + 3;
            float right = (i + 1) * slot - 3;
            c.drawRect(left, 66 - h, right, 66, p >= 50 ? barHi : bar);
            if (times != null && i < times.length && times[i] != null && times[i].length() >= 13 && i % 3 == 0) {
                c.drawText(times[i].substring(11, 13) + "h", left, 88, txt);
            }
        }
        c.drawLine(0, 66, W, 66, axis);
        return bmp;
    }

    /** WMO weather-code → {emoji, short French label}. */
    static String[] conditionFor(int code) {
        if (code == 0) return new String[]{"\u2600\uFE0F", "D\u00E9gag\u00E9"};
        if (code == 1) return new String[]{"\uD83C\uDF24\uFE0F", "Peu nuageux"};
        if (code == 2) return new String[]{"\u26C5", "Partiellement nuageux"};
        if (code == 3) return new String[]{"\u2601\uFE0F", "Couvert"};
        if (code == 45 || code == 48) return new String[]{"\uD83C\uDF2B\uFE0F", "Brouillard"};
        if (code >= 51 && code <= 57) return new String[]{"\uD83C\uDF26\uFE0F", "Bruine"};
        if (code == 56 || code == 57) return new String[]{"\uD83C\uDF27\uFE0F", "Bruine vergla\u00E7ante"};
        if ((code >= 61 && code <= 67) || (code >= 80 && code <= 82)) return new String[]{"\uD83C\uDF27\uFE0F", "Pluie"};
        if ((code >= 71 && code <= 77) || code == 85 || code == 86) return new String[]{"\u2744\uFE0F", "Neige"};
        if (code == 95) return new String[]{"\u26C8\uFE0F", "Orage"};
        if (code == 96 || code == 99) return new String[]{"\u26C8\uFE0F", "Orage gr\u00EAle"};
        return new String[]{"\u2601\uFE0F", ""};
    }
}
