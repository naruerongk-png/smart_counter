import sqlite3
import threading
import json
import io
import csv
import time
import logging
from datetime import datetime
from config import DB_FILE, system_settings

logger = logging.getLogger(__name__)

class LocalBuffer:
    def __init__(self):
        self.lock = threading.Lock()
        try:
            self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
            self.cursor = self.conn.cursor()
            
            # ตารางเก็บข้อมูลดิบ (เหมือนเดิม)
            self.cursor.execute('''CREATE TABLE IF NOT EXISTS pending_data (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            self.cursor.execute('''CREATE TABLE IF NOT EXISTS history_log (id INTEGER PRIMARY KEY AUTOINCREMENT, cam_id TEXT, in_count INTEGER, out_count INTEGER, checkout_count INTEGER DEFAULT 0, is_staff INTEGER DEFAULT 0, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            
            # --- [ใหม่] ตารางเก็บสถิติรายวัน ---
            # ใช้เก็บยอดรวมของแต่ละวันแยกตามกล้อง ทำให้ดึงรายงานรายวัน/เดือนได้เร็วมาก
            self.cursor.execute('''CREATE TABLE IF NOT EXISTS daily_stats (
                                    date TEXT, 
                                    cam_id TEXT, 
                                    in_count INTEGER DEFAULT 0, 
                                    out_count INTEGER DEFAULT 0, 
                                    checkout_count INTEGER DEFAULT 0, 
                                    PRIMARY KEY (date, cam_id))''')
            
            self.conn.commit()
            
            # ตรวจสอบและดึงข้อมูลเก่ามาใส่ตารางใหม่ (Migration) ถ้าตารางยังว่าง
            self.migrate_old_data()
            
        except Exception as e:
            logger.exception(f"DB Init Error: {e}")

    def migrate_old_data(self):
        """ดึงข้อมูลจาก history_log มาใส่ daily_stats หากเพิ่งสร้างตารางใหม่"""
        with self.lock:
            try:
                # เช็คว่ามีข้อมูลใน daily_stats หรือยัง
                count = self.cursor.execute("SELECT COUNT(*) FROM daily_stats").fetchone()[0]
                if count == 0:
                    logger.info("Migrating old history_log to daily_stats...")
                    # Query รวมข้อมูลเก่ารายวัน (เฉพาะลูกค้า ไม่รวมพนักงาน)
                    query = """
                        INSERT INTO daily_stats (date, cam_id, in_count, out_count, checkout_count)
                        SELECT date(timestamp, 'localtime') as d, cam_id, 
                               SUM(in_count), SUM(out_count), SUM(checkout_count)
                        FROM history_log
                        WHERE is_staff = 0
                        GROUP BY d, cam_id
                    """
                    self.cursor.execute(query)
                    self.conn.commit()
                    logger.info("Migration completed.")
            except Exception as e:
                logger.error(f"Migration failed: {e}")

    def update_daily_stats(self, cam_id, payload):
        """อัปเดตยอดรายวันทันทีที่มีข้อมูลใหม่"""
        # เฉพาะข้อมูลลูกค้าเท่านั้น (is_staff = 0)
        if payload.get('is_staff', 0) == 1:
            return

        try:
            today_str = datetime.now().strftime('%Y-%m-%d')
            inc = payload.get('in', 0)
            outc = payload.get('out', 0)
            chk = payload.get('checkout', 0)
            
            # ใช้ UPSERT: ถ้ามีแถวของวันนี้แล้วให้อัปเดตบวกเพิ่ม ถ้ายังไม่มีให้สร้างใหม่
            query = """
                INSERT INTO daily_stats (date, cam_id, in_count, out_count, checkout_count) 
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(date, cam_id) DO UPDATE SET
                in_count = in_count + excluded.in_count,
                out_count = out_count + excluded.out_count,
                checkout_count = checkout_count + excluded.checkout_count
            """
            self.cursor.execute(query, (today_str, cam_id, inc, outc, chk))
        except Exception as e:
            logger.error(f"Failed to update daily stats: {e}")

    def save(self, payload):
        with self.lock:
            try:
                data = json.dumps(payload)
                # บันทึกข้อมูลส่ง MQTT
                if payload.get('is_staff', 0) == 0: 
                    self.cursor.execute('INSERT INTO pending_data (payload) VALUES (?)', (data,))
                
                # บันทึก Log ละเอียด
                self.cursor.execute('INSERT INTO history_log (cam_id, in_count, out_count, checkout_count, is_staff) VALUES (?, ?, ?, ?, ?)', 
                                    (payload.get('cam_id'), payload.get('in',0), payload.get('out',0), payload.get('checkout',0), payload.get('is_staff', 0)))
                
                # [ใหม่] อัปเดตตารางสถิติ
                self.update_daily_stats(payload.get('cam_id'), payload)
                
                self.conn.commit()
            except: pass

    def save_history_only(self, payload):
        with self.lock:
            try:
                self.cursor.execute('INSERT INTO history_log (cam_id, in_count, out_count, checkout_count, is_staff) VALUES (?, ?, ?, ?, ?)', 
                                    (payload.get('cam_id'), payload.get('in',0), payload.get('out',0), payload.get('checkout',0), payload.get('is_staff', 0)))
                
                # [ใหม่] อัปเดตตารางสถิติ
                self.update_daily_stats(payload.get('cam_id'), payload)
                
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
                # ลบข้อมูลดิบ (Log) เก่า แต่ข้อมูลใน daily_stats จะยังคงอยู่
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
    # ยังใช้ history_log เพราะ daily_stats ไม่เก็บรายชั่วโมง
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
    # [ปรับปรุง] ใช้ daily_stats แทน history_log เพื่อความเร็ว
    def get_daily_stats(self):
        with self.lock:
            try:
                # ดึงข้อมูลจากตาราง daily_stats
                query = """SELECT strftime('%d', date) as day, SUM(in_count), SUM(out_count), SUM(checkout_count) 
                           FROM daily_stats 
                           WHERE strftime('%Y-%m', date) = strftime('%Y-%m', 'now', 'localtime')
                           GROUP BY day"""
                self.cursor.execute(query)
                rows = self.cursor.fetchall()
                stats = {d: {'in': 0, 'out': 0, 'checkout': 0} for d in range(1, 32)}
                for r in rows:
                    d = int(r[0])
                    if d in stats:
                        stats[d]['in'] = r[1]
                        stats[d]['out'] = r[2]
                        stats[d]['checkout'] = r[3]
                return stats
            except Exception as e: 
                logger.error(f"Get daily stats error: {e}")
                return {}

    # --- สถิติรายเดือน (ปีนี้) ---
    # [ปรับปรุง] ใช้ daily_stats รวมข้อมูลเป็นรายเดือน
    def get_monthly_stats(self):
        with self.lock:
            try:
                # ดึงข้อมูลจากตาราง daily_stats
                query = """SELECT strftime('%m', date) as month, SUM(in_count), SUM(out_count), SUM(checkout_count) 
                           FROM daily_stats 
                           WHERE strftime('%Y', date) = strftime('%Y', 'now', 'localtime')
                           GROUP BY month"""
                self.cursor.execute(query)
                rows = self.cursor.fetchall()
                stats = {m: {'in': 0, 'out': 0, 'checkout': 0} for m in range(1, 13)}
                for r in rows:
                    m = int(r[0])
                    if m in stats:
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