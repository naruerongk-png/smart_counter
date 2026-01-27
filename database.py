import sqlite3
import threading
import json
import io
import csv
import time
import logging
from config import DB_FILE, system_settings

logger = logging.getLogger(__name__)

class LocalBuffer:
    def __init__(self):
        self.lock = threading.Lock()
        try:
            self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
            self.cursor = self.conn.cursor()
            self.cursor.execute('''CREATE TABLE IF NOT EXISTS pending_data (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            self.cursor.execute('''CREATE TABLE IF NOT EXISTS history_log (id INTEGER PRIMARY KEY AUTOINCREMENT, cam_id TEXT, in_count INTEGER, out_count INTEGER, checkout_count INTEGER DEFAULT 0, is_staff INTEGER DEFAULT 0, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            self.conn.commit()
        except Exception as e:
            logger.exception(f"DB Init Error: {e}")

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
