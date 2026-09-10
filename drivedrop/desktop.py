"""Windows tray, per-account singleton and crash supervisor."""
import ctypes
import hashlib
import subprocess
import sys
import threading
import time

class Instance:
    def __init__(self, data_dir):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        self.kernel.CreateMutexW.restype = ctypes.c_void_p
        self.kernel.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        self.kernel.CreateEventW.restype = ctypes.c_void_p
        self.kernel.SetEvent.argtypes = [ctypes.c_void_p]
        self.kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        identity = hashlib.sha256(str(data_dir.absolute()).casefold().encode()).hexdigest()[:32]
        self.mutex = self.kernel.CreateMutexW(None, False, "Local\\DriveDrop-Boss-" + identity)
        if not self.mutex:
            raise OSError("Cannot create DriveDrop singleton")
        self.primary = ctypes.get_last_error() != 183
        self.event = self.kernel.CreateEventW(None, False, False, "Local\\DriveDrop-Show-" + identity)
        if not self.event:
            self.close()
            raise OSError("Cannot create DriveDrop activation event")
        if not self.primary:
            self.kernel.SetEvent(self.event)
    def requested(self):
        return self.kernel.WaitForSingleObject(self.event, 0) == 0
    def close(self):
        for name in ("event", "mutex"):
            handle = getattr(self, name, None)
            if handle:
                self.kernel.CloseHandle(handle)
                setattr(self, name, None)

def tray(actions):
    import pystray
    from PIL import Image, ImageDraw
    picture = Image.new("RGB", (64,64), "#2563eb")
    canvas = ImageDraw.Draw(picture)
    canvas.rectangle((17,13,44,49), outline="white", width=5)
    ready = threading.Event()
    icon = pystray.Icon("DriveDrop", picture, "DriveDrop — Máy chủ đang chạy", pystray.Menu(
        pystray.MenuItem("Mở bảng quản lý", lambda *args: actions.put("show"), default=True),
        pystray.MenuItem("Theo dõi nhân viên", lambda *args: actions.put("monitor")),
        pystray.MenuItem("Thoát và dừng máy chủ", lambda *args: actions.put("quit"))))
    def setup(item):
        item.visible = True
        ready.set()
    threading.Thread(target=lambda: icon.run(setup), daemon=True).start()
    if not ready.wait(5):
        icon.stop()
        raise RuntimeError("Tray did not start")
    return icon

def supervise(data_dir):
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        raise RuntimeError("Supervisor requires the Windows Boss executable")
    delay = 10
    while True:
        code = subprocess.call([sys.executable, "--data-dir", str(data_dir), "gui", "--background"],
                               creationflags=subprocess.CREATE_NO_WINDOW)
        if code == 0:
            return 0
        time.sleep(delay)
        delay = min(delay * 2, 60)
