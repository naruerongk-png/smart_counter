import time
import os
import cv2
from functools import wraps
from flask import Flask, Response, render_template_string, jsonify, request, send_file, session, redirect, url_for

from config import system_settings, cameras_config, network_status, save_settings, save_cameras_config, WG_CONFIG_FILE
from database import db
from utils import get_hw_stats
from camera import active_cameras, start_camera, stop_remove_camera, init_cameras

# ==========================================
# 6. WEB SERVER
# ==========================================
app = Flask(__name__)
# กุญแจลับสำหรับ Session (ใช้แบบคงที่เพื่อให้ Session ไม่หลุดตอน Restart บ่อยๆ)
app.secret_key = 'smart_counter_secret_key_change_me'

# HTML หน้า Login
LOGIN_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Login - Smart Counter</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background-color: #f4f6f9; display: flex; align-items: center; justify-content: center; height: 100vh; }
        .login-card { max-width: 400px; width: 100%; padding: 2rem; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); background: white; }
    </style>
</head>
<body>
    <div class="login-card text-center">
        <h3 class="mb-4">🔐 Smart Counter</h3>
        {% if error %}<div class="alert alert-danger">{{ error }}</div>{% endif %}
        <form method="POST">
            <div class="mb-3">
                <input type="password" name="password" class="form-control" placeholder="Password" required autofocus>
            </div>
            <button type="submit" class="btn btn-primary w-100">Login</button>
        </form>
    </div>
</body>
</html>
"""

# HTML หน้า Dashboard
DASHBOARD_PAGE = """
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
                <a href="/logout" class="btn btn-sm btn-danger"><i class="bi bi-box-arrow-right"></i></a>
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
                        <div class="mb-2"><label>Admin Password</label><input type="password" class="form-control" name="admin_password" value="{{ settings.admin_password }}" placeholder="Change Password"></div>
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
            
            // [FIX] เช็คว่ามี Event หรือไม่ก่อนเรียกใช้ เพื่อป้องกัน Error ตอนโหลดหน้าครั้งแรก
            if (typeof event !== 'undefined' && event.type === 'click') {
                event.target.classList.add('active');
            } else {
                // คืนค่า Active ให้ปุ่มตามโหมดปัจจุบัน
                const btns = document.querySelectorAll('.btn-group button');
                if (mode === 'hourly' && btns[0]) btns[0].classList.add('active');
                else if (mode === 'daily' && btns[1]) btns[1].classList.add('active');
                else if (mode === 'monthly' && btns[2]) btns[2].classList.add('active');
            }
            
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
        
        // Auto-load first camera
        const firstImg = document.querySelector('.camera-feed'); 
        if(firstImg) firstImg.src = firstImg.getAttribute('data-src');
        
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
        
        setInterval(() => { 
            fetch('/api/stats?mode=' + currentMode).then(r => r.json()).then(data => {
                const setIcon = (id, ok) => { const el = document.getElementById(id); el.className = ok ? "bi bi-check-circle-fill status-icon status-ok" : "bi bi-x-circle-fill status-icon status-err"; };
                setIcon('icon-net', data.network.internet); setIcon('icon-vpn', data.network.vpn);
                
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
"""

# Decorator สำหรับป้องกัน Route
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        # ตรวจสอบรหัสผ่านจาก config
        if password == system_settings.get('admin_password', 'admin'):
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            return render_template_string(LOGIN_PAGE, error="รหัสผ่านไม่ถูกต้อง")
    return render_template_string(LOGIN_PAGE)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template_string(DASHBOARD_PAGE, settings=system_settings, cameras=active_cameras, cameras_config=cameras_config)

@app.route('/video_feed/<cam_id>')
@login_required
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
@login_required
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
@login_required
def api_export():
    csv_io = db.export_csv()
    return send_file(csv_io, mimetype='text/csv', as_attachment=True, download_name=f'export_{int(time.time())}.csv')

# API Configs (ป้องกันทั้งหมด)
@app.route('/api/config/<cam_id>', methods=['POST'])
@login_required
def api_update_config(cam_id):
    if cam_id in active_cameras: active_cameras[cam_id].update_config(request.json)
    return jsonify({"status": "ok"})

@app.route('/api/settings', methods=['POST'])
@login_required
def api_save_settings():
    global system_settings
    system_settings.update(request.json)
    save_settings()
    def restart(): time.sleep(1); os._exit(0)
    threading.Thread(target=restart).start()
    return jsonify({"status": "restarting"})

@app.route('/api/camera/add', methods=['POST'])
@login_required
def api_add_cam():
    data = request.json
    new_id = f"cam{int(time.time())}"
    new_config = {"url": data['url'], "config": {"name": data['name'], "line_ratio": 0.5, "line_pos_x": 0.5, "offset_ratio": 0.05, "line_angle": 0, "line_length": 1.0}}
    cameras_config[new_id] = new_config
    save_cameras_config()
    start_camera(new_id, new_config['url'], new_config['config'])
    return jsonify({"status": "ok"})

@app.route('/api/camera/delete', methods=['POST'])
@login_required
def api_del_cam():
    cam_id = request.json['id']
    if cam_id in cameras_config: del cameras_config[cam_id]; save_cameras_config(); stop_remove_camera(cam_id)
    return jsonify({"status": "ok"})

@app.route('/api/network/wg-config', methods=['GET', 'POST'])
@login_required
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