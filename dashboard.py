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
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e8eaf0;font-size:14px}}
#app{{display:flex;height:100vh;overflow:hidden}}
#map{{flex:1;height:100%;z-index:1}}

/* Sidebar */
#sidebar{{width:390px;height:100%;background:#13151f;border-left:1px solid #2a2d3a;overflow-y:auto;display:flex;flex-direction:column;z-index:10}}
#sb-head{{padding:14px 16px;background:#1a1d2e;border-bottom:1px solid #2a2d3a;flex-shrink:0}}
#sb-body{{padding:14px 16px;flex:1}}
.farm-title{{font-size:1.15em;font-weight:700;color:#4fc3f7}}
.badge{{display:inline-block;margin-top:5px;padding:3px 12px;border-radius:20px;font-size:0.8em;font-weight:600;background:{badge_color}22;color:{badge_color};border:1px solid {badge_color}55}}
.meta{{margin-top:7px;color:#8890a8;font-size:0.76em;line-height:1.9}}

/* Pills */
.pills{{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:14px}}
.pill{{padding:4px 13px;border-radius:20px;font-size:0.78em;font-weight:600;cursor:pointer;background:#21253a;border:1px solid #3a3d4a;color:#c0c8e0;transition:all .2s}}
.pill:hover{{border-color:#4fc3f7;color:#4fc3f7}}
.pill.active{{background:#1976d233;border-color:#4fc3f7;color:#4fc3f7}}
.pill.zone-pill{{border-color:#ab47bc55;color:#ce93d8}}
.pill.zone-pill.active{{background:#ab47bc22;border-color:#ab47bc;color:#ce93d8}}

/* Sections */
.sec{{margin-bottom:16px}}
.sec-title{{font-size:0.67em;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#8890a8;margin-bottom:9px;padding-bottom:5px;border-bottom:1px solid #2a2d3a}}
.mrow{{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #1e2130;gap:8px;flex-wrap:wrap}}
.mrow:last-child{{border-bottom:none}}
.ml{{font-size:0.83em;color:#c0c8e0;flex:1;min-width:130px}}
.plain{{display:block;font-size:0.74em;color:#8890a8;margin-top:1px}}
.mr{{font-size:0.88em;font-weight:700;display:flex;align-items:center;gap:5px;flex-wrap:wrap}}
.bar-outer{{width:70px;height:5px;background:#2a2d3a;border-radius:3px;display:inline-block;vertical-align:middle;margin-right:5px}}
.bar-inner{{height:100%;border-radius:3px}}
.tag{{display:inline-block;padding:1px 8px;border-radius:10px;font-size:0.72em;font-weight:600}}
.tag.red{{background:#e5393522;color:#e53935}}.tag.orange{{background:#fb8c0022;color:#fb8c00}}
.tag.green{{background:#43a04722;color:#43a047}}.tag.blue{{background:#1976d222;color:#4fc3f7}}
.tag.purple{{background:#ab47bc22;color:#ce93d8}}
.fc-table{{width:100%;border-collapse:collapse;font-size:0.8em}}
.fc-table th{{color:#8890a8;font-weight:600;padding:3px 5px;text-align:left;font-size:0.73em;text-transform:uppercase}}
.fc-table td{{padding:5px 5px;border-bottom:1px solid #1e2130}}
.fc-table tr:last-child td{{border-bottom:none}}

/* Toolbar */
#map-toolbar{{position:absolute;top:14px;left:50%;transform:translateX(-50%);z-index:1000;display:flex;gap:8px;background:#13151fee;padding:7px 12px;border-radius:28px;border:1px solid #2a2d3a;box-shadow:0 2px 16px #0009}}
.tb-btn{{background:none;border:1.5px solid #3a3d4a;color:#8890a8;padding:6px 16px;border-radius:20px;font-size:0.8em;font-weight:600;cursor:pointer;transition:all .2s;white-space:nowrap}}
.tb-btn:hover{{border-color:#4fc3f7;color:#4fc3f7}}
.tb-btn.active{{background:#4fc3f722;border-color:#4fc3f7;color:#4fc3f7}}
.tb-btn.split-active{{background:#ab47bc22;border-color:#ab47bc;color:#ce93d8}}
.tb-btn.cancel{{border-color:#e5393555;color:#e53935}}
.tb-btn.cancel:hover{{background:#e5393522}}
#mode-hint{{position:absolute;top:62px;left:50%;transform:translateX(-50%);z-index:1000;background:#1a1d2eee;color:#fdd835;padding:6px 18px;border-radius:16px;font-size:0.8em;font-weight:600;border:1px solid #fdd83555;display:none;white-space:nowrap}}

/* Zone form overlay */
#zone-overlay{{display:none;position:absolute;bottom:0;left:0;right:0;z-index:2000;background:#13151f;border-top:2px solid #4fc3f7;padding:18px;max-height:80vh;overflow-y:auto}}
#zone-overlay.show{{display:block}}
.form-title{{font-size:1em;font-weight:700;color:#4fc3f7;margin-bottom:14px;display:flex;justify-content:space-between;align-items:center}}
.form-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}}
.form-group{{display:flex;flex-direction:column;gap:4px}}
.form-group.full{{grid-column:1/-1}}
label{{font-size:0.75em;font-weight:600;color:#8890a8;text-transform:uppercase;letter-spacing:0.5px}}
input,select,textarea{{background:#1e2130;border:1px solid #3a3d4a;color:#e8eaf0;padding:8px 10px;border-radius:8px;font-size:0.85em;font-family:inherit;outline:none;transition:border .2s}}
input:focus,select:focus{{border-color:#4fc3f7}}
select option{{background:#1e2130}}
textarea{{resize:vertical;min-height:60px}}

/* Section divider inside form */
.form-section-label{{grid-column:1/-1;font-size:0.68em;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#4fc3f7;padding-top:10px;border-top:1px solid #2a2d3a;margin-top:4px}}

.form-actions{{display:flex;gap:10px;justify-content:flex-end}}
.btn-cancel{{background:none;border:1px solid #3a3d4a;color:#8890a8;padding:8px 18px;border-radius:8px;cursor:pointer;font-size:0.85em}}
.btn-save{{background:#4fc3f7;color:#0f1117;border:none;padding:8px 22px;border-radius:8px;font-weight:700;cursor:pointer;font-size:0.85em}}
.btn-save:hover{{background:#81d4fa}}

/* Zone detail panel */
#zone-detail{{display:none;background:#1a1d2e;border-radius:10px;padding:14px;margin-bottom:14px;border:1px solid #ab47bc55}}
#zone-detail.show{{display:block}}
.zone-detail-title{{font-size:1em;font-weight:700;color:#ce93d8;margin-bottom:10px;display:flex;justify-content:space-between}}
.zone-kv{{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #2a2d3a;font-size:0.83em}}
.zone-kv:last-child{{border-bottom:none}}
.zone-k{{color:#8890a8}}.zone-v{{font-weight:600;color:#e8eaf0}}
.btn-edit-zone{{background:none;border:1px solid #ab47bc55;color:#ce93d8;padding:4px 12px;border-radius:8px;font-size:0.75em;cursor:pointer;margin-top:8px}}
.btn-edit-zone:hover{{background:#ab47bc22}}
.btn-delete-zone{{background:none;border:1px solid #e5393555;color:#e53935;padding:4px 10px;border-radius:8px;font-size:0.75em;cursor:pointer;margin-top:8px;margin-left:6px}}

/* Irrigation badge */
.irr-badge{{display:inline-block;padding:2px 10px;border-radius:10px;font-size:0.75em;font-weight:600;background:#1976d233;color:#4fc3f7;border:1px solid #1976d255}}

@media(max-width:700px){{
  #app{{flex-direction:column}}
  #map{{height:50vh}}
  #sidebar{{width:100%;height:50vh;border-left:none;border-top:1px solid #2a2d3a}}
  .form-grid{{grid-template-columns:1fr}}
  #zone-overlay{{max-height:70vh}}
}}
</style>
</head>
<body>
<div id="app">
  <div id="map">
    <div id="map-toolbar">
      <button id="btn-click-zone" class="tb-btn active" onclick="setMode('click')" title="Click anywhere on a plot to zone the whole plot">👆 Select Plot</button>
      <button id="btn-grid" class="tb-btn" onclick="setMode('grid')" title="Show grid cells — click a cell to zone it">⊞ Grid Zones</button>
      <button id="btn-cancel-mode" class="tb-btn cancel" onclick="setMode('click')" style="display:none">✕ Exit Grid</button>
    </div>
    <div id="mode-hint"></div>
  </div>

  <!-- Zone form overlay (shown over map) -->
  <div id="zone-overlay">
    <div class="form-title">
      <span id="form-title-text">✏️ Add Zone Details</span>
      <button class="btn-cancel" onclick="cancelZone()">✕</button>
    </div>

    <div class="form-grid">

      <!-- ── Basic info ── -->
      <div class="form-section-label">Basic Info</div>

      <div class="form-group">
        <label>Zone / Field Name *</label>
        <input id="z-name" type="text" placeholder="e.g. North Section, Khasra 45">
      </div>
      <div class="form-group">
        <label>Khasra / Survey No.</label>
        <input id="z-khasra" type="text" placeholder="e.g. 123/2">
      </div>
      <div class="form-group">
        <label>Land Type</label>
        <select id="z-land-type">
          <option value="">— Select —</option>
          <option value="Irrigated">Irrigated (सिंचित)</option>
          <option value="Rain-fed">Rain-fed (बारानी)</option>
          <option value="Upland">Upland (ऊँची ज़मीन)</option>
          <option value="Lowland">Lowland / Flood-prone (निचली ज़मीन)</option>
          <option value="Bund / Ridge">Bund / Ridge land</option>
        </select>
      </div>
      <div class="form-group">
        <label>Soil Type</label>
        <select id="z-soil">
          <option value="">— Select —</option>
          <option value="Black cotton">Black cotton / Vertisol (काली मिट्टी)</option>
          <option value="Red laterite">Red laterite (लाल मिट्टी)</option>
          <option value="Alluvial loam">Alluvial loam (दोमट)</option>
          <option value="Sandy loam">Sandy loam (बलुई दोमट)</option>
          <option value="Clay">Heavy clay (चिकनी)</option>
          <option value="Sandy">Sandy (बलुई)</option>
          <option value="Saline / Usar">Saline / Usar (ऊसर)</option>
        </select>
      </div>

      <!-- ── Crop ── -->
      <div class="form-section-label">Crop Details</div>

      <div class="form-group">
        <label>Crop Sowed</label>
        <select id="z-crop">
          <option value="">— Select crop —</option>
          <optgroup label="Kharif (खरीफ)">
            <option value="Soybean">Soybean (सोयाबीन)</option>
            <option value="Maize">Maize / Makka (मक्का)</option>
            <option value="Groundnut">Groundnut (मूंगफली)</option>
            <option value="Cotton">Cotton (कपास)</option>
            <option value="Urad Dal">Urad Dal (उड़द)</option>
            <option value="Moong Dal">Moong Dal (मूंग)</option>
            <option value="Arhar / Tur">Arhar / Tur Dal (अरहर)</option>
            <option value="Sesame">Sesame / Til (तिल)</option>
            <option value="Jowar">Jowar (ज्वार)</option>
            <option value="Bajra">Bajra (बाजरा)</option>
            <option value="Rice">Paddy / Rice (धान)</option>
          </optgroup>
          <optgroup label="Rabi (रबी)">
            <option value="Wheat">Wheat (गेहूँ)</option>
            <option value="Chickpea">Chickpea / Chana (चना)</option>
            <option value="Mustard">Mustard / Sarson (सरसों)</option>
            <option value="Lentil">Lentil / Masoor (मसूर)</option>
            <option value="Garlic">Garlic (लहसुन)</option>
            <option value="Onion">Onion (प्याज)</option>
            <option value="Potato">Potato (आलू)</option>
          </optgroup>
          <optgroup label="Other">
            <option value="Sugarcane">Sugarcane (गन्ना)</option>
            <option value="Banana">Banana (केला)</option>
            <option value="Vegetable">Vegetables (सब्ज़ी)</option>
            <option value="Fallow">Fallow / Khali (खाली)</option>
            <option value="Other">Other</option>
          </optgroup>
        </select>
      </div>
      <div class="form-group">
        <label>Seed Variety</label>
        <select id="z-variety">
          <option value="">— Select variety —</option>
          <optgroup label="Soybean">
            <option value="JS 9305">JS 9305</option>
            <option value="JS 335">JS 335</option>
            <option value="JS 9560">JS 9560</option>
            <option value="RKS 24">RKS 24</option>
            <option value="MACS 450">MACS 450</option>
            <option value="NRC 7">NRC 7</option>
          </optgroup>
          <optgroup label="Wheat">
            <option value="GW 322">GW 322</option>
            <option value="HI 8498">HI 8498 (Malav Shakti)</option>
            <option value="MP 3173">MP 3173</option>
            <option value="Raj 4120">Raj 4120</option>
          </optgroup>
          <optgroup label="Maize">
            <option value="DKC 9108">DKC 9108</option>
            <option value="NK 6240">NK 6240</option>
            <option value="PAC 740">PAC 740</option>
          </optgroup>
          <option value="Local / Desi">Local / Desi variety</option>
          <option value="Other">Other (fill in notes)</option>
        </select>
      </div>
      <div class="form-group">
        <label>Sowing Date</label>
        <input id="z-sowing" type="date">
      </div>
      <div class="form-group">
        <label>Seed Rate</label>
        <select id="z-seed-rate">
          <option value="">— Select —</option>
          <option value="20–25 kg/acre">20–25 kg/acre (Soybean standard)</option>
          <option value="30–35 kg/acre">30–35 kg/acre (Soybean heavy)</option>
          <option value="40–45 kg/acre">40–45 kg/acre (Wheat)</option>
          <option value="8–10 kg/acre">8–10 kg/acre (Maize hybrid)</option>
          <option value="3–4 kg/acre">3–4 kg/acre (Cotton / Bajra)</option>
          <option value="6–8 kg/acre">6–8 kg/acre (Chickpea / Arhar)</option>
          <option value="Other">Other (fill in notes)</option>
        </select>
      </div>
      <div class="form-group">
        <label>Seed Treatment</label>
        <select id="z-seed-treat">
          <option value="">— Select —</option>
          <option value="Rhizobium + PSB">Rhizobium + PSB inoculant (Soybean)</option>
          <option value="Thiram + Carbendazim">Thiram + Carbendazim (fungicide)</option>
          <option value="Imidacloprid">Imidacloprid (insecticide seed coat)</option>
          <option value="Trichoderma">Trichoderma (bio-fungicide)</option>
          <option value="Bavistin">Bavistin (Carbendazim)</option>
          <option value="No treatment">No treatment</option>
          <option value="Other">Other (fill in notes)</option>
        </select>
      </div>

      <!-- ── Irrigation ── -->
      <div class="form-section-label">Irrigation</div>

      <div class="form-group">
        <label>Irrigation Method</label>
        <select id="z-irr-method">
          <option value="">— Select —</option>
          <option value="Flood">Flood / Furrow (बाढ़ सिंचाई)</option>
          <option value="Drip">Drip irrigation (टपक)</option>
          <option value="Sprinkler">Sprinkler / Mini sprinkler</option>
          <option value="Canal">Canal water (नहर)</option>
          <option value="Borewell">Borewell / Tubewell</option>
          <option value="Open well">Open well (कुआँ)</option>
          <option value="Farm pond">Farm pond (तालाब)</option>
          <option value="Rain-fed">Rain-fed only (बारानी)</option>
        </select>
      </div>
      <div class="form-group">
        <label>Water Source</label>
        <select id="z-water-source">
          <option value="">— Select —</option>
          <option value="Borewell">Borewell / Tubewell</option>
          <option value="Canal">Canal (नहर)</option>
          <option value="River / Nala">River / Nala</option>
          <option value="Farm pond">Farm pond</option>
          <option value="Open well">Open well (कुआँ)</option>
          <option value="Tanker">Water tanker</option>
          <option value="Rain-fed">Rain only</option>
        </select>
      </div>
      <div class="form-group">
        <label>Last Irrigation Date</label>
        <input id="z-irrigation" type="date">
      </div>
      <div class="form-group">
        <label>Irrigation Frequency</label>
        <select id="z-irr-freq">
          <option value="">— Select —</option>
          <option value="Every 7 days">Every 7 days</option>
          <option value="Every 10 days">Every 10 days</option>
          <option value="Every 15 days">Every 15 days</option>
          <option value="Every 21 days">Every 21 days (3 weeks)</option>
          <option value="As needed">As needed / on demand</option>
          <option value="Rain-fed">No irrigation — rain-fed</option>
        </select>
      </div>

      <!-- ── Inputs applied ── -->
      <div class="form-section-label">Inputs Applied</div>

      <div class="form-group">
        <label>Base Fertiliser (at sowing)</label>
        <select id="z-fert-base">
          <option value="">— Select —</option>
          <option value="DAP 50 kg/acre">DAP 50 kg/acre</option>
          <option value="DAP 25 kg/acre">DAP 25 kg/acre (half dose)</option>
          <option value="SSP 100 kg/acre">SSP 100 kg/acre</option>
          <option value="NPK 12:32:16 50kg/acre">NPK 12:32:16 @ 50 kg/acre</option>
          <option value="Urea 25 kg/acre">Urea 25 kg/acre</option>
          <option value="MOP 25 kg/acre">MOP (Potash) 25 kg/acre</option>
          <option value="Zinc sulphate 10 kg/acre">Zinc sulphate 10 kg/acre</option>
          <option value="FYM 4 ton/acre">FYM / Compost 4 ton/acre</option>
          <option value="No basal">No basal fertiliser</option>
          <option value="Other">Other (fill in notes)</option>
        </select>
      </div>
      <div class="form-group">
        <label>Top Dressing Fertiliser</label>
        <select id="z-fert-top">
          <option value="">— Select —</option>
          <option value="Urea 25 kg/acre at 30 days">Urea 25 kg/acre at 30 days</option>
          <option value="Urea 50 kg/acre at 30 days">Urea 50 kg/acre at 30 days</option>
          <option value="NPK 19:19:19 foliar spray">NPK 19:19:19 foliar spray</option>
          <option value="DAP 2% foliar spray">DAP 2% foliar spray</option>
          <option value="Boron 0.2% foliar">Boron 0.2% foliar spray</option>
          <option value="Micronutrient mix foliar">Micronutrient mix foliar</option>
          <option value="Not applied">Not applied yet</option>
          <option value="Other">Other (fill in notes)</option>
        </select>
      </div>
      <div class="form-group">
        <label>Herbicide / Weedicide</label>
        <select id="z-herbicide">
          <option value="">— Select —</option>
          <option value="Imazethapyr (Pursuit)">Imazethapyr / Pursuit (Soybean)</option>
          <option value="Quizalofop (Targa Super)">Quizalofop / Targa Super</option>
          <option value="Pendimethalin (pre-emergence)">Pendimethalin (pre-emergence)</option>
          <option value="Atrazine (Maize)">Atrazine (Maize)</option>
          <option value="2,4-D (Wheat)">2,4-D (Wheat broadleaf)</option>
          <option value="Clodinafop (Topik Wheat)">Clodinafop / Topik (Wheat narrow)</option>
          <option value="Manual weeding">Manual weeding only</option>
          <option value="Not applied">Not applied</option>
          <option value="Other">Other (fill in notes)</option>
        </select>
      </div>
      <div class="form-group">
        <label>Insecticide / Pesticide</label>
        <select id="z-pesticide">
          <option value="">— Select —</option>
          <option value="Chlorpyrifos 20EC">Chlorpyrifos 20EC</option>
          <option value="Lambda-cyhalothrin">Lambda-cyhalothrin (Karate)</option>
          <option value="Imidacloprid (Confidor)">Imidacloprid / Confidor</option>
          <option value="Profenofos + Cypermethrin">Profenofos + Cypermethrin</option>
          <option value="Thiamethoxam (Actara)">Thiamethoxam / Actara</option>
          <option value="Emamectin benzoate">Emamectin benzoate</option>
          <option value="Neem oil spray">Neem oil spray (organic)</option>
          <option value="Not applied">Not applied</option>
          <option value="Other">Other (fill in notes)</option>
        </select>
      </div>
      <div class="form-group">
        <label>Fungicide</label>
        <select id="z-fungicide">
          <option value="">— Select —</option>
          <option value="Mancozeb (Dithane M-45)">Mancozeb / Dithane M-45</option>
          <option value="Carbendazim + Mancozeb">Carbendazim + Mancozeb</option>
          <option value="Hexaconazole">Hexaconazole</option>
          <option value="Propiconazole (Tilt)">Propiconazole / Tilt</option>
          <option value="Metalaxyl + Mancozeb">Metalaxyl + Mancozeb (Ridomil)</option>
          <option value="Trifloxystrobin">Trifloxystrobin (Nativo)</option>
          <option value="Not applied">Not applied</option>
          <option value="Other">Other (fill in notes)</option>
        </select>
      </div>
      <div class="form-group">
        <label>Previous Season Crop</label>
        <select id="z-prev-crop">
          <option value="">— Select —</option>
          <option value="Soybean">Soybean</option>
          <option value="Wheat">Wheat</option>
          <option value="Chickpea">Chickpea</option>
          <option value="Mustard">Mustard</option>
          <option value="Maize">Maize</option>
          <option value="Cotton">Cotton</option>
          <option value="Fallow">Fallow (khali)</option>
          <option value="Other">Other</option>
        </select>
      </div>

      <!-- ── Notes ── -->
      <div class="form-section-label">Notes</div>

      <div class="form-group full">
        <label>Additional Notes</label>
        <textarea id="z-notes" placeholder="Any other observations — pest sighting, crop damage, flooding, yield estimate..."></textarea>
      </div>

    </div>

    <div class="form-actions" style="margin-top:14px">
      <button class="btn-cancel" onclick="cancelZone()">Cancel</button>
      <button class="btn-save" onclick="saveZone()">Save Zone</button>
    </div>
  </div>

  <!-- Sidebar -->
  <div id="sidebar">
    <div id="sb-head">
      <div class="farm-title">🛰️ {name}</div>
      <div class="badge">{badge}</div>
      <div class="meta">
        📅 {date} &nbsp;·&nbsp; {crop} &nbsp;·&nbsp; {area_bigha} bigha total<br>
        🕐 Updated {generated}
      </div>
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
        <div class="sec-title">📡 Crop Health <span style="font-weight:400;text-transform:none;letter-spacing:0">(from satellite)</span></div>
        <div class="mrow"><span class="ml">NDVI — Crop Greenness<span class="plain">0 = bare soil · 1 = dense crop</span></span><div class="mr">{_bar_html(max(0,ndvi_mean),0.8)}<b>{ndvi_mean}</b></div></div>
        <div class="mrow"><span class="ml">NDRE — Early Warning<span class="plain">catches stress 2–3 weeks before it shows</span></span><div class="mr">{_bar_html(max(0,ndre_mean),0.8)}<b>{ndre_mean}</b>{"<span class='tag orange'>Early stress</span>" if stats.get("ndre_stress") else ""}</div></div>
        <div class="mrow"><span class="ml">LSWI — Leaf Water<span class="plain">water inside the leaves — low = drought</span></span><div class="mr">{_bar_html(max(0,lswi_mean+0.5),1.0)}<b>{lswi_mean}</b>{"<span class='tag red'>Drought stress</span>" if lswi_mean<0 else ""}</div></div>
        <div class="mrow"><span class="ml">EVI — Canopy Thickness<span class="plain">how full and dense the crop is</span></span><div class="mr">{_bar_html(max(0,evi_mean),0.8)}<b>{evi_mean}</b></div></div>
        <div class="mrow"><span class="ml">Stress Area<span class="plain">% of field with unhealthy readings</span></span><div class="mr">{_bar_html(stress_pct,100)}<b>{stress_pct}%</b>{"<span class='tag red'>High</span>" if stress_pct>30 else ("<span class='tag orange'>Moderate</span>" if stress_pct>10 else "<span class='tag green'>Low</span>")}</div></div>
        <div class="mrow"><span class="ml">vs Last Year<span class="plain">same 10-day window in 2025</span></span><span class="mr">{yoy}</span></div>
        <div class="mrow"><span class="ml">Images used<span class="plain">more = more accurate</span></span><span class="mr">{stats.get("image_count","?")} imgs · {stats.get("cloud_pct","?")}% cloud</span></div>
      </div>

      <!-- Weather -->
      <div class="sec">
        <div class="sec-title">🌤️ Current Weather</div>
        <div class="mrow"><span class="ml">Temperature<span class="plain">above 38°C damages soybean flowers</span></span><span class="mr"><b>{temp_c}°C</b> <span style="color:#8890a8;font-size:0.85em">feels {feels}°C</span>{"<span class='tag red'>Heat stress</span>" if (w.get("temp_c") or 0)>38 else ""}</span></div>
        <div class="mrow"><span class="ml">Humidity<span class="plain">low = faster drying</span></span><div class="mr">{_bar_html(w.get("humidity_pct",0),100)}<b>{hum}%</b></div></div>
        <div class="mrow"><span class="ml">Wind<span class="plain">strong wind can lodge standing crop</span></span><span class="mr"><b>{wind} km/h</b> {wind_dir} <span style="color:#8890a8;font-size:0.85em">gusts {gusts}</span></span></div>
        <div class="mrow"><span class="ml">Cloud cover</span><div class="mr">{_bar_html(w.get("cloud_cover_pct",0),100)}<b>{cloud}%</b></div></div>
        {vpd_html}
        <div class="mrow"><span class="ml">Rain today<span class="plain">actual rainfall</span></span><span class="mr"><b>{rain_now} mm</b> <span style="color:#8890a8;font-size:0.85em">{rain_ch}% chance</span></span></div>
        <div class="mrow"><span class="ml">Rain this week<span class="plain">last 7 days total</span></span><span class="mr"><b>{rain_7d} mm</b>{"<span class='tag red'>Drought risk</span>" if (w.get("rain_7d_mm") or 99)<10 else ""}</span></div>
        {et0_html}
      </div>

      <!-- Soil -->
      <div class="sec">
        <div class="sec-title">🌍 Soil</div>
        {soil_html or "<div style='color:#8890a8;font-size:0.83em;padding:8px 0'>No soil data</div>"}
      </div>

      <!-- Forecast -->
      <div class="sec">
        <div class="sec-title">📅 7-Day Forecast</div>
        <table class="fc-table">
          <tr><th>Day</th><th>Max</th><th>Rain</th><th>Chance</th></tr>
          {forecast_rows}
        </table>
      </div>

    </div><!-- sb-body -->
  </div><!-- sidebar -->
</div><!-- app -->

<script>
// ── Map ───────────────────────────────────────────────────────────────────
const map = L.map('map').setView([{center_lat},{center_lon}], 15);

L.tileLayer('https://mt{{s}}.google.com/vt/lyrs=s&x={{x}}&y={{y}}&z={{z}}', {{
  subdomains:'0123', attribution:'© Google', maxZoom:21, maxNativeZoom:21
}}).addTo(map);
L.tileLayer('https://mt{{s}}.google.com/vt/lyrs=h&x={{x}}&y={{y}}&z={{z}}', {{
  subdomains:'0123', attribution:'', maxZoom:21, maxNativeZoom:21, opacity:0.8
}}).addTo(map);

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
// Cell size in degrees at each zoom level (roughly 50–80m wide cells on screen)
const GRID_DEG = {{
  14: 0.003, 15: 0.0015, 16: 0.0008, 17: 0.0004,
  18: 0.0002, 19: 0.0001, 20: 0.00005, 21: 0.000025,
}};

function buildGrid() {{
  if (gridLayer) {{ map.removeLayer(gridLayer); gridLayer = null; }}
  const z = map.getZoom();
  const step = GRID_DEG[Math.round(z)] || GRID_DEG[Math.max(14, Math.min(21, Math.round(z)))] || 0.001;

  gridLayer = L.layerGroup().addTo(map);

  farmData.features.forEach(feat => {{
    const poly = turf.polygon(feat.geometry.coordinates);
    const bb = turf.bbox(poly);                    // [minLng, minLat, maxLng, maxLat]
    const plotName = feat.properties.name;

    // Snap grid origin to step multiples so cells are stable across pans
    const x0 = Math.floor(bb[0] / step) * step;
    const y0 = Math.floor(bb[1] / step) * step;

    for (let x = x0; x < bb[2]; x += step) {{
      for (let y = y0; y < bb[3]; y += step) {{
        const cell = turf.polygon([[
          [x,      y],
          [x+step, y],
          [x+step, y+step],
          [x,      y+step],
          [x,      y],
        ]]);
        const clipped = turf.intersect(poly, cell);
        if (!clipped) continue;

        // Only keep cells with meaningful area (> 2% of a full cell)
        const cellSqm = step * step * 111000 * 111000 * Math.cos(y * Math.PI / 180);
        const clippedSqm = calcAreaSqm(clipped.geometry.coordinates[0]);
        if (clippedSqm < cellSqm * 0.02) continue;

        const lyr = L.geoJSON(clipped, {{
          style: {{ color:'#fdd835', weight:1, fillColor:'#fdd835', fillOpacity:0.08, dashArray:'3 3' }},
        }}).addTo(gridLayer);

        lyr.on('mouseover', () => lyr.setStyle({{ fillOpacity:0.3, weight:2 }}));
        lyr.on('mouseout',  () => lyr.setStyle({{ fillOpacity:0.08, weight:1 }}));
        lyr.on('click', e => {{
          L.DomEvent.stopPropagation(e);
          pendingGeojson = clipped.geometry;
          const sqm = clippedSqm;
          const cx = (x + x + step) / 2, cy = (y + y + step) / 2;
          openZoneForm(null, sqm, plotName + ' — cell');
        }});
      }}
    }}
  }});
}}

// Rebuild grid when zoom changes (cell size changes)
map.on('zoomend', () => {{ if (currentMode === 'grid') buildGrid(); }});

// ── Mode switching ────────────────────────────────────────────────────────
function setMode(mode) {{
  currentMode = mode;
  const hint = document.getElementById('mode-hint');
  const cancelBtn = document.getElementById('btn-cancel-mode');
  document.querySelectorAll('.tb-btn').forEach(b => b.classList.remove('active','split-active'));

  if (mode === 'grid') {{
    document.getElementById('btn-grid').classList.add('split-active');
    hint.textContent = '⊞ Click any yellow cell to zone it — zoom in for smaller cells';
    hint.style.display = 'block';
    cancelBtn.style.display = 'block';
    buildGrid();
  }} else {{
    if (gridLayer) {{ map.removeLayer(gridLayer); gridLayer = null; }}
    document.getElementById('btn-click-zone').classList.add('active');
    hint.style.display = 'none';
    cancelBtn.style.display = 'none';
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

// ── Zone form ─────────────────────────────────────────────────────────────
function openZoneForm(zoneId, preSqm, preName) {{
  editingZoneId = zoneId;

  const FIELD_IDS = ['z-name','z-khasra','z-land-type','z-soil','z-crop','z-variety','z-sowing','z-seed-rate','z-seed-treat','z-irr-method','z-water-source','z-irrigation','z-irr-freq','z-fert-base','z-fert-top','z-herbicide','z-pesticide','z-fungicide','z-prev-crop','z-notes'];
  if (zoneId) {{
    const z = zones.find(x => x.id === zoneId);
    document.getElementById('form-title-text').textContent = '✏️ Edit Zone';
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
    document.getElementById('z-irr-method').value = z.irrigation_method || '';
    document.getElementById('z-water-source').value = z.water_source || '';
    document.getElementById('z-irrigation').value = z.irrigation_date || '';
    document.getElementById('z-irr-freq').value   = z.irrigation_freq || '';
    document.getElementById('z-fert-base').value  = z.fertiliser_base || '';
    document.getElementById('z-fert-top').value   = z.fertiliser_top || '';
    document.getElementById('z-herbicide').value  = z.herbicide || '';
    document.getElementById('z-pesticide').value  = z.pesticide || '';
    document.getElementById('z-fungicide').value  = z.fungicide || '';
    document.getElementById('z-prev-crop').value  = z.prev_crop || '';
    document.getElementById('z-notes').value      = z.notes || '';
  }} else {{
    document.getElementById('form-title-text').textContent = preSqm
      ? `📌 Add Zone — ${{Math.round(preSqm/1333.33*10)/10}} bigha (${{Math.round(preSqm).toLocaleString()}} m²)`
      : '📌 Add Zone';
    FIELD_IDS.forEach(id => {{
      const el = document.getElementById(id); if(el) el.value='';
    }});
    document.getElementById('z-name').value = preName || '';
  }}
  document.getElementById('zone-overlay').classList.add('show');
}}

function cancelZone() {{
  document.getElementById('zone-overlay').classList.remove('show');
  pendingGeojson = null;
  pendingQueue = [];
  editingZoneId = null;
  setMode('click');
}}

function saveZone() {{
  const name = document.getElementById('z-name').value.trim();
  if (!name) {{ alert('Please enter a zone name'); return; }}

  const zoneData = {{
    name,
    khasra:            document.getElementById('z-khasra').value,
    land_type:         document.getElementById('z-land-type').value,
    soil_type:         document.getElementById('z-soil').value,
    crop:              document.getElementById('z-crop').value,
    variety:           document.getElementById('z-variety').value,
    sowing_date:       document.getElementById('z-sowing').value,
    seed_rate:         document.getElementById('z-seed-rate').value,
    seed_treatment:    document.getElementById('z-seed-treat').value,
    irrigation_method: document.getElementById('z-irr-method').value,
    water_source:      document.getElementById('z-water-source').value,
    irrigation_date:   document.getElementById('z-irrigation').value,
    irrigation_freq:   document.getElementById('z-irr-freq').value,
    fertiliser_base:   document.getElementById('z-fert-base').value,
    fertiliser_top:    document.getElementById('z-fert-top').value,
    herbicide:         document.getElementById('z-herbicide').value,
    pesticide:         document.getElementById('z-pesticide').value,
    fungicide:         document.getElementById('z-fungicide').value,
    prev_crop:         document.getElementById('z-prev-crop').value,
    notes:             document.getElementById('z-notes').value,
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

  const irrLine = [z.irrigation_date, z.irrigation_method, z.water_source].filter(Boolean).join(' · ') || '—';

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
    `<div class="zone-kv"><span class="zone-k">Last irrigation</span><span class="zone-v"><span class="irr-badge">💧 ${{irrLine}}</span></span></div>` +
    kv('Irrigation freq',  z.irrigation_freq) +
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
