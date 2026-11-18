import base64
import datetime
import os
import subprocess
import platform
import tempfile

def capture():
    """Captures a screenshot, returns base64 string, machine name, and timestamp."""
    machine_name = platform.node()
    timestamp = datetime.datetime.now().isoformat()
    
    # Platform detection
    system = platform.system()
    
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
        temp_file_path = temp_file.name
    
    try:
        if system == "Linux":
            # Check if gnome-screenshot or scrot is available
            if subprocess.call(["which", "gnome-screenshot"], stdout=subprocess.DEVNULL) == 0:
                subprocess.run(["gnome-screenshot", "-f", temp_file_path])
            elif subprocess.call(["which", "scrot"], stdout=subprocess.DEVNULL) == 0:
                subprocess.run(["scrot", temp_file_path])
            else:
                raise Exception("No screenshot tool found on Linux.")
        elif system == "Darwin": # macOS
            subprocess.run(["screencapture", temp_file_path])
        elif system == "Windows":
            # This is a placeholder for Windows implementation
            raise NotImplementedError("Windows screenshot not implemented yet.")
        else:
            raise Exception(f"Unsupported platform: {system}")
            
        if not os.path.exists(temp_file_path) or os.path.getsize(temp_file_path) == 0:
            raise Exception("Screenshot capture failed.")
            
        with open(temp_file_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            
        return encoded_string, machine_name, timestamp
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)

# Android support needs adb
def capture_android(serial):
    """Captures a screenshot from an Android device via adb."""
    timestamp = datetime.datetime.now().isoformat()
    
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
        temp_file_path = temp_file.name
    
    try:
        # Use a device-side temp file
        device_temp_path = "/sdcard/screenshot.png"
        subprocess.run(["adb", "-s", serial, "shell", "screencap", "-p", device_temp_path], check=True)
        subprocess.run(["adb", "-s", serial, "pull", device_temp_path, temp_file_path], check=True)
        subprocess.run(["adb", "-s", serial, "shell", "rm", device_temp_path], check=True)
        
        with open(temp_file_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            
        return encoded_string, serial, timestamp
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
