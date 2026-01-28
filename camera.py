import cv2
import time
import threading
import math
import datetime
import json
import os
import numpy as np
from ultralytics import YOLO

from config import IS_WINDOWS, UNIFORM_COLORS, system_settings, cameras_config, save_cameras_config, network_status
from database import db
from mqtt import mqtt_client

# ==========================================
# AI MODEL SETUP (OpenVINO Support)
# ==========================================
MODEL_NAME = "yolov8n"
OPENVINO_DIR = f"{MODEL_NAME}_openvino_model"

print("⏳ Checking AI Model...")

# 1. ตรวจสอบว่ามีโฟลเดอร์ OpenVINO หรือยัง ถ้าไม่มีให้ทำการ Export
if not os.path.exists(OPENVINO_DIR):
    print(f"⚙️ OpenVINO model not found. Exporting {MODEL_NAME}.pt to OpenVINO format...")
    try:
        # โหลดโมเดล PyTorch ปกติมาเพื่อ Export
        model = YOLO(f"{MODEL_NAME}.pt")
        # สั่ง Export เป็น OpenVINO (half=True เพื่อความเร็วและประหยัดแรม)
        model.export(format="openvino", half=True)
        print("✅ Export Success!")
    except Exception as e:
        print(f"❌ Export failed: {e}. Fallback to PyTorch model.")

# 2. โหลดโมเดล (เลือก OpenVINO ถ้ามี ถ้าไม่มีใช้ .pt เหมือนเดิม)
if os.path.exists(OPENVINO_DIR):
    print(f"🚀 Loading OpenVINO Model: {OPENVINO_DIR}")
    shared_model = YOLO(OPENVINO_DIR, task="detect")
else:
    print(f"⚠️ Loading Standard PyTorch Model: {MODEL_NAME}.pt")
    shared_model = YOLO(f"{MODEL_NAME}.pt")
    try:
        shared_model.fuse()
    except: pass

print("✅ Model Ready!")

model_lock = threading.Lock()

class VideoCaptureThread:
    def __init__(self, src):
        self.src = src
        # เลือก Driver ให้เหมาะสม
        if str(src).isdigit():
            self.src = int(src)
            if IS_WINDOWS:
                self.stream = cv2.VideoCapture(self.src, cv2.CAP_DSHOW)
            else:
                self.stream = cv2.VideoCapture(self.src)
        else:
            # ใช้ FFMPEG สำหรับ RTSP
            self.stream = cv2.VideoCapture(self.src, cv2.CAP_FFMPEG)
            
        self.grabbed, self.frame = self.stream.read()
        self.stopped = False
        self.lock = threading.Lock()
    
    def start(self):
        threading.Thread(target=self.update, args=(), daemon=True).start()
        return self
    
    def update(self):
        while not self.stopped:
            try:
                grabbed, frame = self.stream.read()
                with self.lock:
                    self.grabbed = grabbed
                    if grabbed: self.frame = frame
                
                if not grabbed: 
                    time.sleep(0.2)
            except Exception:
                time.sleep(1)

    def read(self):
        with self.lock: return self.frame.copy() if self.grabbed else None
    def isOpened(self): return self.stream.isOpened()
    def release(self):
        self.stopped = True
        try: self.stream.release()
        except: pass

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
        
        while self.running:
            cap = None
            try:
                cap = VideoCaptureThread(self.rtsp_url).start()
                time.sleep(2) 
                
                if not cap.isOpened() or not cap.grabbed:
                    print(f"⚠️ [{self.cam_id}] Connection failed. Retrying in 10s...")
                    cap.release()
                    time.sleep(10)
                    continue
                
                print(f"✅ [{self.cam_id}] Stream Connected!")
                object_states, object_types = {}, {}
                
                while self.running:
                    frame = cap.read()
                    if frame is None: 
                        time.sleep(0.1)
                        if cap.stopped: break 
                        continue
                        
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
                        for d in [-1, 1]:
                            bx1, by1 = int(p1_x + d * offset_dist * nx), int(p1_y + d * offset_dist * ny)
                            bx2, by2 = int(p2_x + d * offset_dist * nx), int(p2_y + d * offset_dist * ny)
                            cv2.line(frame, (bx1, by1), (bx2, by2), (0, 255, 255), 1)
                    
                    if cashier_mode:
                        cv2.rectangle(frame, (c_x, c_y), (c_x + c_w, c_y + c_h), (0, 255, 255), 2)
                        cv2.putText(frame, f"CASHIER ({c_time}s)", (c_x, c_y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

                    is_open = system_settings['open_hour'] <= datetime.datetime.now().hour < system_settings['close_hour']

                    # ใช้ Shared Model (OpenVINO)
                    with model_lock:
                        results = shared_model.track(frame, persist=True, classes=[0], conf=conf_thresh, verbose=False, tracker="bytetrack.yaml")
                    
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
                            cv2.rectangle(frame, (int(x-bw/2), int(y-bh/2)), (int(x+bw/2), int(y+bh/2)), color, 2)

                            if cashier_mode and role == 'customer' and is_open:
                                if c_x < center_x < c_x + c_w and c_y < center_y < c_y + c_h:
                                    if track_id not in self.dwell_times: self.dwell_times[track_id] = time.time()
                                    else:
                                        elapsed = time.time() - self.dwell_times[track_id]
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
                                        object_states[track_id] = current_state
                                elif current_state != "ZONE":
                                    object_states[track_id] = current_state
                    
                    display_frame = cv2.resize(frame, (640, int(640 * h / w)))
                    with self.lock: self.output_frame = display_frame
            
            except Exception as e:
                print(f"❌ [{self.cam_id}] System Error: {e}")
                time.sleep(5)
            finally:
                if cap: cap.release()

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