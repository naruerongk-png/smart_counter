import logging
import sys
from logging.handlers import RotatingFileHandler

def setup_logging():
    # ใช้ RotatingFileHandler แทน FileHandler ธรรมดา
    # maxBytes=5MB: ไฟล์จะไม่เกิน 5MB
    # backupCount=3: เก็บสำรองแค่ 3 ไฟล์ล่าสุด
    file_handler = RotatingFileHandler(
        'smart_counter.log', 
        maxBytes=5*1024*1024, 
        backupCount=3,
        encoding='utf-8'
    )
    
    stream_handler = logging.StreamHandler(sys.stdout)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[stream_handler, file_handler]
    )