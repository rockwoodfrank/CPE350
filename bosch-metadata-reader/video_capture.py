#!/usr/bin/env python3
"""
VIDEO CAPTURE ONLY

Continuously captures 15-second video clips from RTSP stream and uploads to API.
Runs independently from metadata parser.

Usage: python video_capture.py <camera_name>
"""

import sys
import os
import subprocess
import time
import requests
from datetime import datetime
import platform
import shutil

from mongointerface import get_camera_data


# -------------------------------------------------------
# Configuration
# -------------------------------------------------------

if len(sys.argv) < 2:
    print("Usage: python video_capture.py <camera_name>")
    sys.exit(1)

camera_name = sys.argv[1]
camera_info = get_camera_data(camera_name)

VIDEO_DURATION = 15  # seconds per clip
VIDEO_OUTPUT_DIR = "~/Documents/CPE 350/bosch-metadata-reader/videos"
API_URL = "http://localhost:8000/videos"

# 🧪 TEST MODE - Set to True to keep videos locally (don't delete)
TEST_MODE = False  # Change to True to view videos locally

# Create output directory
os.makedirs(VIDEO_OUTPUT_DIR, exist_ok=True)


# -------------------------------------------------------
# Video Capture Functions
# -------------------------------------------------------

def upload_video_to_api(filepath: str, timestamp_str: str):
    """
    Uploads captured video to the API endpoint.
    API will return whether to keep or delete the video based on incident detection.
    """
    try:
        with open(filepath, 'rb') as f:
            files = {
                'file': (os.path.basename(filepath), f, 'video/mp4')
            }
            data = {
                'camera': camera_name,
                'timestamp': timestamp_str,
                'duration': VIDEO_DURATION
            }
            
            print(f"📤 Uploading video to {API_URL}...")
            response = requests.post(API_URL, files=files, data=data, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                
                if result.get('saved'):
                    print(f"✅ Video SAVED! ID: {result.get('video_id')} (incident detected)")
                else:
                    print(f"🗑️ Video REJECTED (no incidents detected)")
                
                # 🧪 TEST MODE: Keep file locally if enabled
                if TEST_MODE:
                    print(f"🧪 TEST MODE: Keeping file locally at {filepath}")
                else:
                    # Always delete local file (either uploaded or rejected)
                    os.remove(filepath)
                    print(f"🧹 Deleted local file: {filepath}")
            else:
                print(f"⚠️ Upload failed: {response.status_code} - {response.text}")
                
                # Delete local file even on failure (unless TEST_MODE)
                if not TEST_MODE:
                    os.remove(filepath)
    
    except Exception as e:
        print(f"⚠️ Upload error: {e}")
        
        # Clean up local file on error (unless TEST_MODE)
        if not TEST_MODE:
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except:
                pass


def capture_video_clip():
    """
    Captures a single 15-second video clip from RTSP stream using ffmpeg.
    """
    # Build RTSP URL - using stream1 (main stream)
    rtsp_url = f'rtsp://{camera_info["url"]}/stream1'
    
    # Detect ffmpeg path
    if platform.system() == "Windows":
        ffmpeg_path = r"C:\Users\sammu\Downloads\ffmpeg-2026-01-19-git-43dbc011fa-full_build\bin\ffmpeg.exe"
    else:
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path is None:
            print("⚠️ ffmpeg not found for video capture")
            return None, None
    
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = os.path.join(VIDEO_OUTPUT_DIR, f"{camera_name}_{timestamp_str}.mp4")
    
    print(f"🎥 Starting video capture ({VIDEO_DURATION}s) → {output_file}")
    
    # ffmpeg command to capture 15-second clip
    ffmpeg_cmd = [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-t", str(VIDEO_DURATION),
        "-c", "copy",  # copy codec (no re-encoding)
        "-an",  # no audio
        "-y",  # overwrite if exists
        "-loglevel", "error",
        output_file
    ]
    
    try:
        # Run ffmpeg to capture clip
        result = subprocess.run(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=VIDEO_DURATION + 30
        )
        
        if result.returncode != 0:
            stderr_msg = result.stderr.decode() if result.stderr else "Unknown error"
            print(f"⚠️ ffmpeg video capture failed: {stderr_msg}")
            
            # Clean up partial file if exists
            if os.path.exists(output_file):
                os.remove(output_file)
            
            return None, None
        
        # Check if file exists and has size
        if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
            size_mb = os.path.getsize(output_file) / 1024 / 1024
            print(f"✅ Video captured: {size_mb:.2f} MB")
            return output_file, timestamp_str
        else:
            print(f"⚠️ Video file empty or missing")
            return None, None
    
    except subprocess.TimeoutExpired:
        print(f"⚠️ Video capture timed out after {VIDEO_DURATION + 30}s")
        print(f"   Possible causes:")
        print(f"   - RTSP stream is slow/unavailable")
        print(f"   - Network connectivity issues")
        print(f"   - Camera URL might be wrong")
        
        # Clean up partial file if exists
        if os.path.exists(output_file):
            os.remove(output_file)
            print(f"   🧹 Cleaned up partial file")
        
        return None, None
    
    except Exception as e:
        print(f"⚠️ Video capture error: {e}")
        
        # Clean up partial file if exists
        if os.path.exists(output_file):
            os.remove(output_file)
        
        return None, None


# -------------------------------------------------------
# Main Loop
# -------------------------------------------------------

def main():
    """
    Main loop: continuously capture 15-second clips and upload them.
    """
    print(f"🎥 Video capture started for camera: {camera_name}")
    print(f"📹 Capturing {VIDEO_DURATION}s clips")
    print(f"📤 Uploading to: {API_URL}")
    print(f"💾 Temp directory: {VIDEO_OUTPUT_DIR}")
    print(f"🔄 Press Ctrl+C to stop")
    print("-" * 60)
    
    clip_counter = 0
    
    try:
        while True:
            clip_counter += 1
            print(f"\n🎬 Clip #{clip_counter}")
            
            # Capture video
            filepath, timestamp_str = capture_video_clip()
            
            # Upload if successful
            if filepath and timestamp_str:
                upload_video_to_api(filepath, timestamp_str)
            else:
                print("⚠️ Skipping upload (capture failed)")
                print("⏳ Waiting 10s before retry...")
                time.sleep(10)
    
    except KeyboardInterrupt:
        print("\n\n🛑 Shutting down video capture...")
        print("✅ Goodbye!")
        sys.exit(0)


if __name__ == "__main__":
    main()