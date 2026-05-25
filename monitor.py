"""
Agri Monitor — Satellite-based crop stress detection for Vidisha farms.

Data sources (all free, no extra accounts):
  - Sentinel-2 SR (GEE): NDVI, EVI, NDRE, LSWI — 10m, every 5 days
  - Sentinel-1 SAR (GEE): soil moisture proxy — cloud-free radar
  - CHIRPS Daily (GEE):   historical rainfall
  - Open-Meteo (free API, no key): current weather + 7-day forecast +
                                    soil moisture at 5 depths + ET0

Setup (one-time):
  pip install earthengine-api geemap pillow python-dotenv requests
  earthengine authenticate
  cp .env.example .env

Run:
  python monitor.py          ← check + alert if stress
  python monitor.py --report ← full report, no alert
  python monitor.py --map    ← save NDVI map to maps/
  python monitor.py --force  ← send alert regardless
"""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

ROOT       = Path(__file__).parent
FARMS_FILE = ROOT / "farms.json"
MAPS_DIR   = ROOT / "maps"
MAPS_DIR.mkdir(exist_ok=True)

GEE_PROJECT = os.environ.get("GEE_PROJECT", "agriculture-496920")

# 1 bigha (MP / Madhya Pradesh) = 1333.33 sq metres
MP_BIGHA_SQM = 1333.33


# ---------------------------------------------------------------------------
# Farm helpers
# ---------------------------------------------------------------------------

def load_farms() -> list[dict]:
    return json.loads(FARMS_FILE.read_text())["farms"]


def _polygon_area_sqm(coords: list[list[float]]) -> float:
    """Shoelace on a flat-Earth approximation. Good to <1% for farm-sized polygons."""
    lat0 = sum(c[1] for c in coords) / len(coords)
    mpl  = 111_000.0
    mplo = 111_000.0 * math.cos(math.radians(lat0))
    pts  = [(c[0] * mplo, c[1] * mpl) for c in coords]
    n    = len(pts)
    return abs(sum(pts[i][0] * (pts[(i + 1) % n][1] - pts[(i - 1) % n][1]) for i in range(n))) / 2


def _farm_centroid(coords: list[list[float]]) -> tuple[float, float]:
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return sum(lons) / len(lons), sum(lats) / len(lats)


# ---------------------------------------------------------------------------
# Open-Meteo — weather + soil (free, no key)
# ---------------------------------------------------------------------------

def get_weather(lat: float, lon: float) -> dict:
    """Pull current conditions, 7-day forecast and soil profile from Open-Meteo."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude":  lat,
        "longitude": lon,
        "current": ",".join([
            "temperature_2m", "relative_humidity_2m", "apparent_temperature",
            "precipitation", "wind_speed_10m", "wind_direction_10m",
            "wind_gusts_10m", "cloud_cover", "surface_pressure",
            "vapour_pressure_deficit", "soil_moisture_0_to_1cm", "soil_temperature_0cm",
        ]),
        "hourly": ",".join([
            "soil_moisture_0_to_1cm", "soil_moisture_1_to_3cm",
            "soil_moisture_3_to_9cm", "soil_moisture_9_to_27cm",
            "soil_moisture_27_to_81cm",
            "soil_temperature_6cm", "soil_temperature_18cm",
        ]),
        "daily": ",".join([
            "temperature_2m_max", "temperature_2m_min",
            "precipitation_sum", "precipitation_probability_max",
            "et0_fao_evapotranspiration",
            "wind_speed_10m_max", "wind_gusts_10m_max",
        ]),
        "timezone":       "Asia/Kolkata",
        "forecast_days":  7,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [warn] Open-Meteo fetch failed: {e}")
        return {}

    cur  = data.get("current", {})
    day  = data.get("daily", {})
    hour = data.get("hourly", {})

    # Pick today's hourly index closest to now (IST)
    now_str = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%dT%H:00")
    h_times = hour.get("time", [])
    h_idx   = h_times.index(now_str) if now_str in h_times else 0

    def _d(key: str, idx: int = 0):
        vals = day.get(key, [])
        return vals[idx] if idx < len(vals) else None

    def _h(key: str):
        vals = hour.get(key, [])
        return vals[h_idx] if h_idx < len(vals) else None

    # Wind direction → compass
    wd = cur.get("wind_direction_10m")
    dirs = ["N","NE","E","SE","S","SW","W","NW"]
    compass = dirs[round(wd / 45) % 8] if wd is not None else "—"

    # Soil moisture as percentage of field capacity (volumetric m³/m³ → %)
    # Typical field capacity for black cotton soil (Vidisha): ~0.35–0.40 m³/m³
    # Wilting point: ~0.15 m³/m³
    FIELD_CAP  = 0.38
    WILT_POINT = 0.15
    def _sm_pct(val):
        if val is None: return None
        return round(max(0, min(100, (val - WILT_POINT) / (FIELD_CAP - WILT_POINT) * 100)), 1)

    # 7-day rain total and max rain-chance
    rain_7d    = sum(v for v in (day.get("precipitation_sum") or []) if v)
    rain_chance_today = _d("precipitation_probability_max", 0)

    # ET0 today (evapotranspiration — how much water crop needs)
    et0_today = _d("et0_fao_evapotranspiration", 0)

    return {
        # Current
        "temp_c":           cur.get("temperature_2m"),
        "feels_like_c":     cur.get("apparent_temperature"),
        "humidity_pct":     cur.get("relative_humidity_2m"),
        "wind_kmh":         cur.get("wind_speed_10m"),
        "wind_gusts_kmh":   cur.get("wind_gusts_10m"),
        "wind_dir":         compass,
        "cloud_cover_pct":  cur.get("cloud_cover"),
        "pressure_hpa":     cur.get("surface_pressure"),
        "vpd_kpa":          cur.get("vapour_pressure_deficit"),
        "precip_now_mm":    cur.get("precipitation"),
        # Soil (current surface)
        "soil_temp_surface_c":  cur.get("soil_temperature_0cm"),
        "soil_moisture_pct_0":  _sm_pct(cur.get("soil_moisture_0_to_1cm")),
        "soil_moisture_pct_3":  _sm_pct(_h("soil_moisture_3_to_9cm")),
        "soil_moisture_pct_27": _sm_pct(_h("soil_moisture_27_to_81cm")),
        "soil_temp_6cm_c":      _h("soil_temperature_6cm"),
        "soil_temp_18cm_c":     _h("soil_temperature_18cm"),
        # Forecast
        "temp_max_today":       _d("temperature_2m_max", 0),
        "temp_min_today":       _d("temperature_2m_min", 0),
        "rain_today_mm":        _d("precipitation_sum",  0),
        "rain_chance_pct":      rain_chance_today,
        "rain_7d_mm":           round(rain_7d, 1),
        "et0_today_mm":         et0_today,
        "wind_max_today_kmh":   _d("wind_speed_10m_max", 0),
        # 7-day forecast list for mini table
        "forecast_dates":       day.get("time", [])[:7],
        "forecast_rain":        day.get("precipitation_sum", [])[:7],
        "forecast_rain_chance": day.get("precipitation_probability_max", [])[:7],
        "forecast_max_temp":    day.get("temperature_2m_max", [])[:7],
    }


# ---------------------------------------------------------------------------
# Satellite pull — GEE
# ---------------------------------------------------------------------------

def get_satellite_stats(farm: dict, coords: list, farm_polygon, days_back: int = 10) -> dict:
    import ee

    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days_back)
    end_str  = end_dt.strftime("%Y-%m-%d")
    start_str = start_dt.strftime("%Y-%m-%d")

    s2 = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(farm_polygon)
        .filterDate(start_str, end_str)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 30))
    )
    count = s2.size().getInfo()
    if count == 0:
        return {"status": "no_data", "image_count": 0, "date": end_str,
                "reason": f"No cloud-free images in last {days_back} days."}

    median = s2.median()
    ndvi = median.normalizedDifference(["B8",  "B4" ]).rename("NDVI")
    ndre = median.normalizedDifference(["B8A", "B5" ]).rename("NDRE")
    lswi = median.normalizedDifference(["B8A", "B11"]).rename("LSWI")
    evi  = median.expression(
        "2.5*(NIR-RED)/(NIR+6*RED-7.5*BLUE+1)",
        {"NIR": median.select("B8"), "RED": median.select("B4"), "BLUE": median.select("B2")},
    ).rename("EVI")

    stacked = ndvi.addBands(ndre).addBands(lswi).addBands(evi)
    vi = stacked.reduceRegion(
        reducer=ee.Reducer.mean().combine(ee.Reducer.percentile([10]), sharedInputs=True),
        geometry=farm_polygon, scale=10, maxPixels=1e8,
    ).getInfo()

    ndvi_mean = round(vi.get("NDVI_mean", 0) or 0, 3)
    ndvi_p10  = round(vi.get("NDVI_p10",  0) or 0, 3)
    ndre_mean = round(vi.get("NDRE_mean", 0) or 0, 3)
    lswi_mean = round(vi.get("LSWI_mean", 0) or 0, 3)
    evi_mean  = round(vi.get("EVI_mean",  0) or 0, 3)

    crop = farm.get("current_crop", "soybean")
    stress_threshold = {"soybean": 0.35, "wheat": 0.30, "chickpea": 0.28}.get(crop, 0.35)
    stress_pct = round(
        (ndvi.lt(stress_threshold).reduceRegion(
            reducer=ee.Reducer.mean(), geometry=farm_polygon, scale=10, maxPixels=1e8,
        ).getInfo().get("NDVI", 0) or 0) * 100, 1
    )

    avg_cloud = round(s2.aggregate_mean("CLOUDY_PIXEL_PERCENTAGE").getInfo(), 1)

    # Historical NDVI (same window last year)
    hist_end = end_dt - timedelta(days=365)
    hist_s2  = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(farm_polygon)
        .filterDate((hist_end - timedelta(days=days_back)).strftime("%Y-%m-%d"), hist_end.strftime("%Y-%m-%d"))
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 30))
    )
    ndvi_last_year = None
    if hist_s2.size().getInfo() > 0:
        h = hist_s2.median().normalizedDifference(["B8","B4"]).reduceRegion(
            reducer=ee.Reducer.mean(), geometry=farm_polygon, scale=10, maxPixels=1e8,
        ).getInfo()
        ndvi_last_year = round(h.get("nd", 0) or 0, 3)

    # Sentinel-1 SAR soil moisture
    soil_vv_db = soil_moisture_label = None
    try:
        s1 = (
            ee.ImageCollection("COPERNICUS/S1_GRD")
            .filterBounds(farm_polygon).filterDate(start_str, end_str)
            .filter(ee.Filter.eq("instrumentMode", "IW"))
            .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
            .select("VV").median()
        )
        vv = s1.reduceRegion(reducer=ee.Reducer.mean(), geometry=farm_polygon, scale=10, maxPixels=1e8).getInfo().get("VV")
        if vv is not None:
            soil_vv_db = round(vv, 1)
            soil_moisture_label = (
                "very dry" if vv < -18 else "dry" if vv < -15 else "moist" if vv < -12 else "wet"
            )
    except Exception:
        pass

    ndre_stress = ndre_mean < 0.2 and ndre_mean > 0

    # Days since sowing — determines status
    sowing_date_str = farm.get("sowing_date")
    days_since_sowing = None
    if sowing_date_str:
        sowing_dt = datetime.fromisoformat(sowing_date_str).replace(tzinfo=timezone.utc)
        days_since_sowing = (datetime.now(timezone.utc) - sowing_dt).days

    if days_since_sowing is not None and days_since_sowing < 0:
        status = "pre_sowing"
    elif days_since_sowing is not None and days_since_sowing < 21:
        status = "germination"
    else:
        status = "stress" if stress_pct > 10 else "ok"

    return {
        "date": end_str, "status": status, "crop": crop,
        "ndvi_mean": ndvi_mean, "ndvi_p10": ndvi_p10,
        "ndre_mean": ndre_mean, "ndre_stress": ndre_stress,
        "lswi_mean": lswi_mean, "evi_mean": evi_mean,
        "stress_pct": stress_pct, "stress_threshold": stress_threshold,
        "cloud_pct": avg_cloud, "image_count": count,
        "ndvi_last_year": ndvi_last_year,
        "soil_vv_db": soil_vv_db, "soil_moisture_label": soil_moisture_label,
        "days_since_sowing": days_since_sowing,
    }


def get_farm_stats(farm: dict, days_back: int = 10) -> dict:
    try:
        import ee
    except ImportError:
        raise SystemExit("pip install earthengine-api && earthengine authenticate")

    ee.Initialize(project=GEE_PROJECT)

    # Support both old single-boundary farms and new multi-plot farms
    plots = farm.get("plots")
    if plots:
        all_coords = []
        total_sqm  = 0.0
        plot_info  = []
        for plot in plots:
            c = plot["boundary"]["coordinates"]
            if c[0] != c[-1]:
                c = c + [c[0]]
            sqm = _polygon_area_sqm(c)
            total_sqm += sqm
            all_coords.extend(c)
            plot_info.append({
                "id":   plot["id"],
                "name": plot["name"],
                "area_bigha": round(sqm / MP_BIGHA_SQM, 1),
                "area_sqm":   round(sqm),
            })
        # Union of all plot polygons for GEE query
        farm_polygon = ee.Geometry.MultiPolygon(
            [ee.Geometry.Polygon([p["boundary"]["coordinates"]]) for p in plots]
        )
        area_bigha = round(total_sqm / MP_BIGHA_SQM, 1)
        area_sqm   = round(total_sqm)
        # Centroid from all points
        lon, lat = _farm_centroid(all_coords)
    else:
        coords = farm["boundary"]["coordinates"]
        if coords[0] != coords[-1]:
            coords = coords + [coords[0]]
        farm_polygon = ee.Geometry.Polygon([coords])
        area_sqm   = round(_polygon_area_sqm(coords))
        area_bigha = round(area_sqm / MP_BIGHA_SQM, 1)
        lon, lat   = _farm_centroid(coords)
        plot_info  = []

    sat  = get_satellite_stats(farm, [], farm_polygon, days_back)
    wthr = get_weather(lat, lon)

    # Growth curve — fetch per-image timeseries since sowing
    sowing_date_str = farm.get("sowing_date")
    print("  Fetching NDVI time-series (growth curve)...")
    timeseries = get_ndvi_timeseries(farm_polygon, sowing_date_str)

    # NDVI heatmap — binary stress zones (red=spray, green=skip)
    print("  Generating stress zone map...")
    stress_threshold = sat.get("stress_threshold", 0.35)
    heatmap_url = get_ndvi_heatmap_url(farm_polygon, days_back=days_back,
                                        stress_threshold=stress_threshold)

    # Spray advisory from current weather
    spray = _spray_advisory(wthr)

    # Stress cause diagnosis
    diagnosis = _stress_diagnosis(sat, wthr)

    # Spray savings estimate
    savings = _spray_savings(sat.get("stress_pct", 0) or 0, area_bigha)

    return {
        **sat,
        "area_bigha": area_bigha, "area_sqm": area_sqm,
        "plots": plot_info, "weather": wthr,
        "ndvi_timeseries": timeseries,
        "heatmap_url": heatmap_url,
        "spray_advisory": spray,
        "diagnosis": diagnosis,
        "spray_savings": savings,
    }


# ---------------------------------------------------------------------------
# NDVI time-series (full season growth curve)
# ---------------------------------------------------------------------------

def get_ndvi_timeseries(farm_polygon, sowing_date_str: str | None = None,
                        season_days: int = 120) -> list[dict]:
    """
    Return a list of {date, ndvi, ndre, lswi, cloud_pct} dicts for every
    Sentinel-2 acquisition over the farm since sowing (or last season_days).
    Cloud filter is relaxed to 60% so we capture more points; individual
    per-image cloud % is recorded so the UI can flag noisy points.
    """
    import ee

    end_dt = datetime.now(timezone.utc)
    if sowing_date_str:
        try:
            start_dt = datetime.fromisoformat(sowing_date_str).replace(tzinfo=timezone.utc)
            # Sowing is in the future — no crop yet, nothing to chart
            if start_dt > end_dt:
                return []
            # Don't go more than season_days into the future from sowing
            if (end_dt - start_dt).days > season_days:
                start_dt = end_dt - timedelta(days=season_days)
        except ValueError:
            start_dt = end_dt - timedelta(days=season_days)
    else:
        start_dt = end_dt - timedelta(days=season_days)

    collection = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(farm_polygon)
        .filterDate(start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
    )

    size = collection.size().getInfo()
    if size == 0:
        return []

    def extract_point(img):
        date  = ee.Date(img.get("system:time_start")).format("YYYY-MM-dd")
        cloud = img.getNumber("CLOUDY_PIXEL_PERCENTAGE")
        ndvi  = img.normalizedDifference(["B8",  "B4" ]).rename("NDVI")
        ndre  = img.normalizedDifference(["B8A", "B5" ]).rename("NDRE")
        lswi  = img.normalizedDifference(["B8A", "B11"]).rename("LSWI")
        vi = ndvi.addBands(ndre).addBands(lswi).reduceRegion(
            reducer=ee.Reducer.mean(), geometry=farm_polygon,
            scale=20, maxPixels=1e7,
        )
        return ee.Feature(None, {
            "date":      date,
            "ndvi":      vi.getNumber("NDVI"),
            "ndre":      vi.getNumber("NDRE"),
            "lswi":      vi.getNumber("LSWI"),
            "cloud_pct": cloud,
        })

    features = collection.map(extract_point).getInfo()["features"]
    points = []
    for f in features:
        p = f["properties"]
        if p.get("ndvi") is None:
            continue
        points.append({
            "date":      p["date"],
            "ndvi":      round(p["ndvi"] or 0, 3),
            "ndre":      round(p["ndre"] or 0, 3),
            "lswi":      round(p["lswi"] or 0, 3),
            "cloud_pct": round(p["cloud_pct"] or 0, 1),
        })
    points.sort(key=lambda x: x["date"])
    return points


def get_ndvi_heatmap_url(farm_polygon, days_back: int = 15,
                         stress_threshold: float = 0.35) -> str | None:
    """
    Generate a binary stress-zone tile layer: red = stressed (NDVI below
    threshold), green = healthy. Farmers see exactly where to spray vs skip.
    Returns EE tile URL template ({z}/{x}/{y}) for Leaflet TileLayer.
    """
    import ee

    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days_back)

    s2 = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(farm_polygon)
        .filterDate(start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 30))
    )
    try:
        if s2.size().getInfo() == 0:
            return None

        ndvi = s2.median().normalizedDifference(["B8", "B4"])

        # Binary mask: 0 = stressed (spray), 1 = healthy (skip)
        # Clipped to farm polygon so only the field shows, rest is transparent
        stressed = ndvi.lt(stress_threshold).selfMask()
        healthy  = ndvi.gte(stress_threshold).selfMask()

        # Stack: stressed pixels red, healthy pixels green
        # Use two-value palette trick: render stressed as 0→red, healthy as 1→green
        combined = healthy.where(stressed, ee.Image(0)).blend(
            healthy.multiply(ee.Image(0)).add(ee.Image(1)).updateMask(healthy)
        )

        # Simpler approach: visualize NDVI with just 2 bands of color
        # stressed = red, healthy = semi-transparent green
        stressed_vis = stressed.visualize(**{"palette": ["#ef5350"], "min": 1, "max": 1})
        healthy_vis  = healthy.visualize(**{"palette": ["#66bb6a"], "min": 1, "max": 1})
        zones = stressed_vis.blend(healthy_vis).clip(farm_polygon)

        map_id = zones.getMapId({})
        return map_id["tile_fetcher"].url_format
    except Exception as e:
        print(f"  [warn] heatmap tile URL failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Stress cause diagnosis
# ---------------------------------------------------------------------------

def _stress_diagnosis(sat: dict, wthr: dict) -> dict:
    """
    Rule-based diagnosis of *why* the crop is stressed.
    Returns {cause, cause_hi, confidence, action, action_hi, factors: [str]}
    """
    ndvi  = sat.get("ndvi_mean", 0) or 0
    ndre  = sat.get("ndre_mean", 0) or 0
    lswi  = sat.get("lswi_mean", 0) or 0
    stress_pct = sat.get("stress_pct", 0) or 0
    status = sat.get("status", "ok")

    w           = wthr or {}
    rain_7d     = w.get("rain_7d_mm", 0) or 0
    humidity    = w.get("humidity_pct", 0) or 0
    temp        = w.get("temp_c", 25) or 25
    soil_label  = sat.get("soil_moisture_label") or ""
    soil_vv     = sat.get("soil_vv_db")

    if status in ("pre_sowing", "no_data") or ndvi < 0.08:
        return {
            "cause": "No crop yet", "cause_hi": "फसल नहीं है",
            "confidence": "high",
            "action": "Field is bare — check back after sowing",
            "action_hi": "खेत खाली है — बुवाई के बाद देखें",
            "factors": [], "icon": "🌾"
        }

    scores = {}  # cause → score (higher = more likely)

    # ── Drought / water stress ─────────────────────────────────
    drought_score = 0
    drought_factors = []
    if lswi < 0:
        drought_score += 40; drought_factors.append("LSWI negative — leaves losing water · पत्तियां सूख रही हैं")
    elif lswi < 0.1:
        drought_score += 20; drought_factors.append("LSWI low — leaf moisture low · पत्तियों में कम नमी")
    if rain_7d < 8:
        drought_score += 25; drought_factors.append(f"Only {rain_7d:.0f}mm rain in last 7 days · पिछले 7 दिन कम बारिश")
    if soil_label in ("dry", "very dry"):
        drought_score += 25; drought_factors.append(f"Satellite radar: soil is {soil_label} · मिट्टी सूखी")
    if temp > 36:
        drought_score += 10; drought_factors.append(f"High temp {temp:.0f}°C increasing water loss · गर्मी से पानी उड़ रहा है")
    scores["drought"] = (drought_score, drought_factors,
        "Drought / Water stress", "सूखा / पानी की कमी",
        "Irrigate stressed zones before any spray",
        "पहले सिंचाई करें, फिर छिड़काव करें", "💧")

    # ── Fungal / disease ──────────────────────────────────────
    fungal_score = 0
    fungal_factors = []
    if humidity > 78:
        fungal_score += 30; fungal_factors.append(f"High humidity {humidity:.0f}% — ideal for fungal growth · नमी ज़्यादा है")
    if 18 <= temp <= 30:
        fungal_score += 20; fungal_factors.append(f"Temp {temp:.0f}°C in fungal disease range · तापमान फफूंद के लिए सही है")
    if rain_7d > 20:
        fungal_score += 20; fungal_factors.append(f"{rain_7d:.0f}mm rain this week — wet leaves · हफ्ते में ज़्यादा बारिश")
    if ndre < 0.15 and ndvi > 0.3:
        fungal_score += 25; fungal_factors.append("NDRE dropping while NDVI holds — early disease signal · पत्तियों में तनाव शुरू")
    scores["fungal"] = (fungal_score, fungal_factors,
        "Fungal disease risk", "फफूंद रोग का खतरा",
        "Apply fungicide to stressed zones — spray in morning",
        "सुबह फफूंदनाशक का छिड़काव करें", "🍄")

    # ── Pest / insect damage ──────────────────────────────────
    pest_score = 0
    pest_factors = []
    if ndvi < 0.35 and ndre > 0.15:
        pest_score += 35; pest_factors.append("NDVI low but NDRE holds — leaf area lost, not nutrient stress · पत्तियां कम हुई हैं")
    if stress_pct > 15 and stress_pct < 60:
        pest_score += 20; pest_factors.append(f"{stress_pct:.0f}% of field in patches — pest damage is patchy · खेत में जगह-जगह नुकसान")
    if temp > 30 and humidity < 65:
        pest_score += 15; pest_factors.append("Hot dry conditions favour sucking pests · गर्म-सूखा मौसम कीटों के लिए अनुकूल")
    scores["pest"] = (pest_score, pest_factors,
        "Pest / insect damage", "कीट / कीड़े का नुकसान",
        "Scout field for insects — spray only infested zones",
        "खेत में कीट देखें — सिर्फ प्रभावित हिस्से में छिड़काव करें", "🐛")

    # ── Nutrient deficiency ────────────────────────────────────
    nutrient_score = 0
    nutrient_factors = []
    if ndre < 0.12 and ndvi < 0.4:
        nutrient_score += 35; nutrient_factors.append("Both NDVI and NDRE low — likely nitrogen deficiency · नाइट्रोजन की कमी हो सकती है")
    if lswi > 0.1 and ndvi < 0.35:
        nutrient_score += 20; nutrient_factors.append("Moisture ok but growth weak — nutrient limited · पानी ठीक पर बढ़त कम")
    scores["nutrient"] = (nutrient_score, nutrient_factors,
        "Nutrient deficiency", "पोषण की कमी",
        "Apply foliar fertiliser to stressed zones",
        "कमज़ोर क्षेत्र में पत्ती पर खाद का छिड़काव करें", "🌿")

    # Pick highest scoring cause
    best = max(scores.items(), key=lambda x: x[1][0])
    cause_key, (top_score, factors, cause, cause_hi, action, action_hi, icon) = best

    confidence = "high" if top_score >= 50 else ("medium" if top_score >= 25 else "low")

    return {
        "cause": cause, "cause_hi": cause_hi,
        "action": action, "action_hi": action_hi,
        "confidence": confidence, "score": top_score,
        "factors": factors, "icon": icon,
        "cause_key": cause_key,
    }


# ---------------------------------------------------------------------------
# Spray savings estimate
# ---------------------------------------------------------------------------

def _spray_savings(stress_pct: float, area_bigha: float) -> dict:
    """
    If farmer only sprays stressed zones instead of full field,
    estimate chemical and cost savings.
    """
    healthy_pct   = max(0, 100 - stress_pct)
    saving_pct    = round(healthy_pct)
    stressed_bigha = round(area_bigha * stress_pct / 100, 1)
    saved_bigha    = round(area_bigha * healthy_pct / 100, 1)
    # Rough cost: ₹300/bigha average spray cost in MP
    cost_per_bigha = 300
    saved_cost     = round(saved_bigha * cost_per_bigha)
    return {
        "saving_pct":      saving_pct,
        "stressed_bigha":  stressed_bigha,
        "saved_bigha":     saved_bigha,
        "saved_cost_inr":  saved_cost,
    }


# ---------------------------------------------------------------------------
# Spray advisory (wind + humidity + temperature)
# ---------------------------------------------------------------------------

def _spray_advisory(w: dict) -> dict:
    """
    Return a spray advisory based on current weather.
    Returns {ok: bool, score: 0-100, reasons: [str], label: str}
    """
    wind      = w.get("wind_kmh") or 0
    gusts     = w.get("wind_gusts_kmh") or 0
    humidity  = w.get("humidity_pct") or 0
    temp      = w.get("temp_c") or 25
    cloud     = w.get("cloud_cover_pct") or 0

    reasons = []
    score   = 100  # start perfect, deduct

    # Wind — biggest factor. >15 km/h = drift risk; >25 = don't spray
    if gusts > 25:
        score -= 50; reasons.append("⛔ Gusts >{:.0f} km/h — chemical will drift".format(gusts))
    elif wind > 15:
        score -= 30; reasons.append("⚠ Wind {:.0f} km/h — drift risk, spray early morning".format(wind))
    elif wind < 3:
        score -= 10; reasons.append("☁ Calm air — inversion risk; spray may not penetrate canopy")

    # Humidity — low = chemical dries before absorption
    if humidity < 40:
        score -= 25; reasons.append("⚠ Low humidity ({:.0f}%) — chemical dries on leaf before absorbing".format(humidity))
    elif humidity > 90:
        score -= 10; reasons.append("⚠ Very high humidity — fungal disease risk post-spray")

    # Temperature
    if temp > 38:
        score -= 20; reasons.append("🔥 Temp {:.0f}°C — spray will volatilise; use evening".format(temp))
    elif temp < 10:
        score -= 10; reasons.append("🌡 Low temp — chemical absorption slowed")

    # Rain chance — if rain >30% don't spray (washoff)
    rain_ch = w.get("rain_chance_pct") or 0
    if rain_ch > 50:
        score -= 30; reasons.append("🌧 {:.0f}% rain chance today — spray will be washed off".format(rain_ch))
    elif rain_ch > 30:
        score -= 10; reasons.append("🌦 {:.0f}% rain chance — risk of washoff".format(rain_ch))

    score = max(0, score)
    if score >= 75:
        label = "Good to spray · छिड़काव करें"
    elif score >= 50:
        label = "Spray with caution · सावधानी से करें"
    else:
        label = "Avoid spray today · आज न करें"

    if not reasons:
        reasons = ["✓ Wind, humidity and temperature all within ideal range"]

    return {"ok": score >= 75, "score": score, "label": label, "reasons": reasons}


# ---------------------------------------------------------------------------
# NDVI map export
# ---------------------------------------------------------------------------

def save_ndvi_map(farm: dict, out_path: Path) -> None:
    try:
        import ee, geemap
    except ImportError:
        print("[warn] geemap not installed — skipping map.")
        return

    ee.Initialize(project=GEE_PROJECT)
    all_coords = [c for p in farm.get("plots", []) for c in p["boundary"]["coordinates"]]
    if not all_coords:
        all_coords = farm.get("boundary", {}).get("coordinates", [])
    coords = all_coords
    if coords[0] != coords[-1]:
        coords = coords + [coords[0]]
    farm_polygon = ee.Geometry.Polygon([coords])

    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=15)
    ndvi = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(farm_polygon)
        .filterDate(start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 30))
        .median().normalizedDifference(["B8","B4"]).rename("NDVI")
    )
    geemap.ee_export_image(
        ndvi, filename=str(out_path), scale=10, region=farm_polygon,
        vis_params={"min":0.0,"max":0.8,"palette":["red","orange","yellow","lightgreen","darkgreen"]},
    )
    print(f"  Map saved: {out_path}")


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram_alert(farm: dict, stats: dict, map_path: Path | None = None, dashboard_url: str | None = None) -> None:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id   = os.environ.get("TELEGRAM_CHAT_ID", "") or farm.get("telegram_chat_id", "")

    if not all([bot_token, chat_id]):
        print("  [skip] Telegram not configured")
        print(_format_alert(farm, stats))
        return

    text = _format_alert(farm, stats)
    if dashboard_url:
        text += f'\n\n📊 <a href="{dashboard_url}">View Full Dashboard →</a>'
    base = f"https://api.telegram.org/bot{bot_token}"

    if map_path and map_path.exists():
        resp = requests.post(
            f"{base}/sendPhoto",
            data={"chat_id": chat_id, "caption": text, "parse_mode": "HTML"},
            files={"photo": map_path.open("rb")},
            timeout=30,
        )
    else:
        resp = requests.post(
            f"{base}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=30,
        )

    if resp.ok:
        print(f"  Telegram sent: message_id={resp.json()['result']['message_id']}")
    else:
        print(f"  Telegram error: {resp.status_code} {resp.text}")


# ---------------------------------------------------------------------------
# Message formatter
# ---------------------------------------------------------------------------

def _sowing_advisory(crop: str, days: int, stats: dict, w: dict) -> str:
    """Return plain-language what-to-do advice based on crop growth stage."""
    sm0 = w.get("soil_moisture_pct_0") or 0
    rain_7d = w.get("rain_7d_mm") or 0
    temp = w.get("temp_c") or 30

    if days < 0:
        return ""

    # Soybean growth stages
    if crop.lower() == "soybean":
        if days == 0:
            stage, advice = "Sowing day", [
                "Sow at 2–3 cm depth, row spacing 30–45 cm",
                "Apply basal dose: DAP 50 kg/acre + potash 25 kg/acre",
                "Seed treatment with Rhizobium + PSB culture recommended",
                "Ensure soil moisture is adequate before sowing",
            ]
        elif days <= 7:
            stage, advice = "Germination (Week 1)", [
                "Keep soil moist — do not let surface crust form",
                "Watch for poor germination patches (may need gap filling by day 10)",
                "No fertiliser needed yet",
                f"{'⚠️ Soil looks dry — light irrigation recommended' if sm0 < 40 else '✓ Soil moisture looks fine'}",
            ]
        elif days <= 21:
            stage, advice = "Seedling stage (Week 2–3)", [
                "Gap filling if germination <70% — resow in patches",
                "First weeding: manual or pre-emergence herbicide by day 20",
                "Watch for stem fly and leaf miner attack",
                f"{'⚠️ Irrigate — low rainfall this week' if rain_7d < 15 else '✓ Rainfall adequate this week'}",
            ]
        elif days <= 35:
            stage, advice = "Vegetative growth (Week 4–5)", [
                "Apply urea top dressing: 20 kg/acre",
                "Second weeding if first was missed",
                "Spray for girdle beetle if stem damage seen",
                f"{'🔥 Heat stress risk — irrigate in evening' if temp > 36 else '✓ Temperature ok for growth'}",
            ]
        elif days <= 55:
            stage, advice = "Flowering (Week 6–8) — critical stage", [
                "Do NOT miss irrigation during flowering — yield loss is permanent",
                "Spray boron 0.2% + molybdenum 0.05% to improve pod set",
                "Watch for whitefly and yellow mosaic virus",
                "Avoid pesticide spray during open flower hours (6–10 AM)",
                f"{'⚠️ Low rain — irrigate immediately, flowering stage cannot be missed' if rain_7d < 20 else '✓ Moisture ok for flowering'}",
            ]
        elif days <= 80:
            stage, advice = "Pod filling (Week 9–11)", [
                "Keep field moist — pod fill directly affects grain weight",
                "Spray potassium nitrate 1% to improve seed size",
                "Monitor for pod borer and leaf eating caterpillar",
                f"{'⚠️ Dry conditions — irrigate for pod fill' if rain_7d < 15 else '✓ Rain adequate for pod fill'}",
            ]
        elif days <= 100:
            stage, advice = "Maturity (Week 12–14)", [
                "Stop irrigation 10–12 days before expected harvest",
                "Watch for pod shattering in dry hot winds",
                "Harvest when 95% pods turn brown (moisture ~18%)",
                "Avoid harvesting in afternoon heat — do morning harvest",
            ]
        else:
            stage, advice = "Post harvest", [
                "Deep plough field to break pest cycle",
                "Apply 2–3 tonnes FYM/acre before next crop",
                "Update sowing_date in farms.json for next season",
            ]
    else:
        # Generic advice for other crops
        if days <= 21:
            stage, advice = "Early growth", ["Ensure good germination", "First weeding by day 20"]
        elif days <= 50:
            stage, advice = "Vegetative", ["Top dress with nitrogen", "Monitor for pests"]
        elif days <= 80:
            stage, advice = "Flowering/fruiting — critical", ["Do not miss irrigation", "Monitor closely"]
        else:
            stage, advice = "Maturity", ["Prepare for harvest", "Reduce irrigation"]

    advice_lines = "\n".join(f"  • {a}" for a in advice)
    return f"<b>🌱 Stage: {stage}</b> (Day {days})\n{advice_lines}"


def _bar(value: float, max_val: float, length: int = 8) -> str:
    filled = min(length, round((value / max_val) * length))
    return "🟩" * filled + "⬜" * (length - filled)


def _wind_emoji(kmh: float) -> str:
    if kmh < 15:  return "🍃"
    if kmh < 30:  return "💨"
    if kmh < 50:  return "🌬️"
    return "🌀"


def _format_alert(farm: dict, stats: dict) -> str:
    date   = stats.get("date", "today")
    status = stats.get("status", "ok")
    crop   = stats.get("crop", "soybean").capitalize()
    name   = farm.get("name", "Farm")
    w      = stats.get("weather", {})

    # Real area from polygon
    area_bigha = stats.get("area_bigha", farm.get("area_bigha", "?"))
    area_sqm   = stats.get("area_sqm", "")
    area_str   = f"{area_bigha} bigha ({area_sqm:,} m²)" if area_sqm else f"{area_bigha} bigha"

    # Status badge
    badges = {
        "stress":      "🔴 STRESS DETECTED",
        "no_data":     "☁️ NO SATELLITE DATA",
        "ok":          "🟢 CROP HEALTHY",
        "pre_sowing":  "🌾 FIELD READY — AWAITING SOWING",
        "germination": "🌱 GERMINATION PHASE",
    }
    badge = badges.get(status, "🟡 UNKNOWN")

    # ── HEADER ───────────────────────────────────────────────────────────────
    header = (
        f"🛰️ <b>{name}</b>\n"
        f"<b>{badge}</b>\n"
        f"<i>📅 {date} · {crop} · {area_str}</i>"
    )

    if status == "no_data":
        return f"{header}\n\nNo cloud-free satellite image available.\nWill auto-retry in 2–3 days."

    days_since_sowing = stats.get("days_since_sowing")
    sowing_date = farm.get("sowing_date", "not set")

    if status == "pre_sowing":
        days_to_sow = abs(days_since_sowing) if days_since_sowing is not None else "?"
        w = stats.get("weather", {})
        sm0 = w.get("soil_moisture_pct_0")
        temp = w.get("temp_c")
        rain_7d = w.get("rain_7d_mm", 0)

        soil_ready = sm0 and 40 <= sm0 <= 80
        temp_ok    = temp and temp < 38

        readiness = []
        readiness.append(f"💧 Soil moisture: <b>{sm0}%</b> {'✓ good for sowing' if soil_ready else '⚠️ too dry — irrigate before sowing' if sm0 and sm0 < 40 else '⚠️ too wet — wait for drainage'}")
        readiness.append(f"🌡️ Temperature: <b>{temp}°C</b> {'✓ good' if temp_ok else '⚠️ too hot — sow in evening or early morning'}")
        readiness.append(f"🌧️ Rain this week: <b>{rain_7d} mm</b> {'✓' if rain_7d > 20 else '— irrigate to prep seedbed'}")

        advisory = _sowing_advisory("soybean", 0, stats, w)

        return (
            f"{header}\n\n"
            f"📍 <b>Current field state — bare soil, ready for kharif sowing</b>\n"
            f"Sowing planned: <b>{sowing_date}</b> ({days_to_sow} days away)\n\n"
            f"<b>🔍 Field Readiness</b>\n" + "\n".join(readiness) + "\n\n"
            f"{advisory}\n\n"
            f"<i>Satellite sees bare soil — stress alert will activate once crop is growing</i>"
        )

    if status == "germination":
        days = days_since_sowing or 0
        w    = stats.get("weather", {})
        return (
            f"{header}\n\n"
            f"Seeds sown <b>{days} days ago</b> — germination phase.\n\n"
            + _sowing_advisory("soybean", days, stats, w)
        )

    # ── SATELLITE INDICES ────────────────────────────────────────────────────
    ndvi_mean  = stats.get("ndvi_mean", 0)
    ndre_mean  = stats.get("ndre_mean", 0) or 0
    lswi_mean  = stats.get("lswi_mean", 0) or 0
    evi_mean   = stats.get("evi_mean",  0) or 0
    stress_pct = stats.get("stress_pct", 0)

    ndvi_ly  = stats.get("ndvi_last_year")
    yoy_line = ""
    if ndvi_ly is not None:
        diff = round(ndvi_mean - ndvi_ly, 3)
        yoy_line = f"\n  <i>{'↑' if diff>0.02 else '↓' if diff<-0.02 else '≈'} vs last year: {ndvi_ly} ({'+' if diff>=0 else ''}{diff})</i>"

    ndre_flag = "  ⚠️" if stats.get("ndre_stress") else ""
    lswi_label = "drought stress" if lswi_mean < 0.0 else ("low" if lswi_mean < 0.15 else ("ok" if lswi_mean < 0.35 else "high"))

    sat_block = (
        f"<b>📡 Satellite Indices</b>  <i>(Sentinel-2, {stats.get('image_count')} images)</i>\n"
        f"NDVI  {_bar(max(0,ndvi_mean),0.8)}  <code>{ndvi_mean}</code>  <i>crop health</i>{yoy_line}\n"
        f"NDRE  {_bar(max(0,ndre_mean),0.8)}  <code>{ndre_mean}</code>  <i>early stress</i>{ndre_flag}\n"
        f"LSWI  {_bar(max(0,lswi_mean+0.5),1.0)}  <code>{lswi_mean}</code>  <i>leaf water · {lswi_label}</i>\n"
        f"EVI   {_bar(max(0,evi_mean),0.8)}  <code>{evi_mean}</code>  <i>canopy density</i>"
    )

    stress_bar   = _bar(stress_pct, 100)
    stressed_area = round(stats.get("area_sqm", 0) * stress_pct / 100 / MP_BIGHA_SQM, 1)
    stress_block = (
        f"<b>⚠️ Stress Map</b>\n"
        f"Affected  {stress_bar}  <code>{stress_pct}%</code>"
        + (f"  (~{stressed_area} bigha)" if stressed_area else "")
    )

    # ── CURRENT WEATHER ──────────────────────────────────────────────────────
    weather_lines = []
    if w:
        temp    = w.get("temp_c")
        feels   = w.get("feels_like_c")
        hum     = w.get("humidity_pct")
        wind    = w.get("wind_kmh")
        gusts   = w.get("wind_gusts_kmh")
        wdir    = w.get("wind_dir")
        cloud   = w.get("cloud_cover_pct")
        vpd     = w.get("vpd_kpa")
        precip  = w.get("precip_now_mm", 0)

        heat_flag = "  🔥 <i>heat stress</i>" if temp and temp > 38 else ""
        vpd_flag  = "  ⚠️ <i>high evap demand</i>" if vpd and vpd > 3.0 else ""
        we = _wind_emoji(wind or 0)

        if temp is not None:
            weather_lines.append(f"🌡️ Temp: <b>{temp}°C</b> (feels {feels}°C){heat_flag}")
        if hum is not None:
            weather_lines.append(f"💧 Humidity: <b>{hum}%</b>")
        if wind is not None:
            weather_lines.append(f"{we} Wind: <b>{wind} km/h</b> gusts {gusts} · {wdir}")
        if cloud is not None:
            weather_lines.append(f"☁️ Cloud cover: <b>{cloud}%</b>")
        if vpd is not None:
            weather_lines.append(f"🌫️ VPD: <b>{vpd} kPa</b>{vpd_flag}")
        if precip:
            weather_lines.append(f"🌧️ Rain now: <b>{precip} mm</b>")

    weather_block = ("<b>🌤️ Current Weather</b>\n" + "\n".join(weather_lines)) if weather_lines else ""

    # ── SOIL ─────────────────────────────────────────────────────────────────
    soil_lines = []
    if w:
        sm0  = w.get("soil_moisture_pct_0")
        sm3  = w.get("soil_moisture_pct_3")
        sm27 = w.get("soil_moisture_pct_27")
        st0  = w.get("soil_temp_surface_c")
        st6  = w.get("soil_temp_6cm_c")
        st18 = w.get("soil_temp_18cm_c")
        vv   = stats.get("soil_vv_db")
        sar_label = stats.get("soil_moisture_label")

        if sm0 is not None:
            dry_flag = "  ⚠️ <i>below wilting point</i>" if sm0 < 15 else ""
            soil_lines.append(f"🌱 Surface (0–1cm):   {_bar(sm0,100)} <code>{sm0}%</code>{dry_flag}")
        if sm3 is not None:
            soil_lines.append(f"🌱 Root zone (3–9cm): {_bar(sm3,100)} <code>{sm3}%</code>")
        if sm27 is not None:
            soil_lines.append(f"🌱 Deep (27–81cm):    {_bar(sm27,100)} <code>{sm27}%</code>")
        if st0 is not None:
            soil_lines.append(f"🌡️ Soil temp surface: <code>{st0}°C</code>")
        if st6 is not None:
            soil_lines.append(f"🌡️ Soil temp 6cm:     <code>{st6}°C</code>")
        if st18 is not None:
            soil_lines.append(f"🌡️ Soil temp 18cm:    <code>{st18}°C</code>")
        if vv is not None:
            soil_lines.append(f"📡 SAR moisture (Sentinel-1): <code>{vv} dB</code> · <i>{sar_label}</i>")

    soil_block = ("<b>🌍 Soil Profile</b>\n" + "\n".join(soil_lines)) if soil_lines else ""

    # ── 7-DAY FORECAST ───────────────────────────────────────────────────────
    forecast_block = ""
    if w and w.get("forecast_dates"):
        lines = ["<b>📅 7-Day Forecast</b>"]
        dates    = w["forecast_dates"]
        rains    = w["forecast_rain"]
        chances  = w["forecast_rain_chance"]
        max_temps = w["forecast_max_temp"]
        et0_today = w.get("et0_today_mm")

        for i, d in enumerate(dates):
            label = "Today" if i == 0 else ("Tmrw" if i == 1 else datetime.fromisoformat(d).strftime("%a"))
            r  = rains[i]   if i < len(rains)    else 0
            ch = chances[i] if i < len(chances)  else 0
            mx = max_temps[i] if i < len(max_temps) else "?"
            rain_icon = "🌧️" if (r or 0) > 5 else ("🌦️" if (ch or 0) > 40 else "☀️")
            lines.append(f"{rain_icon} <b>{label}</b>  {mx}°C  {r or 0:.1f}mm  {ch or 0}% chance")

        if et0_today:
            lines.append(f"\n💦 <i>ET0 today: {et0_today:.1f} mm — daily water need of crop</i>")

        forecast_block = "\n".join(lines)

    # ── CAUSE HINTS (stress only) ────────────────────────────────────────────
    advice_block = ""
    if status == "stress":
        hints = []
        if w.get("rainfall_mm") is not None and (w.get("rain_7d_mm") or 0) < 10:
            hints.append("very low rainfall this week")
        if w.get("temp_c") and w["temp_c"] > 38:
            hints.append("heat stress (>38°C)")
        sm0 = w.get("soil_moisture_pct_0") if w else None
        if sm0 and sm0 < 20:
            hints.append("surface soil critically dry")
        vpd = w.get("vpd_kpa") if w else None
        if vpd and vpd > 3.0:
            hints.append("high atmospheric evaporation demand")
        if hints:
            advice_block = f"💡 <b>Likely causes:</b> {' · '.join(hints)}"

    # ── FOOTER ───────────────────────────────────────────────────────────────
    footer = f"<i>Sentinel-2 · {stats.get('cloud_pct')}% cloud · Open-Meteo weather · auto-scan</i>"

    # Sowing advisory
    days_since_sowing = stats.get("days_since_sowing")
    advisory_block = ""
    if days_since_sowing is not None:
        advisory_block = _sowing_advisory(stats.get("crop", "soybean"), days_since_sowing, stats, w)

    parts = [header, sat_block, stress_block]
    if weather_block:   parts.append(weather_block)
    if soil_block:      parts.append(soil_block)
    if forecast_block:  parts.append(forecast_block)
    if advice_block:    parts.append(advice_block)
    if advisory_block:  parts.append(advisory_block)
    parts.append(footer)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Agri Monitor")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--map",    action="store_true")
    parser.add_argument("--farm",   type=str, default=None)
    parser.add_argument("--days",   type=int, default=10)
    parser.add_argument("--force",  action="store_true")
    args = parser.parse_args()

    farms = load_farms()
    if args.farm:
        farms = [f for f in farms if f["id"] == args.farm]
        if not farms:
            raise SystemExit(f"Farm not found: {args.farm}")

    for farm in farms:
        print(f"\n{'='*55}")
        print(f"Farm: {farm['name']}")
        print(f"Crop: {farm.get('current_crop')} | District: {farm.get('district')}")
        print(f"{'='*55}")
        print(f"Pulling satellite + weather data...")

        stats = get_farm_stats(farm, days_back=args.days)
        w     = stats.get("weather", {})

        print(f"\n  Area (calculated):   {stats.get('area_bigha')} bigha ({stats.get('area_sqm'):,} m²)")
        for p in stats.get("plots", []):
            print(f"    └ {p['name']}: {p['area_bigha']} bigha ({p['area_sqm']:,} m²)")
        print(f"  Date:                {stats.get('date')}")
        print(f"  Status:              {stats.get('status', '').upper()}")
        print(f"  NDVI:                {stats.get('ndvi_mean')}  (last year: {stats.get('ndvi_last_year', 'n/a')})")
        print(f"  NDRE (early stress): {stats.get('ndre_mean')}{'  ⚠️' if stats.get('ndre_stress') else ''}")
        print(f"  LSWI (leaf water):   {stats.get('lswi_mean')}")
        print(f"  EVI:                 {stats.get('evi_mean')}")
        print(f"  Stress area:         {stats.get('stress_pct')}%")
        print(f"  Cloud cover (sat):   {stats.get('cloud_pct')}%")
        print(f"  Images used:         {stats.get('image_count')}")
        if stats.get("soil_moisture_label"):
            print(f"  SAR soil moisture:   {stats['soil_moisture_label']} ({stats.get('soil_vv_db')} dB)")

        if w:
            print(f"\n  Temp:                {w.get('temp_c')}°C (feels {w.get('feels_like_c')}°C)")
            print(f"  Humidity:            {w.get('humidity_pct')}%")
            print(f"  Wind:                {w.get('wind_kmh')} km/h {w.get('wind_dir')} (gusts {w.get('wind_gusts_kmh')})")
            print(f"  Cloud cover:         {w.get('cloud_cover_pct')}%")
            print(f"  VPD:                 {w.get('vpd_kpa')} kPa")
            print(f"  Rain today:          {w.get('rain_today_mm')} mm ({w.get('rain_chance_pct')}% chance)")
            print(f"  Rain 7 days:         {w.get('rain_7d_mm')} mm")
            print(f"  ET0 today:           {w.get('et0_today_mm')} mm")
            print(f"  Soil moist. 0–1cm:   {w.get('soil_moisture_pct_0')}%")
            print(f"  Soil moist. 3–9cm:   {w.get('soil_moisture_pct_3')}%")
            print(f"  Soil moist. 27–81cm: {w.get('soil_moisture_pct_27')}%")
            print(f"  Soil temp surface:   {w.get('soil_temp_surface_c')}°C")

        if args.map:
            map_path = MAPS_DIR / f"{farm['id']}_{stats['date']}_ndvi.png"
            print(f"\nExporting map → {map_path}")
            save_ndvi_map(farm, map_path)

        if not args.report:
            map_path_sent = MAPS_DIR / f"{farm['id']}_{stats['date']}_ndvi.png" if args.map else None

            # Build and upload dashboard
            from dashboard import publish_dashboard
            dashboard_url = publish_dashboard(farm, stats)

            if stats["status"] in ("stress", "no_data", "pre_sowing", "germination") or args.force:
                print("\nSending Telegram alert...")
                send_telegram_alert(farm, stats, map_path=map_path_sent, dashboard_url=dashboard_url)
            else:
                print("\n  ✅ No stress — no alert sent. (--force to send anyway)")
                if dashboard_url:
                    print(f"  Dashboard: {dashboard_url}")


if __name__ == "__main__":
    main()
