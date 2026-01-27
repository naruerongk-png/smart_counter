import paho.mqtt.client as mqtt
import threading
import json
import time
from config import system_settings, network_status
from database import db

# ==========================================
# 4. MQTT SYSTEM
# ==========================================
mqtt_client = mqtt.Client()

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        network_status['mqtt'] = True
        threading.Thread(target=sync_offline_data).start()

def on_disconnect(client, userdata, rc): network_status['mqtt'] = False

def sync_offline_data():
    while network_status['mqtt']:
        rows = db.get_batch(5)
        if not rows: break
        for row_id, payload_str in rows:
            try:
                payload = json.loads(payload_str)
                branch = system_settings['branch_name']
                cam_id = payload.get('cam_id', 'unknown')
                topic = f"shop/{branch}/{cam_id}/people_count"
                mqtt_client.publish(topic, payload_str)
                db.delete(row_id)
                time.sleep(0.05)
            except: return

mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect

def start_mqtt_thread():
    while True:
        try:
            broker = system_settings.get('mqtt_broker')
            if broker and broker != '127.0.0.1':
                mqtt_client.connect(broker, int(system_settings.get('mqtt_port', 1883)), 60)
                mqtt_client.loop_start()
                break
            else: time.sleep(10)
        except: time.sleep(5)

threading.Thread(target=start_mqtt_thread, daemon=True).start()
