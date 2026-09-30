package com.sureweather.app;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.widget.RemoteViews;

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

/**
 * Home-screen widget: current temperature + condition for the last place
 * viewed in the app (saved by the web UI via Capacitor Preferences into the
 * "CapacitorStorage" prefs file). Refreshes every 30 min (updatePeriodMillis)
 * and on every tap. Fully offline-tolerant: keeps the last values on error.
 */
public class WeatherWidget extends AppWidgetProvider {

    private static final ExecutorService POOL = Executors.newSingleThreadExecutor();

    @Override
    public void onUpdate(Context context, AppWidgetManager manager, int[] ids) {
        for (int id : ids) {
            refresh(context, manager, id);
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
        RemoteViews views = new RemoteViews(context.getPackageName(), R.layout.widget_weather);
        views.setOnClickPendingIntent(R.id.widget_root, tap);
        views.setTextViewText(R.id.widget_city, fName);
        manager.updateAppWidget(appWidgetId, views);

        POOL.execute(() -> {
            String temp = "--", icon = "\u2601\uFE0F", desc = "";
            try {
                URL url = new URL("https://api.open-meteo.com/v1/forecast?latitude=" + fLat
                        + "&longitude=" + fLon + "&current=temperature_2m,weather_code&timezone=auto&forecast_days=1");
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
                        JSONObject cur = new JSONObject(sb.toString()).getJSONObject("current");
                        double t = cur.getDouble("temperature_2m");
                        int code = cur.optInt("weather_code", 3);
                        temp = String.valueOf(Math.round(t)) + "\u00B0";
                        String[] cond = conditionFor(code);
                        icon = cond[0];
                        desc = cond[1];
                    }
                } finally {
                    c.disconnect();
                }
            } catch (Exception e) {
                // Keep previous values (or placeholders on first run).
            }
            String when;
            try {
                when = new SimpleDateFormat("HH:mm", Locale.getDefault()).format(new Date());
            } catch (Exception e) {
                when = "";
            }
            RemoteViews v = new RemoteViews(context.getPackageName(), R.layout.widget_weather);
            v.setOnClickPendingIntent(R.id.widget_root, tap);
            v.setTextViewText(R.id.widget_city, fName);
            v.setTextViewText(R.id.widget_temp, temp);
            v.setTextViewText(R.id.widget_icon, icon);
            v.setTextViewText(R.id.widget_desc, desc);
            v.setTextViewText(R.id.widget_when, when);
            try {
                manager.updateAppWidget(appWidgetId, v);
            } catch (Exception ignored) {}
        });
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
