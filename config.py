import json
import os
import platform
import logging

# Get a logger for the current module
logger = logging.getLogger(__name__)


# ==========================================
# 1. SYSTEM CONFIG & CONSTANTS
# ==========================================
IS_WINDOWS = platform.system().lower() == 'windows'
DATA_DIR = "data"
DB_FILE = f"{DATA_DIR}/offline_data.db"
SETTINGS_FILE = f"{DATA_DIR}/settings.json"
CAMERAS_FILE = f"{DATA_DIR}/cameras.json"
WG_CONFIG_FILE = f"{DATA_DIR}/wg_client.conf"

os.makedirs(DATA_DIR, exist_ok=True)

UNIFORM_COLORS = {
    "None": None,
    "Red": [([0, 100, 100], [10, 255, 255]), ([170, 100, 100], [180, 255, 255])],
    "Green": [([35, 50, 50], [85, 255, 255])],
    "Blue": [([100, 100, 50], [140, 255, 255])],
    "Yellow": [([20, 100, 100], [35, 255, 255])],
    "Orange": [([10, 100, 100], [20, 255, 255])],
    "Black": [([0, 0, 0], [180, 255, 90])],
    "White": [([0, 0, 180], [180, 50, 255])]
}

DEFAULT_SETTINGS = {
    "branch_name": "Branch_Windows",
    "mqtt_broker": "127.0.0.1",
    "mqtt_port": 1883,
    "vpn_server_ip": "10.200.0.1",
    "open_hour": 0, "close_hour": 24, "keep_days": 365 # เก็บข้อมูล 1 ปี
}

system_settings = DEFAULT_SETTINGS.copy()
cameras_config = {}
network_status = {"internet": False, "vpn": False, "mqtt": False}

def load_settings():
    global system_settings
    if os.path.exists(SETTINGS_FILE):
        try:
        except Exception as e:
            logger.exception(f"Error loading settings file: {e}")
    else: save_settings()

def save_settings():
    try:
    except Exception as e:
        logger.exception(f"Error saving settings file: {e}")

def load_cameras_config():
    global cameras_config
    if os.path.exists(CAMERAS_FILE):
        try:
            with open(CAMERAS_FILE, 'r', encoding='utf-8') as f: cameras_config = json.load(f)
            return
        except Exception as e:
            logger.exception(f"Error loading cameras config file: {e}")
    if not cameras_config: save_cameras_config()

def save_cameras_config():
    try:
        with open(CAMERAS_FILE, 'w', encoding='utf-8') as f: json.dump(cameras_config, f, indent=4)
    except Exception as e:
        logger.exception(f"Error saving cameras config file: {e}")

load_settings()
load_cameras_config()
