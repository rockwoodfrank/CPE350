#!/usr/bin/env python3
"""
run_all_cameras.py

Reads all cameras from the MongoDB 'cameras' collection and launches
a separate macOS Terminal window running ffmpegreader.py for each camera.
"""

import configparser
import os
import subprocess
import sys
import pymongo


def get_db():
    config = configparser.ConfigParser()
    config.read("connection.ini")
    client = pymongo.MongoClient(config["DEFAULT"]["database"])
    return client["camera-counts"]


def get_all_camera_names():
    db = get_db()
    cameras = list(db["cameras"].find({}, {"name": 1, "_id": 0}))
    return [cam["name"] for cam in cameras if "name" in cam]


def open_in_new_terminal(camera_name: str, cwd: str, python: str):
    """Opens a new macOS Terminal window and runs ffmpegreader.py <camera_name>."""
    cmd = f"cd {cwd!r} && {python!r} ffmpegreader.py {camera_name!r}"
    apple_script = f"""
tell application "Terminal"
    do script "{cmd}"
    activate
end tell
"""
    subprocess.Popen(["osascript", "-e", apple_script])


def main():
    camera_names = get_all_camera_names()

    if not camera_names:
        print("No cameras found in the database.")
        sys.exit(1)

    print(f"Found {len(camera_names)} camera(s): {', '.join(camera_names)}")

    cwd = os.path.dirname(os.path.abspath(__file__))
    python = sys.executable

    for name in camera_names:
        print(f"Opening Terminal window for camera: {name}")
        open_in_new_terminal(name, cwd, python)

    print("All Terminal windows launched.")


if __name__ == "__main__":
    main()
