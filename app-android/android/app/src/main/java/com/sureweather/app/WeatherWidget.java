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
            String[] hTime = new String[0];
            double[] hTemp = new double[0];
            int[] hCode = new int[0];
            double[] hProb = new double[0];
            try {
                URL url = new URL("https://api.open-meteo.com/v1/forecast?latitude=" + fLat
                        + "&longitude=" + fLon
                        + "&current=temperature_2m,weather_code&hourly=temperature_2m,weather_code,precipitation_probability"
                        + "&timezone=auto&forecast_days=2");
                HttpURLConnection c = (HttpURLConnection) url.openConnection();
                c.setConnectTimeout(12000);
                c.setReadTimeout(12000);
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
                        JSONObject hourly = root.optJSONObject("hourly");
                        if (hourly != null) {
                            JSONArray times = hourly.optJSONArray("time");
                            JSONArray temps = hourly.optJSONArray("temperature_2m");
                            JSONArray codes = hourly.optJSONArray("weather_code");
                            JSONArray probs = hourly.optJSONArray("precipitation_probability");
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
                                for (int i = 0; i < count; i++) {
                                    hTime[i] = times.optString(start + i, "");
                                    hTemp[i] = temps != null ? temps.optDouble(start + i, Double.NaN) : Double.NaN;
                                    hCode[i] = codes != null ? codes.optInt(start + i, 3) : 3;
                                    hProb[i] = probs != null ? probs.optDouble(start + i, 0) : 0;
                                }
                            }
                        }
                    }
                } finally {
                    c.disconnect();
                }
            } catch (Exception e) {
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
            int[] idsBox = {R.id.h0, R.id.h1, R.id.h2, R.id.h3, R.id.h4, R.id.h5};
            for (int i = 0; i < 6; i++) {
                int h = i + 1;
                if (h < hTime.length && hTime[h] != null && hTime[h].length() >= 13 && !Double.isNaN(hTemp[h])) {
                    v.setViewVisibility(idsBox[i], View.VISIBLE);
                    v.setTextViewText(idsTime[i], hTime[h].substring(11, 13) + "h");
                    v.setTextViewText(idsIcon[i], conditionFor(hCode[h])[0]);
                    v.setTextViewText(idsTemp[i], String.valueOf(Math.round(hTemp[h])) + "\u00B0");
                } else {
                    v.setViewVisibility(idsBox[i], View.GONE);
                }
            }
            // Next rain sentence ("Pluie ~15h" / "Sec 12h").
            try {
                String rainTxt = "";
                if (hTime.length > 1) {
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
                Bitmap map = rainMap(fLat, fLon);
                if (map != null) v.setImageViewBitmap(R.id.widget_rain, map);
                else v.setImageViewBitmap(R.id.widget_rain, rainBitmap(hProb, hTime));
            } catch (Exception e) {
                try { v.setImageViewBitmap(R.id.widget_rain, rainBitmap(hProb, hTime)); } catch (Exception ignored) {}
            }
            try {
                manager.updateAppWidget(appWidgetId, v);
            } catch (Exception ignored) {}
        });
    }

    /** Mini rain map: 2×2 RainViewer tiles around the place, cropped square
     * centered on it. Returns null when the tile service is unreachable. */
    static Bitmap rainMap(double lat, double lon) {
        HttpURLConnection c = null;
        try {
            // Latest radar frame index.
            c = (HttpURLConnection) new URL("https://api.rainviewer.com/public/weather-maps.json").openConnection();
            c.setConnectTimeout(10000);
            c.setReadTimeout(10000);
            String idx = readAll(c);
            if (idx == null) return null;
            JSONObject root = new JSONObject(idx);
            String host = root.optString("host", "");
            JSONArray past = root.optJSONObject("radar") != null
                    ? root.optJSONObject("radar").optJSONArray("past") : null;
            if (host.isEmpty() || past == null || past.length() == 0) return null;
            String path = past.getJSONObject(past.length() - 1).optString("path", "");
            if (path.isEmpty()) return null;
            // Slippy tiles at z7 around the place (2×2 stitched, center crop).
            int z = 7;
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
                    Bitmap tile = fetchBitmap(host + path + "/256/" + z + "/" + (x0 + dx) + "/" + (y0 + dy) + "/2/1_1.png");
                    if (tile == null) return null;
                    cv.drawBitmap(tile, dx * 256, dy * 256, paint);
                    tile.recycle();
                }
            }
            // Centered 320×320 crop on the exact place.
            int px = (int) ((fx - x0) * 256);
            int py = (int) ((fy - y0) * 256);
            int left = Math.max(0, Math.min(512 - 320, px - 160));
            int top = Math.max(0, Math.min(512 - 320, py - 160));
            Bitmap crop = Bitmap.createBitmap(stitched, left, top, 320, 320);
            stitched.recycle();
            return crop;
        } catch (Exception e) {
            return null;
        } finally {
            if (c != null) c.disconnect();
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

    static Bitmap fetchBitmap(String url) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setConnectTimeout(10000);
            c.setReadTimeout(10000);
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
