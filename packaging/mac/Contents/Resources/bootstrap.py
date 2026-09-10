"""First-run setup for an unsigned Mac launcher. Runtime/state stay outside .app."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import platform
import queue
import signal
import stat
import subprocess
import sys
import threading
import time


class Cancelled(Exception):
    pass


def reject_links(path: Path):
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("Đường dẫn DriveDrop có liên kết; cần kiểm tra trước khi chạy.")


def private_dir(path: Path):
    reject_links(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    reject_links(path)
    if not path.is_dir():
        raise RuntimeError("Đường dẫn dữ liệu DriveDrop không phải thư mục.")
    os.chmod(path, 0o700)


def private_file(path: Path, flags):
    reject_links(path)
    fd = os.open(path, flags | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("File trạng thái DriveDrop không hợp lệ.")
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def paths_for(home: Path):
    root = home / "Library" / "Application Support" / "DriveDrop"
    runtime_parent = root / "Runtime"
    runtime = runtime_parent / f"mac-easy-{sys.implementation.cache_tag}-{platform.machine()}-v1"
    logs = root / "Logs"
    return {"root": root, "runtime_parent": runtime_parent, "runtime": runtime,
            "logs": logs, "log": logs / "mac-easy.log", "lock": root / "mac-easy.lock"}


@contextmanager
def launch_lock(path: Path):
    import fcntl
    fd = private_file(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class SetupWorker:
    """No Tk calls here. A single child process is owned and reaped at a time."""
    READY_CODE = (
        "import os,sys,tkinter,cryptography,certifi; "
        "assert sys.version_info >= (3,12); "
        "assert 46 <= int(cryptography.__version__.split('.')[0]) < 51; "
        "assert os.path.isfile(certifi.where()); tkinter.Tcl()"
    )

    def __init__(self, paths, resources: Path, events: queue.Queue):
        self.paths = paths
        self.resources = resources
        self.events = events
        self.cancel = threading.Event()
        self.child_lock = threading.Lock()
        self.child = None
        self.log = None

    def request_cancel(self):
        with self.child_lock:
            self.cancel.set()

    @staticmethod
    def stop_child(child):
        if child.poll() is None:
            if os.name == "posix":
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    child.kill()
                child.wait(timeout=5)
        else:
            child.wait()

    def environment(self):
        environment = dict(os.environ)
        for name in list(environment):
            if name.startswith("PIP_") or name in ("PYTHONHOME", "PYTHONPATH"):
                environment.pop(name, None)
        environment["PIP_CONFIG_FILE"] = os.devnull
        return environment

    def command(self, arguments, *, app=False):
        with self.child_lock:
            if self.cancel.is_set():
                raise Cancelled()
            child = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
                                     stdout=self.log, stderr=subprocess.STDOUT,
                                     cwd=self.paths["runtime"], env=self.environment(),
                                     start_new_session=True)
            self.child = child
        if app:
            self.events.put(("app_started", ""))
        try:
            while child.poll() is None:
                if self.cancel.wait(0.1):
                    self.stop_child(child)
                    raise Cancelled()
            result = child.wait()
            if self.cancel.is_set():
                raise Cancelled()
            return result
        finally:
            with self.child_lock:
                self.child = None

    def ready(self, python):
        if not python.is_file():
            return False
        try:
            return self.command([str(python), "-I", "-c", self.READY_CODE]) == 0
        except OSError:
            return False

    def run(self):
        try:
            for name in ("root", "runtime_parent", "runtime", "logs"):
                private_dir(self.paths[name])
            fd = private_file(self.paths["log"], os.O_WRONLY | os.O_APPEND)
            with os.fdopen(fd, "ab", buffering=0) as self.log:
                self.log.write(("\nDriveDrop Mac launch " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n").encode("utf-8"))
                python = self.paths["runtime"] / "bin" / "python"
                if not self.ready(python):
                    self.events.put(("status", "Đang tạo môi trường Python riêng…"))
                    if self.command([sys.executable, "-I", "-m", "venv", str(self.paths["runtime"])]) != 0:
                        raise RuntimeError("Chưa tạo được môi trường Python. Hãy kiểm tra bản Python đã cài.")
                    self.events.put(("status", "Đang tải thư viện bảo mật. Lần đầu cần Internet…"))
                    arguments = [str(python), "-I", "-m", "pip", "install", "--only-binary=:all:",
                                 "--index-url", "https://pypi.org/simple", "--disable-pip-version-check",
                                 "--no-input", "--timeout", "20", "--retries", "1",
                                 "cryptography>=46,<51", "certifi"]
                    if self.command(arguments) != 0:
                        raise RuntimeError("Chưa tải được thư viện. Kiểm tra Internet rồi mở DriveDrop lại.")
                    if not self.ready(python):
                        raise RuntimeError("Môi trường chưa đủ Python, Tk hoặc chứng chỉ HTTPS. Xem nhật ký để kiểm tra.")
                if self.cancel.is_set():
                    raise Cancelled()
                runner = self.resources / "start_employee.py"
                payload = self.resources / "payload" / "drivedrop" / "client.py"
                if not runner.is_file() or not payload.is_file():
                    raise RuntimeError("Gói ứng dụng thiếu file. Hãy giải nén lại toàn bộ gói sếp gửi.")
                self.events.put(("status", "Đang mở DriveDrop Employee…"))
                result = self.command([str(python), "-I", str(runner)], app=True)
                if result != 0:
                    raise RuntimeError("DriveDrop chưa mở được. Bấm Mở nhật ký và gửi sếp dòng lỗi cuối.")
                self.events.put(("done", ""))
        except Cancelled:
            self.events.put(("cancelled", "Đã dừng cài đặt. Có thể mở lại ứng dụng để thử tiếp."))
        except Exception as exc:
            # Fixed RuntimeError messages above contain no user data or credentials.
            message = str(exc) if isinstance(exc, RuntimeError) else "Chưa hoàn tất thao tác. Kiểm tra quyền thư mục và mở nhật ký."
            self.events.put(("error", message))


def main():
    if sys.platform != "darwin" or sys.version_info < (3, 12):
        print("DriveDrop launcher cần macOS và Python 3.12 trở lên.", file=sys.stderr)
        return 1
    import tkinter as tk
    from tkinter import ttk, messagebox
    window = tk.Tk()
    window.title("DriveDrop Employee")
    window.geometry("540x240")
    window.resizable(False, False)
    frame = ttk.Frame(window, padding=22)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="DriveDrop Employee", font=("Helvetica", 18, "bold")).pack(anchor="w")
    status = tk.StringVar(value="Đang kiểm tra môi trường trên Mac…")
    ttk.Label(frame, textvariable=status, wraplength=490).pack(anchor="w", pady=(15, 12))
    progress = ttk.Progressbar(frame, mode="indeterminate")
    progress.pack(fill="x", pady=(0, 14))
    row = ttk.Frame(frame)
    row.pack(fill="x")
    events = queue.Queue()
    paths = paths_for(Path.home())
    worker = None
    finished = False

    def open_log():
        try:
            reject_links(paths["log"])
            if paths["log"].is_file():
                subprocess.Popen(["/usr/bin/open", "-t", str(paths["log"])], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                messagebox.showinfo("DriveDrop", "Chưa có nhật ký cho lần chạy này.", parent=window)
        except Exception:
            messagebox.showerror("DriveDrop", "Không mở được nhật ký ở đường dẫn an toàn.", parent=window)

    def close():
        nonlocal finished
        if worker is not None and not finished:
            worker.request_cancel()
            status.set("Đang dừng thao tác hiện tại…")
            cancel_button.configure(state="disabled")
        else:
            window.destroy()

    cancel_button = ttk.Button(row, text="Hủy", command=close)
    cancel_button.pack(side="right")
    log_button = ttk.Button(row, text="Mở nhật ký", command=open_log)
    log_button.pack(side="left")
    window.protocol("WM_DELETE_WINDOW", close)

    def pump():
        nonlocal finished
        while True:
            try:
                event, message = events.get_nowait()
            except queue.Empty:
                break
            if event == "status":
                status.set(message)
            elif event == "app_started":
                window.withdraw()
            elif event == "done":
                finished = True
                window.destroy()
                return
            elif event in ("error", "cancelled"):
                finished = True
                progress.stop()
                window.deiconify()
                window.lift()
                status.set(message)
                cancel_button.configure(text="Đóng", state="normal")
        window.after(100, pump)

    try:
        private_dir(paths["root"])
        with launch_lock(paths["lock"]) as acquired:
            if not acquired:
                messagebox.showinfo("DriveDrop", "DriveDrop đang mở hoặc đang chuẩn bị. Hãy chuyển sang cửa sổ đang chạy.", parent=window)
                window.destroy()
                return 0
            worker = SetupWorker(paths, Path(__file__).resolve().parent, events)
            thread = threading.Thread(target=worker.run, daemon=True)
            thread.start()
            progress.start(12)
            pump()
            window.mainloop()
            if thread.is_alive():
                worker.request_cancel()
                thread.join(timeout=12)
            return 0
    except Exception:
        progress.stop()
        messagebox.showerror("DriveDrop", "Không mở được thư mục dữ liệu an toàn. Kiểm tra quyền tài khoản Mac rồi thử lại.", parent=window)
        window.destroy()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
