"""Boss desktop controller, authenticated web administration, and CLI."""
import argparse
import base64
import json
import queue
import socket
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .broker import Broker
from .common import ApiError, atomic_json, load_json, pinned_request


def default_data_dir():
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    return base / "data-boss"


def addresses():
    try:
        found = sorted(set(socket.gethostbyname_ex(socket.gethostname())[2]))
        # UDP connect only selects the outgoing route; it sends no packet.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 443))
            preferred = sock.getsockname()[0]
        return [preferred] + [v for v in found if v != preferred]
    except OSError:
        return []


def diagnostics(broker, log=print):
    if not broker.server:
        raise ApiError("Hãy bật máy chủ trước.", 400)
    log("Đang kiểm tra mã hóa và giải mã trong kho bảo mật hệ điều hành...")
    key = "diagnostic-" + uuid.uuid4().hex
    value = uuid.uuid4().hex
    try:
        broker.store.set(key, value)
        if broker.store.get(key) != value:
            raise ApiError("Kho bảo mật chưa đọc lại được dữ liệu.", 500)
    finally:
        broker.store.delete(key)
    log("Kho bảo mật hệ điều hành: ĐẠT. Đang kiểm tra HTTPS nội bộ...")
    try:
        result = pinned_request(f"https://127.0.0.1:{broker.server.server_port}", broker.pin, "GET", "/health")
    except ApiError:
        log("Chẩn đoán máy chủ: luồng=" + str(bool(broker.thread and broker.thread.is_alive())) + "; mã lỗi=" + str(broker.last_error))
        raise
    if not result.get("ok"):
        raise ApiError("HTTPS nội bộ chưa hoạt động.", 500)
    log("ĐẠT: HTTPS nội bộ đúng chứng chỉ; kho bảo mật hệ điều hành mã hóa/giải mã thành công.")
    return {"https_local": True, "os_secret_store": True, "google_connected": result.get("google_connected", False)}


def selftest(broker, log=print):
    """REAL Drive round-trip with generated test data. Never uses any user media."""
    from .client import Client
    if not broker.drive.connected():
        raise ApiError("Cần nhập OAuth JSON và đăng nhập Google trước khi thử upload thật.", 400)
    port = broker.start()
    run_id = uuid.uuid4().hex
    root = broker.state_dir / "selftests" / run_id
    watch = root / "sample"
    watch.mkdir(parents=True)
    # A fixed one-pixel PNG; kept locally and on Drive for inspection.
    sample = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGPQyLsEAAIqAWk3JmoSAAAAAElFTkSuQmCC")
    enabled = [row for row in broker.channels.list_channels() if row["enabled"]]
    prefix = enabled[0]["code"] + "_000_" if enabled and broker.route_channels else ""
    name = prefix + "DriveDrop_connection_test_" + run_id[:8] + ".png"
    names = [name]
    expected_destinations = set()
    if broker.route_channels:
        names.append("DriveDrop_khong_ma_" + run_id[:8] + ".png")
        base = enabled[0]["name"] + " - " + enabled[0]["code"] if enabled else ""
        expected_destinations.add((base + "/ANH/" if enabled else "KÊNH/") + name)
        expected_destinations.add("KÊNH/" + names[-1])
        if enabled:
            # Different articles deliberately contain the same image filename.
            article1 = enabled[0]["code"] + "_000" + str(int(run_id[:8], 16))
            article2 = enabled[0]["code"] + "_000" + str(int(run_id[:8], 16) + 1)
            albums = [article1 + "/anh_001.png", article1 + "/anh_002.png",
                      article1 + "/canh_phu/anh_001.png", article2 + "/anh_001.png"]
            names.extend(albums)
            expected_destinations.update(base + "/ANH/" + relative for relative in albums)
    for filename in names:
        (watch / filename).parent.mkdir(parents=True, exist_ok=True)
        (watch / filename).write_bytes(sample)
    client = Client(root / "client", log=log)
    device_id = None
    try:
        result = client.enroll(broker.create_enrollment(f"https://127.0.0.1:{port}", "SELFTEST"), "SELFTEST")
        device_id = result["device_id"]
        client.configure(watch, delete_after_verify=False, poll_seconds=1, stable_seconds=0)
        log("Đang thử ảnh lẻ, ảnh không mã và hai thư mục bài có ảnh trùng tên...")
        client.run_once()
        client.run_once()
        rows = [r for r in broker.list_uploads() if r["device"] == device_id and r["state"] == "verified"]
        if len(rows) != len(names) or any((watch / filename).read_bytes() != sample for filename in names):
            raise ApiError("Chưa xác minh được upload thật. Giữ lại file thử để kiểm tra nhật ký.", 409)
        if broker.route_channels:
            destinations = {row["destination"] for row in rows}
            if destinations != expected_destinations:
                raise ApiError("File đã giữ lại nhưng nơi phân kênh chưa đúng như dự kiến.", 409)
            for destination in sorted(destinations):
                log("Đã xác minh trên Drive: " + destination)
        log("THÀNH CÔNG: file thử đã lên Google Drive, size + SHA256 + MD5 khớp. Bản local vẫn còn.")
        return {"ok": True, "real_google_upload": True, "local_preserved": True, "test_names": names,
                "channel_routing": broker.route_channels, "album_routing": bool(enabled and broker.route_channels)}
    finally:
        if device_id:
            broker.revoke_device(device_id)
        client.close()


def gui(data_dir, background_mode=False):
    instance = None
    if sys.platform == "win32":
        from .desktop import Instance
        instance = Instance(Path(data_dir))
        if not instance.primary:
            instance.close()
            return
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog
    from tkinter.scrolledtext import ScrolledText
    root = tk.Tk()
    root.title("DriveDrop — Máy chủ của sếp")
    root.geometry("980x820")
    root.minsize(820, 650)
    root.configure(bg="#f1f5f9")
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TButton", padding=8, font=("Segoe UI", 10))
    style.configure("TLabel", background="#f1f5f9", font=("Segoe UI", 10))
    style.configure("TFrame", background="#f1f5f9")
    frame = ttk.Frame(root, padding=22)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="DriveDrop  ·  Máy chủ", font=("Segoe UI", 24, "bold")).pack(anchor="w")
    ttk.Label(frame, text="Ảnh/video: máy nhân viên → Google Drive. Máy này chỉ cấp quyền và xác minh.", wraplength=850).pack(anchor="w", pady=(6, 16))
    broker = Broker(data_dir)
    from .admin import WebAdmin
    web = WebAdmin(broker, operations={"selftest": lambda log: selftest(broker, log),
                                        "diagnostics": lambda log: diagnostics(broker, log)})
    messages = queue.Queue()
    ui_actions = queue.Queue()
    tray_icon = None
    busy = threading.Event()
    settings_file = Path(data_dir) / "settings.json"
    settings = load_json(settings_file, {})
    url_var = tk.StringVar(value=settings.get("server_url", "https://" + (next((v for v in addresses() if not v.startswith("127.")), "127.0.0.1")) + ":48765"))
    email_var = tk.StringVar(value=settings.get("google_email", ""))
    status = tk.StringVar(value="Máy chủ chưa chạy. Cấu hình Google ở bước 1.")
    ttk.Label(frame, textvariable=status, foreground="#1d4ed8").pack(anchor="w", pady=(0, 10))
    setup = ttk.LabelFrame(frame, text="1. Kết nối Google trên máy sếp", padding=12)
    setup.pack(fill="x", pady=5)
    email_row = ttk.Frame(setup)
    email_row.pack(fill="x", pady=(0, 8))
    ttk.Label(email_row, text="Gmail tổng:").pack(side="left", padx=(0, 8))
    ttk.Entry(email_row, textvariable=email_var, width=45).pack(side="left")
    buttons = ttk.Frame(setup)
    buttons.pack(fill="x")

    def log(message):
        messages.put(str(message))

    def background(action):
        if busy.is_set():
            messagebox.showinfo("Đang xử lý", "Đợi thao tác hiện tại hoàn tất.", parent=root)
            return
        if not broker.operation_lock.acquire(blocking=False):
            log("Đang có tác vụ quản trị chạy trên web. Đợi tác vụ hoàn tất.")
            return
        busy.set()
        def run():
            try:
                action()
            except ApiError as exc:
                log(str(exc))
            except Exception:
                log("Thao tác chưa hoàn tất. Kiểm tra kết nối, file cấu hình và quyền hệ điều hành.")
            finally:
                busy.clear()
                broker.operation_lock.release()
        threading.Thread(target=run, daemon=True).start()

    def import_client():
        filename = filedialog.askopenfilename(title="OAuth Client loại Desktop app", filetypes=[("Google OAuth JSON", "*.json")])
        if filename:
            def action():
                broker.drive.import_client(filename)
                log("Đã lưu OAuth Client bằng kho bảo mật Windows/macOS. Tiếp theo: Đăng nhập Google.")
            background(action)

    def login():
        email = email_var.get().strip()
        if not email or "@" not in email:
            messagebox.showinfo("Chọn tài khoản", "Nhập Gmail tổng cần kết nối để tránh chọn nhầm tài khoản.", parent=root)
            return
        def action():
            broker.drive.authorize(on_message=log, expected_email=email)
            atomic_json(settings_file, {"server_url": url_var.get().strip(), "google_email": email})
        background(action)

    ttk.Button(buttons, text="Mở Google Cloud", command=lambda: webbrowser.open("https://console.cloud.google.com/apis/dashboard")).pack(side="left", padx=(0, 6))
    ttk.Button(buttons, text="Nhập OAuth JSON", command=import_client).pack(side="left", padx=6)
    ttk.Button(buttons, text="Đăng nhập Google", command=login).pack(side="left", padx=6)
    ttk.Button(buttons, text="Thử upload thật", command=lambda: background(lambda: selftest(broker, log))).pack(side="left", padx=6)
    ttk.Label(setup, text="Tài khoản Google One: dùng OAuth Desktop + quyền drive.file. Xem README_VI để tạo cấu hình.", wraplength=830).pack(anchor="w", pady=(8, 0))
    connection = ttk.LabelFrame(frame, text="2. Địa chỉ máy chủ cho nhân viên", padding=12)
    connection.pack(fill="x", pady=5)
    ttk.Entry(connection, textvariable=url_var, width=68).pack(side="left", fill="x", expand=True)

    def start():
        try:
            target = urlsplit(url_var.get().strip())
            if target.scheme != "https" or not target.hostname or target.username or target.password or target.path not in ("", "/") or target.query or target.fragment:
                raise ValueError("Invalid server URL")
            requested_port = target.port or 443
            if broker.server and broker.server.server_port != requested_port:
                raise ValueError("Stop app before changing port")
            actual_port = broker.start(port=requested_port)
            atomic_json(settings_file, {"server_url": url_var.get().strip(), "google_email": email_var.get().strip()})
            status.set(f"Đang nhận kết nối HTTPS cổng {actual_port}. " + ("Đã có quyền Google." if broker.drive.connected() else "Chưa kết nối Google."))
            log("Máy chủ đã chạy. LAN dùng IP nội bộ; mạng ngoài cần đường kết nối VPN tới máy này.")
        except Exception:
            log("Không mở được cổng. Kiểm tra địa chỉ HTTPS và chương trình khác đang dùng cổng; khởi động lại app nếu đổi cổng.")

    ttk.Button(connection, text="Bật máy chủ", command=start).pack(side="left", padx=(8, 0))
    controls = ttk.Frame(frame)
    controls.pack(fill="x", pady=8)

    def export_enrollment():
        if not broker.server:
            start()
        if not broker.server:
            return
        name = simpledialog.askstring("Tên thiết bị", "Tên nhân viên / tên máy:", parent=root)
        if not name:
            return
        filename = filedialog.asksaveasfilename(title="Lưu file kích hoạt dùng một lần", defaultextension=".json", initialfile="DriveDrop-kich-hoat.json", filetypes=[("JSON", "*.json")])
        if filename:
            try:
                config = broker.create_enrollment(url_var.get().strip(), name)
                atomic_json(Path(filename), config)
                log("Đã tạo file kích hoạt, dùng một lần trong 15 phút. Gửi riêng cho đúng nhân viên.")
            except ApiError as exc:
                log(str(exc))

    ttk.Button(controls, text="Tạo file kích hoạt nhân viên", command=export_enrollment).pack(side="left")
    ttk.Button(controls, text="Kiểm tra HTTPS và kho khóa", command=lambda: background(lambda: diagnostics(broker, log))).pack(side="left", padx=8)

    def regroup_videos():
        article = simpledialog.askstring("Gom video cũ theo bài", "Gom TẤT CẢ video đã xác minh nằm thẳng trong VIDEO của kênh.\nNhập mã bài đích, ví dụ TH9_001:", parent=root)
        if article:
            background(lambda: broker.regroup_verified_videos(article.strip(), log, require_source_match=False))
    ttk.Button(controls, text="Gom video cũ", command=regroup_videos).pack(side="left", padx=4)

    def audit_routing():
        with broker.lock:
            rows = broker.db.execute("""SELECT c.source_folders,r.destination,u.state,u.created
                FROM uploads u JOIN upload_routes r ON r.upload_id=u.id
                LEFT JOIN upload_context c ON c.upload_id=u.id
                ORDER BY u.created DESC LIMIT 2000""").fetchall()
        groups = {}
        for row in rows:
            source = " / ".join(json.loads(row["source_folders"])) if row["source_folders"] else "(không có thư mục nguồn)"
            destination = row["destination"].rsplit("/", 1)[0]
            key = (source, destination, row["state"])
            value = groups.setdefault(key, [0, row["created"], row["created"]])
            value[0] += 1
            value[1] = min(value[1], row["created"])
            value[2] = max(value[2], row["created"])
        log("PHÂN BÀI — nguồn nhân viên → đích Drive (tối đa 2000 file gần nhất):")
        for (source, destination, state), (count, first, last) in groups.items():
            times = time.strftime("%H:%M:%S", time.localtime(first)) + "–" + time.strftime("%H:%M:%S", time.localtime(last))
            log(f"{count} file [{state}, {times}]: {source} → {destination}")

    def manage_channels():
        from .channel_ui import show_channel_manager
        show_channel_manager(root, broker.channels, on_change=lambda: log(
            "Đã lưu danh sách kênh. Upload mới phân theo mã; file chưa khớp vào KÊNH."))

    ttk.Button(controls, text="Quản lý kênh", command=manage_channels).pack(side="left", padx=(0, 8))
    ttk.Button(controls, text="Tạo thư mục Drive", command=lambda: background(
        lambda: broker.sync_channel_folders(log))).pack(side="left")
    ttk.Button(frame, text="Kiểm tra phân bài", command=lambda: background(audit_routing)).pack(anchor="w", pady=(0, 4))
    devices = ttk.Treeview(frame, columns=("name", "status"), show="headings", height=5)
    devices.heading("name", text="Thiết bị đã kích hoạt")
    devices.heading("status", text="Quyền")
    devices.column("name", width=560)
    devices.column("status", width=170)
    devices.pack(fill="x", pady=4)

    def refresh():
        for item in devices.get_children():
            devices.delete(item)
        for row in broker.list_devices():
            devices.insert("", "end", iid=row["id"], values=(row["name"], "Đã khóa" if row["revoked"] else "Đang được cấp quyền"))

    def revoke():
        selected = devices.selection()
        if selected:
            broker.revoke_device(selected[0])
            log("Đã khóa cấp phiên mới cho thiết bị. Phiên Google đã cấp có thể vẫn còn hiệu lực.")
            refresh()

    manage = ttk.Frame(frame)
    manage.pack(fill="x", pady=(0, 8))
    ttk.Button(manage, text="Làm mới danh sách", command=refresh).pack(side="left")
    ttk.Button(manage, text="Khóa máy đã chọn", command=revoke).pack(side="left", padx=8)

    def open_drive_folder():
        if not broker.drive.folder_id:
            log("Chưa có thư mục Drive.")
            return
        email = email_var.get().strip() or load_json(settings_file, {}).get("google_email", "").strip()
        query = "?" + urlencode({"authuser": email}) if email else ""
        webbrowser.open("https://drive.google.com/drive/folders/" + broker.drive.folder_id + query)

    ttk.Button(manage, text="Mở thư mục Drive", command=open_drive_folder).pack(side="left")
    def monitor():
        from .monitoring import show_monitor
        show_monitor(root, broker)
    ttk.Button(manage, text="Theo dõi nhân viên", command=monitor).pack(side="left", padx=8)
    ttk.Button(manage, text="Ẩn xuống khay", command=lambda: root.withdraw() if tray_icon else root.iconify()).pack(side="left")
    ttk.Button(frame, text="Quản trị web · DriveDrop 0.4.0", command=lambda: webbrowser.open(web.setup_url()) if web.local_port else log("Web chưa chạy; kiểm tra cổng 48800 và 48801.")).pack(anchor="w", pady=8)
    logs = ScrolledText(frame, height=8, font=("Consolas", 10), wrap="word", state="disabled")
    logs.pack(fill="both", expand=True)
    ttk.Label(frame, text="Giữ thư mục data-boss riêng tư. Máy chủ cần hoạt động để cấp phiên mới và xác minh trước khi xóa.", wraplength=850).pack(anchor="w", pady=(8, 0))

    def pump():
        if instance and instance.requested():
            root.deiconify()
            root.lift()
        while not ui_actions.empty():
            action = ui_actions.get_nowait()
            if action == "quit":
                shutdown()
                return
            if action == "monitor":
                monitor()
            else:
                root.deiconify()
                root.lift()
        while True:
            try:
                message = messages.get_nowait()
            except queue.Empty:
                break
            logs.configure(state="normal")
            logs.insert("end", time.strftime("%H:%M:%S ") + message + "\n")
            if int(logs.index("end-1c").split(".")[0]) > 1000:
                logs.delete("1.0", "200.0")
            logs.see("end")
            logs.configure(state="disabled")
        root.after(200, pump)

    def shutdown():
        web.close()
        broker.stop()
        if tray_icon:
            tray_icon.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", lambda: root.withdraw() if tray_icon else shutdown())
    if sys.platform == "win32":
        try:
            from .desktop import tray
            tray_icon = tray(ui_actions)
            log("Đóng cửa sổ sẽ ẩn xuống khay. Thoát hẳn: biểu tượng DriveDrop → Thoát và dừng máy chủ.")
        except Exception:
            log("Chưa tạo được biểu tượng khay; giữ cửa sổ mở để máy chủ hoạt động.")
    def ready():
        start()
        try:
            web.start()
            log("Quản trị web: http://127.0.0.1:48801 • Tunnel: HTTPS cổng 48800.")
            if not web.configured:
                webbrowser.open(web.setup_url())
        except Exception:
            log("Chưa bật được web. Kiểm tra cổng 48800/48801; ứng dụng máy chủ vẫn mở.")
        if background_mode and tray_icon and broker.server and web.configured:
            root.withdraw()
    root.after(300, ready)
    refresh()
    pump()
    root.mainloop()
    if instance:
        instance.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="DriveDrop Boss — HTTPS control plane, no media relay")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    subs = parser.add_subparsers(dest="command")
    serve = subs.add_parser("serve")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=48765)
    client = subs.add_parser("import-client")
    client.add_argument("file", type=Path)
    login_parser = subs.add_parser("login")
    login_parser.add_argument("--email", help="Expected Google account email; rejects signing into another account")
    subs.add_parser("status")
    subs.add_parser("selftest")
    subs.add_parser("runtime-check", help="Check packaged GUI/assets without opening Boss data or Google")
    subs.add_parser("supervise")
    subs.add_parser("devices")
    subs.add_parser("channels")
    channels_import = subs.add_parser("import-channels")
    channels_import.add_argument("file", type=Path)
    subs.add_parser("sync-channels")
    enroll = subs.add_parser("enroll")
    enroll.add_argument("--url", required=True)
    enroll.add_argument("--name", default="")
    enroll.add_argument("--output", type=Path, required=True)
    revoke = subs.add_parser("revoke")
    revoke.add_argument("id")
    gui_parser = subs.add_parser("gui")
    gui_parser.add_argument("--background", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "runtime-check":
        import tkinter as tk
        from . import __version__
        args.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            root = tk.Tk()
            root.withdraw()
            root.update_idletasks()
            tk_version = root.tk.eval("info patchlevel")
            root.destroy()
            assets = Path(__file__).parent / "web"
            for name in ("index.html", "app.js", "reports.js", "style.css"):
                if not (assets / name).is_file():
                    raise RuntimeError("Packaged web asset is missing: " + name)
        except Exception as exc:
            atomic_json(args.data_dir / "runtime-check.json", {"ok": False, "version": __version__, "error": str(exc)})
            return 1
        atomic_json(args.data_dir / "runtime-check.json", {"ok": True, "version": __version__, "tkinter": tk_version, "web_assets": True})
        return 0
    if args.command is None and sys.platform == "win32" and getattr(sys, "frozen", False):
        from .desktop import supervise
        return supervise(args.data_dir)
    if args.command in (None, "gui"):
        gui(args.data_dir, getattr(args, "background", False))
        return 0
    if args.command == "supervise":
        from .desktop import supervise
        return supervise(args.data_dir)
    broker = Broker(args.data_dir)
    try:
        if args.command == "import-client":
            broker.drive.import_client(args.file)
            print("OAuth client saved securely. Run login next.")
        elif args.command == "login":
            broker.drive.authorize(on_message=print, expected_email=args.email)
        elif args.command == "status":
            print(json.dumps({"google_configured": broker.drive.connected(), "folder_id": broker.drive.folder_id,
                              "local_addresses": addresses(), "state_dir": str(args.data_dir)}, ensure_ascii=False))
        elif args.command == "devices":
            print(json.dumps(broker.list_devices(), ensure_ascii=False))
        elif args.command == "channels":
            print(json.dumps(broker.channels.list_channels(), ensure_ascii=False))
        elif args.command == "import-channels":
            print(json.dumps(broker.channels.import_text(args.file.read_text(encoding="utf-8-sig")), ensure_ascii=False))
        elif args.command == "sync-channels":
            print(json.dumps(broker.sync_channel_folders(print), ensure_ascii=False))
        elif args.command == "enroll":
            atomic_json(args.output, broker.create_enrollment(args.url, args.name))
            print("Enrollment saved; expires in 15 minutes; distribute privately.")
        elif args.command == "revoke":
            broker.revoke_device(args.id)
            print("Device revoked for new requests.")
        elif args.command == "selftest":
            print(json.dumps(selftest(broker), ensure_ascii=False))
        elif args.command == "serve":
            port = broker.start(args.host, args.port)
            print(f"DriveDrop HTTPS listening on {args.host}:{port}. No media relay.", flush=True)
            while True:
                time.sleep(1)
        return 0
    except ApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        broker.close()


if __name__ == "__main__":
    raise SystemExit(main())
