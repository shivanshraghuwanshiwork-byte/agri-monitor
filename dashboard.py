"""
Build a Google Maps-style interactive HTML dashboard and upload to GCS.
- Full-screen satellite map with farm polygons highlighted
- Click any plot → sidebar shows all stats in plain English
- Works on mobile (pinch zoom, tap to select)
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

GCS_BUCKET = os.environ.get("GCS_BUCKET", "agri-monitor-dashboard")
MP_BIGHA_SQM = 1333.33


def _polygon_area_sqm(coords):
    lat0 = sum(c[1] for c in coords) / len(coords)
    mpl  = 111_000.0
    mplo = 111_000.0 * math.cos(math.radians(lat0))
    pts  = [(c[0] * mplo, c[1] * mpl) for c in coords]
    n    = len(pts)
    return abs(sum(pts[i][0] * (pts[(i+1)%n][1] - pts[(i-1)%n][1]) for i in range(n))) / 2


def _bar(value: float, max_val: float, width: int = 120) -> str:
    pct = min(100, max(0, (value / max_val) * 100))
    if pct < 33:   color = "#e53935"
    elif pct < 66: color = "#fb8c00"
    else:          color = "#43a047"
    return (
        f'<div class="bar-outer">'
        f'<div class="bar-inner" style="width:{pct:.1f}%;background:{color}"></div>'
        f'</div>'
    )


def _health_color(ndvi: float) -> str:
    if ndvi < 0.2:  return "#e53935"   # red — bare/dead
    if ndvi < 0.35: return "#fb8c00"   # orange — stressed
    if ndvi < 0.5:  return "#fdd835"   # yellow — moderate
    return "#43a047"                    # green — healthy


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

    status_color = {"stress": "#e53935", "no_data": "#fb8c00", "ok": "#43a047"}.get(status, "#888")
    status_label = {"stress": "⚠ Stress Detected", "no_data": "☁ No Data", "ok": "✓ Healthy"}.get(status, status)

    # Build plots GeoJSON for Leaflet
    plots = farm.get("plots", [])
    if not plots:
        coords = farm.get("boundary", {}).get("coordinates", [])
        plots = [{"id": farm.get("id","farm"), "name": name, "boundary": {"coordinates": coords}}]

    features = []
    for plot in plots:
        coords = plot["boundary"]["coordinates"]
        sqm    = _polygon_area_sqm(coords)
        bigha  = round(sqm / MP_BIGHA_SQM, 1)
        color  = _health_color(ndvi_mean)
        features.append({
            "type": "Feature",
            "properties": {
                "id":    plot["id"],
                "name":  plot["name"],
                "bigha": bigha,
                "sqm":   round(sqm),
                "color": color,
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [coords],
            }
        })

    geojson = json.dumps({"type": "FeatureCollection", "features": features})

    # Centre map on all plots
    all_coords = [c for p in plots for c in p["boundary"]["coordinates"]]
    center_lat = sum(c[1] for c in all_coords) / len(all_coords)
    center_lon = sum(c[0] for c in all_coords) / len(all_coords)

    # ── Year-over-year ────────────────────────────────────────────────────────
    ndvi_ly = stats.get("ndvi_last_year")
    if ndvi_ly is not None:
        diff  = round(ndvi_mean - ndvi_ly, 3)
        arrow = "▲ better" if diff > 0.02 else ("▼ worse" if diff < -0.02 else "≈ similar")
        yoy   = f'{ndvi_ly} <span style="color:{"#43a047" if diff>0.02 else "#e53935"}">{arrow} ({"+"+str(diff) if diff>=0 else str(diff)})</span>'
    else:
        yoy = "—"

    # ── 7-day forecast rows ───────────────────────────────────────────────────
    forecast_rows = ""
    if w and w.get("forecast_dates"):
        for i, d in enumerate(w["forecast_dates"]):
            label = "Today" if i == 0 else ("Tomorrow" if i == 1 else datetime.fromisoformat(d).strftime("%a %d %b"))
            r  = w["forecast_rain"][i]        if i < len(w.get("forecast_rain",[])) else 0
            ch = w["forecast_rain_chance"][i] if i < len(w.get("forecast_rain_chance",[])) else 0
            mx = w["forecast_max_temp"][i]    if i < len(w.get("forecast_max_temp",[])) else "?"
            icon = "🌧️" if (r or 0) > 5 else ("🌦️" if (ch or 0) > 40 else "☀️")
            forecast_rows += f'<tr><td>{icon} {label}</td><td><b>{mx}°C</b></td><td>{r or 0:.1f} mm</td><td>{ch or 0}%</td></tr>'

    # ── Soil rows ─────────────────────────────────────────────────────────────
    sm0  = w.get("soil_moisture_pct_0")
    sm3  = w.get("soil_moisture_pct_3")
    sm27 = w.get("soil_moisture_pct_27")
    st0  = w.get("soil_temp_surface_c")

    def soil_label(pct):
        if pct is None: return ""
        if pct < 15: return '<span class="tag red">Critically dry</span>'
        if pct < 35: return '<span class="tag orange">Dry</span>'
        if pct < 70: return '<span class="tag green">Good</span>'
        return '<span class="tag blue">Waterlogged</span>'

    soil_html = ""
    if sm0  is not None: soil_html += f'<div class="metric-row"><span class="ml">Surface (0–1 cm) <span class="plain">how wet is the topsoil</span></span><div>{_bar(sm0,100)}<span class="mv">{sm0}%</span> {soil_label(sm0)}</div></div>'
    if sm3  is not None: soil_html += f'<div class="metric-row"><span class="ml">Root zone (3–9 cm) <span class="plain">where most roots drink from</span></span><div>{_bar(sm3,100)}<span class="mv">{sm3}%</span> {soil_label(sm3)}</div></div>'
    if sm27 is not None: soil_html += f'<div class="metric-row"><span class="ml">Deep (27–81 cm) <span class="plain">stored water reserve</span></span><div>{_bar(sm27,100)}<span class="mv">{sm27}%</span> {soil_label(sm27)}</div></div>'
    if st0  is not None: soil_html += f'<div class="metric-row"><span class="ml">Soil temperature <span class="plain">too hot can burn roots</span></span><span class="mv">{st0}°C {"🔥" if st0 > 35 else ""}</span></div>'
    sar_label = stats.get("soil_moisture_label")
    if sar_label:        soil_html += f'<div class="metric-row"><span class="ml">Radar moisture (SAR) <span class="plain">satellite radar reading, works through clouds</span></span><span class="mv">{sar_label} ({stats.get("soil_vv_db")} dB)</span></div>'

    # ── Wind ─────────────────────────────────────────────────────────────────
    wind     = w.get("wind_kmh", 0) or 0
    gusts    = w.get("wind_gusts_kmh", "—")
    wind_dir = w.get("wind_dir", "")
    vpd      = w.get("vpd_kpa")
    vpd_html = ""
    if vpd is not None:
        vpd_flag = '<span class="tag orange">High — crop losing water fast</span>' if vpd > 3 else '<span class="tag green">Normal</span>'
        vpd_html = f'<div class="metric-row"><span class="ml">VPD <span class="plain">how thirsty the air is — high means crops dry out faster</span></span><div>{_bar(min(vpd,6),6)}<span class="mv">{vpd} kPa</span> {vpd_flag}</div></div>'

    et0 = w.get("et0_today_mm")
    et0_html = f'<div class="metric-row"><span class="ml">Water need today (ET₀) <span class="plain">how many mm of water the crop needs today</span></span><span class="mv">{et0} mm</span></div>' if et0 else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name} — Agri Monitor</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
html,body {{ height:100%; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:#0f1117; color:#e8eaf0; }}

/* ── Layout ── */
#app {{ display:flex; height:100vh; overflow:hidden; }}
#map {{ flex:1; height:100%; z-index:1; }}

/* ── Sidebar ── */
#sidebar {{
  width:380px; height:100%; background:#13151f;
  border-left:1px solid #2a2d3a; overflow-y:auto;
  transition:transform 0.3s ease; z-index:10;
  display:flex; flex-direction:column;
}}
#sidebar-header {{
  padding:16px; background:#1a1d2e; border-bottom:1px solid #2a2d3a; flex-shrink:0;
}}
#sidebar-content {{ padding:16px; flex:1; }}

.farm-title {{ font-size:1.2em; font-weight:700; color:#4fc3f7; }}
.status-chip {{
  display:inline-block; margin-top:6px; padding:3px 12px;
  border-radius:20px; font-size:0.82em; font-weight:600;
  background:{status_color}22; color:{status_color}; border:1px solid {status_color}55;
}}
.meta {{ margin-top:8px; color:#8890a8; font-size:0.78em; line-height:1.8; }}

/* ── Sections ── */
.section {{ margin-bottom:16px; }}
.section-title {{
  font-size:0.68em; font-weight:700; text-transform:uppercase;
  letter-spacing:1px; color:#8890a8; margin-bottom:10px; padding-bottom:6px;
  border-bottom:1px solid #2a2d3a;
}}
.metric-row {{
  display:flex; justify-content:space-between; align-items:center;
  padding:8px 0; border-bottom:1px solid #1e2130; gap:8px; flex-wrap:wrap;
}}
.metric-row:last-child {{ border-bottom:none; }}
.ml {{ font-size:0.85em; color:#c0c8e0; flex:1; min-width:140px; }}
.plain {{ display:block; font-size:0.75em; color:#8890a8; font-weight:400; margin-top:1px; }}
.mv {{ font-size:0.9em; font-weight:700; white-space:nowrap; }}

/* ── Bar ── */
.bar-outer {{ width:80px; height:5px; background:#2a2d3a; border-radius:3px; display:inline-block; vertical-align:middle; margin-right:6px; }}
.bar-inner {{ height:100%; border-radius:3px; }}

/* ── Tags ── */
.tag {{ display:inline-block; padding:2px 8px; border-radius:10px; font-size:0.72em; font-weight:600; }}
.tag.red    {{ background:#e5393522; color:#e53935; }}
.tag.orange {{ background:#fb8c0022; color:#fb8c00; }}
.tag.green  {{ background:#43a04722; color:#43a047; }}
.tag.blue   {{ background:#1976d222; color:#4fc3f7; }}

/* ── Forecast table ── */
.fc-table {{ width:100%; border-collapse:collapse; font-size:0.82em; }}
.fc-table th {{ color:#8890a8; font-weight:600; padding:4px 6px; text-align:left; font-size:0.75em; text-transform:uppercase; }}
.fc-table td {{ padding:6px 6px; border-bottom:1px solid #1e2130; }}
.fc-table tr:last-child td {{ border-bottom:none; }}

/* ── Plot selector pills ── */
#plot-pills {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; }}
.plot-pill {{
  padding:5px 14px; border-radius:20px; font-size:0.8em; font-weight:600; cursor:pointer;
  background:#21253a; border:1px solid #3a3d4a; color:#c0c8e0; transition:all 0.2s;
}}
.plot-pill.active {{ background:#1976d233; border-color:#4fc3f7; color:#4fc3f7; }}

/* ── Mobile ── */
@media(max-width:700px) {{
  #app {{ flex-direction:column; }}
  #map {{ height:55vh; }}
  #sidebar {{ width:100%; height:45vh; border-left:none; border-top:1px solid #2a2d3a; }}
  .bar-outer {{ width:55px; }}
}}

/* ── Map attribution fix ── */
.leaflet-control-attribution {{ background:rgba(0,0,0,0.6) !important; color:#aaa !important; }}
.leaflet-control-attribution a {{ color:#4fc3f7 !important; }}
</style>
</head>
<body>
<div id="app">
  <div id="map"></div>
  <div id="sidebar">
    <div id="sidebar-header">
      <div class="farm-title">🛰️ {name}</div>
      <div class="status-chip">{status_label}</div>
      <div class="meta">
        📅 {date} &nbsp;·&nbsp; {crop} &nbsp;·&nbsp; {area_bigha} bigha total<br>
        🕐 Updated {generated}
      </div>
    </div>

    <div id="sidebar-content">

      <!-- Plot selector -->
      <div id="plot-pills">
        <div class="plot-pill active" onclick="selectPlot('all')" id="pill-all">All Plots</div>
        {''.join(f'<div class="plot-pill" onclick="selectPlot(\'{p["id"]}\')" id="pill-{p["id"]}">{p["name"]}</div>' for p in (farm.get("plots") or []))}
      </div>

      <!-- Satellite Indices -->
      <div class="section">
        <div class="section-title">📡 Crop Health  <span style="font-weight:400;text-transform:none;letter-spacing:0">(from satellite)</span></div>

        <div class="metric-row">
          <span class="ml">NDVI <span class="plain">overall greenness — 0 = bare soil, 1 = dense crop</span></span>
          <div>{_bar(max(0,ndvi_mean),0.8)}<span class="mv">{ndvi_mean}</span></div>
        </div>
        <div class="metric-row">
          <span class="ml">NDRE — Early Warning <span class="plain">catches stress 2–3 weeks before it shows in NDVI</span></span>
          <div>{_bar(max(0,ndre_mean),0.8)}<span class="mv">{ndre_mean}</span>
          {'<span class="tag orange">Early stress signal</span>' if stats.get("ndre_stress") else ''}</div>
        </div>
        <div class="metric-row">
          <span class="ml">LSWI — Leaf Water <span class="plain">how much water is inside the leaves</span></span>
          <div>{_bar(max(0,lswi_mean+0.5),1.0)}<span class="mv">{lswi_mean}</span>
          {'<span class="tag red">Drought stress</span>' if lswi_mean < 0 else ''}</div>
        </div>
        <div class="metric-row">
          <span class="ml">EVI — Canopy Density <span class="plain">how thick and full the crop canopy is</span></span>
          <div>{_bar(max(0,evi_mean),0.8)}<span class="mv">{evi_mean}</span></div>
        </div>
        <div class="metric-row">
          <span class="ml">Stress Area <span class="plain">% of field with unhealthy crop</span></span>
          <div>{_bar(stress_pct,100)}<span class="mv">{stress_pct}%</span>
          {'<span class="tag red">High</span>' if stress_pct > 30 else ('<span class="tag orange">Moderate</span>' if stress_pct > 10 else '<span class="tag green">Low</span>')}</div>
        </div>
        <div class="metric-row">
          <span class="ml">vs Last Year <span class="plain">same 10-day window in 2025</span></span>
          <span class="mv">{yoy}</span>
        </div>
        <div class="metric-row">
          <span class="ml">Satellite images used <span class="plain">more = more accurate reading</span></span>
          <span class="mv">{stats.get("image_count","?")} images · {stats.get("cloud_pct","?")}% cloud</span>
        </div>
      </div>

      <!-- Weather -->
      <div class="section">
        <div class="section-title">🌤️ Current Weather</div>
        <div class="metric-row">
          <span class="ml">Temperature <span class="plain">above 38°C damages soybean flowers</span></span>
          <span class="mv">{w.get("temp_c","—")}°C &nbsp;<span style="color:#8890a8;font-size:0.85em">feels {w.get("feels_like_c","—")}°C</span>
          {'<span class="tag red">Heat stress</span>' if (w.get("temp_c") or 0) > 38 else ''}</span>
        </div>
        <div class="metric-row">
          <span class="ml">Humidity <span class="plain">low humidity = faster drying, disease risk if too high</span></span>
          <div>{_bar(w.get("humidity_pct",0),100)}<span class="mv">{w.get("humidity_pct","—")}%</span></div>
        </div>
        <div class="metric-row">
          <span class="ml">Wind <span class="plain">strong wind can damage standing crop</span></span>
          <span class="mv">{w.get("wind_kmh","—")} km/h {wind_dir} <span style="color:#8890a8;font-size:0.85em">gusts {gusts} km/h</span></span>
        </div>
        <div class="metric-row">
          <span class="ml">Cloud Cover <span class="plain">affects how much sun the crop gets</span></span>
          <div>{_bar(w.get("cloud_cover_pct",0),100)}<span class="mv">{w.get("cloud_cover_pct","—")}%</span></div>
        </div>
        {vpd_html}
        <div class="metric-row">
          <span class="ml">Rain today <span class="plain">actual rainfall recorded today</span></span>
          <span class="mv">{w.get("rain_today_mm","—")} mm &nbsp;<span style="color:#8890a8;font-size:0.85em">{w.get("rain_chance_pct","—")}% chance</span></span>
        </div>
        <div class="metric-row">
          <span class="ml">Rain this week <span class="plain">total rainfall in last 7 days</span></span>
          <span class="mv">{w.get("rain_7d_mm","—")} mm
          {'<span class="tag red">Drought risk</span>' if (w.get("rain_7d_mm") or 99) < 10 else ''}</span>
        </div>
        {et0_html}
      </div>

      <!-- Soil -->
      <div class="section">
        <div class="section-title">🌍 Soil</div>
        {soil_html if soil_html else '<div class="metric-row"><span style="color:#8890a8;font-size:0.85em">Soil data unavailable</span></div>'}
      </div>

      <!-- Forecast -->
      <div class="section">
        <div class="section-title">📅 7-Day Rain Forecast</div>
        <table class="fc-table">
          <tr><th>Day</th><th>Max Temp</th><th>Rain</th><th>Chance</th></tr>
          {forecast_rows}
        </table>
      </div>

    </div><!-- sidebar-content -->
  </div><!-- sidebar -->
</div><!-- app -->

<script>
// ── Map init ──────────────────────────────────────────────────────────────
const map = L.map('map', {{zoomControl: true}}).setView([{center_lat}, {center_lon}], 15);

// Google Satellite — highest zoom (21), best coverage for rural India
L.tileLayer('https://mt{{s}}.google.com/vt/lyrs=s&x={{x}}&y={{y}}&z={{z}}', {{
  subdomains: '0123',
  attribution: '© Google',
  maxZoom: 21,
  maxNativeZoom: 21,
}}).addTo(map);

// Labels overlay
L.tileLayer('https://mt{{s}}.google.com/vt/lyrs=h&x={{x}}&y={{y}}&z={{z}}', {{
  subdomains: '0123',
  attribution: '',
  maxZoom: 21,
  maxNativeZoom: 21,
  opacity: 0.8,
}}).addTo(map);

// ── GeoJSON plots ─────────────────────────────────────────────────────────
const geojson = {geojson};
let layers = {{}};
let selected = null;

function styleFeature(feature, highlight) {{
  return {{
    color: highlight ? '#ffffff' : feature.properties.color,
    weight: highlight ? 3 : 2,
    opacity: 1,
    fillColor: feature.properties.color,
    fillOpacity: highlight ? 0.55 : 0.35,
  }};
}}

const geoLayer = L.geoJSON(geojson, {{
  style: f => styleFeature(f, false),
  onEachFeature: (feature, layer) => {{
    layers[feature.properties.id] = layer;

    layer.on('click', () => selectPlot(feature.properties.id));
    layer.on('mouseover', () => {{
      if (selected !== feature.properties.id)
        layer.setStyle(styleFeature(feature, true));
    }});
    layer.on('mouseout', () => {{
      if (selected !== feature.properties.id)
        layer.setStyle(styleFeature(feature, false));
    }});

    // Permanent label on each plot
    const c = layer.getBounds().getCenter();
    L.marker(c, {{
      icon: L.divIcon({{
        className: '',
        html: `<div style="background:rgba(0,0,0,0.65);color:#fff;padding:3px 8px;border-radius:10px;font-size:12px;font-weight:600;white-space:nowrap;border:1px solid ${{feature.properties.color}}">${{feature.properties.name}}<br><span style="color:${{feature.properties.color}}">${{feature.properties.bigha}} bigha</span></div>`,
        iconAnchor: [50, 14]
      }})
    }}).addTo(map);
  }}
}}).addTo(map);

map.fitBounds(geoLayer.getBounds(), {{padding: [60, 60], maxZoom: 18}});

// ── Plot selection ────────────────────────────────────────────────────────
function selectPlot(id) {{
  // Reset all pills
  document.querySelectorAll('.plot-pill').forEach(p => p.classList.remove('active'));
  document.getElementById('pill-' + id)?.classList.add('active');

  // Reset all layer styles
  geoLayer.eachLayer(layer => {{
    layer.setStyle(styleFeature(layer.feature, false));
  }});

  selected = id;

  if (id === 'all') {{
    map.fitBounds(geoLayer.getBounds(), {{padding:[40,40]}});
  }} else if (layers[id]) {{
    const layer = layers[id];
    layer.setStyle(styleFeature(layer.feature, true));
    map.fitBounds(layer.getBounds(), {{padding:[60,60], maxZoom: 19}});
  }}
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
        client = storage.Client(project=os.environ.get("GEE_PROJECT", "agriculture-496920"))
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
