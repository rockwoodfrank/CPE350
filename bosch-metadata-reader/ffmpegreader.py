#!/usr/bin/env python3
"""
FFMPEG METADATA XML STREAM PARSER (METADATA ONLY)

- Parses Bosch XML metadata from RTSP stream
- Tracks multiple timestamps per ObjectId
- Preserves frame-by-frame updates internally
- Inserts into MongoDB *every Nth frame* (frame skipping)
- Uses CameraObject attributes directly
"""

import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Dict, List
import sys
import os
import subprocess
import re

from camera_object import CameraObject
from pointSearch import whichLane, setLanePairsFromDBList
from collectData import pushObjectData
from mongointerface import get_camera_data
from broadcastlatlon import connect_to_server, send_websocket_data
from send_to_api import send_to_api

# -------------------------------------------------------
# 1. Initialize camera info + globals
# -------------------------------------------------------

import platform
import shutil

if len(sys.argv) < 2:
    print("Usage: python ffmpegreader.py <camera_name>")
    sys.exit(1)

camera_name = sys.argv[1]
camera_info = get_camera_data(camera_name)

# Websocket for live visualization
connect_to_server(8001)

speedFactor = 2.237  # m/s → mph

activeRoadObjects: Dict[str, CameraObject] = {}
recentQueue: List[CameraObject] = []

currentBin = {
    "counts": defaultdict(lambda: defaultdict(int)),
    "speeds": defaultdict(lambda: defaultdict(float)),
    "timestamp": 0,
    "heatmap": {}
}

lanes = setLanePairsFromDBList(camera_info["zones"])

total_heatmaps: list = []
frameObjects: List[CameraObject] = []
coordinateSet: list = []

timestamp = None
openObject = False
currentObject: CameraObject | None = None

# ⭐ Frame skipping counter
frame_counter = 0
FRAME_SKIP = 5   # save 1 out of every 5 frames


# -------------------------------------------------------
# 2. PARSE LOGIC
# -------------------------------------------------------
def parse_element(event, elem):
    """
    Called for each <start>/<end> of tags by XMLPullParser.
    Builds CameraObject instances and pushes full frames via pushObjectData.
    """
    global timestamp, openObject, currentObject
    global frameObjects, coordinateSet
    global frame_counter, lanes

    tag = elem.tag.split("}")[-1]

    # ------------ FRAME ------------
    if tag == "Frame":
        if event == "start":
            raw_time = elem.attrib.get("UtcTime", "")
            timestamp_str = raw_time.strip()

            if timestamp_str.endswith("Z"):
                timestamp_str = timestamp_str.replace("Z", "+00:00")

            timestamp = timestamp_str

        elif event == "end":

            frame_counter += 1  # ⭐ increment frame index

            # ⭐ ONLY SAVE EVERY Nth FRAME
            if frame_counter % FRAME_SKIP == 0:

                if frameObjects:
                    try:
                        # optional live visualization
                        # send_websocket_data(coordinateSet, camera_info["name"])
                        coordinateSet = []
                    except Exception as e:
                        print("Websocket error:", e)

                    # INSERT FRAME INTO MONGODB (only every N frames)
                    pushObjectData(
                        frameObjects,
                        camera_info["name"],
                        data_push_function=send_to_api,
                        activeRoadObjects=activeRoadObjects,
                        recentQueue=recentQueue,
                        currentBin=currentBin,
                        total_heatmaps=total_heatmaps,
                    )

            # reset for next frame regardless of saving
            frameObjects = []
            return

    # ------------ OBJECT ------------
    if tag == "Object":
        if event == "start":
            openObject = True
            oid = elem.attrib.get("ObjectId")
            currentObject = CameraObject(oid, timestamp)

        elif event == "end":
            openObject = False

            if currentObject is not None:
                coord = currentObject.getCurrentLocation() if hasattr(currentObject, "getCurrentLocation") else None
                zone = currentObject.getCurrentZone() if hasattr(currentObject, "getCurrentZone") else None
                obj_type = getattr(currentObject, "detectedType", None)

                coordinateSet.append({
                    "xy": coord,
                    "zone": zone,
                    "type": obj_type,
                })
                frameObjects.append(currentObject)

            currentObject = None
            elem.clear()
            return

    # ------------ INSIDE OBJECT ------------
    if openObject and currentObject is not None:

        # GEOLOCATION
        if tag == "GeoLocation":
            lat_str = elem.attrib.get("lat")
            lon_str = elem.attrib.get("lon")
            if lat_str and lon_str:
                try:
                    lat = float(lat_str) + float(camera_info["coordinates"][0])
                    lon = float(lon_str) + float(camera_info["coordinates"][1])
                    currentObject.setLatLon(lat, lon)

                    lane = whichLane((lat, lon), lanes)
                    currentObject.add_lane(lane)

                except Exception as e:
                    print(f"⚠ GeoLocation parse failed ({timestamp}): {e}")

        # TYPE + CONFIDENCE
        elif tag == "Type":
            if elem.text and "Likelihood" in elem.attrib:
                try:
                    currentObject.setDetectedType(elem.text)
                    currentObject.setDetectionCertainty(float(elem.attrib["Likelihood"]))
                except Exception as e:
                    print(f"⚠ Type parse failed ({timestamp}): {e}")

        # SPEED (m/s → mph)
        elif tag == "Speed":
            if elem.text:
                try:
                    speed_mps = float(elem.text.strip())
                    currentObject.setSpeed(speed_mps * speedFactor)
                except Exception as e:
                    print(f"⚠ Speed parse failed ({timestamp}): {e}")

        return


# -------------------------------------------------------
# 3. XML STREAM PARSER (LIVE RTSP URL VIA FFMPEG)
# -------------------------------------------------------

# Build RTSP URL from camera_info
rtsp_url = f'rtsp://{camera_info["url"]}/rtsp_tunnel?p=0&line=1&inst=1&vcd=2'

if platform.system() == "Windows":
    FFMPEG_PATH = r"C:\Users\Mike\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0.1-full_build\bin\ffmpeg.exe"
else:
    FFMPEG_PATH = shutil.which("ffmpeg")

    if FFMPEG_PATH is None:
        raise RuntimeError(
            "ffmpeg not found. Install it with `brew install ffmpeg` on macOS."
        )

ffmpeg_cmd = [
    "ffmpeg",
    "-i", rtsp_url,
    "-map", "0:d",
    "-c", "copy",
    "-copy_unknown",
    "-loglevel", "fatal",
    "-f", "data",
    "-"
]


parser = ET.XMLPullParser(['start', 'end'])
parser.feed("<root>")

foundStart = False
chunk_counter = 0

with subprocess.Popen(
    ffmpeg_cmd,
    stdout=subprocess.PIPE,
    bufsize=0
) as process:

    while True:
        chunk = process.stdout.read(4096)

        if not chunk:
            continue

        chunk_counter += 1

        # Look for start of metadata packet
        if not foundStart:
            try:
                decoded = chunk.decode("utf-8", errors="ignore")
                match = re.search("<tt:MetadataStream", decoded)
                if match:
                    chunk = chunk[match.start():]
                    foundStart = True
            except Exception:
                continue

        if foundStart:
            try:
                parser.feed(chunk)

                for event, elem in parser.read_events():
                    try:
                        parse_element(event, elem)
                    except Exception as e:
                        print("Parse error:", e)
                    finally:
                        elem.clear()

            except ET.ParseError:
                # Recover from broken XML packets
                print("⚠ XML malformed — resetting parser")
                foundStart = False
                parser = ET.XMLPullParser(['start', 'end'])
                parser.feed("<root>")