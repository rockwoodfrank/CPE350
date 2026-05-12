#!/usr/bin/env python3
"""
fake_data_simulator.py

Simulates vehicle data from multiple camera locations and broadcasts it via WebSocket.
Acts as a FAKE SERVER that client.py connects to.

Usage:
    1. Stop your real server.py
    2. Run: python3 fake_data_simulator.py
    3. Run: python3 client.py (it will connect to this fake server)

This will simulate:
- Patterson camera (SLO area): 34.441507, -119.8084
- Foothill camera (Salinas area): 35.29415, -120.66821
"""

import asyncio
import websockets
import json
import random
import time
from datetime import datetime, timedelta

# Camera locations (hardcoded from map_viewer.py)
CAMERA_COORDS = {
    "patterson": {"lat": 34.441507, "lon": -119.8084},
    "foothill": {"lat": 35.29415, "lon": -120.66821},
}

# Server settings (same as real server.py)
HOST = "127.0.0.1"
PORT = 8000
WS_PATH = "/data/stream"

# Store connected clients
CONNECTED_CLIENTS = set()


def generate_random_vehicle(location, base_lat, base_lon, vehicle_id, timestamp):
    """Generate a random vehicle near the camera location."""
    # Random offset within ~100 meters of camera
    lat_offset = random.uniform(-0.001, 0.001)  # ~111 meters
    lon_offset = random.uniform(-0.001, 0.001)
    
    vehicle_types = ["car", "truck", "person", "bus"]
    
    return {
        "id": vehicle_id,
        "location": location,
        "lat": base_lat + lat_offset,
        "lon": base_lon + lon_offset,
        "detected_type": random.choice(vehicle_types),
        "certainty": random.uniform(0.85, 0.99),
        "timestamp": timestamp.isoformat(),
        "speed_mps": random.uniform(5, 25),  # 5-25 m/s
        "accel": random.uniform(-2, 2),
        "incidents": [],  # Most vehicles have no incidents
        "zones": [f"zone_{random.randint(1, 5)}"],
    }


def generate_random_incident(location, vehicles, timestamp):
    """Randomly generate an incident (10% chance)."""
    if random.random() < 0.90:  # 90% no incident
        return None
    
    # Pick 1-2 random vehicles from this location
    location_vehicles = [v for v in vehicles if v["location"] == location]
    if not location_vehicles:
        return None
    
    involved = random.sample(location_vehicles, min(random.randint(1, 2), len(location_vehicles)))
    
    return {
        "_id": f"incident_{int(time.time())}_{location}_{random.randint(1000, 9999)}",
        "incident_type": random.choice(["near-miss", "collision", "anomaly"]),
        "severity": random.uniform(0.5, 0.95),
        "vehicles": [str(v["id"]) for v in involved],
        "timestamp": timestamp.isoformat(),
        "location": location,
        "lat": involved[0]["lat"],
        "lon": involved[0]["lon"],
    }


def generate_window_data(location, window_start, num_vehicles=10):
    """Generate 15 seconds of vehicle data for a single location."""
    base_lat = CAMERA_COORDS[location]["lat"]
    base_lon = CAMERA_COORDS[location]["lon"]
    
    vehicles = []
    vehicle_ids = []
    
    # Generate vehicles over 15 seconds (1 observation per second per vehicle)
    for t in range(15):
        timestamp = window_start + timedelta(seconds=t)
        
        # Each vehicle appears in multiple frames
        for v_idx in range(num_vehicles):
            vehicle_id = f"{location}_{v_idx}_{int(window_start.timestamp())}"
            vehicle_ids.append(vehicle_id)
            
            vehicle = generate_random_vehicle(
                location, base_lat, base_lon, vehicle_id, timestamp
            )
            vehicles.append(vehicle)
    
    # Maybe generate an incident
    incidents = []
    incident = generate_random_incident(location, vehicles, window_start)
    if incident:
        incidents.append(incident)
        # Mark involved vehicles
        for v in vehicles:
            if str(v["id"]) in incident["vehicles"]:
                v["incidents"] = [incident["_id"]]
    
    return {
        "type": "window_complete",
        "location": location,
        "timestamp": window_start.isoformat(),
        "vehicle_count": len(vehicles),
        "incident_count": len(incidents),
        "vehicles": vehicles,
        "incidents": incidents,
    }


async def broadcast_to_clients(message):
    """Send message to all connected clients."""
    if CONNECTED_CLIENTS:
        # Create tasks to send to all clients
        tasks = [client.send(message) for client in CONNECTED_CLIENTS]
        await asyncio.gather(*tasks, return_exceptions=True)


async def simulate_cameras():
    """
    Generate data for multiple cameras and broadcast to clients.
    Runs continuously in background.
    """
    print("[SIMULATOR] Starting camera data generation...")
    
    locations = ["patterson", "foothill"]
    window_counts = {loc: 0 for loc in locations}
    
    # Stagger camera start times
    camera_offsets = {"patterson": 0, "foothill": 3}
    
    while True:
        current_time = time.time()
        
        for location in locations:
            # Check if it's time for this camera to send data
            offset = camera_offsets[location]
            if (current_time - offset) % 15 < 1:  # Send every 15 seconds (with offset)
                window_start = datetime.now()
                
                # Generate data
                data = generate_window_data(location, window_start, num_vehicles=random.randint(5, 15))
                
                window_counts[location] += 1
                
                print(f"[{location.upper()}] Window #{window_counts[location]}: {data['vehicle_count']} vehicles, {data['incident_count']} incidents")
                
                # Broadcast to all connected clients
                await broadcast_to_clients(json.dumps(data))
        
        await asyncio.sleep(1)  # Check every second


async def handle_client(websocket):
    """Handle a client connection."""
    print(f"[CLIENT] New connection from {websocket.remote_address}")
    
    # Add to connected clients
    CONNECTED_CLIENTS.add(websocket)
    
    try:
        # Keep connection alive
        async for message in websocket:
            # Handle ping/pong
            if message == "ping":
                await websocket.send("pong")
    
    except websockets.exceptions.ConnectionClosed:
        print(f"[CLIENT] Connection closed")
    
    finally:
        # Remove from connected clients
        CONNECTED_CLIENTS.discard(websocket)


async def main():
    print("=" * 70)
    print(" " * 15 + "FAKE DATA SIMULATOR SERVER")
    print("=" * 70)
    print()
    print("Simulating multiple camera locations:")
    print("  1. Patterson (SLO area) - sends every 15s starting at t=0")
    print("  2. Foothill (Salinas area) - sends every 15s starting at t=3")
    print()
    print(f"WebSocket Server: ws://{HOST}:{PORT}{WS_PATH}")
    print()
    print("Waiting for client.py to connect...")
    print("Press Ctrl+C to stop")
    print("=" * 70)
    print()
    
    # Start WebSocket server
    server = await websockets.serve(handle_client, HOST, PORT)
    
    # Start camera simulation in background
    simulator_task = asyncio.create_task(simulate_cameras())
    
    # Keep running
    await asyncio.Future()  # Run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n[EXIT] Stopping simulator...")