import cv2
import time
import json
import os
import sqlite3
import datetime
import threading
import math
import sys
import subprocess
import csv
import io
import psutil
import numpy as np
import platform
import paho.mqtt.client as mqtt
from ultralytics import YOLO
from flask import Flask, Response, render_template_string, jsonify, request, send_file

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
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f: system_settings.update(json.load(f))
        except: pass
    else: save_settings()

def save_settings():
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f: json.dump(system_settings, f, indent=4)
    except: pass

def load_cameras_config():
    global cameras_config
    if os.path.exists(CAMERAS_FILE):
        try:
            with open(CAMERAS_FILE, 'r', encoding='utf-8') as f: cameras_config = json.load(f)
            return
        except: pass
    if not cameras_config: save_cameras_config()

def save_cameras_config():
    try:
        with open(CAMERAS_FILE, 'w', encoding='utf-8') as f: json.dump(cameras_config, f, indent=4)
    except: pass

load_settings(); load_cameras_config()

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
    except: pass
    return {"cpu": cpu, "ram": ram, "disk": disk, "temp": temp}

def check_ping(host):
    try:
        param = '-n' if IS_WINDOWS else '-c'
        return subprocess.run(['ping', param, '1', host], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 0
    except: return False

def monitor_loop():
    while True:
        network_status['internet'] = check_ping('8.8.8.8')
        network_status['vpn'] = check_ping(system_settings.get('vpn_server_ip', '10.200.0.1'))
        time.sleep(10)

threading.Thread(target=monitor_loop, daemon=True).start()

# ==========================================
# 3. DATABASE & ANALYTICS (UPGRADED)
# ==========================================
class LocalBuffer:
    def __init__(self):
        self.lock = threading.Lock()
        try:
            self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
            self.cursor = self.conn.cursor()
            self.cursor.execute('''CREATE TABLE IF NOT EXISTS pending_data (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            self.cursor.execute('''CREATE TABLE IF NOT EXISTS history_log (id INTEGER PRIMARY KEY AUTOINCREMENT, cam_id TEXT, in_count INTEGER, out_count INTEGER, checkout_count INTEGER DEFAULT 0, is_staff INTEGER DEFAULT 0, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            self.conn.commit()
        except Exception as e: print(f"DB Init Error: {e}")

    def save(self, payload):
        with self.lock:
            try:
                data = json.dumps(payload)
                if payload.get('is_staff', 0) == 0: self.cursor.execute('INSERT INTO pending_data (payload) VALUES (?)', (data,))
                self.cursor.execute('INSERT INTO history_log (cam_id, in_count, out_count, checkout_count, is_staff) VALUES (?, ?, ?, ?, ?)', 
                                    (payload.get('cam_id'), payload.get('in',0), payload.get('out',0), payload.get('checkout',0), payload.get('is_staff', 0)))
                self.conn.commit()
            except: pass

    def save_history_only(self, payload):
        with self.lock:
            try:
                self.cursor.execute('INSERT INTO history_log (cam_id, in_count, out_count, checkout_count, is_staff) VALUES (?, ?, ?, ?, ?)', 
                                    (payload.get('cam_id'), payload.get('in',0), payload.get('out',0), payload.get('checkout',0), payload.get('is_staff', 0)))
                self.conn.commit()
            except: pass

    def get_batch(self, limit=10):
        with self.lock:
            self.cursor.execute('SELECT id, payload FROM pending_data ORDER BY id ASC LIMIT ?', (limit,))
            return self.cursor.fetchall()

    def delete(self, row_id):
        with self.lock:
            self.cursor.execute('DELETE FROM pending_data WHERE id = ?', (row_id,))
            self.conn.commit()
    
    def count_pending(self):
        with self.lock:
            try: return self.cursor.execute('SELECT COUNT(*) FROM pending_data').fetchone()[0]
            except: return 0

    def cleanup_old_data(self, days):
        with self.lock:
            try:
                self.cursor.execute(f"DELETE FROM history_log WHERE timestamp < date('now', '-{days} days')")
                self.conn.commit()
            except: pass

    def export_csv(self):
        with self.lock:
            self.cursor.execute("SELECT * FROM history_log ORDER BY id DESC")
            rows = self.cursor.fetchall()
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(['ID', 'Camera', 'IN', 'OUT', 'CHECKOUT', 'Is Staff', 'Timestamp'])
            writer.writerows(rows)
            output.seek(0)
            return output

    # --- สถิติรายชั่วโมง (วันนี้) ---
    def get_hourly_stats(self):
        with self.lock:
            try:
                query = """SELECT strftime('%H', timestamp, 'localtime') as hour, SUM(in_count), SUM(out_count), SUM(checkout_count) 
                           FROM history_log 
                           WHERE date(timestamp, 'localtime') = date('now', 'localtime') AND is_staff = 0 
                           GROUP BY hour"""
                self.cursor.execute(query)
                rows = self.cursor.fetchall()
                stats = {h: {'in': 0, 'out': 0, 'checkout': 0} for h in range(24)}
                for r in rows:
                    h = int(r[0])
                    stats[h]['in'] = r[1]
                    stats[h]['out'] = r[2]
                    stats[h]['checkout'] = r[3]
                return stats
            except: return {}

    # --- สถิติรายวัน (เดือนนี้) ---
    def get_daily_stats(self):
        with self.lock:
            try:
                query = """SELECT strftime('%d', timestamp, 'localtime') as day, SUM(in_count), SUM(out_count), SUM(checkout_count) 
                           FROM history_log 
                           WHERE strftime('%Y-%m', timestamp, 'localtime') = strftime('%Y-%m', 'now', 'localtime') AND is_staff = 0 
                           GROUP BY day"""
                self.cursor.execute(query)
                rows = self.cursor.fetchall()
                # สร้าง Dict 1-31 วัน
                stats = {d: {'in': 0, 'out': 0, 'checkout': 0} for d in range(1, 32)}
                for r in rows:
                    d = int(r[0])
                    stats[d]['in'] = r[1]
                    stats[d]['out'] = r[2]
                    stats[d]['checkout'] = r[3]
                return stats
            except: return {}

    # --- สถิติรายเดือน (ปีนี้) ---
    def get_monthly_stats(self):
        with self.lock:
            try:
                query = """SELECT strftime('%m', timestamp, 'localtime') as month, SUM(in_count), SUM(out_count), SUM(checkout_count) 
                           FROM history_log 
                           WHERE strftime('%Y', timestamp, 'localtime') = strftime('%Y', 'now', 'localtime') AND is_staff = 0 
                           GROUP BY month"""
                self.cursor.execute(query)
                rows = self.cursor.fetchall()
                # สร้าง Dict 1-12 เดือน
                stats = {m: {'in': 0, 'out': 0, 'checkout': 0} for m in range(1, 13)}
                for r in rows:
                    m = int(r[0])
                    stats[m]['in'] = r[1]
                    stats[m]['out'] = r[2]
                    stats[m]['checkout'] = r[3]
                return stats
            except: return {}

db = LocalBuffer()

def cleanup_loop():
    while True:
        days = int(system_settings.get('keep_days', 365))
        db.cleanup_old_data(days)
        time.sleep(86400)

threading.Thread(target=cleanup_loop, daemon=True).start()

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

# ==========================================
# 5. SMART CAMERA CLASS
# ==========================================
class VideoCaptureThread:
    def __init__(self, src):
        if str(src).isdigit(): src = int(src)
        self.stream = cv2.VideoCapture(src)
        if IS_WINDOWS and not self.stream.isOpened():
             self.stream = cv2.VideoCapture(src, cv2.CAP_DSHOW)
        self.grabbed, self.frame = self.stream.read()
        self.stopped = False
        self.lock = threading.Lock()
    def start(self):
        threading.Thread(target=self.update, args=(), daemon=True).start()
        return self
    def update(self):
        while not self.stopped:
            grabbed, frame = self.stream.read()
            with self.lock:
                self.grabbed = grabbed
                if grabbed: self.frame = frame
            if not grabbed: time.sleep(0.1)
    def read(self):
        with self.lock: return self.frame.copy() if self.grabbed else None
    def isOpened(self): return self.stream.isOpened()
    def release(self):
        self.stopped = True
        self.stream.release()

class SmartCamera(threading.Thread):
    def __init__(self, cam_id, rtsp_url, config=None):
        super().__init__()
        self.cam_id = cam_id
        self.rtsp_url = rtsp_url
        self.running = True
        self.output_frame = None
        self.lock = threading.Lock()
        self.stats = {"in": 0, "out": 0, "staff_in": 0, "staff_out": 0, "checkout": 0}
        self.config = config if config else {
            "name": f"Camera {cam_id}",
            "line_ratio": 0.5, "line_pos_x": 0.5, "offset_ratio": 0.05,
            "line_angle": 0, "line_length": 1.0, "uniform_color": "None",
            "conf_threshold": 0.3, "invert_dir": False,
            "cashier_mode": False, 
            "cashier_x": 0.3, "cashier_y": 0.3, "cashier_w": 0.4, "cashier_h": 0.4, "cashier_time": 5.0
        }
        self.dwell_times = {}
        self.checked_out_ids = set()

    def stop(self): self.running = False
    def update_config(self, new_config):
        self.config.update(new_config)
        cameras_config[self.cam_id]['config'] = self.config
        save_cameras_config()
    def get_frame(self):
        with self.lock: return self.output_frame.copy() if self.output_frame is not None else None

    def check_uniform(self, frame, x, y, w, h, color_name):
        if color_name == "None" or color_name not in UNIFORM_COLORS: return False
        def get_color_ratio(roi, color_key):
            if roi.size == 0: return 0
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            mask_final = np.zeros(hsv.shape[:2], dtype="uint8")
            for (lower, upper) in UNIFORM_COLORS[color_key]:
                mask_final = cv2.bitwise_or(mask_final, cv2.inRange(hsv, np.array(lower, dtype="uint8"), np.array(upper, dtype="uint8")))
            return cv2.countNonZero(mask_final) / (roi.shape[0] * roi.shape[1])

        t_y1, t_y2 = int(max(0, y - h * 0.4)), int(min(frame.shape[0], y + h * 0.1))
        t_x1, t_x2 = int(max(0, x - w * 0.25)), int(min(frame.shape[1], x + w * 0.25))
        l_y1, l_y2 = int(max(0, y + h * 0.1)), int(min(frame.shape[0], y + h * 0.45))
        l_x1, l_x2 = int(max(0, x - w * 0.25)), int(min(frame.shape[1], x + w * 0.25))

        if color_name == "Black": 
            shirt_black = get_color_ratio(frame[t_y1:t_y2, t_x1:t_x2], "Black")
            pants_black = get_color_ratio(frame[l_y1:l_y2, l_x1:l_x2], "Black")
            return shirt_black > 0.4 and pants_black > 0.4
        else:
            return get_color_ratio(frame[t_y1:t_y2, t_x1:t_x2], color_name) > 0.3

    def run(self):
        print(f"🚀 [{self.cam_id}] AI Engine Started ({self.rtsp_url})")
        model_name = "yolov8n"
        model = YOLO(f"{model_name}.pt") 
        
        while self.running:
            cap = VideoCaptureThread(self.rtsp_url).start()
            if not cap.isOpened(): cap.release(); time.sleep(5); continue
            object_states, object_types = {}, {}
            
            while self.running:
                frame = cap.read()
                if frame is None: time.sleep(0.1); continue
                h, w, _ = frame.shape
                
                cy = int(h * self.config.get('line_ratio', 0.5))
                cx = int(w * self.config.get('line_pos_x', 0.5))
                offset_dist = int(h * self.config.get('offset_ratio', 0.05))
                angle_deg = self.config.get('line_angle', 0)
                length_ratio = self.config.get('line_length', 1.0)
                uniform_color = self.config.get('uniform_color', 'None')
                conf_thresh = self.config.get('conf_threshold', 0.3)
                invert = self.config.get('invert_dir', False)
                
                cashier_mode = self.config.get('cashier_mode', False)
                c_x = int(w * self.config.get('cashier_x', 0.3))
                c_y = int(h * self.config.get('cashier_y', 0.3))
                c_w = int(w * self.config.get('cashier_w', 0.4))
                c_h = int(h * self.config.get('cashier_h', 0.4))
                c_time = self.config.get('cashier_time', 5.0)

                angle_rad = math.radians(angle_deg)
                cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
                nx, ny = -sin_a, cos_a 
                half_len = int((w * length_ratio) / 2)
                p1_x, p1_y = int(cx - half_len * cos_a), int(cy - half_len * sin_a)
                p2_x, p2_y = int(cx + half_len * cos_a), int(cy + half_len * sin_a)

                if not cashier_mode:
                    cv2.line(frame, (p1_x, p1_y), (p2_x, p2_y), (0, 255, 0), 2)
                    m_len = 20
                    cv2.line(frame, (int(p1_x - m_len*nx), int(p1_y - m_len*ny)), (int(p1_x + m_len*nx), int(p1_y + m_len*ny)), (0, 0, 255), 2)
                    cv2.line(frame, (int(p2_x - m_len*nx), int(p2_y - m_len*ny)), (int(p2_x + m_len*nx), int(p2_y + m_len*ny)), (0, 0, 255), 2)
                    for d in [-1, 1]:
                        bx1, by1 = int(p1_x + d * offset_dist * nx), int(p1_y + d * offset_dist * ny)
                        bx2, by2 = int(p2_x + d * offset_dist * nx), int(p2_y + d * offset_dist * ny)
                        cv2.line(frame, (bx1, by1), (bx2, by2), (0, 255, 255), 1)
                
                if cashier_mode:
                    cv2.rectangle(frame, (c_x, c_y), (c_x + c_w, c_y + c_h), (0, 255, 255), 2)
                    cv2.putText(frame, f"CASHIER ({c_time}s)", (c_x, c_y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

                is_open = system_settings['open_hour'] <= datetime.datetime.now().hour < system_settings['close_hour']

                results = model.track(frame, persist=True, classes=[0], conf=conf_thresh, verbose=False, tracker="bytetrack.yaml")
                if results[0].boxes.id is not None:
                    boxes = results[0].boxes.xywh.cpu()
                    ids = results[0].boxes.id.int().cpu().tolist()
                    current_ids = set(ids)
                    
                    for tid in list(self.dwell_times.keys()):
                        if tid not in current_ids: del self.dwell_times[tid]

                    for box, track_id in zip(boxes, ids):
                        x, y, bw, bh = box
                        center_x, center_y = int(x), int(y)
                        
                        if track_id not in object_types: object_types[track_id] = 'staff' if self.check_uniform(frame, x, y, bw, bh, uniform_color) else 'customer'
                        role = object_types[track_id]
                        color = (0, 0, 255) if role == 'staff' else (0, 165, 255)
                        label = "STAFF" if role == 'staff' else f"ID:{track_id}"
                        cv2.rectangle(frame, (int(x-bw/2), int(y-bh/2)), (int(x+bw/2), int(y+bh/2)), color, 2)
                        cv2.putText(frame, label, (int(x-bw/2), int(y-bh/2)-5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

                        if cashier_mode and role == 'customer' and is_open:
                            if c_x < center_x < c_x + c_w and c_y < center_y < c_y + c_h:
                                if track_id not in self.dwell_times: self.dwell_times[track_id] = time.time()
                                else:
                                    elapsed = time.time() - self.dwell_times[track_id]
                                    remaining = max(0, c_time - elapsed)
                                    cv2.putText(frame, f"{remaining:.1f}s", (center_x, center_y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                                    if elapsed >= c_time and track_id not in self.checked_out_ids:
                                        self.stats['checkout'] += 1
                                        self.checked_out_ids.add(track_id)
                                        payload = {"branch": system_settings['branch_name'], "cam_id": self.cam_id, "ts": time.time(), "checkout": 1, "is_staff": 0}
                                        if network_status['mqtt']:
                                            mqtt_client.publish(f"shop/{system_settings['branch_name']}/{self.cam_id}/people_count", json.dumps(payload))
                                            db.save_history_only(payload)
                                        else: db.save(payload)
                                        cv2.rectangle(frame, (c_x, c_y), (c_x + c_w, c_y + c_h), (0, 255, 0), -1) 
                            else:
                                if track_id in self.dwell_times: del self.dwell_times[track_id]

                        if not cashier_mode:
                            dx, dy = center_x - cx, center_y - cy
                            if abs(dx * cos_a + dy * sin_a) > half_len: continue
                            dist_from_line = dx * nx + dy * ny
                            current_state = "UP" if dist_from_line < -offset_dist else ("DOWN" if dist_from_line > offset_dist else "ZONE")

                            if current_state != "ZONE" and track_id in object_states:
                                last_state = object_states[track_id]
                                if current_state != last_state:
                                    raw_dir = None
                                    if last_state == "UP" and current_state == "DOWN": raw_dir = "in"
                                    elif last_state == "DOWN" and current_state == "UP": raw_dir = "out"
                                    if raw_dir:
                                        final_dir = "out" if (raw_dir == "in" and invert) or (raw_dir == "out" and not invert) else "in"
                                        if invert and raw_dir == "out": final_dir = "in" 
                                        if invert and raw_dir == "in": final_dir = "out"
                                        
                                        if role == 'staff':
                                            self.stats[f'staff_{final_dir}'] += 1
                                            payload = {"branch": system_settings['branch_name'], "cam_id": self.cam_id, "ts": time.time(), "in": 1 if final_dir=="in" else 0, "out": 1 if final_dir=="out" else 0, "is_staff": 1}
                                            db.save_history_only(payload)
                                        elif is_open:
                                            self.stats[final_dir] += 1
                                            payload = {"branch": system_settings['branch_name'], "cam_id": self.cam_id, "ts": time.time(), "in": 1 if final_dir=="in" else 0, "out": 1 if final_dir=="out" else 0, "is_staff": 0}
                                            if network_status['mqtt']:
                                                mqtt_client.publish(f"shop/{system_settings['branch_name']}/{self.cam_id}/people_count", json.dumps(payload))
                                                db.save_history_only(payload)
                                            else: db.save(payload)
                                            cv2.circle(frame, (center_x, center_y), 15, (0, 255, 0), -1)
                                            cv2.putText(frame, final_dir.upper(), (center_x, center_y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255,255,255), 2)
                                    object_states[track_id] = current_state
                            elif current_state != "ZONE":
                                object_states[track_id] = current_state
                
                display_frame = cv2.resize(frame, (640, int(640 * h / w)))
                with self.lock: self.output_frame = display_frame
            cap.release()

active_cameras = {}
def init_cameras():
    for cam_id, data in cameras_config.items(): start_camera(cam_id, data['url'], data.get('config'))
def start_camera(cam_id, url, config=None):
    if cam_id in active_cameras: active_cameras[cam_id].stop(); active_cameras[cam_id].join()
    cam = SmartCamera(cam_id, url, config)
    active_cameras[cam_id] = cam
    cam.start()
def stop_remove_camera(cam_id):
    if cam_id in active_cameras: active_cameras[cam_id].stop(); active_cameras[cam_id].join(); del active_cameras[cam_id]

init_cameras()

# ==========================================
# 6. WEB SERVER
# ==========================================
app = Flask(__name__)

@app.route('/')
def index():
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="th">
    <head>
        <title>{{ settings.branch_name }}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css">
        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body { background: #f4f6f9; font-family: 'Sarabun', sans-serif; }
            .feed-container { width: 100%; max-width: 800px; margin: 0 auto; border-radius: 12px; overflow: hidden; background: black; }
            .camera-feed { width: 100%; display: block; }
            .cam-view { display: none; } .cam-view.active { display: block; }
            .status-icon { margin-right: 5px; } .status-ok { color: #198754; } .status-err { color: #dc3545; }
        </style>
    </head>
    <body>
        <nav class="navbar navbar-dark bg-dark mb-4">
            <div class="container">
                <span class="navbar-brand">🎥 {{ settings.branch_name }}</span>
                <div class="text-light d-flex align-items-center gap-3">
                    <span title="Net"><i id="icon-net" class="bi bi-globe status-icon status-err"></i></span>
                    <span title="VPN"><i id="icon-vpn" class="bi bi-shield-lock status-icon status-err"></i></span>
                    <a href="/api/export" class="btn btn-sm btn-outline-light"><i class="bi bi-download"></i> CSV</a>
                </div>
            </div>
        </nav>

        <div class="container">
            <ul class="nav nav-pills mb-3">
                <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#dashboard">Dashboard</button></li>
                <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#settings">⚙️ ตั้งค่า</button></li>
                <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#network">🌐 VPN</button></li>
            </ul>

            <div class="tab-content">
                <div class="tab-pane fade show active" id="dashboard">
                    <div class="card mb-3 shadow-sm">
                        <div class="card-header bg-light d-flex justify-content-between align-items-center">
                            <span class="fw-bold">📊 สถิติร้านค้า</span>
                            <div class="btn-group btn-group-sm">
                                <button class="btn btn-outline-secondary active" onclick="loadChart('hourly')">วันนี้ (รายชั่วโมง)</button>
                                <button class="btn btn-outline-secondary" onclick="loadChart('daily')">เดือนนี้ (รายวัน)</button>
                                <button class="btn btn-outline-secondary" onclick="loadChart('monthly')">ปีนี้ (รายเดือน)</button>
                            </div>
                        </div>
                        <div class="card-body">
                            <div style="height: 250px;"><canvas id="mainChart"></canvas></div>
                        </div>
                    </div>
                    <ul class="nav nav-tabs mb-3" id="camTabs">{% for cam_id, cam in cameras.items() %}<li class="nav-item"><button class="nav-link {% if loop.first %}active{% endif %}" onclick="switchCam('{{ cam_id }}')">{{ cam.config.name }}</button></li>{% endfor %}</ul>
                    {% for cam_id, cam in cameras.items() %}
                    <div id="view-{{ cam_id }}" class="cam-view {% if loop.first %}active{% endif %}">
                        <div class="feed-container"><img src="" data-src="{{ url_for('video_feed', cam_id=cam_id) }}" class="camera-feed" id="img-{{ cam_id }}"></div>
                        <div class="row g-2 mt-2 justify-content-center">
                            <div class="col-3 text-center border-bottom border-success border-3 py-2 bg-white rounded mx-1"><small>IN</small><h4 class="text-success m-0" id="in-{{ cam_id }}">0</h4></div>
                            <div class="col-3 text-center border-bottom border-warning border-3 py-2 bg-white rounded mx-1"><small>OUT</small><h4 class="text-dark m-0" id="out-{{ cam_id }}">0</h4></div>
                            <div class="col-3 text-center border-bottom border-info border-3 py-2 bg-white rounded mx-1"><small>CHECKOUT</small><h4 class="text-info m-0" id="checkout-{{ cam_id }}">0</h4></div>
                        </div>
                        <div class="row g-2 mt-1 justify-content-center">
                            <div class="col-3 text-center border-bottom border-danger border-3 py-2 bg-white rounded mx-1"><small>STAFF IN</small><h5 class="text-danger m-0" id="staff_in-{{ cam_id }}">0</h5></div>
                            <div class="col-3 text-center border-bottom border-danger border-3 py-2 bg-white rounded mx-1"><small>STAFF OUT</small><h5 class="text-danger m-0" id="staff_out-{{ cam_id }}">0</h5></div>
                        </div>
                        <div class="card mt-3 shadow-sm"><div class="card-body">
                            <h6 class="card-title">Config: {{ cam.config.name }}</h6>
                            <div class="row mb-3">
                                <div class="col-12">
                                    <div class="form-check form-switch p-2 bg-light rounded border">
                                        <input class="form-check-input" type="checkbox" {% if cam.config.cashier_mode %}checked{% endif %} onchange="upd('{{ cam_id }}', 'cashier_mode', this.checked)">
                                        <label class="form-check-label fw-bold text-info ms-2">🛒 เปิดโหมดแคชเชียร์ (Cashier Mode)</label>
                                    </div>
                                </div>
                            </div>
                            <div id="cashier-ctrl-{{ cam_id }}" class="row" style="display: {% if cam.config.cashier_mode %}flex{% else %}none{% endif %};">
                                <div class="col-6"><label class="small">📍 Box X</label><input type="range" class="form-range" min="0.0" max="1.0" step="0.05" value="{{ cam.config.cashier_x }}" onchange="upd('{{ cam_id }}', 'cashier_x', this.value)"></div>
                                <div class="col-6"><label class="small">📍 Box Y</label><input type="range" class="form-range" min="0.0" max="1.0" step="0.05" value="{{ cam.config.cashier_y }}" onchange="upd('{{ cam_id }}', 'cashier_y', this.value)"></div>
                                <div class="col-6"><label class="small">↔️ Box Width</label><input type="range" class="form-range" min="0.1" max="1.0" step="0.05" value="{{ cam.config.cashier_w }}" onchange="upd('{{ cam_id }}', 'cashier_w', this.value)"></div>
                                <div class="col-6"><label class="small">↕️ Box Height</label><input type="range" class="form-range" min="0.1" max="1.0" step="0.05" value="{{ cam.config.cashier_h }}" onchange="upd('{{ cam_id }}', 'cashier_h', this.value)"></div>
                                <div class="col-12 mt-2"><label class="small">⏱️ เวลาขั้นต่ำ (วินาที) ที่ต้องยืน: <span id="time-val-{{ cam_id }}">{{ cam.config.cashier_time }}</span>s</label><input type="range" class="form-range" min="1" max="20" step="1" value="{{ cam.config.cashier_time }}" oninput="document.getElementById('time-val-{{ cam_id }}').innerText=this.value" onchange="upd('{{ cam_id }}', 'cashier_time', this.value)"></div>
                            </div>
                            <div id="line-ctrl-{{ cam_id }}" class="row" style="display: {% if cam.config.cashier_mode %}none{% else %}flex{% endif %};">
                                <div class="col-6 mb-2"><div class="form-check form-switch"><input class="form-check-input" type="checkbox" {% if cam.config.invert_dir %}checked{% endif %} onchange="upd('{{ cam_id }}', 'invert_dir', this.checked)"><label class="form-check-label text-danger small fw-bold">🔄 สลับเข้า/ออก</label></div></div>
                                <div class="col-6 mb-2"><label class="small">Conf</label><input type="range" class="form-range" min="0.1" max="0.9" step="0.05" value="{{ cam.config.conf_threshold }}" onchange="upd('{{ cam_id }}', 'conf_threshold', this.value)"></div>
                                <div class="col-6"><label class="small">↕️ Y</label><input type="range" class="form-range" min="0.1" max="0.9" step="0.05" value="{{ cam.config.line_ratio }}" onchange="upd('{{ cam_id }}', 'line_ratio', this.value)"></div>
                                <div class="col-6"><label class="small">↔️ X</label><input type="range" class="form-range" min="0.1" max="0.9" step="0.05" value="{{ cam.config.line_pos_x }}" onchange="upd('{{ cam_id }}', 'line_pos_x', this.value)"></div>
                                <div class="col-6"><label class="small">📐 Angle</label><input type="range" class="form-range" min="-45" max="45" step="1" value="{{ cam.config.line_angle }}" onchange="upd('{{ cam_id }}', 'line_angle', this.value)"></div>
                                <div class="col-6"><label class="small">✂️ Length</label><input type="range" class="form-range" min="0.1" max="1.0" step="0.05" value="{{ cam.config.line_length }}" onchange="upd('{{ cam_id }}', 'line_length', this.value)"></div>
                            </div>
                            <div class="row mt-2 border-top pt-2"><div class="col-12"><label class="small fw-bold text-primary">👔 สีชุดพนักงาน</label><select class="form-select form-select-sm" onchange="upd('{{ cam_id }}', 'uniform_color', this.value)">{% for color in ['None', 'Red', 'Green', 'Blue', 'Yellow', 'Orange', 'Black', 'White'] %}<option value="{{ color }}" {% if cam.config.uniform_color == color %}selected{% endif %}>{{ color }}</option>{% endfor %}</select></div></div>
                        </div></div>
                    </div>
                    {% endfor %}
                </div>
                <div class="tab-pane fade" id="settings"><div class="row"><div class="col-md-6 mb-3"><div class="card h-100"><div class="card-header bg-primary text-white">General Settings</div><div class="card-body"><form id="sysForm">
                            <div class="mb-2"><label>Branch Name</label><input type="text" class="form-control" name="branch_name" value="{{ settings.branch_name }}"></div>
                            <div class="mb-2"><label>MQTT IP</label><input type="text" class="form-control" name="mqtt_broker" value="{{ settings.mqtt_broker }}"></div>
                            <div class="mb-2"><label>VPN Check IP</label><input type="text" class="form-control" name="vpn_server_ip" value="{{ settings.vpn_server_ip }}"></div>
                            <div class="row mb-2"><div class="col"><label>Open (Hr)</label><input type="number" class="form-control" name="open_hour" value="{{ settings.open_hour }}"></div><div class="col"><label>Close (Hr)</label><input type="number" class="form-control" name="close_hour" value="{{ settings.close_hour }}"></div></div>
                            <button type="button" onclick="saveSystem()" class="btn btn-success w-100 mt-2">Save & Restart</button>
                        </form></div></div></div><div class="col-md-6 mb-3"><div class="card h-100"><div class="card-header bg-dark text-white">Cameras</div><div class="card-body p-0"><ul class="list-group list-group-flush">{% for cam_id, data in cameras_config.items() %}<li class="list-group-item d-flex justify-content-between align-items-center"><div><strong>{{ data.config.name }}</strong><br><small class="text-muted text-truncate d-inline-block" style="max-width: 200px;">{{ data.url }}</small></div><button class="btn btn-sm btn-danger" onclick="delCam('{{ cam_id }}')">Del</button></li>{% endfor %}</ul><div class="p-3 border-top"><input type="text" id="newCamName" class="form-control mb-2" placeholder="Name"><input type="text" id="newCamUrl" class="form-control mb-2" placeholder="RTSP URL"><button onclick="addCam()" class="btn btn-primary w-100">Add Camera</button></div></div></div></div></div></div>
                <div class="tab-pane fade" id="network"><div class="card"><div class="card-header bg-warning text-dark">WireGuard Config (Local on Windows: copy to WG App)</div><div class="card-body"><textarea id="wgConfig" class="form-control mb-3" rows="8"></textarea><button onclick="saveWG()" class="btn btn-success w-100">Save Config</button></div></div></div>
            </div>
        </div>

        <script>
            const ctx = document.getElementById('mainChart').getContext('2d');
            let mainChart;
            
            function renderChart(labels, inData, outData, chkData) {
                if(mainChart) mainChart.destroy();
                mainChart = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: labels,
                        datasets: [
                            {label: 'เข้า (IN)', data: inData, backgroundColor: 'rgba(25, 135, 84, 0.7)'},
                            {label: 'ออก (OUT)', data: outData, backgroundColor: 'rgba(255, 193, 7, 0.7)'},
                            {label: 'ชำระเงิน', data: chkData, backgroundColor: 'rgba(13, 202, 240, 0.7)'}
                        ]
                    },
                    options: {responsive: true, maintainAspectRatio: false, scales: {y: {beginAtZero: true}}}
                });
            }

            let currentMode = 'hourly';
            
            function loadChart(mode) {
                currentMode = mode;
                // Highlight active button
                document.querySelectorAll('.btn-group button').forEach(b => b.classList.remove('active'));
                event.target.classList.add('active');
                
                fetch('/api/stats?mode=' + mode).then(r => r.json()).then(data => {
                    let labels = [], ins = [], outs = [], chks = [];
                    const d = data.chart_data;
                    
                    if(mode === 'hourly') {
                        labels = Array.from({length: 24}, (_, i) => i+":00");
                        for(let i=0; i<24; i++) {
                            ins.push(d[i]?.in || 0); outs.push(d[i]?.out || 0); chks.push(d[i]?.checkout || 0);
                        }
                    } else if(mode === 'daily') {
                        labels = Array.from({length: 31}, (_, i) => i+1);
                        for(let i=1; i<=31; i++) {
                            ins.push(d[i]?.in || 0); outs.push(d[i]?.out || 0); chks.push(d[i]?.checkout || 0);
                        }
                    } else if(mode === 'monthly') {
                        labels = ['ม.ค.', 'ก.พ.', 'มี.ค.', 'เม.ย.', 'พ.ค.', 'มิ.ย.', 'ก.ค.', 'ส.ค.', 'ก.ย.', 'ต.ค.', 'พ.ย.', 'ธ.ค.'];
                        for(let i=1; i<=12; i++) {
                            ins.push(d[i]?.in || 0); outs.push(d[i]?.out || 0); chks.push(d[i]?.checkout || 0);
                        }
                    }
                    renderChart(labels, ins, outs, chks);
                });
            }

            // Initial Load
            loadChart('hourly');

            function switchCam(id) { document.querySelectorAll('.cam-view').forEach(el => el.classList.remove('active')); document.querySelectorAll('#camTabs .nav-link').forEach(el => el.classList.remove('active')); document.getElementById('view-' + id).classList.add('active'); event.target.classList.add('active'); const img = document.getElementById('img-' + id); if(!img.src) img.src = img.getAttribute('data-src'); }
            const firstImg = document.querySelector('.camera-feed'); if(firstImg) firstImg.src = firstImg.getAttribute('data-src');
            function upd(id, key, val) { 
                let v = val; if(key === 'invert_dir' || key === 'cashier_mode') v = val; else if(key !== 'uniform_color') v = parseFloat(val); 
                fetch('/api/config/' + id, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({[key]: v}) });
                if(key === 'cashier_mode') { document.getElementById('cashier-ctrl-'+id).style.display = val ? 'flex' : 'none'; document.getElementById('line-ctrl-'+id).style.display = val ? 'none' : 'flex'; }
            }
            function saveSystem() { const formData = new FormData(document.getElementById('sysForm')); const data = Object.fromEntries(formData.entries()); data.open_hour = parseInt(data.open_hour); data.close_hour = parseInt(data.close_hour); if(confirm("Confirm Restart?")) fetch('/api/settings', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) }).then(() => { alert("Restarting..."); setTimeout(() => location.reload(), 5000); }); }
            function addCam() { const name = document.getElementById('newCamName').value; const url = document.getElementById('newCamUrl').value; if(!name || !url) return alert("Required fields missing"); fetch('/api/camera/add', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name, url}) }).then(() => location.reload()); }
            function delCam(id) { if(confirm("Delete?")) fetch('/api/camera/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id}) }).then(() => location.reload()); }
            function loadWG() { fetch('/api/network/wg-config').then(r => r.json()).then(d => document.getElementById('wgConfig').value = d.config); }
            function saveWG() { fetch('/api/network/wg-config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({config: document.getElementById('wgConfig').value}) }).then(() => alert("Saved. Reboot required.")); }
            document.querySelector('[data-bs-target="#network"]').addEventListener('click', loadWG);
            
            // Auto Refresh Stats & Chart
            setInterval(() => { 
                fetch('/api/stats?mode=' + currentMode).then(r => r.json()).then(data => {
                    const setIcon = (id, ok) => { const el = document.getElementById(id); el.className = ok ? "bi bi-check-circle-fill status-icon status-ok" : "bi bi-x-circle-fill status-icon status-err"; };
                    setIcon('icon-net', data.network.internet); setIcon('icon-vpn', data.network.vpn);
                    
                    // Update Chart Data (without redraw entire chart)
                    const d = data.chart_data;
                    const ins = [], outs = [], chks = [];
                    if(currentMode === 'hourly') {
                        for(let i=0; i<24; i++) { ins.push(d[i]?.in || 0); outs.push(d[i]?.out || 0); chks.push(d[i]?.checkout || 0); }
                    } else if(currentMode === 'daily') {
                        for(let i=1; i<=31; i++) { ins.push(d[i]?.in || 0); outs.push(d[i]?.out || 0); chks.push(d[i]?.checkout || 0); }
                    } else if(currentMode === 'monthly') {
                        for(let i=1; i<=12; i++) { ins.push(d[i]?.in || 0); outs.push(d[i]?.out || 0); chks.push(d[i]?.checkout || 0); }
                    }
                    if(mainChart) {
                        mainChart.data.datasets[0].data = ins;
                        mainChart.data.datasets[1].data = outs;
                        mainChart.data.datasets[2].data = chks;
                        mainChart.update('none');
                    }

                    for (const [id, stats] of Object.entries(data.cameras)) { 
                        const inEl = document.getElementById('in-' + id); if(inEl) inEl.innerText = stats.in; 
                        const outEl = document.getElementById('out-' + id); if(outEl) outEl.innerText = stats.out; 
                        const chkEl = document.getElementById('checkout-' + id); if(chkEl) chkEl.innerText = stats.checkout; 
                        const stIn = document.getElementById('staff_in-' + id); if(stIn) stIn.innerText = stats.staff_in; 
                        const stOut = document.getElementById('staff_out-' + id); if(stOut) stOut.innerText = stats.staff_out; 
                    }
                }); 
            }, 3000);
        </script>
    </body>
    </html>
    """, settings=system_settings, cameras=active_cameras, cameras_config=cameras_config)

@app.route('/video_feed/<cam_id>')
def video_feed(cam_id):
    if cam_id not in active_cameras: return "404", 404
    def gen(cam):
        while True:
            frame = cam.get_frame()
            if frame is not None:
                (flag, enc) = cv2.imencode(".jpg", frame)
                if flag: yield(b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + bytearray(enc) + b'\r\n')
            else: time.sleep(0.1)
    return Response(gen(active_cameras[cam_id]), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/stats')
def api_stats():
    mode = request.args.get('mode', 'hourly')
    stats = {cid: cam.stats for cid, cam in active_cameras.items()}
    
    chart_data = {}
    if mode == 'hourly': chart_data = db.get_hourly_stats()
    elif mode == 'daily': chart_data = db.get_daily_stats()
    elif mode == 'monthly': chart_data = db.get_monthly_stats()

    return jsonify({
        "network": network_status, "hw": get_hw_stats(), "pending": db.count_pending(), 
        "cameras": stats, "chart_data": chart_data
    })

@app.route('/api/export')
def api_export():
    csv_io = db.export_csv()
    return send_file(csv_io, mimetype='text/csv', as_attachment=True, download_name=f'export_{int(time.time())}.csv')

# API Configs
@app.route('/api/config/<cam_id>', methods=['POST'])
def api_update_config(cam_id):
    if cam_id in active_cameras: active_cameras[cam_id].update_config(request.json)
    return jsonify({"status": "ok"})
@app.route('/api/settings', methods=['POST'])
def api_save_settings():
    global system_settings
    system_settings.update(request.json)
    save_settings()
    def restart(): time.sleep(1); os._exit(0)
    threading.Thread(target=restart).start()
    return jsonify({"status": "restarting"})
@app.route('/api/camera/add', methods=['POST'])
def api_add_cam():
    data = request.json
    new_id = f"cam{int(time.time())}"
    new_config = {"url": data['url'], "config": {"name": data['name'], "line_ratio": 0.5, "line_pos_x": 0.5, "offset_ratio": 0.05, "line_angle": 0, "line_length": 1.0}}
    cameras_config[new_id] = new_config
    save_cameras_config()
    start_camera(new_id, new_config['url'], new_config['config'])
    return jsonify({"status": "ok"})
@app.route('/api/camera/delete', methods=['POST'])
def api_del_cam():
    cam_id = request.json['id']
    if cam_id in cameras_config: del cameras_config[cam_id]; save_cameras_config(); stop_remove_camera(cam_id)
    return jsonify({"status": "ok"})
@app.route('/api/network/wg-config', methods=['GET', 'POST'])
def api_wg_config():
    if request.method == 'POST':
        try:
            with open(WG_CONFIG_FILE, 'w') as f: f.write(request.json.get('config'))
            return jsonify({"status": "saved"})
        except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500
    else:
        content = ""
        if os.path.exists(WG_CONFIG_FILE):
            with open(WG_CONFIG_FILE, 'r') as f: content = f.read()
        return jsonify({"config": content})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)