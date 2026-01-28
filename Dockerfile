FROM python:3.10-slim

WORKDIR /app

# ติดตั้ง System Dependencies ที่จำเป็น
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    iputils-ping \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# --- เทคนิคแก้เน็ตหลุด/โหลดช้า ---
# 1. อัปเกรด pip
# 2. ลง PyTorch แบบ CPU (สำหรับ Intel) ก่อน -> ไฟล์เล็กกว่า NVIDIA มาก
# 3. ลง requirements ที่เหลือ โดยเพิ่ม timeout เป็น 1000 วินาที
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --default-timeout=1000 torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir --default-timeout=1000 -r requirements.txt

COPY . .

CMD ["python", "main.py"]
