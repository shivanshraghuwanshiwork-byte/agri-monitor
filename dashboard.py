"""
Build a Google Maps-style interactive HTML dashboard and upload to GCS.
- Full-screen satellite map with farm plot polygons
- Draw custom zones on any part of the field
- Zone form: name, crop, sowing date, irrigation date, + custom fields
- Zones saved to localStorage — persist across refreshes
- Click plot/zone → sidebar with full stats in plain English
- Irrigation detection via Sentinel-1 SAR delta
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

GCS_BUCKET   = os.environ.get("GCS_BUCKET", "agri-monitor-dashboard")
MP_BIGHA_SQM = 1333.33


def _polygon_area_sqm(coords):
    lat0 = sum(c[1] for c in coords) / len(coords)
    mpl  = 111_000.0
    mplo = 111_000.0 * math.cos(math.radians(lat0))
    pts  = [(c[0] * mplo, c[1] * mpl) for c in coords]
    n    = len(pts)
    return abs(sum(pts[i][0] * (pts[(i+1)%n][1] - pts[(i-1)%n][1]) for i in range(n))) / 2


def _health_color(ndvi: float) -> str:
    if ndvi < 0.2:  return "#e53935"
    if ndvi < 0.35: return "#fb8c00"
    if ndvi < 0.5:  return "#fdd835"
    return "#43a047"


def _bar_html(value: float, max_val: float) -> str:
    pct = min(100, max(0, (value / max_val) * 100))
    color = "#e53935" if pct < 33 else ("#fb8c00" if pct < 66 else "#43a047")
    return (
        f'<div class="bar-outer">'
        f'<div class="bar-inner" style="width:{pct:.1f}%;background:{color}"></div>'
        f'</div>'
    )


def build_html(farm: dict, stats: dict) -> str:
    w          = stats.get("weather", {})
    date       = stats.get("date", "")
    status     = stats.get("status", "ok")
    crop       = stats.get("crop", "soybean").capitalize()
    name       = farm.get("name", "Farm")
    area_bigha = stats.get("area_bigha", "?")
    generated  = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    ndvi_mean  = stats.get("ndvi_mean", 0) or 0
    ndre_mean  = stats.get("ndre_mean", 0) or 0
    lswi_mean  = stats.get("lswi_mean", 0) or 0
    evi_mean   = stats.get("evi_mean",  0) or 0
    stress_pct = stats.get("stress_pct", 0)

    # Plain-language cards
    crop_health_pct   = min(100, max(0, round(ndvi_mean / 0.8 * 100)))
    crop_health_color = "#888888" if crop_health_pct < 15 else _health_color(ndvi_mean)
    crop_health_no_crop = crop_health_pct < 15
    crop_health_stage = (
        "नंगी ज़मीन · No crop"        if crop_health_pct < 15 else
        "शुरुआती बढ़त · Early growth" if crop_health_pct < 45 else
        "अच्छी बढ़त · Growing well"   if crop_health_pct < 65 else
        "पूरी बढ़त · Peak growth"     if crop_health_pct < 85 else
        "घनी फसल · Dense crop"
    )
    crop_health_display = "—" if crop_health_no_crop else f"{crop_health_pct}%"
    if lswi_mean < 0:
        water_val   = "Drought"; water_sub = "सूखे का खतरा · leaves losing water"; water_color = "#ef5350"
    elif lswi_mean < 0.15:
        water_val   = "Low";     water_sub = "कम नमी · moisture getting low";       water_color = "#ffa726"
    elif lswi_mean < 0.35:
        water_val   = "OK";      water_sub = "ठीक है · leaf moisture normal";       water_color = "#fdd835"
    else:
        water_val   = "Good";    water_sub = "अच्छी नमी · good leaf moisture";      water_color = "#66bb6a"
    water_bar_pct  = min(100, max(0, round((lswi_mean + 0.5) * 100)))
    stress_color   = "#ef5350" if stress_pct > 30 else ("#ffa726" if stress_pct > 10 else "#66bb6a")
    stress_sub     = ("ज़्यादा कमज़ोर · large area struggling" if stress_pct > 30 else
                      ("कुछ हिस्सा कमज़ोर · some patches weak"  if stress_pct > 10 else
                       "खेत ठीक है · field mostly healthy"))
    early_warn_html = ("<div class='mc-tag tag orange' style='margin-top:5px'>⚠️ उभरता तनाव · Early stress</div>"
                       if stats.get("ndre_stress") else "")

    # Growth curve timeseries
    timeseries   = stats.get("ndvi_timeseries", [])
    ts_json      = json.dumps(timeseries)
    heatmap_url  = stats.get("heatmap_url") or ""
    spray        = stats.get("spray_advisory") or {}
    spray_ok     = spray.get("ok", True)
    spray_score  = spray.get("score", 80)
    spray_label  = spray.get("label", "—")
    spray_color  = "#66bb6a" if spray_score >= 75 else ("#ffa726" if spray_score >= 50 else "#ef5350")
    spray_reasons_html = "".join(
        f'<div class="spray-reason">{r}</div>'
        for r in spray.get("reasons", [])
    )

    # Stress diagnosis
    diag          = stats.get("diagnosis") or {}
    diag_cause    = diag.get("cause", "—")
    diag_cause_hi = diag.get("cause_hi", "—")
    diag_action   = diag.get("action", "—")
    diag_action_hi= diag.get("action_hi", "—")
    diag_conf     = diag.get("confidence", "low")
    diag_icon     = diag.get("icon", "⚠️")
    diag_factors_html = "".join(
        f'<div class="diag-factor">{f}</div>'
        for f in diag.get("factors", [])
    )
    diag_conf_color = {"high": "#ef5350", "medium": "#ffa726", "low": "#8a9bb0"}.get(diag_conf, "#8a9bb0")
    diag_conf_label = {"high": "High confidence · पक्का", "medium": "Medium · संभावित", "low": "Low · अनुमान"}.get(diag_conf, "—")
    diag_show = diag.get("cause_key") not in (None, "") and diag_cause != "—"

    if diag_show:
        diag_section_html = f"""
      <div class="sec">
        <div class="sec-title"><span class="sec-title-icon">🔬</span> तनाव का कारण · Stress Diagnosis</div>
        <div class="diag-card">
          <div class="diag-header">
            <div class="diag-icon">{diag_icon}</div>
            <div class="diag-title">
              <div class="diag-cause">{diag_cause}</div>
              <div class="diag-cause-hi">{diag_cause_hi}</div>
              <span class="diag-conf" style="color:{diag_conf_color}">{diag_conf_label}</span>
            </div>
          </div>
          <div class="diag-action-box">
            <div class="diag-action">👉 {diag_action}</div>
            <div class="diag-action-hi">{diag_action_hi}</div>
          </div>
          {diag_factors_html}
        </div>
      </div>"""
    else:
        diag_section_html = ""

    # Spray savings
    sav           = stats.get("spray_savings") or {}
    sav_pct       = sav.get("saving_pct", 0)
    sav_stressed  = sav.get("stressed_bigha", 0)
    sav_saved_bh  = sav.get("saved_bigha", 0)
    sav_cost      = sav.get("saved_cost_inr", 0)
    area_bigha    = stats.get("area_bigha", 0)
    stress_pct_val= stats.get("stress_pct", 0) or 0

    # Crop calendar
    cal         = stats.get("crop_calendar") or {}
    cal_stages  = cal.get("stages", [])
    cal_current = cal.get("current_stage") or {}
    cal_tasks   = cal.get("today_tasks", [])
    cal_upcoming= cal.get("upcoming", [])
    cal_days    = cal.get("days_since_sowing")
    cal_sowing  = cal.get("sowing_date", "—")

    cal_tasks_html = "".join(
        f'<div class="cal-task"><span class="cal-task-dot"></span>{t}</div>'
        for t in cal_tasks
    )
    cal_upcoming_html = "".join(
        f'<div class="cal-upcoming-row"><span class="cal-up-icon">{u["icon"]}</span>'
        f'<span class="cal-up-name">{u["name"]} · {u["name_hi"]}</span>'
        f'<span class="cal-up-when">{"in " + str(u["days_away"]) + "d"} · {u["date"]}</span></div>'
        for u in cal_upcoming
    )
    cal_timeline_html = ""
    for s in cal_stages:
        cls = "cal-tl-current" if s["is_current"] else ("cal-tl-past" if s["is_past"] else "cal-tl-future")
        cal_timeline_html += (
            f'<div class="cal-tl-row {cls}">'
            f'<span class="cal-tl-icon">{s["icon"]}</span>'
            f'<span class="cal-tl-name">{s["name"]} · {s["name_hi"]}</span>'
            f'<span class="cal-tl-date">{s["date_start"]}</span>'
            f'</div>'
        )
    cal_current_name = f'{cal_current.get("icon","")} {cal_current.get("name","")} · {cal_current.get("name_hi","")}' if cal_current else "—"

    # Field events
    field_events     = stats.get("field_events") or []
    _ev_icons        = {"plough":"🚜","level":"🏞️","fym":"💩","basal":"🌿","sowing":"🌱",
                        "irrigation":"💧","weeding":"✂️","topdress":"🧪","other":"📝"}
    _ev_labels       = {"plough":"Ploughing · जुताई","level":"Levelling · लेवलिंग",
                        "fym":"FYM · गोबर खाद","basal":"Basal fertiliser · बेसल खाद",
                        "sowing":"Sowing · बुवाई","irrigation":"Irrigation · सिंचाई",
                        "weeding":"Weeding · निराई","topdress":"Top dressing · टॉप ड्रेसिंग",
                        "other":"Other · अन्य"}
    field_events_html = "".join(
        f'<div class="fev-row">'
        f'<span class="fev-icon">{_ev_icons.get(e.get("type","other"),"📝")}</span>'
        f'<span class="fev-label">{_ev_labels.get(e.get("type","other"),"Other")}</span>'
        f'<span class="fev-date">{e.get("date","")}</span>'
        f'{"<span class=fev-note>" + e["note"] + "</span>" if e.get("note") else ""}'
        f'</div>'
        for e in sorted(field_events, key=lambda x: x.get("date",""), reverse=True)[:8]
    )
    field_events_json = json.dumps(field_events)

    # Disease risk forecast
    disease_risk = stats.get("disease_risk") or []
    disease_html = ""
    for d in disease_risk:
        fa = d.get("alerts", [])
        alerts_html = "".join(f'<div class="dis-alert">{a}</div>' for a in fa[:2])
        disease_html += (
            f'<div class="dis-row">'
            f'<div class="dis-date">{d["date_short"]}</div>'
            f'<div class="dis-badge" style="background:{d["fungal_color"]}22;color:{d["fungal_color"]};border:1px solid {d["fungal_color"]}44">'
            f'🍄 {d["fungal_label"]}</div>'
            f'<div class="dis-badge" style="background:{d["pest_color"]}22;color:{d["pest_color"]};border:1px solid {d["pest_color"]}44">'
            f'🐛 {d["pest_label"]}</div>'
            f'</div>'
            f'{alerts_html if d["fungal_score"]>=35 or d["pest_score"]>=35 else ""}'
        )

    # Per-plot stats
    plot_stats      = stats.get("plot_stats") or []
    plot_stats_json = json.dumps(plot_stats)

    # Spray reduction
    red              = stats.get("spray_reduction") or {}
    red_count        = red.get("spray_count", 0)
    red_sprayed      = red.get("total_sprayed_bigha", 0)
    red_baseline     = red.get("baseline_bigha", 0)
    red_saved_bh     = red.get("saved_bigha", 0)
    red_pct          = red.get("reduction_pct", 0)
    red_cost         = red.get("saved_cost_inr", 0)
    red_events       = red.get("events", [])
    red_events_html  = "".join(
        f'<div class="red-event">'
        f'<span class="red-ev-date">{e.get("date","")}</span>'
        f'<span class="red-ev-area">{e.get("bigha","")} bh</span>'
        f'<span class="red-ev-chem">{e.get("chemical","—")}</span>'
        f'</div>'
        for e in red_events[-5:]
    )
    farm_id_js = farm.get("id", "farm")

    badges = {
        "stress":      "🔴 Stress Detected",
        "no_data":     "☁️ No Data",
        "ok":          "🟢 Healthy",
        "pre_sowing":  "🌾 Awaiting Sowing",
        "germination": "🌱 Germination",
    }
    badge        = badges.get(status, "🟡 Unknown")
    badge_color  = {"stress":"#e53935","no_data":"#fb8c00","ok":"#43a047","pre_sowing":"#7986cb","germination":"#4fc3f7"}.get(status,"#888")

    # Plots GeoJSON
    plots = farm.get("plots", [])
    if not plots:
        coords = farm.get("boundary", {}).get("coordinates", [])
        plots  = [{"id": farm.get("id","farm"), "name": name, "boundary": {"coordinates": coords}}]

    features = []
    for plot in plots:
        coords = plot["boundary"]["coordinates"]
        sqm    = _polygon_area_sqm(coords)
        bigha  = round(sqm / MP_BIGHA_SQM, 1)
        features.append({
            "type": "Feature",
            "properties": {"id": plot["id"], "name": plot["name"], "bigha": bigha, "sqm": round(sqm), "color": _health_color(ndvi_mean), "type": "plot"},
            "geometry":   {"type": "Polygon", "coordinates": [coords]},
        })
    geojson = json.dumps({"type": "FeatureCollection", "features": features})

    all_coords  = [c for p in plots for c in p["boundary"]["coordinates"]]
    center_lat  = sum(c[1] for c in all_coords) / len(all_coords)
    center_lon  = sum(c[0] for c in all_coords) / len(all_coords)

    # Year-over-year
    ndvi_ly = stats.get("ndvi_last_year")
    yoy = "—"
    if ndvi_ly is not None:
        diff  = round(ndvi_mean - ndvi_ly, 3)
        arrow = "▲ better" if diff > 0.02 else ("▼ worse" if diff < -0.02 else "≈ similar")
        color = "#43a047" if diff > 0.02 else ("#e53935" if diff < -0.02 else "#888")
        yoy   = f'{ndvi_ly} <span style="color:{color}">{arrow} ({("+" if diff>=0 else "")}{diff})</span>'

    # 7-day forecast
    forecast_rows = ""
    if w and w.get("forecast_dates"):
        for i, d in enumerate(w["forecast_dates"]):
            label = "Today" if i==0 else ("Tomorrow" if i==1 else datetime.fromisoformat(d).strftime("%a %d %b"))
            r  = (w["forecast_rain"][i]        if i < len(w.get("forecast_rain",[]))        else 0) or 0
            ch = (w["forecast_rain_chance"][i]  if i < len(w.get("forecast_rain_chance",[])) else 0) or 0
            mx = (w["forecast_max_temp"][i]     if i < len(w.get("forecast_max_temp",[]))    else "?")
            icon = "🌧️" if r > 5 else ("🌦️" if ch > 40 else "☀️")
            forecast_rows += f'<tr><td>{icon} {label}</td><td><b>{mx}°C</b></td><td>{r:.1f}mm</td><td>{ch}%</td></tr>'

    # Soil
    sm0  = w.get("soil_moisture_pct_0")
    sm3  = w.get("soil_moisture_pct_3")
    sm27 = w.get("soil_moisture_pct_27")
    st0  = w.get("soil_temp_surface_c")

    def soil_tag(pct):
        if pct is None: return ""
        if pct < 15: return '<span class="tag red">Critically dry</span>'
        if pct < 35: return '<span class="tag orange">Dry</span>'
        if pct < 70: return '<span class="tag green">Good</span>'
        return '<span class="tag blue">Waterlogged</span>'

    soil_html = ""
    if sm0  is not None: soil_html += f'<div class="mrow"><span class="ml">Surface (0–1 cm)<span class="plain">how wet is the topsoil</span></span><div class="mr">{_bar_html(sm0,100)}<b>{sm0}%</b> {soil_tag(sm0)}</div></div>'
    if sm3  is not None: soil_html += f'<div class="mrow"><span class="ml">Root zone (3–9 cm)<span class="plain">where roots drink from</span></span><div class="mr">{_bar_html(sm3,100)}<b>{sm3}%</b> {soil_tag(sm3)}</div></div>'
    if sm27 is not None: soil_html += f'<div class="mrow"><span class="ml">Deep (27–81 cm)<span class="plain">stored water reserve</span></span><div class="mr">{_bar_html(sm27,100)}<b>{sm27}%</b> {soil_tag(sm27)}</div></div>'
    if st0  is not None: soil_html += f'<div class="mrow"><span class="ml">Soil temperature<span class="plain">too hot can burn roots</span></span><b class="mr">{st0}°C {"🔥" if st0>35 else ""}</b></div>'
    sar = stats.get("soil_moisture_label")
    if sar: soil_html += f'<div class="mrow"><span class="ml">Radar moisture (SAR)<span class="plain">satellite radar, works through clouds</span></span><b class="mr">{sar} ({stats.get("soil_vv_db")} dB)</b></div>'

    vpd    = w.get("vpd_kpa")
    et0    = w.get("et0_today_mm")
    vpd_html = f'<div class="mrow"><span class="ml">Air dryness (VPD)<span class="plain">how thirsty the air is — high = crops dry out faster</span></span><div class="mr">{_bar_html(min(vpd,6),6)}<b>{vpd} kPa</b> {"<span class=tag orange>High</span>" if vpd>3 else "<span class=tag green>Normal</span>"}</div></div>' if vpd else ""
    et0_html = f'<div class="mrow"><span class="ml">Water need today (ET₀)<span class="plain">mm of water the crop needs today</span></span><b class="mr">{et0} mm</b></div>' if et0 else ""

    wind     = w.get("wind_kmh","—")
    gusts    = w.get("wind_gusts_kmh","—")
    wind_dir = w.get("wind_dir","")
    temp_c   = w.get("temp_c","—")
    feels    = w.get("feels_like_c","—")
    hum      = w.get("humidity_pct","—")
    cloud    = w.get("cloud_cover_pct","—")
    rain_now = w.get("rain_today_mm","—")
    rain_ch  = w.get("rain_chance_pct","—")
    rain_7d  = w.get("rain_7d_mm","—")

    # Plot pills
    plot_pills = ''.join(
        f'<button class="pill" onclick="selectItem(\'{p["id"]}\',\'plot\')" id="pill-{p["id"]}">{p["name"]}</button>'
        for p in (farm.get("plots") or [])
    )

    # Days since sowing
    days_sowing = stats.get("days_since_sowing")
    sowing_date = farm.get("sowing_date","not set")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{name} — Agri Monitor</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/@turf/turf@6/turf.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0c14;color:#e2e8f0;font-size:14px}}
#app{{display:flex;height:100vh;overflow:hidden}}
#map{{flex:1;height:100%;z-index:1;position:relative;overflow:hidden}}

/* ── Sidebar shell ─────────────────────────────────────────── */
#sidebar{{width:400px;height:100%;background:#0f1117;border-left:1px solid #1e2130;overflow:hidden;display:flex;flex-direction:column;z-index:10;box-shadow:-4px 0 24px #00000044}}
#sb-head{{padding:16px 18px 14px;background:#0a0c14;border-bottom:1px solid #1e2130;flex-shrink:0;border-left:3px solid {badge_color}}}
#sb-body{{padding:14px 16px;flex:1;overflow-y:auto;scrollbar-width:thin;scrollbar-color:#2a2d3a transparent}}
#sb-body::-webkit-scrollbar{{width:4px}}
#sb-body::-webkit-scrollbar-track{{background:transparent}}
#sb-body::-webkit-scrollbar-thumb{{background:#2a2d3a;border-radius:4px}}

.farm-title{{font-size:1.1em;font-weight:700;color:#f0f4ff;letter-spacing:-0.2px}}
.farm-subtitle{{font-size:0.75em;color:#5a6380;margin-top:2px}}
.badge{{display:inline-flex;align-items:center;gap:5px;margin-top:8px;padding:4px 12px;border-radius:20px;font-size:0.78em;font-weight:600;background:{badge_color}18;color:{badge_color};border:1px solid {badge_color}40;letter-spacing:0.2px}}
@keyframes pulse-dot{{0%,100%{{opacity:1}}50%{{opacity:0.4}}}}
.badge-dot{{width:6px;height:6px;border-radius:50%;background:{badge_color};animation:pulse-dot 2s ease-in-out infinite}}
.meta{{margin-top:8px;color:#4a5270;font-size:0.74em;line-height:2}}

/* ── Pills ─────────────────────────────────────────────────── */
.pills{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px}}
.pill{{padding:4px 12px;border-radius:20px;font-size:0.77em;font-weight:600;cursor:pointer;background:#141824;border:1px solid #252836;color:#8890a8;transition:all .18s}}
.pill:hover{{border-color:#4fc3f7;color:#4fc3f7;background:#4fc3f708}}
.pill.active{{background:#4fc3f714;border-color:#4fc3f7;color:#4fc3f7}}
.pill.zone-pill{{border-color:#ab47bc33;color:#ce93d8}}
.pill.zone-pill.active{{background:#ab47bc14;border-color:#ab47bc;color:#ce93d8}}

/* ── Sections ──────────────────────────────────────────────── */
.sec{{margin-bottom:20px}}
.sec-title{{font-size:0.65em;font-weight:700;text-transform:uppercase;letter-spacing:1.2px;color:#4a5270;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid #1a1d2a;display:flex;align-items:center;gap:6px}}
.sec-title-icon{{font-size:1.1em}}

/* ── Metric cards (2×2 grid) ───────────────────────────────── */
.metric-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}}
.metric-card{{background:#141824;border:1px solid #1e2235;border-radius:10px;padding:11px 13px;transition:border-color .2s}}
.metric-card:hover{{border-color:#2a2d4a}}
.mc-label{{font-size:0.7em;font-weight:600;color:#4a5270;text-transform:uppercase;letter-spacing:0.8px;margin-bottom:6px}}
.mc-value{{font-size:1.3em;font-weight:700;color:#e2e8f0;line-height:1;margin-bottom:5px}}
.mc-sub{{font-size:0.71em;color:#5a6380;margin-bottom:6px}}
.mc-bar{{height:4px;background:#1e2235;border-radius:2px;overflow:hidden}}
.mc-bar-fill{{height:100%;border-radius:2px;transition:width .5s ease}}
.mc-tag{{display:inline-block;margin-top:5px;padding:1px 7px;border-radius:8px;font-size:0.68em;font-weight:600}}

/* ── Thin data rows (weather detail, soil, etc.) ───────────── */
.mrow{{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid #141824;gap:8px;flex-wrap:wrap}}
.mrow:last-child{{border-bottom:none}}
.ml{{font-size:0.82em;color:#b0b8d0;flex:1;min-width:120px}}
.plain{{display:block;font-size:0.72em;color:#4a5270;margin-top:1px}}
.mr{{font-size:0.87em;font-weight:700;display:flex;align-items:center;gap:5px;flex-wrap:wrap}}
.bar-outer{{width:65px;height:4px;background:#1e2235;border-radius:2px;display:inline-block;vertical-align:middle;margin-right:5px}}
.bar-inner{{height:100%;border-radius:2px}}

/* ── Tags ──────────────────────────────────────────────────── */
.tag{{display:inline-block;padding:1px 8px;border-radius:8px;font-size:0.7em;font-weight:600}}
.tag.red{{background:#e5393518;color:#ef5350}}.tag.orange{{background:#fb8c0018;color:#ffa726}}
.tag.green{{background:#43a04718;color:#66bb6a}}.tag.blue{{background:#1976d218;color:#4fc3f7}}
.tag.purple{{background:#ab47bc18;color:#ce93d8}}

/* ── Weather tile grid ─────────────────────────────────────── */
.wx-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin-bottom:10px}}
.wx-tile{{background:#141824;border:1px solid #1e2235;border-radius:10px;padding:10px 8px;text-align:center}}
.wx-icon{{font-size:1.4em;margin-bottom:4px}}
.wx-val{{font-size:1.1em;font-weight:700;color:#e2e8f0}}
.wx-lbl{{font-size:0.67em;color:#4a5270;margin-top:2px;text-transform:uppercase;letter-spacing:0.6px}}

/* ── Forecast table ────────────────────────────────────────── */
.fc-table{{width:100%;border-collapse:collapse;font-size:0.79em}}
.fc-table th{{color:#4a5270;font-weight:600;padding:4px 6px;text-align:left;font-size:0.72em;text-transform:uppercase;letter-spacing:0.6px}}
.fc-table td{{padding:6px 6px;border-bottom:1px solid #141824}}
.fc-table tr:last-child td{{border-bottom:none}}
.fc-table tr:hover td{{background:#14182400}}

/* ── Map toolbar ───────────────────────────────────────────── */
#map-toolbar{{position:absolute;top:16px;left:50%;transform:translateX(-50%);z-index:1000;display:flex;gap:6px;background:#0a0c14ee;padding:7px 10px;border-radius:32px;border:1px solid #1e2130;box-shadow:0 4px 24px #00000066;backdrop-filter:blur(8px)}}
.tb-btn{{background:none;border:1.5px solid #252836;color:#6070a0;padding:6px 16px;border-radius:20px;font-size:0.79em;font-weight:600;cursor:pointer;transition:all .18s;white-space:nowrap;letter-spacing:0.2px}}
.tb-btn:hover{{border-color:#4fc3f7;color:#4fc3f7;background:#4fc3f708}}
.tb-btn.active{{background:#4fc3f714;border-color:#4fc3f7;color:#4fc3f7}}
.tb-btn.split-active{{background:#ab47bc14;border-color:#ab47bc;color:#ce93d8}}
.tb-btn.cancel{{border-color:#e5393540;color:#ef5350}}
.tb-btn.cancel:hover{{background:#e5393514}}
#mode-hint{{position:absolute;top:64px;left:50%;transform:translateX(-50%);z-index:1000;background:#0a0c14ee;color:#fdd835;padding:6px 18px;border-radius:16px;font-size:0.79em;font-weight:600;border:1px solid #fdd83540;display:none;white-space:nowrap;backdrop-filter:blur(8px);box-shadow:0 2px 12px #00000055}}

/* ── Zone form (slide-up panel over map) ───────────────────── */
#zone-overlay{{position:absolute;bottom:0;left:0;right:0;z-index:2000;background:#0f1117;border-top:2px solid #4fc3f7;display:flex;flex-direction:column;transform:translateY(105%);transition:transform .28s cubic-bezier(.4,0,.2,1);pointer-events:none;visibility:hidden;height:52vh;min-height:200px;max-height:90vh}}
#zone-overlay.show{{transform:translateY(0);pointer-events:auto;visibility:visible}}
#form-resize-handle{{flex-shrink:0;height:18px;cursor:ns-resize;display:flex;align-items:center;justify-content:center;background:#0a0c14;border-bottom:1px solid #1e2130;user-select:none;-webkit-user-select:none}}
#form-resize-handle::before{{content:'';display:block;width:32px;height:4px;border-radius:2px;background:#2a2d3a}}
#form-resize-handle:hover::before{{background:#4fc3f7}}
.form-header{{padding:10px 18px 10px;background:#0a0c14;border-bottom:1px solid #1e2130;flex-shrink:0;display:flex;justify-content:space-between;align-items:center}}
.form-title{{font-size:0.95em;font-weight:700;color:#4fc3f7}}
.form-body{{padding:16px 18px;overflow-y:auto;flex:1;scrollbar-width:thin;scrollbar-color:#2a2d3a transparent}}
.form-body::-webkit-scrollbar{{width:4px}}
.form-body::-webkit-scrollbar-thumb{{background:#2a2d3a;border-radius:4px}}
.form-footer{{padding:12px 18px;background:#0a0c14;border-top:1px solid #1e2130;flex-shrink:0;display:flex;gap:10px;justify-content:flex-end}}
.form-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:4px}}
.form-group{{display:flex;flex-direction:column;gap:4px}}
.form-group.full{{grid-column:1/-1}}
label{{font-size:0.72em;font-weight:600;color:#4a5270;text-transform:uppercase;letter-spacing:0.6px}}
input,select,textarea{{background:#141824;border:1px solid #252836;color:#e2e8f0;padding:8px 11px;border-radius:8px;font-size:0.84em;font-family:inherit;outline:none;transition:border-color .18s,box-shadow .18s}}
input:focus,select:focus,textarea:focus{{border-color:#4fc3f7;box-shadow:0 0 0 3px #4fc3f714}}
select option{{background:#141824}}
textarea{{resize:vertical;min-height:58px}}
.form-section-label{{grid-column:1/-1;font-size:0.66em;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#4fc3f7;padding-top:12px;border-top:1px solid #1e2130;margin-top:6px}}
.btn-cancel{{background:none;border:1px solid #252836;color:#6070a0;padding:8px 18px;border-radius:8px;cursor:pointer;font-size:0.84em;transition:all .18s}}
.btn-cancel:hover{{border-color:#4a5270;color:#e2e8f0}}
.btn-save{{background:#4fc3f7;color:#060810;border:none;padding:8px 22px;border-radius:8px;font-weight:700;cursor:pointer;font-size:0.84em;transition:background .18s}}
.btn-save:hover{{background:#81d4fa}}

/* ── Zone detail panel ─────────────────────────────────────── */
#zone-detail{{display:none;background:#141824;border-radius:12px;padding:14px;margin-bottom:14px;border:1px solid #ab47bc33}}
#zone-detail.show{{display:block}}
.zone-detail-title{{font-size:0.95em;font-weight:700;color:#ce93d8;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center}}
.zone-kv{{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #1e2130;font-size:0.82em}}
.zone-kv:last-child{{border-bottom:none}}
.zone-k{{color:#4a5270}}.zone-v{{font-weight:600;color:#e2e8f0;text-align:right;max-width:60%}}
.btn-edit-zone{{background:none;border:1px solid #ab47bc33;color:#ce93d8;padding:4px 12px;border-radius:8px;font-size:0.74em;cursor:pointer;margin-top:10px;transition:all .18s}}
.btn-edit-zone:hover{{background:#ab47bc14}}
.btn-delete-zone{{background:none;border:1px solid #e5393530;color:#ef5350;padding:4px 10px;border-radius:8px;font-size:0.74em;cursor:pointer;margin-top:10px;margin-left:6px;transition:all .18s}}
.btn-delete-zone:hover{{background:#e5393514}}

/* ── Growth curve chart ────────────────────────────────────── */
.stage-table{{margin-top:10px;background:#0a0c14;border:1px solid #1e2235;border-radius:10px;padding:10px 12px}}
.stage-title{{font-size:0.7em;font-weight:700;color:#8a9bb0;text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px}}
.stage-row{{display:grid;grid-template-columns:10px 44px 1fr;align-items:center;gap:8px;padding:4px 6px;border-radius:6px;font-size:0.78em;color:#8a9bb0}}
.stage-row-active{{background:#1a1f30;color:#e0e8f0;font-weight:600}}
.stage-dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0}}
.stage-range{{font-size:0.9em;color:#5a6a7a;white-space:nowrap}}
.stage-row-active .stage-range{{color:#8a9bb0}}
.chart-wrap{{background:#0a0c14;border:1px solid #1e2235;border-radius:10px;padding:12px;margin-bottom:4px;position:relative}}
.chart-wrap canvas{{max-height:180px}}
.chart-legend{{display:flex;gap:12px;flex-wrap:wrap;margin-top:8px}}
.chart-leg-item{{display:flex;align-items:center;gap:5px;font-size:0.72em;color:#8890a8}}
.chart-leg-dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0}}
.chart-empty{{color:#4a5270;font-size:0.82em;text-align:center;padding:24px 0}}
.chart-note{{font-size:0.68em;color:#4a5270;margin-top:6px;text-align:right}}

/* ── Crop calendar ─────────────────────────────────────────── */
.cal-current-banner{{background:#0d2010;border:1px solid #2a4a2a;border-radius:10px;padding:10px 12px;margin-bottom:8px}}
.cal-current-name{{font-size:0.9em;font-weight:700;color:#66bb6a;margin-bottom:6px}}
.cal-task{{font-size:0.78em;color:#b0c4b0;padding:3px 0;display:flex;gap:7px;align-items:flex-start;border-bottom:1px solid #141824}}
.cal-task:last-child{{border-bottom:none}}
.cal-task-dot{{width:5px;height:5px;border-radius:50%;background:#66bb6a;flex-shrink:0;margin-top:5px}}
.cal-upcoming-row{{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid #141824;font-size:0.78em}}
.cal-upcoming-row:last-child{{border-bottom:none}}
.cal-up-icon{{font-size:1em;flex-shrink:0}}
.cal-up-name{{flex:1;color:#b0b8d0}}
.cal-up-when{{color:#4fc3f7;font-weight:600;white-space:nowrap;font-size:0.9em}}
.cal-timeline{{background:#0a0c14;border:1px solid #1e2235;border-radius:10px;padding:8px 10px;margin-top:8px}}
.cal-tl-row{{display:grid;grid-template-columns:18px 1fr auto;gap:6px;align-items:center;padding:4px 4px;border-radius:6px;font-size:0.76em}}
.cal-tl-past{{color:#3a4050;opacity:0.6}}
.cal-tl-current{{background:#1a2a1a;color:#66bb6a;font-weight:700}}
.cal-tl-future{{color:#6070a0}}
.cal-tl-icon{{text-align:center}}
.cal-tl-name{{}}
.cal-tl-date{{color:#4a5270;font-size:0.9em;white-space:nowrap}}
.cal-tl-current .cal-tl-date{{color:#66bb6a}}

/* ── Field events ───────────────────────────────────────────── */
.fev-row{{display:grid;grid-template-columns:20px 1fr auto;gap:6px;align-items:center;padding:5px 0;border-bottom:1px solid #0d1020;font-size:0.78em}}
.fev-row:last-child{{border-bottom:none}}
.fev-icon{{font-size:1em;text-align:center}}
.fev-label{{color:#b0b8d0}}
.fev-date{{color:#4fc3f7;font-weight:600;white-space:nowrap;font-size:0.9em}}
.fev-note{{grid-column:2/-1;font-size:0.85em;color:#6070a0;margin-top:-2px}}
.btn-log-event{{width:100%;background:#1a1a2a;border:1.5px dashed #2a2a4a;color:#8a9bb0;border-radius:8px;padding:8px;font-size:0.82em;font-weight:600;cursor:pointer;transition:background .2s;margin-bottom:8px}}
.btn-log-event:hover{{background:#1e1e38;color:#b0b8d0}}

/* ── Field event modal ──────────────────────────────────────── */
#event-modal{{position:fixed;inset:0;z-index:9000;background:#000000bb;display:none;align-items:center;justify-content:center}}
#event-modal.open{{display:flex}}
.event-modal-box{{background:#141824;border:1px solid #252836;border-radius:14px;padding:20px;width:min(360px,90vw);box-shadow:0 8px 32px #00000088}}
.event-type-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin-bottom:12px}}
.event-type-btn{{background:#0a0c14;border:1px solid #1e2235;border-radius:8px;padding:8px 4px;text-align:center;cursor:pointer;transition:all .15s;font-size:0.78em;color:#8a9bb0}}
.event-type-btn:hover{{border-color:#4a5a8a;color:#b0b8d0}}
.event-type-btn.selected{{border-color:#4fc3f7;background:#0d1a2a;color:#4fc3f7;font-weight:700}}
.event-type-icon{{font-size:1.3em;display:block;margin-bottom:3px}}

/* ── Disease risk ───────────────────────────────────────────── */
.dis-row{{display:grid;grid-template-columns:42px 1fr 1fr;gap:5px;align-items:center;padding:4px 0;border-bottom:1px solid #0d1020}}
.dis-row:last-of-type{{border-bottom:none}}
.dis-date{{font-size:0.72em;color:#6070a0;font-weight:600}}
.dis-badge{{font-size:0.68em;font-weight:700;padding:2px 6px;border-radius:10px;text-align:center;white-space:nowrap}}
.dis-alert{{font-size:0.72em;color:#ffa726;padding:2px 0 2px 48px;margin-top:-2px;margin-bottom:2px}}

/* ── Spray reduction tracker ───────────────────────────────── */
.red-hero{{background:linear-gradient(135deg,#0a2010,#0d2a18);border:1px solid #1a4a28;border-radius:12px;padding:14px 16px;margin-bottom:10px;text-align:center}}
.red-hero-pct{{font-size:2.8em;font-weight:900;color:#66bb6a;line-height:1}}
.red-hero-label{{font-size:0.78em;color:#8a9bb0;margin-top:4px}}
.red-stat-row{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:10px}}
.red-stat{{background:#141824;border-radius:8px;padding:7px 8px;text-align:center}}
.red-stat-val{{font-size:0.95em;font-weight:700}}
.red-stat-lbl{{font-size:0.63em;color:#8a9bb0;margin-top:2px;line-height:1.2}}
.red-events-title{{font-size:0.68em;font-weight:700;color:#4a5270;text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px}}
.red-event{{display:grid;grid-template-columns:70px 44px 1fr;gap:6px;align-items:center;padding:4px 0;border-bottom:1px solid #0d1020;font-size:0.76em}}
.red-event:last-child{{border-bottom:none}}
.red-ev-date{{color:#6070a0}}
.red-ev-area{{color:#4fc3f7;font-weight:600}}
.red-ev-chem{{color:#b0b8d0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.btn-log-spray{{width:100%;background:#1a2a1a;border:1.5px dashed #2a5a2a;color:#66bb6a;border-radius:8px;padding:9px;font-size:0.82em;font-weight:600;cursor:pointer;transition:background .2s;margin-bottom:8px}}
.btn-log-spray:hover{{background:#1e3a1e}}

/* ── Log spray modal ────────────────────────────────────────── */
#spray-modal{{position:fixed;inset:0;z-index:9000;background:#000000bb;display:none;align-items:center;justify-content:center}}
#spray-modal.open{{display:flex}}
.spray-modal-box{{background:#141824;border:1px solid #252836;border-radius:14px;padding:20px;width:min(360px,90vw);box-shadow:0 8px 32px #00000088}}
.spray-modal-title{{font-size:1em;font-weight:700;margin-bottom:14px}}
.spray-modal-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}}
.spray-modal-full{{margin-bottom:10px}}
.spray-modal-label{{font-size:0.72em;color:#8a9bb0;margin-bottom:4px}}
.spray-modal-input{{width:100%;background:#0a0c14;border:1px solid #1e2235;border-radius:8px;padding:8px 10px;color:#e0e8f0;font-size:0.85em;box-sizing:border-box}}
.spray-modal-actions{{display:flex;gap:8px;margin-top:6px}}
.btn-spray-save{{flex:1;background:#1a4a1a;border:1px solid #2a6a2a;color:#66bb6a;border-radius:8px;padding:9px;font-weight:700;cursor:pointer}}
.btn-spray-cancel{{background:#1a1f30;border:1px solid #252836;color:#6070a0;border-radius:8px;padding:9px 14px;cursor:pointer}}

/* ── Per-plot stats ─────────────────────────────────────────── */
.plot-stat-card{{background:#0a0c14;border:1px solid #1e2235;border-radius:10px;padding:10px 12px;margin-bottom:6px}}
.plot-stat-name{{font-size:0.78em;font-weight:700;color:#8a9bb0;margin-bottom:6px}}
.plot-stat-grid{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}}
.plot-stat-item{{text-align:center}}
.plot-stat-val{{font-size:1em;font-weight:700}}
.plot-stat-lbl{{font-size:0.62em;color:#4a5270;margin-top:1px}}

/* ── Stress diagnosis ──────────────────────────────────────── */
.diag-card{{background:#0a0c14;border:1px solid #1e2235;border-radius:10px;padding:12px 14px;margin-bottom:8px}}
.diag-header{{display:flex;align-items:flex-start;gap:10px;margin-bottom:8px}}
.diag-icon{{font-size:1.8em;line-height:1;flex-shrink:0}}
.diag-title{{flex:1}}
.diag-cause{{font-size:1em;font-weight:700;color:#e0e8f0;line-height:1.2}}
.diag-cause-hi{{font-size:0.82em;color:#8a9bb0;margin-top:2px}}
.diag-conf{{display:inline-block;font-size:0.68em;font-weight:700;padding:2px 7px;border-radius:10px;background:#1a1f30;margin-top:5px}}
.diag-action-box{{background:#141824;border-left:3px solid #4fc3f7;border-radius:0 8px 8px 0;padding:8px 10px;margin-bottom:8px}}
.diag-action{{font-size:0.82em;font-weight:600;color:#e0e8f0}}
.diag-action-hi{{font-size:0.78em;color:#8a9bb0;margin-top:2px}}
.diag-factor{{font-size:0.76em;color:#8a9bb0;padding:3px 0;border-bottom:1px solid #141824;display:flex;gap:6px;align-items:flex-start}}
.diag-factor:last-child{{border-bottom:none}}
.diag-factor::before{{content:"›";color:#4a5a6a;flex-shrink:0}}

/* ── Spray zone targeting ──────────────────────────────────── */
.spray-target{{background:#0a0c14;border:1px solid #1e2235;border-radius:10px;padding:12px 14px;margin-bottom:8px}}
.spray-target-title{{font-size:0.72em;font-weight:700;color:#8a9bb0;text-transform:uppercase;letter-spacing:.04em;margin-bottom:10px}}
.spray-zones-bar{{height:20px;border-radius:6px;overflow:hidden;display:flex;margin-bottom:8px}}
.spray-zones-bar-stress{{background:#ef5350;display:flex;align-items:center;justify-content:center;font-size:0.65em;font-weight:700;color:#fff;transition:width .4s}}
.spray-zones-bar-ok{{background:#2a3a2a;display:flex;align-items:center;justify-content:center;font-size:0.65em;color:#66bb6a}}
.spray-stat-row{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:8px}}
.spray-stat{{background:#141824;border-radius:8px;padding:7px 8px;text-align:center}}
.spray-stat-val{{font-size:1em;font-weight:700}}
.spray-stat-lbl{{font-size:0.65em;color:#8a9bb0;margin-top:2px;line-height:1.2}}
.spray-saving-banner{{background:linear-gradient(135deg,#0d2a0d,#112211);border:1px solid #2a4a2a;border-radius:8px;padding:8px 10px;text-align:center}}
.spray-saving-big{{font-size:1.4em;font-weight:800;color:#66bb6a}}
.spray-saving-sub{{font-size:0.72em;color:#8a9bb0;margin-top:2px}}
.btn-spray-zones{{width:100%;margin-top:8px;background:#1a2a3a;border:1px solid #2a4a6a;color:#4fc3f7;border-radius:8px;padding:8px;font-size:0.82em;font-weight:600;cursor:pointer;transition:background .2s}}
.btn-spray-zones:hover{{background:#1e3048}}

/* ── Spray advisory ────────────────────────────────────────── */
.spray-score-ring{{display:flex;align-items:center;gap:12px;margin-bottom:10px}}
.spray-ring-num{{font-size:1.8em;font-weight:800;line-height:1}}
.spray-ring-label{{font-size:0.85em;font-weight:700}}
.spray-reason{{font-size:0.79em;color:#b0b8d0;padding:4px 0;border-bottom:1px solid #141824;display:flex;gap:6px}}
.spray-reason:last-child{{border-bottom:none}}

/* ── Heatmap toggle ────────────────────────────────────────── */
#btn-heatmap{{background:none;border:1.5px solid #252836;color:#6070a0;padding:6px 14px;border-radius:20px;font-size:0.79em;font-weight:600;cursor:pointer;transition:all .18s;white-space:nowrap}}
#btn-heatmap:hover{{border-color:#ffa726;color:#ffa726}}
#btn-heatmap.active{{background:#ffa72614;border-color:#ffa726;color:#ffa726}}
#btn-heatmap.unavail{{opacity:0.35;cursor:not-allowed}}
#heatmap-legend{{position:absolute;bottom:90px;left:12px;z-index:1001;background:#0a0c14ee;border:1px solid #1e2130;border-radius:10px;padding:8px 12px;display:none;backdrop-filter:blur(8px)}}
#heatmap-legend.show{{display:block}}

/* ── Irrigation event list ─────────────────────────────────── */
.irr-list{{display:flex;flex-direction:column;gap:8px;margin-top:6px}}
.irr-row{{background:#0f1117;border:1px solid #1e2235;border-radius:10px;padding:10px 12px;display:flex;flex-direction:column;gap:7px;position:relative}}
.irr-row-top{{display:grid;grid-template-columns:1fr 1fr;gap:7px}}
.irr-row-bot{{display:grid;grid-template-columns:1fr 1fr;gap:7px}}
.irr-row-num{{position:absolute;top:-8px;left:12px;font-size:0.65em;font-weight:700;color:#4fc3f7;background:#0a0c14;padding:0 6px;border-radius:4px;letter-spacing:0.5px}}
.irr-row-del{{position:absolute;top:8px;right:10px;background:none;border:none;color:#e5393560;font-size:1em;cursor:pointer;padding:2px 6px;border-radius:6px;line-height:1;transition:color .15s}}
.irr-row-del:hover{{color:#ef5350}}
.btn-add-irr{{display:flex;align-items:center;gap:6px;background:none;border:1.5px dashed #1e2235;color:#4a5270;padding:8px 14px;border-radius:10px;cursor:pointer;font-size:0.82em;font-weight:600;width:100%;justify-content:center;margin-top:4px;transition:all .18s}}
.btn-add-irr:hover{{border-color:#4fc3f7;color:#4fc3f7}}
.irr-summary{{margin-top:8px;background:#141824;border:1px solid #1e2235;border-radius:10px;padding:10px 14px;font-size:0.78em;display:flex;flex-wrap:wrap;gap:10px;align-items:center}}
.irr-sum-chip{{display:inline-flex;align-items:center;gap:4px;padding:2px 10px;border-radius:8px;font-weight:600}}
.irr-sum-chip.blue{{background:#1976d218;color:#4fc3f7;border:1px solid #1976d230}}
.irr-sum-chip.orange{{background:#fb8c0018;color:#ffa726;border:1px solid #fb8c0030}}
.irr-sum-chip.green{{background:#43a04718;color:#66bb6a;border:1px solid #43a04730}}
.irr-sum-chip.red{{background:#e5393518;color:#ef5350;border:1px solid #e5393530}}
/* detail timeline */
.irr-timeline{{display:flex;flex-direction:column;gap:6px;margin-top:4px}}
.irr-tl-row{{display:flex;gap:10px;align-items:flex-start;padding:6px 0;border-bottom:1px solid #1a1d2a;font-size:0.8em}}
.irr-tl-row:last-child{{border-bottom:none}}
.irr-tl-dot{{width:8px;height:8px;border-radius:50%;background:#4fc3f7;flex-shrink:0;margin-top:4px}}
.irr-tl-dot.overlap{{background:#ffa726}}
.irr-tl-body{{flex:1}}
.irr-tl-date{{font-weight:700;color:#e2e8f0}}
.irr-tl-meta{{color:#4a5270;margin-top:2px}}

/* ── Location button & marker ──────────────────────────────── */
#btn-locate{{border:1.5px solid #252836;color:#6070a0;width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;cursor:pointer;transition:all .18s;flex-shrink:0;font-size:1em;position:absolute;bottom:90px;right:12px;z-index:1000;box-shadow:0 2px 10px #00000055;background:#0a0c14ee}}
.leaflet-popup-content-wrapper,.leaflet-popup-tip{{background:#141824;border:1px solid #252836;box-shadow:0 4px 16px #00000066;padding:0}}
.leaflet-popup-content{{margin:0}}
#btn-locate:hover{{border-color:#4fc3f7;color:#4fc3f7}}
#btn-locate.locating{{border-color:#fdd835;color:#fdd835;animation:spin .8s linear infinite}}
#btn-locate.located{{border-color:#66bb6a;color:#66bb6a}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
#loc-toast{{position:absolute;bottom:136px;right:12px;z-index:1001;background:#0a0c14ee;color:#e2e8f0;padding:6px 14px;border-radius:10px;font-size:0.75em;font-weight:600;border:1px solid #1e2130;display:none;white-space:nowrap;backdrop-filter:blur(8px)}}

/* ── Misc ──────────────────────────────────────────────────── */
.irr-badge{{display:inline-block;padding:2px 10px;border-radius:8px;font-size:0.74em;font-weight:600;background:#1976d218;color:#4fc3f7;border:1px solid #1976d230}}
.empty-state{{color:#4a5270;font-size:0.82em;padding:10px 0;text-align:center}}

@keyframes fadeIn{{from{{opacity:0;transform:translateY(4px)}}to{{opacity:1;transform:translateY(0)}}}}
.sec{{animation:fadeIn .3s ease both}}

@media(max-width:700px){{
  #app{{flex-direction:column}}
  #map{{height:52vh}}
  #sidebar{{width:100%;height:48vh;border-left:none;border-top:1px solid #1e2130}}
  .form-grid{{grid-template-columns:1fr}}
  .metric-grid{{grid-template-columns:1fr 1fr}}
  .wx-grid{{grid-template-columns:repeat(3,1fr)}}
}}
</style>
</head>
<body>
<div id="app">
  <div id="map">
    <div id="map-toolbar">
      <button id="btn-click-zone" class="tb-btn active" onclick="setMode('click')">👆 Select Plot</button>
      <button id="btn-draw" class="tb-btn" onclick="setMode('draw')">✏️ Draw Zone</button>
      <button id="btn-grid" class="tb-btn" onclick="setMode('grid')">⊞ Grid Zones</button>
      <button id="btn-cancel-mode" class="tb-btn cancel" onclick="setMode('click')" style="display:none">✕ Cancel</button>
      <button id="btn-heatmap" onclick="toggleHeatmap()" title="Spray zone map">🎯 Spray Zones</button>
    </div>
    <div id="mode-hint"></div>
    <div id="heatmap-legend">
      <div style="font-size:0.72em;font-weight:700;color:#e0e8f0;margin-bottom:6px">Spray Zone Map · छिड़काव नक्शा</div>
      <div style="display:flex;flex-direction:column;gap:4px">
        <div style="display:flex;align-items:center;gap:7px;font-size:0.75em">
          <span style="width:14px;height:14px;background:#ef5350;border-radius:3px;flex-shrink:0;display:inline-block"></span>
          <span style="color:#ef5350;font-weight:600">Spray here · यहाँ करें</span>
        </div>
        <div style="display:flex;align-items:center;gap:7px;font-size:0.75em">
          <span style="width:14px;height:14px;background:#66bb6a;border-radius:3px;flex-shrink:0;display:inline-block"></span>
          <span style="color:#66bb6a;font-weight:600">Skip this · यह छोड़ें</span>
        </div>
      </div>
      <div style="font-size:0.63em;color:#4a5270;margin-top:6px">Sentinel-2 · {date}</div>
    </div>
    <button id="btn-locate" onclick="locateMe()" title="My location · मेरी लोकेशन">📍</button>
    <div id="loc-toast"></div>
    <div id="draw-undo-btn" onclick="drawUndo()" style="display:none;position:absolute;bottom:72px;left:50%;transform:translateX(-50%);z-index:1000;background:#1a1d2eee;color:#e8eaf0;padding:7px 20px;border-radius:20px;font-size:0.8em;font-weight:600;cursor:pointer;box-shadow:0 2px 12px #0009;border:1px solid #3a3d4a;white-space:nowrap">↩ Undo last point</div>
    <div id="draw-confirm-btn" onclick="drawFinish()" style="display:none;position:absolute;bottom:24px;left:50%;transform:translateX(-50%);z-index:1000;background:#4fc3f7;color:#0f1117;padding:10px 28px;border-radius:28px;font-size:0.88em;font-weight:700;cursor:pointer;box-shadow:0 2px 16px #0009;white-space:nowrap;border:none"></div>
    <div id="grid-confirm-btn" onclick="confirmGridSelection()" style="display:none;position:absolute;bottom:24px;left:50%;transform:translateX(-50%);z-index:1000;background:#ff9800;color:#0f1117;padding:10px 28px;border-radius:28px;font-size:0.88em;font-weight:700;cursor:pointer;box-shadow:0 2px 16px #0009;white-space:nowrap;border:none"></div>
  </div>

  <!-- Zone form overlay (shown over map) -->
  <div id="zone-overlay">
    <div id="form-resize-handle"></div>
    <div class="form-header">
      <span class="form-title" id="form-title-text">✏️ Add Zone Details</span>
      <button class="btn-cancel" onclick="cancelZone()" style="padding:4px 10px;font-size:0.8em">✕</button>
    </div>
    <div class="form-body">
    <div class="form-grid">

      <!-- ── Basic info / बेसिक जानकारी ── -->
      <div class="form-section-label">Basic Info · बेसिक जानकारी</div>

      <div class="form-group">
        <label>Zone / Field Name · क्षेत्र का नाम *</label>
        <input id="z-name" type="text" placeholder="e.g. North Section, Khasra 45 / उत्तरी खेत">
      </div>
      <div class="form-group">
        <label>Khasra / Survey No. · खसरा नं.</label>
        <input id="z-khasra" type="text" placeholder="e.g. 123/2">
      </div>
      <div class="form-group">
        <label>Land Type · भूमि प्रकार</label>
        <select id="z-land-type">
          <option value="">— Select / चुनें —</option>
          <option value="Irrigated">Irrigated · सिंचित</option>
          <option value="Rain-fed">Rain-fed · बारानी</option>
          <option value="Upland">Upland · ऊँची ज़मीन</option>
          <option value="Lowland">Lowland / Flood-prone · निचली ज़मीन</option>
          <option value="Bund / Ridge">Bund / Ridge · मेड़ की ज़मीन</option>
        </select>
      </div>
      <div class="form-group">
        <label>Soil Type · मिट्टी का प्रकार</label>
        <select id="z-soil">
          <option value="">— Select / चुनें —</option>
          <option value="Black cotton">Black cotton · काली मिट्टी (Vertisol)</option>
          <option value="Red laterite">Red laterite · लाल मिट्टी</option>
          <option value="Alluvial loam">Alluvial loam · दोमट</option>
          <option value="Sandy loam">Sandy loam · बलुई दोमट</option>
          <option value="Clay">Heavy clay · चिकनी मिट्टी</option>
          <option value="Sandy">Sandy · बलुई</option>
          <option value="Saline / Usar">Saline / Usar · ऊसर</option>
        </select>
      </div>

      <!-- ── Crop / फसल ── -->
      <div class="form-section-label">Crop Details · फसल की जानकारी</div>

      <div class="form-group">
        <label>Crop Sowed · बोई गई फसल</label>
        <select id="z-crop">
          <option value="">— Select crop / फसल चुनें —</option>
          <optgroup label="Kharif · खरीफ">
            <option value="Soybean">Soybean · सोयाबीन</option>
            <option value="Maize">Maize · मक्का</option>
            <option value="Groundnut">Groundnut · मूंगफली</option>
            <option value="Cotton">Cotton · कपास</option>
            <option value="Urad Dal">Urad Dal · उड़द</option>
            <option value="Moong Dal">Moong Dal · मूंग</option>
            <option value="Arhar / Tur">Arhar / Tur Dal · अरहर</option>
            <option value="Sesame">Sesame · तिल</option>
            <option value="Jowar">Jowar · ज्वार</option>
            <option value="Bajra">Bajra · बाजरा</option>
            <option value="Rice">Paddy / Rice · धान</option>
          </optgroup>
          <optgroup label="Rabi · रबी">
            <option value="Wheat">Wheat · गेहूँ</option>
            <option value="Chickpea">Chickpea · चना</option>
            <option value="Mustard">Mustard · सरसों</option>
            <option value="Lentil">Lentil · मसूर</option>
            <option value="Garlic">Garlic · लहसुन</option>
            <option value="Onion">Onion · प्याज</option>
            <option value="Potato">Potato · आलू</option>
          </optgroup>
          <optgroup label="Other · अन्य">
            <option value="Sugarcane">Sugarcane · गन्ना</option>
            <option value="Banana">Banana · केला</option>
            <option value="Vegetable">Vegetables · सब्ज़ी</option>
            <option value="Fallow">Fallow · खाली / परती</option>
            <option value="Other">Other · अन्य</option>
          </optgroup>
        </select>
      </div>
      <div class="form-group">
        <label>Seed Variety · बीज किस्म</label>
        <select id="z-variety">
          <option value="">— Select / चुनें —</option>
          <optgroup label="Soybean · सोयाबीन">
            <option value="JS 9305">JS 9305</option>
            <option value="JS 335">JS 335</option>
            <option value="JS 9560">JS 9560</option>
            <option value="RKS 24">RKS 24</option>
            <option value="MACS 450">MACS 450</option>
            <option value="NRC 7">NRC 7</option>
          </optgroup>
          <optgroup label="Wheat · गेहूँ">
            <option value="GW 322">GW 322</option>
            <option value="HI 8498">HI 8498 (Malav Shakti)</option>
            <option value="MP 3173">MP 3173</option>
            <option value="Raj 4120">Raj 4120</option>
          </optgroup>
          <optgroup label="Maize · मक्का">
            <option value="DKC 9108">DKC 9108</option>
            <option value="NK 6240">NK 6240</option>
            <option value="PAC 740">PAC 740</option>
          </optgroup>
          <option value="Local / Desi">Local / Desi · देसी किस्म</option>
          <option value="Other">Other · अन्य (notes में लिखें)</option>
        </select>
      </div>
      <div class="form-group">
        <label>Sowing Date · बुवाई की तारीख</label>
        <input id="z-sowing" type="date">
      </div>
      <div class="form-group">
        <label>Seed Rate · बीज दर</label>
        <select id="z-seed-rate">
          <option value="">— Select / चुनें —</option>
          <option value="20–25 kg/acre">20–25 kg/acre (Soybean · सोयाबीन)</option>
          <option value="30–35 kg/acre">30–35 kg/acre (Soybean heavy)</option>
          <option value="40–45 kg/acre">40–45 kg/acre (Wheat · गेहूँ)</option>
          <option value="8–10 kg/acre">8–10 kg/acre (Maize · मक्का hybrid)</option>
          <option value="3–4 kg/acre">3–4 kg/acre (Cotton · कपास / Bajra · बाजरा)</option>
          <option value="6–8 kg/acre">6–8 kg/acre (Chickpea · चना / Arhar · अरहर)</option>
          <option value="Other">Other · अन्य</option>
        </select>
      </div>
      <div class="form-group">
        <label>Seed Treatment · बीज उपचार</label>
        <select id="z-seed-treat">
          <option value="">— Select / चुनें —</option>
          <option value="Rhizobium + PSB">Rhizobium + PSB (Soybean · सोयाबीन)</option>
          <option value="Thiram + Carbendazim">Thiram + Carbendazim (फफूंदनाशी)</option>
          <option value="Imidacloprid">Imidacloprid (कीटनाशी बीज लेप)</option>
          <option value="Trichoderma">Trichoderma (जैव फफूंदनाशी)</option>
          <option value="Bavistin">Bavistin (Carbendazim)</option>
          <option value="No treatment">No treatment · उपचार नहीं</option>
          <option value="Other">Other · अन्य</option>
        </select>
      </div>

      <!-- ── Irrigation / सिंचाई ── -->
      <div class="form-section-label" style="grid-column:1/-1">Irrigation Events · सिंचाई का रिकॉर्ड</div>

      <div class="form-group full">
        <div class="irr-list" id="irr-list"></div>
        <button type="button" class="btn-add-irr" onclick="addIrrRow()">＋ Add irrigation event · सिंचाई जोड़ें</button>
        <div class="irr-summary" id="irr-summary" style="display:none"></div>
      </div>

      <!-- ── Inputs applied / खाद व दवाइयाँ ── -->
      <div class="form-section-label">Inputs Applied · खाद एवं दवाइयाँ</div>

      <div class="form-group">
        <label>Base Fertiliser · मूल खाद (बुवाई पर)</label>
        <select id="z-fert-base">
          <option value="">— Select / चुनें —</option>
          <option value="DAP 50 kg/acre">DAP 50 kg/acre</option>
          <option value="DAP 25 kg/acre">DAP 25 kg/acre (आधा डोज़)</option>
          <option value="SSP 100 kg/acre">SSP 100 kg/acre</option>
          <option value="NPK 12:32:16 50kg/acre">NPK 12:32:16 @ 50 kg/acre</option>
          <option value="Urea 25 kg/acre">Urea · यूरिया 25 kg/acre</option>
          <option value="MOP 25 kg/acre">MOP (Potash · पोटाश) 25 kg/acre</option>
          <option value="Zinc sulphate 10 kg/acre">Zinc sulphate · जिंक 10 kg/acre</option>
          <option value="FYM 4 ton/acre">FYM / Compost · गोबर खाद 4 ton/acre</option>
          <option value="No basal">No basal · कोई नहीं</option>
          <option value="Other">Other · अन्य</option>
        </select>
      </div>
      <div class="form-group">
        <label>Top Dressing · टॉप ड्रेसिंग</label>
        <select id="z-fert-top">
          <option value="">— Select / चुनें —</option>
          <option value="Urea 25 kg/acre at 30 days">Urea 25 kg/acre at 30 days</option>
          <option value="Urea 50 kg/acre at 30 days">Urea 50 kg/acre at 30 days</option>
          <option value="NPK 19:19:19 foliar spray">NPK 19:19:19 foliar · पत्ती स्प्रे</option>
          <option value="DAP 2% foliar spray">DAP 2% foliar spray</option>
          <option value="Boron 0.2% foliar">Boron · बोरॉन 0.2% foliar</option>
          <option value="Micronutrient mix foliar">Micronutrient mix · सूक्ष्म पोषक</option>
          <option value="Not applied">Not applied · अभी नहीं</option>
          <option value="Other">Other · अन्य</option>
        </select>
      </div>
      <div class="form-group">
        <label>Herbicide · खरपतवारनाशी</label>
        <select id="z-herbicide">
          <option value="">— Select / चुनें —</option>
          <option value="Imazethapyr (Pursuit)">Imazethapyr / Pursuit (Soybean)</option>
          <option value="Quizalofop (Targa Super)">Quizalofop / Targa Super</option>
          <option value="Pendimethalin (pre-emergence)">Pendimethalin (pre-emergence · बुवाई पहले)</option>
          <option value="Atrazine (Maize)">Atrazine (Maize · मक्का)</option>
          <option value="2,4-D (Wheat)">2,4-D (Wheat · गेहूँ broadleaf)</option>
          <option value="Clodinafop (Topik Wheat)">Clodinafop / Topik (Wheat narrow)</option>
          <option value="Manual weeding">Manual weeding · हाथ से निराई</option>
          <option value="Not applied">Not applied · नहीं डाला</option>
          <option value="Other">Other · अन्य</option>
        </select>
      </div>
      <div class="form-group">
        <label>Insecticide · कीटनाशी</label>
        <select id="z-pesticide">
          <option value="">— Select / चुनें —</option>
          <option value="Chlorpyrifos 20EC">Chlorpyrifos 20EC</option>
          <option value="Lambda-cyhalothrin">Lambda-cyhalothrin (Karate)</option>
          <option value="Imidacloprid (Confidor)">Imidacloprid / Confidor</option>
          <option value="Profenofos + Cypermethrin">Profenofos + Cypermethrin</option>
          <option value="Thiamethoxam (Actara)">Thiamethoxam / Actara</option>
          <option value="Emamectin benzoate">Emamectin benzoate</option>
          <option value="Neem oil spray">Neem oil · नीम तेल (जैविक)</option>
          <option value="Not applied">Not applied · नहीं डाला</option>
          <option value="Other">Other · अन्य</option>
        </select>
      </div>
      <div class="form-group">
        <label>Fungicide · फफूंदनाशी</label>
        <select id="z-fungicide">
          <option value="">— Select / चुनें —</option>
          <option value="Mancozeb (Dithane M-45)">Mancozeb / Dithane M-45</option>
          <option value="Carbendazim + Mancozeb">Carbendazim + Mancozeb</option>
          <option value="Hexaconazole">Hexaconazole</option>
          <option value="Propiconazole (Tilt)">Propiconazole / Tilt</option>
          <option value="Metalaxyl + Mancozeb">Metalaxyl + Mancozeb (Ridomil)</option>
          <option value="Trifloxystrobin">Trifloxystrobin (Nativo)</option>
          <option value="Not applied">Not applied · नहीं डाला</option>
          <option value="Other">Other · अन्य</option>
        </select>
      </div>
      <div class="form-group">
        <label>Previous Season Crop · पिछली फसल</label>
        <select id="z-prev-crop">
          <option value="">— Select / चुनें —</option>
          <option value="Soybean">Soybean · सोयाबीन</option>
          <option value="Wheat">Wheat · गेहूँ</option>
          <option value="Chickpea">Chickpea · चना</option>
          <option value="Mustard">Mustard · सरसों</option>
          <option value="Maize">Maize · मक्का</option>
          <option value="Cotton">Cotton · कपास</option>
          <option value="Fallow">Fallow · खाली / परती</option>
          <option value="Other">Other · अन्य</option>
        </select>
      </div>

      <!-- ── Notes / टिप्पणी ── -->
      <div class="form-section-label">Notes · टिप्पणी</div>

      <div class="form-group full">
        <label>Additional Notes · अन्य जानकारी</label>
        <textarea id="z-notes" placeholder="Any observations · कोई भी जानकारी — pest sighting · कीट, crop damage · नुकसान, flooding · बाढ़, yield estimate · उपज अनुमान..."></textarea>
      </div>

    </div>
    </div><!-- form-body -->

    <div class="form-footer">
      <button class="btn-cancel" onclick="cancelZone()">Cancel · रद्द करें</button>
      <button class="btn-save" onclick="saveZone()">Save Zone · सेव करें</button>
    </div>
  </div>

  <!-- Sidebar -->
  <div id="sidebar">
    <div id="sb-head">
      <div class="farm-title">🛰️ {name}</div>
      <div class="farm-subtitle">{crop} &nbsp;·&nbsp; {area_bigha} bigha &nbsp;·&nbsp; {date}</div>
      <div class="badge"><span class="badge-dot"></span>{badge}</div>
      <div class="meta">🕐 Updated {generated}</div>
    </div>

    <div id="sb-body">
      <!-- Pills -->
      <div class="pills" id="pills">
        <button class="pill active" onclick="selectItem('all','all')" id="pill-all">All</button>
        {plot_pills}
        <span id="zone-pills"></span>
      </div>

      <!-- Zone detail card (shown when a zone is selected) -->
      <div id="zone-detail"></div>

      <!-- Satellite -->
      <div class="sec">
        <div class="sec-title"><span class="sec-title-icon">📡</span> फसल की स्थिति · Crop Status</div>
        <div class="metric-grid" style="grid-template-columns:1fr 1fr 1fr">

          <div class="metric-card">
            <div class="mc-label">फसल स्वास्थ्य<br>Crop Health</div>
            <div class="mc-value" style="color:{crop_health_color}">{crop_health_display}</div>
            <div class="mc-bar"><div class="mc-bar-fill" style="width:{crop_health_pct}%;background:{crop_health_color}"></div></div>
            <div class="mc-sub" style="margin-top:4px">{crop_health_stage}</div>
            {early_warn_html}
          </div>

          <div class="metric-card">
            <div class="mc-label">पानी की स्थिति<br>Water Stress</div>
            <div class="mc-value" style="color:{water_color}">{water_val}</div>
            <div class="mc-bar"><div class="mc-bar-fill" style="width:{water_bar_pct}%;background:{water_color}"></div></div>
            <div class="mc-sub" style="margin-top:4px">{water_sub}</div>
          </div>

          <div class="metric-card">
            <div class="mc-label">कमज़ोर क्षेत्र<br>Stressed Area</div>
            <div class="mc-value" style="color:{stress_color}">{stress_pct}%</div>
            <div class="mc-bar"><div class="mc-bar-fill" style="width:{min(100,stress_pct):.0f}%;background:{stress_color}"></div></div>
            <div class="mc-sub" style="margin-top:4px">{stress_sub}</div>
          </div>

        </div>
        <div class="mrow"><span class="ml">पिछले साल से · vs Last Year<span class="plain">same 10-day window last year</span></span><span class="mr">{yoy}</span></div>
        <div class="mrow"><span class="ml">उपग्रह चित्र · Images used<span class="plain">more = more accurate</span></span><span class="mr">{stats.get("image_count","?")} imgs · {stats.get("cloud_pct","?")}% cloud</span></div>

        <!-- Stage reference table -->
        <div class="stage-table">
          <div class="stage-title">फसल अवस्था गाइड · Crop Stage Guide</div>
          <div class="stage-row {"stage-row-active" if crop_health_no_crop else ""}">
            <span class="stage-dot" style="background:#888"></span>
            <span class="stage-range">—</span>
            <span class="stage-name">नंगी ज़मीन · No crop</span>
          </div>
          <div class="stage-row {"stage-row-active" if 15<=crop_health_pct<45 else ""}">
            <span class="stage-dot" style="background:#e53935"></span>
            <span class="stage-range">1–44%</span>
            <span class="stage-name">शुरुआती बढ़त · Early growth</span>
          </div>
          <div class="stage-row {"stage-row-active" if 45<=crop_health_pct<65 else ""}">
            <span class="stage-dot" style="background:#fdd835"></span>
            <span class="stage-range">45–64%</span>
            <span class="stage-name">अच्छी बढ़त · Growing well</span>
          </div>
          <div class="stage-row {"stage-row-active" if 65<=crop_health_pct<85 else ""}">
            <span class="stage-dot" style="background:#66bb6a"></span>
            <span class="stage-range">65–84%</span>
            <span class="stage-name">पूरी बढ़त · Peak growth</span>
          </div>
          <div class="stage-row {"stage-row-active" if crop_health_pct>=85 else ""}">
            <span class="stage-dot" style="background:#1a9850"></span>
            <span class="stage-range">85–100%</span>
            <span class="stage-name">घनी फसल · Dense crop</span>
          </div>
        </div>
      </div>

      <!-- Weather -->
      <div class="sec">
        <div class="sec-title"><span class="sec-title-icon">🌤️</span> Current Weather</div>
        <div class="wx-grid">
          <div class="wx-tile">
            <div class="wx-icon">🌡️</div>
            <div class="wx-val">{temp_c}°C</div>
            <div class="wx-lbl">Temp (feels {feels}°C){"<br><span class='tag red' style='margin-top:3px;display:inline-block'>Heat stress</span>" if (w.get("temp_c") or 0)>38 else ""}</div>
          </div>
          <div class="wx-tile">
            <div class="wx-icon">💧</div>
            <div class="wx-val">{hum}%</div>
            <div class="wx-lbl">Humidity</div>
          </div>
          <div class="wx-tile">
            <div class="wx-icon">🌧️</div>
            <div class="wx-val">{rain_now} mm</div>
            <div class="wx-lbl">Rain today ({rain_ch}%)</div>
          </div>
          <div class="wx-tile">
            <div class="wx-icon">💨</div>
            <div class="wx-val">{wind}</div>
            <div class="wx-lbl">Wind km/h {wind_dir}</div>
          </div>
          <div class="wx-tile">
            <div class="wx-icon">☁️</div>
            <div class="wx-val">{cloud}%</div>
            <div class="wx-lbl">Cloud cover</div>
          </div>
          <div class="wx-tile">
            <div class="wx-icon">🌦️</div>
            <div class="wx-val">{rain_7d} mm</div>
            <div class="wx-lbl">Last 7 days{"<br><span class='tag red' style='margin-top:3px;display:inline-block'>Low</span>" if (w.get("rain_7d_mm") or 99)<10 else ""}</div>
          </div>
        </div>
        {vpd_html}
        {et0_html}
      </div>

      <!-- Soil -->
      <div class="sec">
        <div class="sec-title"><span class="sec-title-icon">🌍</span> Soil</div>
        {soil_html or "<div class='empty-state'>No soil data available</div>"}
      </div>

      <!-- Growth Curve -->
      <div class="sec">
        <div class="sec-title"><span class="sec-title-icon">📈</span> फसल की वृद्धि · Crop Growth</div>
        {"<div class='chart-wrap'><canvas id='growth-chart'></canvas><div class='chart-note'>" + str(len(timeseries)) + " satellite images since sowing</div></div>" if timeseries else "<div class='chart-empty'>📡 No growth data yet<br><span style='font-size:0.85em'>Run monitor.py after sowing date is set</span></div>"}
      </div>

      <!-- Stress Diagnosis -->
      {diag_section_html}

      <!-- Spray Zone Targeting -->
      <div class="sec">
        <div class="sec-title"><span class="sec-title-icon">🎯</span> Spray Zone Targeting · कहाँ छिड़काव करें</div>
        <div class="spray-target">
          <div class="spray-target-title">Field coverage · खेत का हिस्सा</div>
          <div class="spray-zones-bar">
            <div class="spray-zones-bar-stress" style="width:{min(100,stress_pct_val):.0f}%">{f"{stress_pct_val:.0f}% stressed" if stress_pct_val > 8 else ""}</div>
            <div class="spray-zones-bar-ok" style="width:{max(0,100-stress_pct_val):.0f}%">{f"{100-stress_pct_val:.0f}% healthy" if stress_pct_val < 92 else ""}</div>
          </div>
          <div class="spray-stat-row">
            <div class="spray-stat">
              <div class="spray-stat-val" style="color:#ef5350">{sav_stressed} bh</div>
              <div class="spray-stat-lbl">Spray this · यहाँ करें</div>
            </div>
            <div class="spray-stat">
              <div class="spray-stat-val" style="color:#66bb6a">{sav_saved_bh} bh</div>
              <div class="spray-stat-lbl">Skip this · यह छोड़ें</div>
            </div>
            <div class="spray-stat">
              <div class="spray-stat-val" style="color:#ffa726">{sav_pct}%</div>
              <div class="spray-stat-lbl">Chemical saved · बचत</div>
            </div>
          </div>
          <div class="spray-saving-banner">
            <div class="spray-saving-big">₹{sav_cost:,} saved</div>
            <div class="spray-saving-sub">अगर सिर्फ कमज़ोर हिस्से में छिड़काव करें · if you spray only stressed zones</div>
          </div>
          {"<button class='btn-spray-zones' onclick='toggleHeatmap()'>🌡 Show stressed zones on map · नक्शे पर देखें</button>" if heatmap_url else ""}
        </div>
      </div>

      <!-- Spray Advisory -->
      <div class="sec">
        <div class="sec-title"><span class="sec-title-icon">🌿</span> Spray Advisory · छिड़काव सलाह</div>
        <div style="background:#141824;border:1px solid #1e2235;border-radius:10px;padding:12px">
          <div class="spray-score-ring">
            <div class="spray-ring-num" style="color:{spray_color}">{spray_score}</div>
            <div>
              <div class="spray-ring-label" style="color:{spray_color}">{spray_label}</div>
              <div style="font-size:0.72em;color:#4a5270;margin-top:2px">Score out of 100 · आज का छिड़काव स्कोर</div>
            </div>
          </div>
          {spray_reasons_html}
        </div>
      </div>

      <!-- Field Activity Log -->
      <div class="sec">
        <div class="sec-title"><span class="sec-title-icon">📋</span> खेत गतिविधि · Field Log</div>
        <button class="btn-log-event" onclick="openEventModal()">+ गतिविधि दर्ज करें · Log field activity</button>
        {"<div style='font-size:0.75em;color:#4a5270;text-align:center;padding:4px 0 8px'>No activities logged yet · अभी कुछ दर्ज नहीं</div>" if not field_events else ""}
        {field_events_html}
        {"" if not field_events else "<div style='font-size:0.68em;color:#4a5270;margin-top:6px'>💡 Also via Telegram: <code>log plough</code>, <code>log sowing</code>, <code>field log</code></div>"}
      </div>

      <!-- Crop Calendar -->
      <div class="sec">
        <div class="sec-title"><span class="sec-title-icon">📅</span> फसल कैलेंडर · Crop Calendar</div>
        {"" if not cal_current else f"""
        <div class='cal-current-banner'>
          <div class='cal-current-name'>{cal_current_name} &nbsp;·&nbsp; Day {cal_days}</div>
          {cal_tasks_html}
        </div>"""}
        {"" if not cal_upcoming else f"""
        <div style='font-size:0.68em;font-weight:700;color:#4a5270;text-transform:uppercase;letter-spacing:.04em;margin:8px 0 4px'>
          आगे क्या · Coming up
        </div>
        {cal_upcoming_html}"""}
        {"" if not cal_timeline_html else f"""
        <div class='cal-timeline'>{cal_timeline_html}</div>"""}
        <div class="mrow" style="margin-top:8px">
          <span class="ml">बुवाई · Sowing date</span>
          <span class="mr">{cal_sowing}</span>
        </div>
      </div>

      <!-- Disease Risk Forecast -->
      <div class="sec">
        <div class="sec-title"><span class="sec-title-icon">🦠</span> रोग पूर्वानुमान · Disease Risk</div>
        <div style="background:#0a0c14;border:1px solid #1e2235;border-radius:10px;padding:10px 12px">
          <div style="display:grid;grid-template-columns:42px 1fr 1fr;gap:5px;margin-bottom:6px">
            <div></div>
            <div style="font-size:0.65em;font-weight:700;color:#4a5270;text-align:center">🍄 Fungal · फफूंद</div>
            <div style="font-size:0.65em;font-weight:700;color:#4a5270;text-align:center">🐛 Pest · कीट</div>
          </div>
          {disease_html}
        </div>
      </div>

      <!-- Per-plot stats -->
      {"" if not plot_stats else f"""
      <div class='sec'>
        <div class='sec-title'><span class='sec-title-icon'>🗺️</span> Plot-wise Health · प्लॉट का हाल</div>
        {"".join(
          f"<div class='plot-stat-card'>"
          f"<div class='plot-stat-name'>{p['plot_name']} &nbsp;·&nbsp; {p['area_bigha']} bigha</div>"
          f"<div class='plot-stat-grid'>"
          f"<div class='plot-stat-item'><div class='plot-stat-val' style='color:{'#888' if p['health_pct'] is None else ('#ef5350' if p['health_pct']<45 else '#66bb6a')}'>"
          f"{'—' if p['health_pct'] is None else str(p['health_pct'])+'%'}</div><div class='plot-stat-lbl'>Crop Health</div></div>"
          f"<div class='plot-stat-item'><div class='plot-stat-val' style='color:{'#888' if p['stress_pct'] is None else ('#ef5350' if p['stress_pct']>30 else '#66bb6a')}'>"
          f"{'—' if p['stress_pct'] is None else str(p['stress_pct'])+'%'}</div><div class='plot-stat-lbl'>Stressed Area</div></div>"
          f"<div class='plot-stat-item'><div class='plot-stat-val' style='color:#8a9bb0'>"
          f"{'—' if p['ndvi_mean'] is None else str(p['ndvi_mean'])}</div><div class='plot-stat-lbl'>NDVI</div></div>"
          f"</div></div>"
          for p in plot_stats
        )}
      </div>"""}

      <!-- Spray Reduction Tracker -->
      <div class="sec">
        <div class="sec-title"><span class="sec-title-icon">💊</span> Pesticide Reduction · कीटनाशक बचत</div>
        <button class="btn-log-spray" onclick="openSprayModal()">+ छिड़काव दर्ज करें · Log a spray event</button>
        {"" if red_count == 0 else f"""
        <div class='red-hero'>
          <div class='red-hero-pct'>{red_pct}%</div>
          <div class='red-hero-label'>pesticide reduction this season · इस सीजन कीटनाशक बचत</div>
        </div>
        <div class='red-stat-row'>
          <div class='red-stat'>
            <div class='red-stat-val' style='color:#4fc3f7'>{red_count}</div>
            <div class='red-stat-lbl'>Sprays logged · छिड़काव</div>
          </div>
          <div class='red-stat'>
            <div class='red-stat-val' style='color:#ffa726'>{red_sprayed} bh</div>
            <div class='red-stat-lbl'>Area sprayed · क्षेत्र</div>
          </div>
          <div class='red-stat'>
            <div class='red-stat-val' style='color:#66bb6a'>₹{red_cost:,}</div>
            <div class='red-stat-lbl'>Cost saved · बचत</div>
          </div>
        </div>
        <div class='red-events-title'>Recent sprays · हाल के छिड़काव</div>
        {red_events_html}"""}
        {"<div style='font-size:0.75em;color:#4a5270;text-align:center;padding:8px 0'>No sprays logged yet this season · अभी कोई छिड़काव दर्ज नहीं</div>" if red_count == 0 else ""}
        <div style="font-size:0.68em;color:#4a5270;margin-top:8px">
          💡 Also log via Telegram: <code>log spray &lt;bigha&gt; &lt;chemical&gt;</code>
        </div>
      </div>

      <!-- Forecast -->
      <div class="sec">
        <div class="sec-title"><span class="sec-title-icon">🌦️</span> 7-Day Forecast · मौसम पूर्वानुमान</div>
        <table class="fc-table">
          <tr><th>Day</th><th>Max</th><th>Rain</th><th>Chance</th></tr>
          {forecast_rows}
        </table>
      </div>

    </div><!-- sb-body -->
  </div><!-- sidebar -->
</div><!-- app -->

<!-- Field Event Modal -->
<div id="event-modal">
  <div class="event-modal-box">
    <div class="spray-modal-title">📋 खेत गतिविधि दर्ज करें · Log Field Activity</div>
    <div class="event-type-grid">
      <div class="event-type-btn" data-type="plough" onclick="selectEventType(this)">
        <span class="event-type-icon">🚜</span>Ploughing<br>जुताई
      </div>
      <div class="event-type-btn" data-type="level" onclick="selectEventType(this)">
        <span class="event-type-icon">🏞️</span>Levelling<br>लेवलिंग
      </div>
      <div class="event-type-btn" data-type="fym" onclick="selectEventType(this)">
        <span class="event-type-icon">💩</span>FYM<br>गोबर खाद
      </div>
      <div class="event-type-btn" data-type="basal" onclick="selectEventType(this)">
        <span class="event-type-icon">🌿</span>Fertiliser<br>बेसल खाद
      </div>
      <div class="event-type-btn" data-type="irrigation" onclick="selectEventType(this)">
        <span class="event-type-icon">💧</span>Irrigation<br>सिंचाई
      </div>
      <div class="event-type-btn" data-type="weeding" onclick="selectEventType(this)">
        <span class="event-type-icon">✂️</span>Weeding<br>निराई
      </div>
      <div class="event-type-btn" data-type="sowing" onclick="selectEventType(this)">
        <span class="event-type-icon">🌱</span>Sowing<br>बुवाई
      </div>
      <div class="event-type-btn" data-type="topdress" onclick="selectEventType(this)">
        <span class="event-type-icon">🧪</span>Top dress<br>टॉप ड्रेस
      </div>
      <div class="event-type-btn" data-type="other" onclick="selectEventType(this)">
        <span class="event-type-icon">📝</span>Other<br>अन्य
      </div>
    </div>
    <div class="spray-modal-grid">
      <div>
        <div class="spray-modal-label">Date · तारीख</div>
        <input id="event-date" type="date" class="spray-modal-input">
      </div>
      <div>
        <div class="spray-modal-label">Note (optional) · नोट</div>
        <input id="event-note" type="text" class="spray-modal-input" placeholder="e.g. 2 rounds deep">
      </div>
    </div>
    <div class="spray-modal-actions">
      <button class="btn-spray-save" onclick="saveFieldEvent()">✅ Save · सहेजें</button>
      <button class="btn-spray-cancel" onclick="closeEventModal()">Cancel</button>
    </div>
  </div>
</div>

<!-- Log Spray Modal -->
<div id="spray-modal">
  <div class="spray-modal-box">
    <div class="spray-modal-title">💊 छिड़काव दर्ज करें · Log Spray Event</div>
    <div class="spray-modal-grid">
      <div>
        <div class="spray-modal-label">Date · तारीख</div>
        <input id="spray-date" type="date" class="spray-modal-input">
      </div>
      <div>
        <div class="spray-modal-label">Area (bigha) · क्षेत्र</div>
        <input id="spray-bigha" type="number" class="spray-modal-input" placeholder="e.g. 45">
      </div>
    </div>
    <div class="spray-modal-full">
      <div class="spray-modal-label">Chemical / दवाई</div>
      <input id="spray-chemical" type="text" class="spray-modal-input" placeholder="e.g. Chlorpyrifos 2ml/L · नीम तेल">
    </div>
    <div class="spray-modal-full">
      <div class="spray-modal-label">Zone / क्षेत्र (optional)</div>
      <input id="spray-zone" type="text" class="spray-modal-input" placeholder="e.g. East Plot, full field">
    </div>
    <div class="spray-modal-actions">
      <button class="btn-spray-save" onclick="saveSprayEvent()">✅ Save · सहेजें</button>
      <button class="btn-spray-cancel" onclick="closeSprayModal()">Cancel</button>
    </div>
  </div>
</div>

<script>
// ── Map ───────────────────────────────────────────────────────────────────
const map = L.map('map').setView([{center_lat},{center_lon}], 15);

L.tileLayer('https://mt{{s}}.google.com/vt/lyrs=s&x={{x}}&y={{y}}&z={{z}}', {{
  subdomains:'0123', attribution:'© Google', maxZoom:21, maxNativeZoom:21
}}).addTo(map);
L.tileLayer('https://mt{{s}}.google.com/vt/lyrs=h&x={{x}}&y={{y}}&z={{z}}', {{
  subdomains:'0123', attribution:'', maxZoom:21, maxNativeZoom:21, opacity:0.8
}}).addTo(map);

// ── Crop growth curve (farmer-friendly single line) ───────────────────────
(function() {{
  const ts = {ts_json};
  if (!ts || !ts.length) return;
  const ctx = document.getElementById('growth-chart');
  if (!ctx) return;

  // Convert NDVI (0–0.8) to a plain 0–100 "crop health %" farmers understand
  // 0.0 = 0% (bare soil), 0.8 = 100% (peak dense crop)
  const labels      = ts.map(d => {{
    const dt = new Date(d.date);
    return dt.toLocaleDateString('en-IN', {{day:'numeric', month:'short'}});
  }});
  const healthPct   = ts.map(d => Math.round(Math.max(0, d.ndvi / 0.8) * 100));
  const pointColors = ts.map(d => {{
    const h = Math.round(Math.max(0, d.ndvi / 0.8) * 100);
    // red → orange → yellow → green depending on health
    if (h < 25) return '#ef5350';
    if (h < 45) return '#ffa726';
    if (h < 65) return '#fdd835';
    return '#66bb6a';
  }});
  // Fade points that were taken through clouds
  const pointOpacity = ts.map(d => Math.max(0.35, 1 - (d.cloud_pct || 0) / 80));
  const pointBg = pointColors.map((c, i) => {{
    const hex = c.replace('#','');
    const r = parseInt(hex.substring(0,2),16);
    const g = parseInt(hex.substring(2,4),16);
    const b = parseInt(hex.substring(4,6),16);
    return `rgba(${{r}},${{g}},${{b}},${{pointOpacity[i]}})`;
  }});

  // Stage boundary lines (days after sowing → chart x-index)
  // We'll draw as vertical annotation-like background bands using a plugin-free approach
  const sowingStr = '{farm.get("sowing_date","") or ""}';
  const sowingMs  = sowingStr ? new Date(sowingStr).getTime() : null;

  // Stage labels to show at the top of the chart via custom plugin
  const STAGES = [
    {{upTo: 21,  label: 'Germination · अंकुरण',    color: '#4fc3f720'}},
    {{upTo: 35,  label: 'Vegetative · बढ़वार',       color: '#66bb6a18'}},
    {{upTo: 55,  label: 'Flowering · फूल',           color: '#fdd83518'}},
    {{upTo: 80,  label: 'Pod fill · दाना भराई',      color: '#ffa72618'}},
    {{upTo: 9999,label: 'Maturity · पकाव',           color: '#ab47bc18'}},
  ];

  // Compute day-since-sowing for each point so we can annotate stage
  function stageName(ndvi) {{
    const h = Math.round(Math.max(0, ndvi / 0.8) * 100);
    if (h < 10)  return 'Bare soil · नंगी ज़मीन';
    if (h < 30)  return 'Early growth · शुरुआती बढ़त';
    if (h < 55)  return 'Growing well · अच्छी बढ़त';
    if (h < 75)  return 'Peak growth · पूरी बढ़त';
    return 'Dense crop · घनी फसल';
  }}

  Chart.defaults.color      = '#4a5270';
  Chart.defaults.borderColor = '#1e2235';

  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels,
      datasets: [{{
        label: 'Crop Health',
        data: healthPct,
        borderColor: '#66bb6a',
        borderWidth: 2.5,
        tension: 0.4,
        fill: true,
        backgroundColor: (context) => {{
          const chart = context.chart;
          const {{ctx: c, chartArea}} = chart;
          if (!chartArea) return 'transparent';
          const gradient = c.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
          gradient.addColorStop(0,   'rgba(102,187,106,0.35)');
          gradient.addColorStop(0.6, 'rgba(102,187,106,0.08)');
          gradient.addColorStop(1,   'rgba(102,187,106,0.01)');
          return gradient;
        }},
        pointBackgroundColor: pointBg,
        pointBorderColor:     '#0a0c14',
        pointBorderWidth:     1.5,
        pointRadius:          5,
        pointHoverRadius:     7,
      }}],
    }},
    options: {{
      responsive:true,
      maintainAspectRatio:true,
      animation:{{duration:700}},
      plugins: {{
        legend: {{display:false}},
        tooltip: {{
          backgroundColor: '#141824',
          borderColor:     '#252836',
          borderWidth:     1,
          titleColor:      '#e2e8f0',
          titleFont:       {{weight:'700', size:13}},
          bodyColor:       '#8890a8',
          padding:         10,
          callbacks: {{
            title: (items) => `${{items[0].label}}`,
            label: (item) => {{
              const i   = item.dataIndex;
              const h   = item.raw;
              const sn  = stageName(ts[i].ndvi);
              const cl  = ts[i].cloud_pct > 30 ? ` (cloudy ${{ts[i].cloud_pct}}%)` : '';
              return [`Crop health: ${{h}}%${{cl}}`, sn];
            }},
          }},
        }},
      }},
      scales: {{
        x: {{
          ticks: {{maxTicksLimit:5, maxRotation:0, font:{{size:10}}}},
          grid:  {{color:'#1a1d2a'}},
        }},
        y: {{
          min:0, max:100,
          ticks: {{
            stepSize:25,
            font:{{size:10}},
            callback: (v) => v + '%',
          }},
          grid: {{color:'#1a1d2a'}},
        }},
      }},
    }},
  }});
}})();

// ── NDVI heatmap overlay ──────────────────────────────────────────────────
const HEATMAP_URL  = '{heatmap_url}';
const PLOT_STATS   = {plot_stats_json};
const FARM_ID      = '{farm_id_js}';
const FARM_BIGHA   = {area_bigha};
let heatmapLayer = null;
let heatmapOn    = false;

function toggleHeatmap() {{
  const btn = document.getElementById('btn-heatmap');
  const leg = document.getElementById('heatmap-legend');
  if (!HEATMAP_URL) {{
    btn.classList.add('unavail');
    btn.title = 'Run monitor.py to generate heatmap';
    return;
  }}
  heatmapOn = !heatmapOn;
  if (heatmapOn) {{
    if (!heatmapLayer) {{
      // EE tile layer — aligns perfectly, no expiry
      heatmapLayer = L.tileLayer(HEATMAP_URL, {{
        opacity: 0.75,
        attribution: 'Google Earth Engine · Sentinel-2',
        maxZoom: 20,
      }}).addTo(map);
    }} else {{
      heatmapLayer.addTo(map);
    }}
    btn.classList.add('active');
    leg.classList.add('show');
  }} else {{
    if (heatmapLayer) {{ map.removeLayer(heatmapLayer); }}
    btn.classList.remove('active');
    leg.classList.remove('show');
  }}
}}
// Grey out button if no URL available
if (!HEATMAP_URL) document.getElementById('btn-heatmap').classList.add('unavail');

// ── Farm plots ────────────────────────────────────────────────────────────
const farmData = {geojson};
let plotLayers = {{}};

function plotStyle(f, hl) {{
  return {{ color: hl?'#fff':f.properties.color, weight:hl?3:2, fillColor:f.properties.color, fillOpacity:hl?0.55:0.3, opacity:1 }};
}}

const farmLayer = L.geoJSON(farmData, {{
  style: f => plotStyle(f, false),
  onEachFeature: (f, layer) => {{
    plotLayers[f.properties.id] = layer;
    layer.on('click', () => selectItem(f.properties.id, 'plot'));
    layer.on('mouseover', () => {{ if(selected.id !== f.properties.id) layer.setStyle(plotStyle(f,true)); }});
    layer.on('mouseout',  () => {{ if(selected.id !== f.properties.id) layer.setStyle(plotStyle(f,false)); }});
    const c = layer.getBounds().getCenter();
    L.marker(c, {{ icon: L.divIcon({{
      className:'',
      html:`<div style="background:rgba(0,0,0,.65);color:#fff;padding:3px 9px;border-radius:10px;font-size:11px;font-weight:600;white-space:nowrap;border:1px solid ${{f.properties.color}}">${{f.properties.name}}<br><span style="color:${{f.properties.color}}">${{f.properties.bigha}}b</span></div>`,
      iconAnchor:[50,14]
    }}) }}).addTo(map);
  }}
}}).addTo(map);
map.fitBounds(farmLayer.getBounds(), {{padding:[60,60], maxZoom:18}});

// ── Zones (localStorage) ──────────────────────────────────────────────────
const STORAGE_KEY = 'agri_zones_{farm.get("id","farm")}';
let zones = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
let zoneLayers = {{}};
let editingZoneId = null;
let pendingGeojson = null;
let pendingLayer  = null;   // highlighted layer shown while form is open
let selected = {{id:'all', type:'all'}};
let currentMode = 'click';   // 'click' | 'grid'
let gridLayer = null;         // L.LayerGroup holding current grid cells

function zoneColor(z) {{
  if (!z.sowing_date) return '#ab47bc';
  const days = Math.floor((Date.now() - new Date(z.sowing_date)) / 86400000);
  if (days < 0)  return '#7986cb';
  if (days < 21) return '#4fc3f7';
  if (days < 55) return '#66bb6a';
  if (days < 80) return '#ffa726';
  return '#ef5350';
}}

function renderZones() {{
  Object.values(zoneLayers).forEach(l => map.removeLayer(l));
  zoneLayers = {{}};
  document.getElementById('zone-pills').innerHTML = '';
  zones.forEach(z => {{
    const color = zoneColor(z);
    const layer = L.geoJSON(z.geojson, {{
      style: {{ color, weight:2.5, fillColor:color, fillOpacity:0.38, dashArray:'7 4' }},
    }}).addTo(map);
    layer.on('click', e => {{ L.DomEvent.stopPropagation(e); selectItem(z.id, 'zone'); }});
    layer.on('mouseover', () => layer.setStyle({{ fillOpacity:0.6, weight:3 }}));
    layer.on('mouseout',  () => {{ if(selected.id!==z.id) layer.setStyle({{ fillOpacity:0.38, weight:2.5 }}); }});
    zoneLayers[z.id] = layer;
    const pill = document.createElement('button');
    pill.className = 'pill zone-pill'; pill.id = 'pill-' + z.id;
    pill.textContent = z.name;
    pill.onclick = () => selectItem(z.id, 'zone');
    document.getElementById('zone-pills').appendChild(pill);
  }});
}}
renderZones();

// ── Grid overlay ─────────────────────────────────────────────────────────
const DIVISIONS = {{14:2,15:2,16:3,17:4,18:5,19:6,20:8,21:10}};
let selectedCells = [];   // {{rect, geo, sqm, plotName}}

function updateConfirmBtn() {{
  const btn = document.getElementById('grid-confirm-btn');
  if (selectedCells.length === 0) {{
    btn.style.display = 'none';
    return;
  }}
  const totalSqm = selectedCells.reduce((s, c) => s + c.sqm, 0);
  const bigha = (totalSqm / 1333.33).toFixed(1);
  btn.textContent = `✔ Zone ${{selectedCells.length}} cell${{selectedCells.length>1?'s':''}} — ${{bigha}} bigha`;
  btn.style.display = 'block';
}}

function confirmGridSelection() {{
  if (!selectedCells.length) return;
  const totalSqm = selectedCells.reduce((s, c) => s + c.sqm, 0);
  const plotName = selectedCells[0].plotName;

  // Try to union all cells into one polygon; fall back to MultiPolygon
  let geo;
  try {{
    let merged = turf.polygon(selectedCells[0].geo.coordinates);
    for (let i = 1; i < selectedCells.length; i++) {{
      merged = turf.union(merged, turf.polygon(selectedCells[i].geo.coordinates));
    }}
    geo = merged.geometry;
  }} catch(e) {{
    geo = {{
      type: 'MultiPolygon',
      coordinates: selectedCells.map(c => c.geo.coordinates),
    }};
  }}

  pendingGeojson = geo;
  openZoneForm(null, totalSqm, plotName + ' zone');
}}

function clearGridSelection() {{
  selectedCells.forEach(c => c.rect.setStyle({{fillOpacity:0.15, weight:1.5, color:'#fdd835'}}));
  selectedCells = [];
  updateConfirmBtn();
}}

function buildGrid() {{
  // Preserve selected area across zoom — save centre points of selected cells
  const prevSelectedCentres = selectedCells.map(c => {{
    const coords = c.geo.coordinates[0];
    const lngs = coords.map(p=>p[0]), lats = coords.map(p=>p[1]);
    return [
      (Math.min(...lngs)+Math.max(...lngs))/2,
      (Math.min(...lats)+Math.max(...lats))/2,
    ];
  }});
  const prevSqm = selectedCells.reduce((s,c)=>s+c.sqm, 0);
  const prevPlotName = selectedCells.length ? selectedCells[0].plotName : '';

  if (gridLayer) {{ map.removeLayer(gridLayer); gridLayer = null; }}
  selectedCells = [];   // reset array but NOT the confirm button yet
  const z = Math.min(21, Math.max(14, Math.round(map.getZoom())));
  const divs = DIVISIONS[z];
  gridLayer = L.layerGroup().addTo(map);

  farmData.features.forEach(feat => {{
    const ring = feat.geometry.coordinates[0];
    const closed = (ring[0][0]===ring[ring.length-1][0] && ring[0][1]===ring[ring.length-1][1])
      ? ring : [...ring, ring[0]];
    const poly = turf.polygon([closed]);
    const bb = turf.bbox(poly);
    const plotName = feat.properties.name;
    const step = Math.min(bb[2]-bb[0], bb[3]-bb[1]) / divs;

    for (let x = bb[0]; x < bb[2]; x += step) {{
      for (let y = bb[1]; y < bb[3]; y += step) {{
        const cx = x+step/2, cy = y+step/2;
        if (!turf.booleanPointInPolygon(turf.point([cx, cy]), poly)) continue;

        const sqm = calcAreaSqm([[x,y],[x+step,y],[x+step,y+step],[x,y+step]]);
        const geo = {{type:'Polygon',coordinates:[[[x,y],[x+step,y],[x+step,y+step],[x,y+step],[x,y]]]}};

        // Re-select if any previously selected centre falls inside this new cell
        const wasSelected = prevSelectedCentres.some(([px,py]) =>
          px >= x && px <= x+step && py >= y && py <= y+step
        );

        const rect = L.rectangle([[y,x],[y+step,x+step]], {{
          color: wasSelected?'#ff9800':'#fdd835',
          weight: wasSelected?2.5:1.5,
          fillColor: wasSelected?'#ff9800':'#fdd835',
          fillOpacity: wasSelected?0.6:0.15,
          dashArray:'4 3',
        }}).addTo(gridLayer);

        const cellObj = {{rect, geo, sqm, plotName}};
        if (wasSelected) selectedCells.push(cellObj);

        rect.on('mouseover', () => {{
          if (!selectedCells.includes(cellObj))
            rect.setStyle({{fillOpacity:0.35, weight:2, color:'#fff'}});
        }});
        rect.on('mouseout', () => {{
          if (!selectedCells.includes(cellObj))
            rect.setStyle({{fillOpacity:0.15, weight:1.5, color:'#fdd835'}});
        }});
        rect.on('click', e => {{
          L.DomEvent.stopPropagation(e);
          const idx = selectedCells.indexOf(cellObj);
          if (idx === -1) {{
            selectedCells.push(cellObj);
            rect.setStyle({{fillColor:'#ff9800', fillOpacity:0.6, color:'#ff9800', weight:2.5}});
          }} else {{
            selectedCells.splice(idx, 1);
            rect.setStyle({{fillOpacity:0.15, weight:1.5, color:'#fdd835', fillColor:'#fdd835'}});
          }}
          updateConfirmBtn();
        }});
      }}
    }}
  }});

  updateConfirmBtn();
}}

map.on('zoomend', () => {{ if (currentMode === 'grid') buildGrid(); }});

// ── Draw Zone mode ────────────────────────────────────────────────────────
// Click to add points, double-click or click near start to close polygon.
let drawPoints  = [];   // [[lng,lat], ...]
let drawPolyline = null;
let drawPolygon  = null;
let drawMarkers  = [];

const CLOSE_PX = 20;   // pixels — snap to first point within this distance

function drawReset() {{
  drawPoints = [];
  drawMarkers.forEach(m => map.removeLayer(m));
  drawMarkers = [];
  if (drawPolyline) {{ map.removeLayer(drawPolyline); drawPolyline = null; }}
  if (drawPolygon)  {{ map.removeLayer(drawPolygon);  drawPolygon  = null; }}
  document.getElementById('draw-confirm-btn').style.display = 'none';
  document.getElementById('draw-undo-btn').style.display = 'none';
}}

function drawUpdatePreview() {{
  if (drawPolyline) map.removeLayer(drawPolyline);
  if (drawPolygon)  map.removeLayer(drawPolygon);

  if (drawPoints.length < 2) {{
    drawPolyline = null; drawPolygon = null; return;
  }}

  // Closed preview polygon when ≥3 points
  if (drawPoints.length >= 3) {{
    drawPolygon = L.polygon(drawPoints.map(p=>[p[1],p[0]]), {{
      color:'#4fc3f7', weight:2, fillColor:'#4fc3f7', fillOpacity:0.2, dashArray:'6 3',
    }}).addTo(map);
  }}
  // Live edge to show the boundary
  const allPts = [...drawPoints, drawPoints[0]];
  drawPolyline = L.polyline(allPts.map(p=>[p[1],p[0]]), {{
    color:'#4fc3f7', weight:2, dashArray:'6 3',
  }}).addTo(map);
}}

function drawAddPoint(latlng) {{
  const pt = [latlng.lng, latlng.lat];

  // Snap-close: if ≥3 points and click is within CLOSE_PX of first point
  if (drawPoints.length >= 3) {{
    const firstPx = map.latLngToContainerPoint(L.latLng(drawPoints[0][1], drawPoints[0][0]));
    const clickPx = map.latLngToContainerPoint(latlng);
    const dist = Math.hypot(firstPx.x - clickPx.x, firstPx.y - clickPx.y);
    if (dist <= CLOSE_PX) {{ drawFinish(); return; }}
  }}

  drawPoints.push(pt);

  // Marker for the point — first point gets a special "close here" ring
  const isFirst = drawPoints.length === 1;
  const marker = L.circleMarker([latlng.lat, latlng.lng], {{
    radius: isFirst ? 7 : 4,
    color: isFirst ? '#fff' : '#4fc3f7',
    fillColor: isFirst ? '#4fc3f7' : '#fff',
    fillOpacity: 1, weight: 2,
  }}).addTo(map);
  drawMarkers.push(marker);

  drawUpdatePreview();

  const confirmBtn = document.getElementById('draw-confirm-btn');
  const undoBtn    = document.getElementById('draw-undo-btn');
  undoBtn.style.display = 'block';

  if (drawPoints.length >= 3) {{
    const sqm = calcAreaSqm(drawPoints);
    confirmBtn.textContent = `✔ Close polygon — ${{(sqm/1333.33).toFixed(1)}} bigha`;
    confirmBtn.style.display = 'block';
  }} else {{
    confirmBtn.style.display = 'none';
  }}
}}

function drawUndo() {{
  if (!drawPoints.length) return;
  drawPoints.pop();
  const m = drawMarkers.pop();
  if (m) map.removeLayer(m);
  drawUpdatePreview();
  const confirmBtn = document.getElementById('draw-confirm-btn');
  const undoBtn    = document.getElementById('draw-undo-btn');
  if (drawPoints.length < 3) confirmBtn.style.display = 'none';
  if (drawPoints.length === 0) undoBtn.style.display = 'none';
  if (drawPoints.length >= 3) {{
    const sqm = calcAreaSqm(drawPoints);
    confirmBtn.textContent = `✔ Close polygon — ${{(sqm/1333.33).toFixed(1)}} bigha`;
  }}
}}

function drawFinish() {{
  if (drawPoints.length < 3) return;
  const closed = [...drawPoints, drawPoints[0]];
  const sqm = calcAreaSqm(drawPoints);
  pendingGeojson = {{ type:'Polygon', coordinates:[closed] }};
  drawReset();
  setMode('click');
  openZoneForm(null, sqm, 'My Zone');
}}

// Guard: ignore the very first map-click that fires when the toolbar button is pressed,
// and suppress the two rapid clicks that browsers fire before dblclick.
let drawIgnoreNextClick = false;
let drawDblClickPending = false;

map.on('click', e => {{
  if (currentMode !== 'draw') return;
  if (drawIgnoreNextClick) {{ drawIgnoreNextClick = false; return; }}
  if (drawDblClickPending) return;   // swallow the 2nd click of a dbl-click
  drawAddPoint(e.latlng);
}});

map.on('dblclick', e => {{
  if (currentMode !== 'draw') return;
  L.DomEvent.stopPropagation(e);
  drawDblClickPending = true;
  setTimeout(() => {{ drawDblClickPending = false; }}, 300);
  if (drawPoints.length >= 3) drawFinish();
}});

// ── Mode switching ────────────────────────────────────────────────────────
function setMode(mode) {{
  currentMode = mode;
  const hint = document.getElementById('mode-hint');
  const cancelBtn = document.getElementById('btn-cancel-mode');
  document.querySelectorAll('.tb-btn').forEach(b => b.classList.remove('active','split-active'));

  // Always clean up previous modes
  if (mode !== 'draw') {{ drawReset(); map.doubleClickZoom.enable(); }}
  if (mode !== 'grid') {{
    if (gridLayer) {{ map.removeLayer(gridLayer); gridLayer = null; }}
    clearGridSelection();
  }}

  if (mode === 'draw') {{
    document.getElementById('btn-draw').classList.add('split-active');
    hint.textContent = '✏️ Click to place points — 3+ points needed — double-click or tap ✔ button to finish area';
    hint.style.display = 'block';
    cancelBtn.style.display = 'block';
    map.getContainer().style.cursor = 'crosshair';
    map.doubleClickZoom.disable();
    drawIgnoreNextClick = true;   // swallow the click that activated this button
  }} else if (mode === 'grid') {{
    document.getElementById('btn-grid').classList.add('split-active');
    hint.textContent = '⊞ Click cells to select (orange) — zoom in for smaller cells — tap confirm to zone';
    hint.style.display = 'block';
    cancelBtn.style.display = 'block';
    map.getContainer().style.cursor = '';
    buildGrid();
  }} else {{
    document.getElementById('btn-click-zone').classList.add('active');
    hint.style.display = 'none';
    cancelBtn.style.display = 'none';
    map.getContainer().style.cursor = '';
  }}
}}

// ── Click on farm plot (select-plot mode) → zone whole plot ───────────────
farmLayer.eachLayer(layer => {{
  layer.on('click', e => {{
    if (currentMode !== 'click') return;
    L.DomEvent.stopPropagation(e);
    const f = layer.feature;
    pendingGeojson = f.geometry;
    const coords = f.geometry.coordinates[0].map(c=>[c[0],c[1]]);
    openZoneForm(null, calcAreaSqm(coords), f.properties.name);
  }});
}});

// ── Irrigation event rows ─────────────────────────────────────────────────
const IRR_METHODS = ['','Flood · बाढ़','Drip · टपक','Sprinkler · फव्वारा','Canal · नहर','Borewell · ट्यूबवेल','Open well · कुआँ','Farm pond · तालाब','Rain-fed · बारानी'];
const IRR_SOURCES = ['','Borewell · ट्यूबवेल','Canal · नहर','River / Nala · नदी','Farm pond · तालाब','Open well · कुआँ','Tanker · टैंकर','Rain · वर्षा जल'];

function irrMethodOpts(sel) {{
  return IRR_METHODS.map(m => `<option value="${{m}}"${{m===sel?' selected':''}}>${{m||'— Method / विधि —'}}</option>`).join('');
}}
function irrSourceOpts(sel) {{
  return IRR_SOURCES.map(s => `<option value="${{s}}"${{s===sel?' selected':''}}>${{s||'— Source / स्रोत —'}}</option>`).join('');
}}

function addIrrRow(ev={{}}) {{
  const list = document.getElementById('irr-list');
  const idx  = list.children.length;
  const ordinal = ['1st पहला','2nd दूसरा','3rd तीसरा','4th चौथा','5th पाँचवाँ','6th छठा','7th सातवाँ','8th आठवाँ'][idx] || `${{idx+1}}th`;
  const div = document.createElement('div');
  div.className = 'irr-row';
  div.innerHTML = `
    <span class="irr-row-num">${{ordinal}} water · पानी</span>
    <button type="button" class="irr-row-del" onclick="removeIrrRow(this)" title="Remove">✕</button>
    <div class="irr-row-top">
      <div class="form-group">
        <label>Date · तारीख</label>
        <input type="date" class="irr-date" value="${{ev.date||''}}" onchange="updateIrrSummary()">
      </div>
      <div class="form-group">
        <label>Area (bigha) · क्षेत्र (बीघा)</label>
        <input type="number" class="irr-bigha" min="0" step="0.1" placeholder="e.g. 25" value="${{ev.bigha||''}}" oninput="updateIrrSummary()">
      </div>
    </div>
    <div class="irr-row-bot">
      <div class="form-group">
        <label>Method · विधि</label>
        <select class="irr-method" onchange="updateIrrSummary()">${{irrMethodOpts(ev.method||'')}}</select>
      </div>
      <div class="form-group">
        <label>Source · स्रोत</label>
        <select class="irr-source">${{irrSourceOpts(ev.source||'')}}</select>
      </div>
    </div>`;
  list.appendChild(div);
  updateIrrSummary();
}}

function removeIrrRow(btn) {{
  btn.closest('.irr-row').remove();
  // Re-label ordinals
  const list = document.getElementById('irr-list');
  const labels = ['1st पहला','2nd दूसरा','3rd तीसरा','4th चौथा','5th पाँचवाँ','6th छठा','7th सातवाँ','8th आठवाँ'];
  Array.from(list.children).forEach((row, i) => {{
    row.querySelector('.irr-row-num').textContent = (labels[i]||`${{i+1}}th`) + ' water · पानी';
  }});
  updateIrrSummary();
}}

function updateIrrSummary() {{
  const list    = document.getElementById('irr-list');
  const summary = document.getElementById('irr-summary');
  const rows    = Array.from(list.querySelectorAll('.irr-row'));
  if (!rows.length) {{ summary.style.display = 'none'; return; }}

  // Zone area for overlap calc — read from form title which has bigha, fall back to pendingGeojson
  let zoneBigha = null;
  const titleEl = document.getElementById('form-title-text');
  const m = titleEl ? titleEl.textContent.match(/([0-9.]+) *bigha/) : null;
  if (m) zoneBigha = parseFloat(m[1]);
  else if (pendingGeojson) {{
    const coords = pendingGeojson.coordinates[0].map(c=>[c[0],c[1]]);
    zoneBigha = calcAreaSqm(coords) / 1333.33;
  }} else if (editingZoneId) {{
    const z = zones.find(x=>x.id===editingZoneId);
    if (z) zoneBigha = z.area_bigha || null;
  }}

  const events = rows.map(r => ({{
    date:   r.querySelector('.irr-date').value,
    bigha:  parseFloat(r.querySelector('.irr-bigha').value) || 0,
    method: r.querySelector('.irr-method').value,
  }})).filter(e => e.date || e.bigha);

  const totalBigha = events.reduce((s,e) => s+e.bigha, 0);
  const count      = events.length;
  const lastDate   = events.filter(e=>e.date).map(e=>e.date).sort().pop() || '—';

  let overlapBigha = 0;
  if (zoneBigha && totalBigha > zoneBigha) {{
    overlapBigha = Math.round((totalBigha - zoneBigha) * 10) / 10;
  }}

  let html = `<span class="irr-sum-chip blue">💧 ${{count}} event${{count!==1?'s':''}}</span>`;
  html    += `<span class="irr-sum-chip blue">${{Math.round(totalBigha*10)/10}} bigha total irrigated</span>`;
  if (lastDate !== '—') html += `<span class="irr-sum-chip green">Last: ${{lastDate}}</span>`;
  if (overlapBigha > 0) {{
    html += `<span class="irr-sum-chip orange">⚠ ~${{overlapBigha}} bigha re-irrigated (overlap)</span>`;
  }} else if (zoneBigha && totalBigha > 0 && totalBigha < zoneBigha) {{
    const pct = Math.round(totalBigha/zoneBigha*100);
    html += `<span class="irr-sum-chip${{pct<50?' red':' green'}}">${{pct}}% of zone covered</span>`;
  }}

  summary.innerHTML = html;
  summary.style.display = 'flex';
}}

function getIrrEvents() {{
  return Array.from(document.querySelectorAll('#irr-list .irr-row')).map(r => ({{
    date:   r.querySelector('.irr-date').value,
    bigha:  parseFloat(r.querySelector('.irr-bigha').value) || 0,
    method: r.querySelector('.irr-method').value,
    source: r.querySelector('.irr-source').value,
  }})).filter(e => e.date || e.bigha || e.method);
}}

function loadIrrEvents(events) {{
  document.getElementById('irr-list').innerHTML = '';
  (events||[]).forEach(ev => addIrrRow(ev));
  // backward-compat: single legacy fields
  updateIrrSummary();
}}

// ── Pending-zone highlight ────────────────────────────────────────────────
function clearPendingLayer() {{
  if (pendingLayer) {{ map.removeLayer(pendingLayer); pendingLayer = null; }}
}}

function showPendingLayer(geo) {{
  clearPendingLayer();
  if (!geo) return;
  pendingLayer = L.geoJSON(geo, {{
    style: {{ color:'#ffd600', weight:3, fillColor:'#ffd600', fillOpacity:0.25, dashArray:'6 3' }},
  }}).addTo(map);
}}

// ── Zone form ─────────────────────────────────────────────────────────────
function openZoneForm(zoneId, preSqm, preName) {{
  editingZoneId = zoneId;

  const FIELD_IDS = ['z-name','z-khasra','z-land-type','z-soil','z-crop','z-variety','z-sowing','z-seed-rate','z-seed-treat','z-fert-base','z-fert-top','z-herbicide','z-pesticide','z-fungicide','z-prev-crop','z-notes'];
  if (zoneId) {{
    const z = zones.find(x => x.id === zoneId);
    document.getElementById('form-title-text').textContent = '✏️ Edit Zone · बदलाव करें';
    FIELD_IDS.forEach(id => {{
      const el = document.getElementById(id); if(el) el.value = '';
    }});
    document.getElementById('z-name').value       = z.name || '';
    document.getElementById('z-khasra').value     = z.khasra || '';
    document.getElementById('z-land-type').value  = z.land_type || '';
    document.getElementById('z-soil').value       = z.soil_type || '';
    document.getElementById('z-crop').value       = z.crop || '';
    document.getElementById('z-variety').value    = z.variety || '';
    document.getElementById('z-sowing').value     = z.sowing_date || '';
    document.getElementById('z-seed-rate').value  = z.seed_rate || '';
    document.getElementById('z-seed-treat').value = z.seed_treatment || '';
    document.getElementById('z-fert-base').value  = z.fertiliser_base || '';
    document.getElementById('z-fert-top').value   = z.fertiliser_top || '';
    document.getElementById('z-herbicide').value  = z.herbicide || '';
    document.getElementById('z-pesticide').value  = z.pesticide || '';
    document.getElementById('z-fungicide').value  = z.fungicide || '';
    document.getElementById('z-prev-crop').value  = z.prev_crop || '';
    document.getElementById('z-notes').value      = z.notes || '';
    // Load irrigation events (with backward-compat for old single-field format)
    const legacyEvt = z.irrigation_date
      ? [{{date: z.irrigation_date, bigha: z.area_bigha||0, method: z.irrigation_method||'', source: z.water_source||''}}]
      : [];
    loadIrrEvents(z.irrigation_events && z.irrigation_events.length ? z.irrigation_events : legacyEvt);
  }} else {{
    document.getElementById('form-title-text').textContent = preSqm
      ? `📌 Add Zone — ${{Math.round(preSqm/1333.33*10)/10}} bigha (${{Math.round(preSqm).toLocaleString()}} m²)`
      : '📌 Add Zone';
    FIELD_IDS.forEach(id => {{
      const el = document.getElementById(id); if(el) el.value='';
    }});
    document.getElementById('z-name').value = preName || '';
    loadIrrEvents([]);
  }}
  // Highlight the area being filled in
  if (zoneId) {{
    const z = zones.find(x => x.id === zoneId);
    if (z) showPendingLayer(z.geojson);
  }} else {{
    showPendingLayer(pendingGeojson);
  }}
  document.getElementById('zone-overlay').classList.add('show');
}}

function cancelZone() {{
  document.getElementById('zone-overlay').classList.remove('show');
  clearPendingLayer();
  pendingGeojson = null;
  editingZoneId = null;
  drawReset();
  clearGridSelection();
  setMode('click');
}}

function saveZone() {{
  const name = document.getElementById('z-name').value.trim();
  if (!name) {{ alert('Please enter a zone name'); return; }}

  const irrEvents = getIrrEvents();
  const lastIrr   = irrEvents.filter(e=>e.date).map(e=>e.date).sort().pop() || null;
  const zoneData = {{
    name,
    khasra:             document.getElementById('z-khasra').value,
    land_type:          document.getElementById('z-land-type').value,
    soil_type:          document.getElementById('z-soil').value,
    crop:               document.getElementById('z-crop').value,
    variety:            document.getElementById('z-variety').value,
    sowing_date:        document.getElementById('z-sowing').value,
    seed_rate:          document.getElementById('z-seed-rate').value,
    seed_treatment:     document.getElementById('z-seed-treat').value,
    irrigation_events:  irrEvents,
    irrigation_date:    lastIrr,   // keep for backward-compat display
    fertiliser_base:    document.getElementById('z-fert-base').value,
    fertiliser_top:     document.getElementById('z-fert-top').value,
    herbicide:          document.getElementById('z-herbicide').value,
    pesticide:          document.getElementById('z-pesticide').value,
    fungicide:          document.getElementById('z-fungicide').value,
    prev_crop:          document.getElementById('z-prev-crop').value,
    notes:              document.getElementById('z-notes').value,
  }};

  if (editingZoneId) {{
    const z = zones.find(x => x.id === editingZoneId);
    Object.assign(z, zoneData, {{ updated_at: new Date().toISOString() }});
  }} else {{
    const geo = pendingGeojson;
    const coords = geo.coordinates[0].map(c => [c[0],c[1]]);
    const sqm = calcAreaSqm(coords);
    zones.push({{
      id: 'zone_' + Date.now(),
      ...zoneData,
      geojson: geo,
      area_sqm: Math.round(sqm),
      area_bigha: Math.round(sqm / 1333.33 * 10) / 10,
      created_at: new Date().toISOString(),
    }});
    pendingGeojson = null;
  }}

  localStorage.setItem(STORAGE_KEY, JSON.stringify(zones));
  document.getElementById('zone-overlay').classList.remove('show');
  clearPendingLayer();
  renderZones();
  if (editingZoneId) showZoneDetail(editingZoneId);
  editingZoneId = null;

  // If more pieces queued (from split), open next form
  if (pendingQueue.length > 0) processQueue();
  else setMode('click');
}}

function deleteZone(id) {{
  if (!confirm('Delete this zone?')) return;
  zones = zones.filter(z => z.id !== id);
  localStorage.setItem(STORAGE_KEY, JSON.stringify(zones));
  renderZones();
  document.getElementById('zone-detail').classList.remove('show');
  selectItem('all','all');
}}

// ── Area calc (client-side) ───────────────────────────────────────────────
function calcAreaSqm(coords) {{
  const lat0 = coords.reduce((s,c)=>s+c[1],0)/coords.length;
  const mpl=111000, mplo=111000*Math.cos(lat0*Math.PI/180);
  const pts=coords.map(c=>[c[0]*mplo,c[1]*mpl]);
  const n=pts.length;
  let area=0;
  for(let i=0;i<n;i++) area+=pts[i][0]*(pts[(i+1)%n][1]-pts[(i-1+n)%n][1]);
  return Math.abs(area)/2;
}}

// ── Selection / sidebar ───────────────────────────────────────────────────
function selectItem(id, type) {{
  selected = {{id, type}};

  // Reset all pills
  document.querySelectorAll('.pill').forEach(p => p.classList.remove('active'));
  const pill = document.getElementById('pill-'+id);
  if (pill) pill.classList.add('active');

  // Reset plot styles
  farmLayer.eachLayer(layer => layer.setStyle(plotStyle(layer.feature, false)));
  Object.entries(zoneLayers).forEach(([zid, layer]) => layer.setStyle({{ fillOpacity:0.35, weight:2 }}));

  document.getElementById('zone-detail').classList.remove('show');

  if (id === 'all') {{
    map.fitBounds(farmLayer.getBounds(), {{padding:[60,60],maxZoom:18}});
  }} else if (type === 'plot' && plotLayers[id]) {{
    const layer = plotLayers[id];
    layer.setStyle(plotStyle(layer.feature, true));
    map.fitBounds(layer.getBounds(), {{padding:[60,60],maxZoom:19}});
    const ps = PLOT_STATS.find(p => p.plot_id === id);
    if (ps) {{
      const hc = ps.health_pct;
      const hColor = hc === null ? '#888' : hc < 45 ? '#ef5350' : hc < 65 ? '#ffa726' : '#66bb6a';
      const sc = ps.stress_pct;
      const sColor = sc === null ? '#888' : sc > 30 ? '#ef5350' : sc > 10 ? '#ffa726' : '#66bb6a';
      layer.bindPopup(`
        <div style="padding:10px 12px;min-width:160px">
          <div style="font-weight:700;font-size:0.9em;margin-bottom:8px">${{ps.plot_name}}</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
            <div style="text-align:center;background:#141824;border-radius:8px;padding:6px">
              <div style="font-size:1.1em;font-weight:800;color:${{hColor}}">${{hc === null ? '—' : hc + '%'}}</div>
              <div style="font-size:0.65em;color:#8a9bb0;margin-top:2px">Crop Health</div>
            </div>
            <div style="text-align:center;background:#141824;border-radius:8px;padding:6px">
              <div style="font-size:1.1em;font-weight:800;color:${{sColor}}">${{sc === null ? '—' : sc + '%'}}</div>
              <div style="font-size:0.65em;color:#8a9bb0;margin-top:2px">Stressed Area</div>
            </div>
          </div>
          <div style="font-size:0.7em;color:#4a5270;margin-top:6px;text-align:center">${{ps.area_bigha}} bigha · NDVI ${{ps.ndvi_mean ?? '—'}}</div>
        </div>`, {{className:'dark-popup', maxWidth:220}}).openPopup();
    }}
  }} else if (type === 'zone') {{
    if (zoneLayers[id]) {{
      zoneLayers[id].setStyle({{ fillOpacity:0.6, weight:3 }});
      map.fitBounds(zoneLayers[id].getBounds(), {{padding:[60,60],maxZoom:20}});
    }}
    showZoneDetail(id);
  }}
}}

function kv(label, val) {{
  if (!val) return '';
  return `<div class="zone-kv"><span class="zone-k">${{label}}</span><span class="zone-v">${{val}}</span></div>`;
}}

function showZoneDetail(id) {{
  const z = zones.find(x => x.id === id);
  if (!z) return;
  const el = document.getElementById('zone-detail');
  el.classList.add('show');

  const days = z.sowing_date ? Math.floor((Date.now()-new Date(z.sowing_date))/86400000) : null;
  const stage = days===null ? '—' : days<0 ? `Sowing in ${{Math.abs(days)}} days` : days<21 ? `Germination (Day ${{days}})` : days<35 ? `Vegetative (Day ${{days}})` : days<55 ? `Flowering (Day ${{days}})` : days<80 ? `Pod fill (Day ${{days}})` : `Maturity (Day ${{days}})`;

  // Build irrigation timeline
  const events = z.irrigation_events && z.irrigation_events.length
    ? z.irrigation_events
    : (z.irrigation_date ? [{{date:z.irrigation_date, bigha:z.area_bigha||0, method:z.irrigation_method||'', source:z.water_source||''}}] : []);
  const totalIrrBigha = events.reduce((s,e)=>s+(e.bigha||0), 0);
  const overlapBigha  = z.area_bigha && totalIrrBigha > z.area_bigha
    ? Math.round((totalIrrBigha - z.area_bigha)*10)/10 : 0;

  let irrHtml = '';
  if (events.length) {{
    irrHtml += `<div class="zone-kv" style="flex-direction:column;align-items:flex-start;gap:6px">
      <span class="zone-k">💧 Irrigation history · सिंचाई रिकॉर्ड</span>
      <div class="irr-timeline">`;
    events.forEach((ev, i) => {{
      const isOverlap = i > 0 && z.area_bigha && ev.bigha > 0;
      irrHtml += `<div class="irr-tl-row">
        <div class="irr-tl-dot${{isOverlap?' overlap':''}}"></div>
        <div class="irr-tl-body">
          <div class="irr-tl-date">${{ev.date||'Date unknown'}} &nbsp;·&nbsp; ${{ev.bigha||'?'}} bigha</div>
          <div class="irr-tl-meta">${{[ev.method,ev.source].filter(Boolean).join(' · ')||'Method not recorded'}}</div>
        </div>
      </div>`;
    }});
    irrHtml += `</div>`;
    if (totalIrrBigha > 0) {{
      irrHtml += `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:4px">
        <span class="irr-sum-chip blue" style="font-size:0.75em">Total: ${{Math.round(totalIrrBigha*10)/10}} bigha</span>`;
      if (overlapBigha > 0) {{
        irrHtml += `<span class="irr-sum-chip orange" style="font-size:0.75em">⚠ ~${{overlapBigha}} bigha overlap</span>`;
      }}
      irrHtml += `</div>`;
    }}
    irrHtml += `</div>`;
  }} else {{
    irrHtml = kv('Irrigation · सिंचाई', '—');
  }}

  const rows =
    kv('Area',             `${{z.area_bigha||'?'}} bigha (${{(z.area_sqm||0).toLocaleString()}} m²)`) +
    kv('Khasra / Survey',  z.khasra) +
    kv('Land type',        z.land_type) +
    kv('Soil type',        z.soil_type) +
    `<div style="height:4px"></div>` +
    kv('Crop',             z.crop) +
    kv('Variety',          z.variety) +
    kv('Sowing date',      z.sowing_date) +
    kv('Growth stage',     stage) +
    kv('Seed rate',        z.seed_rate) +
    kv('Seed treatment',   z.seed_treatment) +
    `<div style="height:4px"></div>` +
    irrHtml +
    `<div style="height:4px"></div>` +
    kv('Base fertiliser',  z.fertiliser_base) +
    kv('Top dressing',     z.fertiliser_top) +
    kv('Herbicide',        z.herbicide) +
    kv('Pesticide',        z.pesticide) +
    kv('Fungicide',        z.fungicide) +
    kv('Previous crop',    z.prev_crop) +
    (z.notes ? `<div class="zone-kv"><span class="zone-k">Notes</span><span class="zone-v" style="font-weight:400;color:#c0c8e0">${{z.notes}}</span></div>` : '');

  el.innerHTML = `
    <div class="zone-detail-title">
      <span>📌 ${{z.name}}</span>
      <span style="font-size:0.75em;color:#8890a8">${{z.area_bigha||'?'}} bigha</span>
    </div>
    ${{rows}}
    <button class="btn-edit-zone" onclick="openZoneForm('${{z.id}}')">✏️ Edit</button>
    <button class="btn-delete-zone" onclick="deleteZone('${{z.id}}')">🗑 Delete</button>`;

  document.getElementById('sb-body').scrollTop = 0;
}}

// ── Current location ─────────────────────────────────────────────────────
let locMarker  = null;
let locCircle  = null;
let locToastTimer = null;

function showLocToast(msg, color, duration) {{
  const t = document.getElementById('loc-toast');
  t.textContent = msg;
  t.style.borderColor = color || '#1e2130';
  t.style.color       = color || '#e2e8f0';
  t.style.display     = 'block';
  clearTimeout(locToastTimer);
  if (duration) locToastTimer = setTimeout(() => {{ t.style.display = 'none'; }}, duration);
}}

// ── Spray logger ──────────────────────────────────────────────────────────
function openSprayModal() {{
  document.getElementById('spray-date').value = new Date().toISOString().slice(0,10);
  document.getElementById('spray-bigha').value = '';
  document.getElementById('spray-chemical').value = '';
  document.getElementById('spray-zone').value = '';
  document.getElementById('spray-modal').classList.add('open');
}}

function closeSprayModal() {{
  document.getElementById('spray-modal').classList.remove('open');
}}

function saveSprayEvent() {{
  const date     = document.getElementById('spray-date').value;
  const bigha    = parseFloat(document.getElementById('spray-bigha').value) || FARM_BIGHA;
  const chemical = document.getElementById('spray-chemical').value.trim() || 'not specified';
  const zone     = document.getElementById('spray-zone').value.trim() || 'full field';

  // Save to localStorage (synced to monitor.py on next run via spray_logs/)
  const key    = `spray_log_${{FARM_ID}}`;
  const log    = JSON.parse(localStorage.getItem(key) || '[]');
  const event  = {{
    id:         'spray_' + Date.now(),
    date,
    bigha,
    chemical,
    zone_name:  zone,
    logged_at:  new Date().toISOString(),
    source:     'dashboard',
  }};
  log.push(event);
  localStorage.setItem(key, JSON.stringify(log));

  closeSprayModal();

  // Show confirmation toast
  const toast = document.createElement('div');
  toast.style.cssText = 'position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:#1a4a1a;border:1px solid #2a6a2a;color:#66bb6a;padding:10px 18px;border-radius:10px;font-size:0.85em;font-weight:600;z-index:9999;text-align:center';
  toast.innerHTML = `✅ Spray logged: ${{bigha}} bigha · ${{chemical}}<br><span style="font-size:0.85em;color:#8a9bb0">Reload after running monitor.py to see updated stats</span>`;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}}

// Close modal on backdrop click
document.getElementById('spray-modal').addEventListener('click', function(e) {{
  if (e.target === this) closeSprayModal();
}});

// ── Field event logger ────────────────────────────────────────────────────
let selectedEventType = null;

function openEventModal() {{
  document.getElementById('event-date').value = new Date().toISOString().slice(0,10);
  document.getElementById('event-note').value = '';
  selectedEventType = null;
  document.querySelectorAll('.event-type-btn').forEach(b => b.classList.remove('selected'));
  document.getElementById('event-modal').classList.add('open');
}}

function closeEventModal() {{
  document.getElementById('event-modal').classList.remove('open');
}}

function selectEventType(btn) {{
  document.querySelectorAll('.event-type-btn').forEach(b => b.classList.remove('selected'));
  btn.classList.add('selected');
  selectedEventType = btn.dataset.type;
}}

function saveFieldEvent() {{
  if (!selectedEventType) {{
    alert('Please select an activity type · गतिविधि चुनें');
    return;
  }}
  const date  = document.getElementById('event-date').value;
  const note  = document.getElementById('event-note').value.trim();
  const key   = `field_log_${{FARM_ID}}`;
  const log   = JSON.parse(localStorage.getItem(key) || '[]');
  const event = {{
    id:        'ev_' + Date.now(),
    type:      selectedEventType,
    date,
    note,
    logged_at: new Date().toISOString(),
    source:    'dashboard',
  }};
  log.push(event);
  localStorage.setItem(key, JSON.stringify(log));
  closeEventModal();

  const labels = {{plough:'Ploughing · जुताई', level:'Levelling · लेवलिंग',
    fym:'FYM · गोबर खाद', basal:'Fertiliser · बेसल खाद',
    irrigation:'Irrigation · सिंचाई', weeding:'Weeding · निराई',
    sowing:'Sowing · बुवाई', topdress:'Top dressing · टॉप ड्रेस', other:'Other · अन्य'}};
  const icons  = {{plough:'🚜', level:'🏞️', fym:'💩', basal:'🌿',
    irrigation:'💧', weeding:'✂️', sowing:'🌱', topdress:'🧪', other:'📝'}};

  const toast = document.createElement('div');
  toast.style.cssText = 'position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:#1a1a3a;border:1px solid #2a2a5a;color:#b0b8f0;padding:10px 18px;border-radius:10px;font-size:0.85em;font-weight:600;z-index:9999;text-align:center';
  toast.innerHTML = `${{icons[selectedEventType]}} Logged: ${{labels[selectedEventType]}}<br><span style="font-size:0.85em;color:#6070a0">${{date}}${{note ? ' · ' + note : ''}}</span>`;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}}

document.getElementById('event-modal').addEventListener('click', function(e) {{
  if (e.target === this) closeEventModal();
}});

function locateMe() {{
  const btn = document.getElementById('btn-locate');
  if (!navigator.geolocation) {{
    showLocToast('GPS not supported · GPS उपलब्ध नहीं', '#ef5350', 3000);
    return;
  }}
  btn.classList.add('locating');
  btn.textContent = '⟳';
  showLocToast('Finding location… · लोकेशन खोज रहे हैं…', '#fdd835');

  navigator.geolocation.getCurrentPosition(
    pos => {{
      const lat = pos.coords.latitude;
      const lng = pos.coords.longitude;
      const acc = Math.round(pos.coords.accuracy);

      btn.classList.remove('locating');
      btn.classList.add('located');
      btn.textContent = '📍';

      // Remove previous marker/circle
      if (locMarker) {{ map.removeLayer(locMarker); }}
      if (locCircle) {{ map.removeLayer(locCircle); }}

      // Accuracy circle
      locCircle = L.circle([lat, lng], {{
        radius: acc,
        color: '#4fc3f7', weight: 1.5,
        fillColor: '#4fc3f7', fillOpacity: 0.08,
        dashArray: '4 4',
      }}).addTo(map);

      // Pulsing dot marker
      locMarker = L.marker([lat, lng], {{
        icon: L.divIcon({{
          className: '',
          html: `<div style="width:16px;height:16px;border-radius:50%;background:#4fc3f7;border:3px solid #fff;box-shadow:0 0 0 4px #4fc3f740;animation:pulse-dot 1.6s ease-in-out infinite"></div>`,
          iconAnchor: [8, 8],
        }})
      }}).addTo(map);

      locMarker.bindPopup(`
        <div style="font-family:inherit;font-size:13px;color:#e2e8f0;background:#141824;padding:8px 12px;border-radius:8px;min-width:160px">
          <b style="color:#4fc3f7">📍 You are here · आप यहाँ हैं</b><br>
          <span style="color:#8890a8;font-size:0.85em">
            ${{lat.toFixed(6)}}, ${{lng.toFixed(6)}}<br>
            Accuracy · सटीकता: ±${{acc}} m
          </span>
        </div>
      `, {{className:'loc-popup', maxWidth:220}}).openPopup();

      map.flyTo([lat, lng], Math.max(map.getZoom(), 17), {{animate:true, duration:1.2}});
      showLocToast(`±${{acc}} m accuracy · सटीकता`, '#66bb6a', 4000);
    }},
    err => {{
      btn.classList.remove('locating');
      btn.textContent = '📍';
      const msgs = {{
        1: 'Permission denied · अनुमति नहीं दी',
        2: 'Position unavailable · स्थान अनुपलब्ध',
        3: 'Timeout · समय समाप्त',
      }};
      showLocToast(msgs[err.code] || 'Location error', '#ef5350', 4000);
    }},
    {{ enableHighAccuracy: true, timeout: 12000, maximumAge: 0 }}
  );
}}

// ── Form panel drag-to-resize ─────────────────────────────────────────────
(function() {{
  const handle  = document.getElementById('form-resize-handle');
  const overlay = document.getElementById('zone-overlay');
  let dragging = false, startY = 0, startH = 0;

  handle.addEventListener('mousedown', e => {{
    dragging = true;
    startY = e.clientY;
    startH = overlay.offsetHeight;
    document.body.style.cursor = 'ns-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  }});
  handle.addEventListener('touchstart', e => {{
    dragging = true;
    startY = e.touches[0].clientY;
    startH = overlay.offsetHeight;
    e.preventDefault();
  }}, {{passive:false}});

  function onMove(clientY) {{
    if (!dragging) return;
    const delta = startY - clientY;            // drag up = larger panel
    const newH  = Math.min(window.innerHeight * 0.92, Math.max(160, startH + delta));
    overlay.style.height = newH + 'px';
  }}
  document.addEventListener('mousemove', e => onMove(e.clientY));
  document.addEventListener('touchmove', e => onMove(e.touches[0].clientY), {{passive:false}});

  function onUp() {{
    dragging = false;
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  }}
  document.addEventListener('mouseup', onUp);
  document.addEventListener('touchend', onUp);
}})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# GCS upload
# ---------------------------------------------------------------------------

def publish_dashboard(farm: dict, stats: dict) -> str | None:
    html      = build_html(farm, stats)
    farm_id   = farm.get("id", "farm")
    blob_name = f"{farm_id}/dashboard.html"

    try:
        from google.cloud import storage
        client      = storage.Client(project=os.environ.get("GEE_PROJECT", "agriculture-496920"))
        bucket_name = os.environ.get("GCS_BUCKET", GCS_BUCKET)

        try:
            bucket = client.get_bucket(bucket_name)
        except Exception:
            bucket = client.create_bucket(bucket_name, location="ASIA-SOUTH1")
            bucket.iam_configuration.uniform_bucket_level_access_enabled = True
            bucket.patch()
            policy = bucket.get_iam_policy(requested_policy_version=3)
            policy.bindings.append({"role": "roles/storage.objectViewer", "members": {"allUsers"}})
            bucket.set_iam_policy(policy)
            print(f"  Created bucket: {bucket_name}")

        blob = bucket.blob(blob_name)
        blob.upload_from_string(html.encode("utf-8"), content_type="text/html; charset=utf-8")
        blob.cache_control = "no-cache, max-age=0"
        blob.patch()

        url = f"https://storage.googleapis.com/{bucket_name}/{blob_name}"
        print(f"  Dashboard: {url}")
        return url

    except Exception as e:
        print(f"  [warn] GCS upload failed: {e}")
        local = Path(__file__).parent / "maps" / f"{farm_id}_dashboard.html"
        local.write_text(html, encoding="utf-8")
        print(f"  Saved locally: {local}")
        return None
