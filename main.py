from camera import init_cameras
from app import app

if __name__ == '__main__':
    init_cameras()
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
