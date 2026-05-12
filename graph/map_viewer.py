#!/usr/bin/env python3
"""
map_viewer.py

Continuous live feed with 15-second animated playback.
Starts immediately with empty map, populates when data arrives.
Enhanced UI with video playback on incident detection.

Location filter behavior:
- All vehicles/incidents from ALL locations are always rendered.
- Selecting a location only changes the map center + zoom (camera focus).
- "All Locations" zooms out to a California-wide view so every cluster is visible.
- Animation frames are synced across locations by timestamp.
- If a selected location isn't in CAMERA_COORDS, its center is computed from the
  mean lat/lon of vehicles at that location in the current data window.
"""

import os
import math
import json
import hashlib
import requests
from pathlib import Path
from dotenv import load_dotenv
from collections import defaultdict
import time

import pandas as pd
import dash
from dash import Dash, dcc, html
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate
import plotly.graph_objects as go


# =========================
# Data source (injected by client.py)
# =========================

def get_latest_data():
    """Placeholder - replaced by client.py at runtime."""
    return None

load_dotenv()
MAPBOX_TOKEN = os.getenv("MAPBOX_ACCESS_TOKEN")
if not MAPBOX_TOKEN:
    raise RuntimeError("MAPBOX_ACCESS_TOKEN is missing in .env")

ANCHOR = {"lat": 34.441560, "lon": -119.808362}

# Fixed camera positions — no DB lookup needed, cameras don't move.
# Locations NOT in this dict will have their center computed dynamically from
# the average lat/lon of their vehicles in the current data window.
CAMERA_COORDS = {
    "patterson": {"lat": 34.441507, "lon": -119.8084},
    "foothill":  {"lat": 35.29415,  "lon": -120.66821},
}

API_BASE_URL = "http://localhost:8000"

MAP_STYLE = "mapbox://styles/mapbox/satellite-streets-v12"
DEFAULT_ZOOM = 18
ALL_LOCATIONS_ZOOM = 6   # California-wide view when no specific location is selected

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8050"))

PLAYBACK_FPS = 3
FRAME_INTERVAL_MS = int(1000 / 3)
MAX_TRAJECTORY_POINTS_PER_OBJECT = 120

COLOR_MAP = {
    "car": "rgb(255,0,0)",
    "truck": "rgb(255,140,0)",
    "person": "rgb(0,255,0)",
    "bus": "rgb(255,255,0)",
}
DEFAULT_COLOR = "rgb(0,128,255)"
INCIDENT_COLOR = "rgb(255,0,255)"

# Caltrans Colors (matching dashboard.py)
CALTRANS_BLUE = "#003366"
CALTRANS_GREEN = "#007B5F"


# =========================
# Data Processing
# =========================

def process_window_data(raw_data, location_filter="all"):
    """
    Build animation frames from raw window data.

    NOTE: We do NOT filter vehicles/incidents by location here. All locations are
    always rendered on the map. The `location_filter` value is only stored on the
    returned dict so the map callback can decide where to center the camera.
    """
    if not raw_data:
        return None

    try:
        vehicles = raw_data.get("vehicles", [])
        incidents = raw_data.get("incidents", [])
        timestamp = raw_data.get("timestamp")

        vehicles_sorted = sorted(vehicles, key=lambda v: v.get("timestamp", ""))

        # Frames are built across ALL locations, keyed by timestamp,
        # so locations animate in sync.
        # Performance: batch-parse all timestamps at once with pd.to_datetime on a
        # list — this is ~90x faster than calling pd.to_datetime per-vehicle inside
        # the loop, which matters a lot when the replayer ships thousands of
        # observations per window.
        frames_dict = defaultdict(lambda: {"vehicles": [], "timestamp_display": None})

        ts_strings = [v.get("timestamp") for v in vehicles_sorted]
        ts_series = pd.to_datetime(ts_strings, errors="coerce", utc=False)
        # Bucket observations into 250ms frames so vehicles from
        # different locations (and slightly different timestamps)
        # actually group together into the same animation frame.
        ts_floored = ts_series.floor('250ms')

        for v, ts_rounded in zip(vehicles_sorted, ts_floored):
            if pd.isna(ts_rounded):
                continue
            ts_key = ts_rounded.isoformat()
            frames_dict[ts_key]["vehicles"].append(v)
            if frames_dict[ts_key]["timestamp_display"] is None:
                frames_dict[ts_key]["timestamp_display"] = ts_rounded.strftime('%H:%M:%S.%f')[:-3]

        sorted_timestamps = sorted(frames_dict.keys())

        frames = []
        for ts_key in sorted_timestamps:
            frame_data = frames_dict[ts_key]
            frames.append({
                "timestamp": ts_key,
                "timestamp_display": frame_data["timestamp_display"],
                "vehicles": frame_data["vehicles"]
            })

        # Use a content-based fingerprint instead of the top-level timestamp.
        # In multi-location mode, one location can update while the "latest"
        # timestamp stays unchanged, which previously caused updates to be skipped.
        snapshot = {
            "timestamp": timestamp,
            "vehicle_count": len(vehicles_sorted),
            "incident_count": len(incidents),
            "vehicle_ids": [str(v.get("id")) for v in vehicles_sorted],
            "vehicle_timestamps": [v.get("timestamp") for v in vehicles_sorted],
        }
        data_fingerprint = hashlib.sha1(
            json.dumps(snapshot, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

        return {
            "vehicles": vehicles_sorted,
            "incidents": incidents,
            "timestamp": timestamp,
            "frames": frames,
            "data_id": data_fingerprint,
            "location_filter": location_filter,
        }

    except Exception as e:
        print(f"Error processing data: {e}")
        import traceback
        traceback.print_exc()
        return None


def compute_center_for_location(all_vehicles, location):
    """
    Compute the geographic center of a location from its vehicles.

    Returns a dict {"lat": ..., "lon": ...} or None if no usable points exist.
    Uses the mean of lat/lon across all vehicle observations at that location.
    """
    if not all_vehicles or not location:
        return None

    lats, lons = [], []
    for v in all_vehicles:
        if v.get("location") != location:
            continue
        lat, lon = v.get("lat"), v.get("lon")
        if lat is None or lon is None:
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        # Skip obviously bogus 0,0 coords
        if lat == 0 and lon == 0:
            continue
        lats.append(lat)
        lons.append(lon)

    if not lats:
        return None

    return {"lat": sum(lats) / len(lats), "lon": sum(lons) / len(lons)}


def build_figure(center, frame_vehicles, all_vehicles, incidents,
                 lat_off=0.0, lon_off=0.0, zoom=DEFAULT_ZOOM):
    fig = go.Figure()

    # --- Trajectories (every vehicle from every location) ---
    # Group points by object id, then emit ALL trajectories in a single
    # Scattermapbox trace using None separators between objects. With thousands
    # of vehicles, one-trace-per-object will overwhelm Plotly; this keeps it
    # to a single trace regardless of vehicle count.
    trajectories = defaultdict(list)
    for v in all_vehicles:
        obj_id = str(v.get("id"))
        lat, lon = v.get("lat"), v.get("lon")
        if lat is None or lon is None:
            continue
        lat = lat + lat_off
        lon = lon + lon_off
        ts = v.get("timestamp")
        if ts:
            trajectories[obj_id].append((ts, lat, lon))

    traj_lats, traj_lons = [], []
    for obj_id, points in trajectories.items():
        if len(points) < 2:
            continue
        points.sort()
        if len(points) > MAX_TRAJECTORY_POINTS_PER_OBJECT:
            points = points[-MAX_TRAJECTORY_POINTS_PER_OBJECT:]
        for _, plat, plon in points:
            traj_lats.append(plat)
            traj_lons.append(plon)
        # None breaks the line between objects so Plotly doesn't connect them
        traj_lats.append(None)
        traj_lons.append(None)

    if traj_lats:
        fig.add_trace(go.Scattermapbox(
            lon=traj_lons,
            lat=traj_lats,
            mode="lines",
            line=dict(width=1, color="rgba(150,150,255,0.3)"),
            hoverinfo="skip",
            showlegend=False,
        ))

    # --- Current-frame vehicle markers ---
    if frame_vehicles:
        lons, lats, colors, texts, customdata = [], [], [], [], []

        for v in frame_vehicles:
            lat, lon = v.get("lat"), v.get("lon")
            if lat is None or lon is None:
                continue
            lat = lat + lat_off
            lon = lon + lon_off

            if not (math.isfinite(lat) and math.isfinite(lon)):
                continue

            has_incident = len(v.get("incidents", [])) > 0

            if has_incident:
                color = INCIDENT_COLOR
                typ = f"{v.get('detected_type', 'unknown')} [INCIDENT]"
            else:
                typ = str(v.get("detected_type", "unknown")).lower()
                color = COLOR_MAP.get(typ, DEFAULT_COLOR)

            lats.append(lat)
            lons.append(lon)
            colors.append(color)
            texts.append(typ)

            speed = v.get("speed_mps", 0)
            accel = v.get("accel")

            customdata.append([
                v.get("id"),
                typ,
                f"{speed:.1f} m/s",
                f"{accel:.2f} m/s²" if accel is not None else "N/A"
            ])

        fig.add_trace(go.Scattermapbox(
            lon=lons,
            lat=lats,
            mode="markers",
            marker=dict(size=7, opacity=0.95, color=colors),
            text=texts,
            customdata=customdata,
            hovertemplate=(
                "ID: %{customdata[0]}<br>"
                "Type: %{customdata[1]}<br>"
                "Speed: %{customdata[2]}<br>"
                "Accel: %{customdata[3]}<extra></extra>"
            ),
        ))

    # --- Incident markers ---
    if incidents and frame_vehicles:
        inc_lats, inc_lons, inc_texts = [], [], []
        vehicles_by_id = {str(v.get("id")): v for v in frame_vehicles}

        for inc in incidents:
            vehicle_ids = inc.get("vehicles", [])
            if not vehicle_ids:
                continue

            first_vehicle = vehicles_by_id.get(str(vehicle_ids[0]))

            if not first_vehicle:
                continue

            lat, lon = first_vehicle.get("lat"), first_vehicle.get("lon")
            if lat is None or lon is None:
                continue
            lat = lat + lat_off
            lon = lon + lon_off

            inc_lats.append(lat)
            inc_lons.append(lon)
            inc_texts.append(f"{inc['incident_type']}<br>Severity: {inc['severity']:.2f}")

        if inc_lats:
            fig.add_trace(go.Scattermapbox(
                lon=inc_lons,
                lat=inc_lats,
                mode="markers+text",
                marker=dict(size=22, opacity=0.7, color="rgb(255,0,0)"),
                text=["!"] * len(inc_lats),
                hovertext=inc_texts,
                hoverinfo="text",
            ))

    fig.update_layout(
        mapbox=dict(
            accesstoken=MAPBOX_TOKEN,
            style=MAP_STYLE,
            center=dict(lat=center["lat"], lon=center["lon"]),
            zoom=zoom,
            pitch=60,
            bearing=30,
        ),
        margin=dict(l=0, r=0, t=0, b=0),
        showlegend=False,
        uirevision="constant",
    )

    return fig


# =========================
# Shared theme helpers
# =========================

TOGGLE_CSS = """
body { margin: 0; padding: 0; overflow-x: hidden; }

.main-container {
    padding: 30px;
    background: #f5f7fa;
    min-height: 100vh;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
    color: #333;
    transition: background 0.2s ease, color 0.2s ease;
}
[data-theme="dark"] .main-container {
    background: #1a1a1a !important;
    color: #e0e0e0 !important;
}

@keyframes spin {
    0% { transform: rotate(0deg); }
    100% { transform: rotate(360deg); }
}

.theme-toggle-btn {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 18px;
    border-radius: 4px;
    border: 2px solid rgba(255,255,255,0.2);
    background: rgba(255,255,255,0.1);
    cursor: pointer;
    font-size: 14px;
    font-weight: 600;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    color: white;
    transition: all 0.25s ease;
    user-select: none;
    white-space: nowrap;
}
.theme-toggle-btn:hover {
    background: rgba(255,255,255,0.2);
}
.theme-toggle-btn .toggle-track {
    width: 38px;
    height: 22px;
    border-radius: 11px;
    background: #ccc;
    position: relative;
    transition: background 0.25s ease;
    flex-shrink: 0;
}
.theme-toggle-btn .toggle-track.active {
    background: #007B5F;
}
.theme-toggle-btn .toggle-thumb {
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: white;
    position: absolute;
    top: 2px;
    left: 2px;
    transition: transform 0.25s ease;
    box-shadow: 0 1px 4px rgba(0,0,0,0.2);
}
.theme-toggle-btn .toggle-track.active .toggle-thumb {
    transform: translateX(16px);
}

/* ---- Dark mode for map_viewer components ---- */
[data-theme="dark"] #stats-bar {
    background: #2d2d2d !important;
    border-color: #404040 !important;
    color: #e0e0e0 !important;
}
[data-theme="dark"] #time-display {
    color: #90aac8 !important;
}
[data-theme="dark"] .controls-panel {
    background: #2d2d2d !important;
    border-color: #404040 !important;
    color: #e0e0e0 !important;
}
[data-theme="dark"] .controls-panel label {
    color: #aaa !important;
}
[data-theme="dark"] .controls-panel .rc-slider-track { background: #007B5F; }
[data-theme="dark"] .controls-panel .rc-slider-rail { background: #404040; }
[data-theme="dark"] #offset-display {
    color: #999 !important;
}
[data-theme="dark"] #no-data-overlay > div {
    background: #2d2d2d !important;
}
[data-theme="dark"] #no-data-overlay h2 {
    color: #aaa !important;
}
[data-theme="dark"] #no-data-overlay p {
    color: #777 !important;
}
[data-theme="dark"] #incident-modal > div {
    background: #2d2d2d !important;
}
[data-theme="dark"] #incident-modal > div > div:last-child {
    border-top-color: #404040 !important;
}
[data-theme="dark"] #incident-modal-text {
    color: #e0e0e0 !important;
}
[data-theme="dark"] #incident-modal-text > div:first-child {
    color: #e0e0e0 !important;
}
[data-theme="dark"] #incident-modal-text div[style*="background"] {
    background: #1a1a1a !important;
}
[data-theme="dark"] #incident-ok-btn {
    background: #2d2d2d !important;
    color: #aaa !important;
    border-color: #404040 !important;
}

/* Force location dropdown in sub-header to always be light */
#location-selector .Select-control {
    background: white !important;
    border-color: #e0e0e0 !important;
}
#location-selector .Select-value-label,
#location-selector .Select-placeholder,
#location-selector input {
    color: #333 !important;
}
#location-selector .Select-menu-outer {
    background: white !important;
    border-color: #e0e0e0 !important;
}
#location-selector .VirtualizedSelectOption {
    background: white !important;
    color: #333 !important;
}
#location-selector .VirtualizedSelectOption:hover,
#location-selector .VirtualizedSelectFocusedOption {
    background: #f0f0f0 !important;
    color: #333 !important;
}
#location-selector .Select-arrow-zone .Select-arrow {
    border-color: #333 transparent transparent !important;
}
"""

TOGGLE_JS = """
window.DASHBOARD_THEME_KEY = 'dashboard_theme';
(function() {
    var match = document.cookie.match('(?:^|; )dashboard_theme=([^;]*)');
    var saved = match ? match[1] : null;
    if (saved) document.documentElement.setAttribute('data-theme', saved);
})();
"""

INDEX_STRING = '''
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        {%css%}
        <style>''' + TOGGLE_CSS + '''</style>
        <script>''' + TOGGLE_JS + '''</script>
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>
'''


def make_theme_toggle(toggle_id="theme-toggle"):
    return html.Div(
        id=f"{toggle_id}-wrapper",
        children=[
            dcc.Checklist(
                id=toggle_id,
                options=[{"label": "", "value": "dark"}],
                value=[],
                style={"display": "none"},
            ),
            html.Button(
                id=f"{toggle_id}-btn",
                children=[
                    html.Div([
                        html.Div(className="toggle-thumb"),
                    ], id=f"{toggle_id}-track", className="toggle-track"),
                    html.Span("Theme: Light", id=f"{toggle_id}-label"),
                ],
                className="theme-toggle-btn",
                n_clicks=0,
            ),
        ],
        style={
            "position": "fixed",
            "top": "25px",
            "right": "30px",
            "zIndex": 10000,
        }
    )


# =========================
# Dash App
# =========================

def main():
    print("Starting map viewer (waiting for data)...")

    data = {
        "vehicles": [],
        "incidents": [],
        "timestamp": "Waiting...",
        "frames": [],
        "data_id": None,
        "location_filter": "all"
    }

    center = ANCHOR
    fig = build_figure(center, [], [], [], zoom=ALL_LOCATIONS_ZOOM)

    app = Dash(__name__)
    app.title = "Live Traffic Feed"
    app.index_string = INDEX_STRING

    app.layout = html.Div([
        make_theme_toggle("theme-toggle"),

        # ── Main header — Caltrans style matching dashboard.py ──
        html.Div([
            html.H1("LIVE TRAFFIC FEED", style={
                "color": "white",
                "margin": "0",
                "fontSize": "32px",
                "fontWeight": "800",
                "letterSpacing": "1.5px",
            }),
            html.Div(style={
                "width": "50px",
                "height": "4px",
                "background": CALTRANS_GREEN,
                "margin": "12px auto",
            }),
            html.P("DIVISION OF TRAFFIC OPERATIONS • REAL-TIME INCIDENT DETECTION", style={
                "color": "rgba(255,255,255,0.9)",
                "margin": "0",
                "fontSize": "13px",
                "letterSpacing": "2px",
                "fontWeight": "500",
            }),
        ], style={
            "padding": "35px 30px",
            "background": CALTRANS_BLUE,
            "borderRadius": "4px 4px 0 0",
            "boxShadow": "0 4px 15px rgba(0,0,0,0.1)",
            "textAlign": "center",
            "borderBottom": f"6px solid {CALTRANS_GREEN}",
        }),

        # ── Sub-header bar: location selector + nav buttons ──
        html.Div([
            html.Div([
                html.Label("Location:", style={
                    "color": "white",
                    "marginRight": "10px",
                    "fontSize": "13px",
                    "fontWeight": "600",
                    "letterSpacing": "0.5px",
                }),
                dcc.Dropdown(
                    id="location-selector",
                    options=[],
                    value="all",
                    clearable=False,
                    style={"width": "180px", "display": "inline-block"}
                ),
            ], style={"display": "flex", "alignItems": "center"}),

            html.Div([
                html.A(
                    html.Button("Home", style={
                        "fontSize": "13px",
                        "padding": "9px 20px",
                        "background": "rgba(255,255,255,0.12)",
                        "color": "white",
                        "border": "1px solid rgba(255,255,255,0.3)",
                        "borderRadius": "4px",
                        "cursor": "pointer",
                        "fontWeight": "600",
                        "letterSpacing": "0.5px",
                        "marginRight": "8px",
                        "transition": "all 0.2s ease",
                    }),
                    href="http://127.0.0.1:8050",
                ),
                html.A(
                    html.Button("All Incidents", style={
                        "fontSize": "13px",
                        "padding": "9px 20px",
                        "background": CALTRANS_GREEN,
                        "color": "white",
                        "border": "none",
                        "borderRadius": "4px",
                        "cursor": "pointer",
                        "fontWeight": "600",
                        "letterSpacing": "0.5px",
                        "marginRight": "8px",
                        "transition": "all 0.2s ease",
                    }),
                    href="http://127.0.0.1:8051",
                ),
                html.A(
                    html.Button("Traffic Heatmap", style={
                        "fontSize": "13px",
                        "padding": "9px 20px",
                        "background": "rgba(255,255,255,0.15)",
                        "color": "white",
                        "border": "1px solid rgba(255,255,255,0.3)",
                        "borderRadius": "4px",
                        "cursor": "pointer",
                        "fontWeight": "600",
                        "letterSpacing": "0.5px",
                        "transition": "all 0.2s ease",
                    }),
                    href="http://127.0.0.1:8052",
                ),
            ], style={"display": "flex", "alignItems": "center"}),
        ], style={
            "display": "flex",
            "alignItems": "center",
            "justifyContent": "space-between",
            "padding": "12px 24px",
            "background": "#002a57",
            "borderRadius": "0 0 4px 4px",
            "marginBottom": "20px",
            "boxShadow": "0 3px 10px rgba(0,0,0,0.15)",
        }),

        # Stats bar
        html.Div(id="stats-bar", style={
            "fontSize": "14px",
            "marginBottom": "15px",
            "padding": "12px 20px",
            "background": "white",
            "borderRadius": "4px",
            "boxShadow": "0 2px 10px rgba(0,0,0,0.05)",
            "border": "1px solid #e0e0e0",
            "letterSpacing": "0.3px",
            "fontWeight": "500",
        }),

        # Map
        html.Div([
            dcc.Graph(id="map", figure=fig, style={"height": "100%", "width": "100%"}),
            html.Div(
                id="no-data-overlay",
                style={"display": "none"},
                children=[
                    html.Div([
                        html.H2("System Standby", style={
                            "color": CALTRANS_BLUE,
                            "marginBottom": "10px",
                            "fontSize": "28px",
                            "fontWeight": "700",
                        }),
                        html.P("Waiting for vehicle data from camera...", style={"color": "#999", "fontSize": "16px"}),
                        html.Div("", id="waiting-spinner", style={
                            "width": "50px",
                            "height": "50px",
                            "border": "5px solid #f3f3f3",
                            "borderTop": f"5px solid {CALTRANS_GREEN}",
                            "borderRadius": "50%",
                            "animation": "spin 1s linear infinite",
                            "margin": "20px auto"
                        }),
                    ], style={
                        "background": "white",
                        "padding": "40px",
                        "borderRadius": "4px",
                        "boxShadow": "0 4px 20px rgba(0,0,0,0.1)",
                        "textAlign": "center"
                    })
                ]
            )
        ], style={
            "height": "700px",
            "borderRadius": "4px",
            "overflow": "hidden",
            "boxShadow": "0 4px 20px rgba(0,0,0,0.1)",
            "marginBottom": "20px",
            "position": "relative"
        }),

        # Controls panel
        html.Div([
            html.Div([
                html.Div(id="time-display", style={
                    "fontSize": "15px",
                    "fontWeight": "700",
                    "marginBottom": "15px",
                    "color": CALTRANS_BLUE,
                    "letterSpacing": "0.5px",
                }),

                html.Div([
                    html.Button("Pause", id="pause-btn", n_clicks=0, style={
                        "marginRight": "10px",
                        "padding": "10px 20px",
                        "fontSize": "13px",
                        "borderRadius": "4px",
                        "border": "none",
                        "background": "#c0392b",
                        "color": "white",
                        "cursor": "pointer",
                        "fontWeight": "600",
                        "letterSpacing": "0.5px",
                    }),
                    html.Button("Resume", id="play-btn", n_clicks=0, style={
                        "padding": "10px 20px",
                        "fontSize": "13px",
                        "borderRadius": "4px",
                        "border": "none",
                        "background": CALTRANS_GREEN,
                        "color": "white",
                        "cursor": "pointer",
                        "fontWeight": "600",
                        "letterSpacing": "0.5px",
                    }),
                ], style={"marginBottom": "25px"}),

                html.Div([
                    html.Div([
                        html.Label("Camera Pitch", style={"fontWeight": "600", "marginBottom": "8px", "display": "block", "color": "#555", "fontSize": "13px", "letterSpacing": "0.5px"}),
                        dcc.Slider(0, 85, step=10, value=60, id="pitch-slider", marks={0: "0°", 45: "45°", 85: "85°"}),
                    ], style={"marginBottom": "20px"}),

                    html.Div([
                        html.Label("Camera Bearing", style={"fontWeight": "600", "marginBottom": "8px", "display": "block", "color": "#555", "fontSize": "13px", "letterSpacing": "0.5px"}),
                        dcc.Slider(0, 360, step=15, value=30, id="bearing-slider", marks={0: "N", 90: "E", 180: "S", 270: "W"}),
                    ], style={"marginBottom": "20px"}),

                    html.Div([
                        html.Label("Latitude Offset", style={"fontWeight": "600", "marginBottom": "8px", "display": "block", "color": "#555", "fontSize": "13px", "letterSpacing": "0.5px"}),
                        dcc.Slider(
                            id="lat-offset-slider",
                            min=-0.001, max=0.001, step=0.00001, value=0.0,
                            marks={-0.001: "−", 0.0: "0", 0.001: "+"},
                            tooltip={"placement": "bottom", "always_visible": True}
                        ),
                    ], style={"marginBottom": "20px"}),

                    html.Div([
                        html.Label("Longitude Offset", style={"fontWeight": "600", "marginBottom": "8px", "display": "block", "color": "#555", "fontSize": "13px", "letterSpacing": "0.5px"}),
                        dcc.Slider(
                            id="lon-offset-slider",
                            min=-0.001, max=0.001, step=0.00001, value=0.0,
                            marks={-0.001: "−", 0.0: "0", 0.001: "+"},
                            tooltip={"placement": "bottom", "always_visible": True}
                        ),
                    ], style={"marginBottom": "10px"}),

                    html.Div(id="offset-display", style={"fontSize": "12px", "color": "#777", "letterSpacing": "0.3px"}),
                ])
            ])
        ], className="controls-panel", style={
            "padding": "25px",
            "background": "white",
            "borderRadius": "4px",
            "boxShadow": "0 2px 10px rgba(0,0,0,0.05)",
            "border": "1px solid #e0e0e0"
        }),

        dcc.Interval(id="animation-interval", interval=FRAME_INTERVAL_MS, n_intervals=0),
        dcc.Interval(id="reload-interval", interval=1000, n_intervals=0),

        dcc.Store(id="data-store", data=data),
        dcc.Location(id="url", refresh=False),
        dcc.Store(id="current-frame", data=0),
        dcc.Store(id="playing", data=True),
        dcc.Store(id="alert-active", data=False),
        dcc.Store(id="last-alerted-data-id", data=None),
        dcc.Store(id="current-incident-id", data=None),
        dcc.Store(id="raw-data-store", data=None),
        dcc.Store(id="theme-store", data="light"),
        dcc.Store(id="location-initialized", data=False),

        # Incident modal
        html.Div(
            id="incident-modal",
            style={"display": "none"},
            children=[
                html.Div(
                    style={
                        "backgroundColor": "white",
                        "padding": "0px",
                        "borderRadius": "4px",
                        "width": "600px",
                        "maxWidth": "90vw",
                        "boxShadow": "0 20px 60px rgba(0,0,0,0.3)",
                        "overflow": "hidden"
                    },
                    children=[
                        html.Div([
                            html.H2("Incident Detected", style={
                                "margin": "0",
                                "color": "white",
                                "fontSize": "22px",
                                "fontWeight": "700",
                                "letterSpacing": "1px",
                            }),
                        ], style={
                            "padding": "20px 30px",
                            "background": CALTRANS_BLUE,
                            "borderBottom": f"4px solid {CALTRANS_GREEN}",
                        }),
                        html.Div([
                            html.Div(id="incident-modal-text", style={"marginBottom": "20px"}),
                            html.Div(id="video-preview-container", style={"marginTop": "20px"}),
                        ], style={"padding": "30px"}),
                        html.Div([
                            html.Button("Watch Video", id="watch-video-btn", n_clicks=0, style={
                                "padding": "11px 24px",
                                "fontSize": "14px",
                                "borderRadius": "4px",
                                "border": "none",
                                "background": CALTRANS_GREEN,
                                "color": "white",
                                "cursor": "pointer",
                                "fontWeight": "600",
                                "letterSpacing": "0.5px",
                                "marginRight": "10px",
                                "boxShadow": "0 4px 15px rgba(0,0,0,0.15)"
                            }),
                            html.Button("Dismiss", id="incident-ok-btn", n_clicks=0, style={
                                "padding": "11px 24px",
                                "fontSize": "14px",
                                "borderRadius": "4px",
                                "border": "2px solid #ddd",
                                "background": "white",
                                "color": "#666",
                                "cursor": "pointer",
                                "fontWeight": "600",
                                "letterSpacing": "0.5px",
                            }),
                        ], style={
                            "padding": "20px 30px",
                            "borderTop": "1px solid #eee",
                            "display": "flex",
                            "justifyContent": "flex-end"
                        }),
                    ],
                )
            ],
        ),

        html.Div(
            f"Official System Dashboard • {PLAYBACK_FPS} FPS Playback • http://{HOST}:{PORT}",
            style={"marginTop": "20px", "textAlign": "center", "color": "#999", "fontSize": "12px", "letterSpacing": "0.5px"}
        ),
    ], id="main-container", className="main-container")

    # ---- Theme toggle clientside callbacks ----
    app.clientside_callback(
        """
        function(n_clicks, current_value) {
            if (n_clicks === undefined || n_clicks === null) {
                return window.dash_clientside.no_update;
            }
            var isDark = current_value && current_value.includes('dark');
            return isDark ? [] : ['dark'];
        }
        """,
        Output("theme-toggle", "value"),
        Input("theme-toggle-btn", "n_clicks"),
        State("theme-toggle", "value"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        """
        function(id) {
            var match = document.cookie.match('(?:^|; )dashboard_theme=([^;]*)');
            var saved = match ? match[1] : null;
            if (saved === 'dark') return ['dark'];
            return [];
        }
        """,
        Output("theme-toggle", "value", allow_duplicate=True),
        Input("theme-toggle", "id"),
        prevent_initial_call='initial_duplicate',
    )

    app.clientside_callback(
        """
        function(value) {
            var isDark = value && value.includes('dark');
            var theme = isDark ? 'dark' : 'light';
            document.cookie = 'dashboard_theme=' + theme + ';path=/;max-age=31536000;SameSite=Lax';
            document.documentElement.setAttribute('data-theme', theme);
            var btn = document.getElementById('theme-toggle-btn');
            var track = document.getElementById('theme-toggle-track');
            var label = document.getElementById('theme-toggle-label');
            if (btn && track && label) {
                if (isDark) {
                    track.classList.add('active');
                    label.textContent = 'Theme: Dark';
                } else {
                    track.classList.remove('active');
                    label.textContent = 'Theme: Light';
                }
            }
            return theme;
        }
        """,
        Output("theme-store", "data"),
        Input("theme-toggle", "value"),
    )

    @app.callback(
        Output("no-data-overlay", "style"),
        Input("data-store", "data"),
    )
    def toggle_no_data_overlay(data):
        if not data or not data.get("vehicles"):
            return {
                "display": "flex",
                "position": "absolute",
                "top": 0, "left": 0,
                "width": "100%", "height": "100%",
                "backgroundColor": "rgba(245, 247, 250, 0.95)",
                "zIndex": 1000,
                "alignItems": "center",
                "justifyContent": "center",
                "borderRadius": "4px"
            }
        return {"display": "none"}

    @app.callback(
        Output("raw-data-store", "data"),
        Input("reload-interval", "n_intervals"),
    )
    def fetch_raw_data(n):
        raw_data = get_latest_data()
        if raw_data is None:
            return dash.no_update
        return raw_data

    @app.callback(
        Output("location-selector", "options"),
        Output("location-selector", "value"),
        Output("location-initialized", "data"),
        Input("raw-data-store", "data"),
        State("location-initialized", "data"),
        State("url", "search"),
    )
    def populate_location_dropdown(raw_data, is_initialized, url_search):
        if not raw_data or not raw_data.get("vehicles"):
            return [], "all", False

        locations = sorted(set(v.get("location") for v in raw_data["vehicles"] if v.get("location")))
        options = [{"label": "All Locations", "value": "all"}]
        options.extend([{"label": loc.title(), "value": loc} for loc in locations])

        # Only apply the URL ?location= param on the very first load
        if not is_initialized:
            url_loc = None
            if url_search:
                from urllib.parse import parse_qs
                params = parse_qs(url_search.lstrip("?"))
                url_loc = params.get("location", [None])[0]
            if url_loc and url_loc in locations:
                return options, url_loc, True
            return options, "all", True

        # Already initialized — update options but preserve whatever the user selected
        return options, dash.no_update, True

    @app.callback(
        Output("data-store", "data"),
        Output("current-frame", "data"),
        Input("raw-data-store", "data"),
        Input("location-selector", "value"),
        State("data-store", "data"),
    )
    def process_and_filter_data(raw_data, location_filter, current_data):
        new_data = process_window_data(raw_data, location_filter)
        if new_data is None:
            return dash.no_update, dash.no_update
        # Only skip update if BOTH the data id and the selected location match
        if (current_data
                and new_data["data_id"] == current_data.get("data_id")
                and new_data["location_filter"] == current_data.get("location_filter")):
            return dash.no_update, dash.no_update
        print(f"New data! {len(new_data['frames'])} frames, {len(new_data['vehicles'])} observations (selected: {location_filter})")
        return new_data, 0

    @app.callback(
        Output("stats-bar", "children"),
        Input("data-store", "data"),
    )
    def update_stats(data):
        if not data or not data.get("vehicles"):
            return html.Div("Waiting for data...", style={"color": "#999"})

        unique_ids = len(set(v.get("id") for v in data["vehicles"]))
        incident_count = len(data.get("incidents", []))
        location = data.get("location_filter", "all")

        return html.Div([
            html.Span(data.get('timestamp', 'Unknown'), style={"marginRight": "30px", "fontWeight": "600", "color": CALTRANS_BLUE}),
            html.Span(f"{unique_ids} Vehicles", style={"marginRight": "30px", "fontWeight": "500"}),
            html.Span(f"{len(data.get('frames', []))} Frames", style={"marginRight": "30px", "fontWeight": "500"}),
            html.Span(f"Viewing: {location.upper()}", style={"marginRight": "30px", "fontWeight": "500"}),
            html.Span(
                f"{incident_count} Incidents",
                style={
                    "fontWeight": "700",
                    "color": "#c0392b" if incident_count > 0 else CALTRANS_GREEN,
                    "padding": "5px 12px",
                    "borderRadius": "4px",
                    "background": "rgba(192, 57, 43, 0.1)" if incident_count > 0 else "rgba(0, 123, 95, 0.1)",
                    "letterSpacing": "0.3px",
                }
            ),
        ])

    @app.callback(
        Output("incident-modal", "style"),
        Output("incident-modal-text", "children"),
        Output("alert-active", "data"),
        Output("playing", "data", allow_duplicate=True),
        Output("last-alerted-data-id", "data"),
        Output("current-incident-id", "data"),
        Input("data-store", "data"),
        State("last-alerted-data-id", "data"),
        State("alert-active", "data"),
        prevent_initial_call=True,
    )
    def maybe_show_incident_modal(data, last_alerted_id, alert_active):
        if not data:
            raise PreventUpdate

        data_id = data.get("data_id")
        incidents = data.get("incidents") or []

        if not incidents:
            raise PreventUpdate
        if last_alerted_id is not None and data_id == last_alerted_id:
            raise PreventUpdate
        if alert_active:
            raise PreventUpdate

        lines = []
        for i, inc in enumerate(incidents[:5], 1):
            itype = inc.get("incident_type", "incident").upper()
            sev = inc.get("severity")
            sev_txt = f"{sev:.2f}" if isinstance(sev, (int, float)) else "N/A"
            vids = inc.get("vehicles", [])
            lines.append(
                html.Div([
                    html.Span(f"#{i} ", style={"fontWeight": "bold", "color": CALTRANS_BLUE}),
                    html.Span(f"{itype}", style={"fontWeight": "700", "marginRight": "10px", "letterSpacing": "0.5px"}),
                    html.Span(f"Severity: {sev_txt}", style={"color": "#c0392b", "fontWeight": "600", "marginRight": "10px"}),
                    html.Span(f"Vehicles: {len(vids)}", style={"color": "#666"}),
                ], style={"marginBottom": "8px", "padding": "10px", "background": "#f8f9fa", "borderRadius": "4px"})
            )

        modal_text = html.Div([
            html.Div(f"{len(incidents)} incident(s) detected in this 15-second window", style={
                "marginBottom": "15px", "fontSize": "15px", "fontWeight": "600", "color": "#333", "letterSpacing": "0.3px"
            }),
            html.Div(lines),
        ])

        modal_style = {
            "display": "flex",
            "position": "fixed",
            "top": 0, "left": 0,
            "width": "100vw", "height": "100vh",
            "backgroundColor": "rgba(0,0,0,0.7)",
            "zIndex": 9999,
            "alignItems": "center",
            "justifyContent": "center",
            "backdropFilter": "blur(4px)"
        }

        first_incident_id = str(incidents[0].get("_id")) if incidents else None
        return modal_style, modal_text, True, False, data_id, first_incident_id

    @app.callback(
        Output("video-preview-container", "children"),
        Input("watch-video-btn", "n_clicks"),
        State("current-incident-id", "data"),
        prevent_initial_call=True,
    )
    def show_video_preview(n_clicks, incident_id):
        if not n_clicks or not incident_id:
            raise PreventUpdate

        try:
            response = requests.get(f"{API_BASE_URL}/videos/incident/{incident_id}", timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "success":
                    video_info = data.get("video")
                    video_id = video_info.get("_id")
                    return html.Div([
                        html.Hr(style={"margin": "20px 0"}),
                        html.H4("Incident Video", style={
                            "marginBottom": "15px",
                            "color": CALTRANS_BLUE,
                            "fontWeight": "700",
                            "letterSpacing": "0.5px",
                        }),
                        html.Video(
                            src=f"{API_BASE_URL}/videos/{video_id}",
                            controls=True,
                            autoPlay=True,
                            style={"width": "100%", "borderRadius": "4px", "boxShadow": "0 4px 15px rgba(0,0,0,0.2)"}
                        ),
                    ])
                else:
                    return html.Div("No video available for this incident", style={"color": "#c0392b", "marginTop": "15px"})
            return html.Div("Could not load video", style={"color": "#c0392b", "marginTop": "15px"})
        except Exception as e:
            return html.Div(f"Error loading video: {str(e)}", style={"color": "#c0392b", "marginTop": "15px"})

    @app.callback(
        Output("incident-modal", "style", allow_duplicate=True),
        Output("alert-active", "data", allow_duplicate=True),
        Output("playing", "data", allow_duplicate=True),
        Output("video-preview-container", "children", allow_duplicate=True),
        Input("incident-ok-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def dismiss_incident_modal(n_clicks):
        if not n_clicks:
            raise PreventUpdate
        return {"display": "none"}, False, True, ""

    @app.callback(
        Output("time-display", "children"),
        Input("current-frame", "data"),
        State("data-store", "data"),
    )
    def update_time_display(frame_idx, data):
        if not data or not data.get("frames"):
            return "Waiting for data..."
        frames = data["frames"]
        if frame_idx >= len(frames):
            return "Loading..."
        frame = frames[frame_idx]
        ts_display = frame.get("timestamp_display", "")
        num_vehicles = len(frame.get("vehicles", []))
        return f"{ts_display} • Frame {frame_idx + 1}/{len(frames)} • {num_vehicles} vehicles"

    @app.callback(
        Output("current-frame", "data", allow_duplicate=True),
        Input("animation-interval", "n_intervals"),
        State("current-frame", "data"),
        State("data-store", "data"),
        State("playing", "data"),
        prevent_initial_call=True,
    )
    def advance_frame(n, current_frame, data, is_playing):
        if not data or not data.get("frames") or not is_playing:
            return dash.no_update
        frames = data["frames"]
        if current_frame >= len(frames) - 1:
            return dash.no_update
        return current_frame + 1

    @app.callback(
        Output("playing", "data"),
        Input("play-btn", "n_clicks"),
        Input("pause-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def toggle_playback(play_clicks, pause_clicks):
        ctx = dash.callback_context
        if not ctx.triggered:
            return True
        button_id = ctx.triggered[0]["prop_id"].split(".")[0]
        return button_id == "play-btn"

    @app.callback(
        Output("map", "figure"),
        Input("current-frame", "data"),
        Input("pitch-slider", "value"),
        Input("bearing-slider", "value"),
        Input("lat-offset-slider", "value"),
        Input("lon-offset-slider", "value"),
        Input("location-selector", "value"),
        State("data-store", "data"),
    )
    def update_map(frame_idx, pitch, bearing, lat_offset, lon_offset, location_filter, data):
        # Empty state
        if not data or not data.get("frames"):
            empty_fig = build_figure(
                ANCHOR, [], [], [],
                lat_off=lat_offset, lon_off=lon_offset, zoom=ALL_LOCATIONS_ZOOM,
            )
            empty_fig.update_layout(mapbox=dict(pitch=int(pitch), bearing=int(bearing)))
            return empty_fig

        frames = data["frames"]
        if frame_idx >= len(frames):
            frame_idx = len(frames) - 1

        frame_vehicles = frames[frame_idx]["vehicles"]   # all locations, current frame
        all_vehicles = data["vehicles"]                  # all locations, all frames
        incidents = data["incidents"]                    # all locations

        # Decide center + zoom based on selected location.
        # We always render every dot/trajectory — the selection only moves the camera.
        if location_filter and location_filter != "all":
            # Priority: hardcoded camera position > computed center from data > ANCHOR fallback
            if location_filter in CAMERA_COORDS:
                center = CAMERA_COORDS[location_filter]
            else:
                computed = compute_center_for_location(all_vehicles, location_filter)
                center = computed if computed else ANCHOR
            zoom = DEFAULT_ZOOM
        else:
            # All Locations: zoom out so every cluster is visible
            center = ANCHOR
            zoom = ALL_LOCATIONS_ZOOM

        new_fig = build_figure(
            center, frame_vehicles, all_vehicles, incidents,
            lat_off=lat_offset, lon_off=lon_offset, zoom=zoom,
        )
        new_fig.update_layout(mapbox=dict(pitch=int(pitch), bearing=int(bearing)))
        return new_fig

    @app.callback(
        Output("offset-display", "children"),
        Input("lat-offset-slider", "value"),
        Input("lon-offset-slider", "value"),
    )
    def show_offsets(lat_off, lon_off):
        return f"Current offsets: lat {lat_off:+.6f}°, lon {lon_off:+.6f}°"

    print("\n" + "=" * 60)
    print("Live Traffic Feed - Enhanced UI")
    print("=" * 60)
    print(f"Open: http://{HOST}:{PORT}")
    print(f"Playing at {PLAYBACK_FPS} FPS (real-time)")
    print("=" * 60 + "\n")

    app.run(debug=False, host=HOST, port=PORT, use_reloader=False)


if __name__ == "__main__":
    main()