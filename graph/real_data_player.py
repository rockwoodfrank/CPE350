#!/usr/bin/env python3
"""
real_data_replayer.py

Reads real vehicle data from data.txt and replays it to client.py via WebSocket.
Acts as a fake server that emits the EXACT same wire format as the real server.py.

Matched against server.py:
  - broadcast_window_data() -> top-level message shape
  - process_window_async()  -> per-vehicle shape

Differences from before (now corrected):
  - top-level timestamp uses datetime.utcnow().isoformat() (no tz suffix)
  - per-vehicle `id` is a string
  - per-vehicle `heading_deg` field added (was missing)
  - per-vehicle `accel` defaults to None (was 0)
  - per-vehicle `zones` field removed (real server doesn't emit it)

Usage:
    1. Put your real data in data.txt (one JSON object per line)
    2. Stop your real server.py
    3. Run: python3 real_data_replayer.py
    4. Run: python3 client.py
"""

import asyncio
import websockets
import json
import time
from datetime import datetime
from collections import defaultdict

import pandas as pd

# Server settings (same as real server.py)
HOST = "127.0.0.1"
PORT = 8000
WS_PATH = "/data/stream"

# Match the real server's window length exactly
WINDOW_SECONDS = 15

# Store connected clients
CONNECTED_CLIENTS = set()

# Data file
DATA_FILE = "data.txt"

# Location aliases: incoming raw names → canonical names used everywhere downstream
LOCATION_ALIASES = {
    "patterson1": "patterson",
    "foothill1": "foothill",
}


def normalize_location(raw_location):
    """Map raw location names (e.g. 'patterson1') to canonical ones ('patterson')."""
    if not raw_location:
        return "unknown"
    return LOCATION_ALIASES.get(raw_location, raw_location)


def parse_real_data_line(line):
    """
    Parse one line of data.txt into our internal point shape.
    Each "point" = one raw camera packet (one timestamp, multiple objects).

    The vehicle dicts produced here match EXACTLY the shape emitted by
    server.py's process_window_async (per-vehicle dict construction).
    """
    try:
        raw = json.loads(line.strip())
        raw_location = raw.get("location", "unknown")
        location = normalize_location(raw_location)
        data = raw.get("data", {})
        ts_iso = data.get("timestamp")
        objects = data.get("objects", [])

        if not ts_iso:
            return None

        ts = pd.to_datetime(ts_iso, errors="coerce", utc=True)
        if pd.isna(ts):
            return None

        # Real server's per-vehicle timestamp comes from `row["timestamp"].isoformat()`
        # after feature extraction. pandas Timestamp.isoformat() produces no trailing
        # "Z" and no "+00:00" if the timestamp is naive. To match closely, strip the
        # source's "Z" and emit a naive-style ISO string.
        if ts_iso.endswith("Z"):
            vehicle_ts = ts_iso[:-1]  # strip trailing Z
        else:
            vehicle_ts = ts_iso

        vehicles = []
        for obj in objects:
            loc = obj.get("location", [0, 0])
            lat = loc[0] if len(loc) > 0 else None
            lon = loc[1] if len(loc) > 1 else None

            # Match server.py shape exactly:
            vehicles.append({
                "id": str(obj.get("id")),                # stringified, like server
                "lat": float(lat) if lat is not None else None,
                "lon": float(lon) if lon is not None else None,
                "speed_mps": float(obj.get("speed", 0) or 0),
                "heading_deg": 0.0,                      # not in raw data; real server defaults to 0
                "accel": None,                           # real server: None when missing (NOT 0)
                "timestamp": vehicle_ts,
                "detected_type": obj.get("type", "Car").lower(),
                "location": location,
                "certainty": float(obj.get("certainty", 0) or 0),
                "incidents": [],                         # we don't run detection here
            })

        return {
            "location": location,
            "timestamp": ts,                             # for window-bucketing only
            "vehicles": vehicles,
        }

    except Exception as e:
        print(f"[ERROR] Failed to parse line: {e}")
        return None


def load_real_data(filename):
    """
    Load all data from data.txt and split each location's stream into
    consecutive ~WINDOW_SECONDS windows of *timestamp* (not count).

    Returns: dict of {canonical_location: [windows]}
        where each window = list of points, each spanning <= WINDOW_SECONDS
        from the window's first point.
    """
    print(f"[LOADER] Reading data from {filename}...")

    location_points = defaultdict(list)

    try:
        with open(filename, "r") as f:
            for line_num, line in enumerate(f, 1):
                if not line.strip():
                    continue

                parsed = parse_real_data_line(line)
                if parsed:
                    location_points[parsed["location"]].append(parsed)

                if line_num % 1000 == 0:
                    print(f"[LOADER] Read {line_num} lines...")

    except FileNotFoundError:
        print(f"[ERROR] File not found: {filename}")
        print(f"[ERROR] Please create {filename} with your real data (one JSON per line)")
        return {}

    print(f"[LOADER] Loaded raw points for {len(location_points)} locations")
    for loc, pts in location_points.items():
        print(f"  - {loc}: {len(pts)} raw points")

    windows_by_location = {}

    for location, points in location_points.items():
        points.sort(key=lambda p: p["timestamp"])

        windows = []
        current_window = []
        window_start_ts = None

        for p in points:
            if not current_window:
                current_window = [p]
                window_start_ts = p["timestamp"]
                continue

            delta = (p["timestamp"] - window_start_ts).total_seconds()
            if delta >= WINDOW_SECONDS:
                windows.append(current_window)
                current_window = [p]
                window_start_ts = p["timestamp"]
            else:
                current_window.append(p)

        if current_window:
            windows.append(current_window)

        durations = []
        for w in windows:
            span = (w[-1]["timestamp"] - w[0]["timestamp"]).total_seconds()
            durations.append(span)

        avg_dur = sum(durations) / len(durations) if durations else 0
        avg_pts = sum(len(w) for w in windows) / len(windows) if windows else 0
        print(
            f"  - {location}: split into {len(windows)} windows "
            f"| avg duration={avg_dur:.1f}s | avg points/window={avg_pts:.1f}"
        )

        windows_by_location[location] = windows

    return windows_by_location


def create_window_message(location, window_points):
    """
    Build the EXACT same shape the real server emits via broadcast_window_data.

    Match reference (server.py):
        message = {
            "type": "window_complete",
            "location": location,
            "timestamp": datetime.utcnow().isoformat(),
            "vehicle_count": len(vehicles),
            "incident_count": len(incidents),
            "vehicles": vehicles,
            "incidents": incidents
        }
    """
    all_vehicles = []
    for pt in window_points:
        all_vehicles.extend(pt["vehicles"])

    return {
        "type": "window_complete",
        "location": location,
        # Use naive UTC, matching real server's `datetime.utcnow().isoformat()`
        "timestamp": datetime.utcnow().isoformat(),
        "vehicle_count": len(all_vehicles),
        "incident_count": 0,
        "vehicles": all_vehicles,
        "incidents": [],
    }


async def broadcast_to_clients(message):
    """Send a JSON-encoded message to every connected client."""
    if not CONNECTED_CLIENTS:
        return
    payload = json.dumps(message)
    tasks = [client.send(payload) for client in CONNECTED_CLIENTS]
    await asyncio.gather(*tasks, return_exceptions=True)


async def replay_data(windows_by_location):
    """
    Replay loop. Each location pushes its next window every WINDOW_SECONDS of
    wall-clock time, looping back to the first window after exhausting them.

    First send happens IMMEDIATELY on startup (no 15s wait), matching the user
    experience of the real server where the first window closes ~15s after the
    first packet arrives.
    """
    if not windows_by_location:
        print("[REPLAYER] No data to replay!")
        return

    print("[REPLAYER] Starting data replay...")

    window_indices = {loc: 0 for loc in windows_by_location}
    next_send_at = {}

    start_time = time.monotonic()
    for i, loc in enumerate(sorted(windows_by_location)):
        # First send: immediate for location 0, +1s for location 1, +2s for 2, etc.
        # Just enough stagger to keep logs readable; doesn't affect correctness.
        next_send_at[loc] = start_time + (i * 1.0)

    while True:
        now_mono = time.monotonic()

        for location, windows in windows_by_location.items():
            if not windows:
                continue
            if now_mono < next_send_at[location]:
                continue

            idx = window_indices[location]
            window_points = windows[idx]

            message = create_window_message(location, window_points)

            span = (window_points[-1]["timestamp"] - window_points[0]["timestamp"]).total_seconds()
            print(
                f"[{location.upper()}] window #{idx + 1}/{len(windows)} "
                f"| points={len(window_points)} | vehicles={message['vehicle_count']} "
                f"| source-span={span:.1f}s"
            )

            await broadcast_to_clients(message)

            window_indices[location] = (idx + 1) % len(windows)
            next_send_at[location] = now_mono + WINDOW_SECONDS

        await asyncio.sleep(0.25)


async def handle_client(websocket):
    """One WebSocket client. Just keeps the connection alive and answers pings."""
    print(f"[CLIENT] New connection from {websocket.remote_address}")
    CONNECTED_CLIENTS.add(websocket)

    try:
        async for message in websocket:
            if message == "ping":
                await websocket.send("pong")

    except websockets.exceptions.ConnectionClosed:
        print("[CLIENT] Connection closed")

    finally:
        CONNECTED_CLIENTS.discard(websocket)


async def main():
    print("=" * 70)
    print(" " * 15 + "REAL DATA REPLAYER SERVER")
    print("=" * 70)
    print()

    windows_by_location = load_real_data(DATA_FILE)

    if not windows_by_location:
        print("\n[ERROR] No data loaded. Exiting.")
        return

    print()
    print(f"WebSocket Server: ws://{HOST}:{PORT}{WS_PATH}")
    print(f"Window length:    {WINDOW_SECONDS}s (matches real server)")
    print(f"Wire format:      identical to server.py broadcast_window_data()")
    print()
    print("Waiting for client.py to connect...")
    print("Press Ctrl+C to stop")
    print("=" * 70)
    print()

    await websockets.serve(handle_client, HOST, PORT)
    asyncio.create_task(replay_data(windows_by_location))
    await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n[EXIT] Stopping replayer...")