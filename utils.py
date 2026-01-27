import psutil
import subprocess
import threading
import time
import logging
from config import IS_WINDOWS, system_settings, network_status

logger = logging.getLogger(__name__)

# ==========================================
# 2. SYSTEM MONITOR
# ==========================================
def get_hw_stats():
    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage('.').percent
    temp = 0
    try:
        if not IS_WINDOWS:
            temps = psutil.sensors_temperatures()
            if 'coretemp' in temps: temp = temps['coretemp'][0].current
            elif 'thermal_zone0' in temps: temp = temps['thermal_zone0'][0].current
    except Exception as e:
        logger.debug(f"Could not read temperature: {e}")
    return {"cpu": cpu, "ram": ram, "disk": disk, "temp": temp}

def check_ping(host):
    try:
        param = '-n' if IS_WINDOWS else '-c'
        return subprocess.run(['ping', param, '1', host], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 0
    except Exception as e:
        logger.error(f"Error checking ping for host {host}: {e}")
        return False

def monitor_loop():
    while True:
        network_status['internet'] = check_ping('8.8.8.8')
        network_status['vpn'] = check_ping(system_settings.get('vpn_server_ip', '10.200.0.1'))
        time.sleep(10)

threading.Thread(target=monitor_loop, daemon=True).start()
