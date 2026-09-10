"""Local channel editor and routing preview. This UI never contacts Google."""
from pathlib import Path

from .common import ApiError
from .channels import MAX_TEXT_BYTES


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif", ".dng"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mpeg", ".mpg", ".3gp", ".mts", ".m2ts"}


def show_channel_manager(parent, catalog, on_change=None):
    """Return a non-modal Toplevel. on_change() is called after a saved edit/import."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    window = tk.Toplevel(parent)
    window.title("DriveDrop — Quản lý kênh")
    window.geometry("980x700")
    window.minsize(780, 610)
    frame = ttk.Frame(window, padding=18)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Quản lý kênh và mã tên file", font=("Segoe UI", 17, "bold")).pack(anchor="w")
    ttk.Label(frame, text="Thay đổi được lưu trên máy sếp. Thư mục Google được xử lý khi có lần upload tiếp theo.", wraplength=920).pack(anchor="w", pady=(6, 12))
    count = tk.StringVar()
    ttk.Label(frame, textvariable=count).pack(anchor="w")
    table_box = ttk.Frame(frame)
    table_box.pack(fill="both", expand=True, pady=(6, 12))
    tree = ttk.Treeview(table_box, columns=("name", "code", "enabled"), show="headings", selectmode="browse", height=12)
    for column, title, width in (("name", "Tên kênh", 410), ("code", "Mã file", 220), ("enabled", "Trạng thái", 140)):
        tree.heading(column, text=title)
        tree.column(column, width=width, minwidth=90, anchor="w")
    scroll = ttk.Scrollbar(table_box, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    name = tk.StringVar()
    code = tk.StringVar()
    selected = {"id": None}
    editor = ttk.Frame(frame)
    editor.pack(fill="x")
    ttk.Label(editor, text="Tên kênh:").grid(row=0, column=0, sticky="w", padx=(0, 6))
    name_entry = ttk.Entry(editor, textvariable=name)
    name_entry.grid(row=0, column=1, sticky="ew", padx=(0, 14))
    ttk.Label(editor, text="Mã:").grid(row=0, column=2, sticky="w", padx=(0, 6))
    ttk.Entry(editor, textvariable=code, width=22).grid(row=0, column=3, sticky="ew")
    editor.columnconfigure(1, weight=1)
    actions = ttk.Frame(frame)
    actions.pack(fill="x", pady=(10, 12))
    notice = tk.StringVar(value="Mã ví dụ: KL1, RM4_9. Dấu gạch dưới cuối mã được bỏ: NA13_ → NA13.")
    ttk.Label(frame, textvariable=notice, wraplength=920).pack(anchor="w", pady=(0, 12))

    def report_error(exc):
        message = str(exc) if isinstance(exc, ApiError) else "Không lưu được thay đổi. Kiểm tra quyền đọc/ghi danh sách kênh."
        messagebox.showerror("Quản lý kênh", message, parent=window)

    def changed():
        if on_change is not None:
            try:
                on_change()
            except Exception:
                notice.set("Đã lưu danh sách; cửa sổ chính chưa cập nhật. Có thể đóng và mở lại mục quản lý kênh.")

    def refresh(select_id=None):
        for item in tree.get_children():
            tree.delete(item)
        rows = catalog.list_channels()
        for row in rows:
            tree.insert("", "end", iid=row["id"], values=(row["name"], row["code"], "Đang bật" if row["enabled"] else "Đã tắt"))
        count.set(f'{len(rows)} kênh · {sum(row["enabled"] for row in rows)} đang bật')
        if select_id and tree.exists(select_id):
            tree.selection_set(select_id)
            tree.see(select_id)
            selected["id"] = select_id
            selection_changed()

    def selection_changed(event=None):
        choice = tree.selection()
        if not choice:
            return
        row = next((row for row in catalog.list_channels() if row["id"] == choice[0]), None)
        if row is None:
            return
        selected["id"] = row["id"]
        name.set(row["name"])
        code.set(row["code"])
        save_button.configure(text="Lưu thay đổi")
        toggle_button.configure(text="Tắt kênh" if row["enabled"] else "Bật kênh", state="normal")

    def new():
        tree.selection_remove(*tree.selection())
        selected["id"] = None
        name.set("")
        code.set("")
        save_button.configure(text="Thêm kênh")
        toggle_button.configure(state="disabled", text="Bật / tắt")
        name_entry.focus_set()

    def save():
        try:
            row = catalog.upsert(name.get(), code.get(), selected["id"])
            refresh(row["id"])
            notice.set("Đã lưu trên máy sếp. Đổi tên/mã giữ nguyên ID kênh; Google xử lý thư mục ở lần upload tiếp theo.")
            changed()
        except Exception as exc:
            report_error(exc)

    def toggle():
        row = next((row for row in catalog.list_channels() if row["id"] == selected["id"]), None)
        if row is None:
            return
        try:
            updated = catalog.set_enabled(row["id"], not row["enabled"])
            refresh(updated["id"])
            notice.set("Đã đổi trạng thái. File không khớp kênh đang bật sẽ vào thư mục chung KÊNH.")
            changed()
        except Exception as exc:
            report_error(exc)

    def import_txt():
        filename = filedialog.askopenfilename(parent=window, title="Nhập danh sách tên kênh và mã", filetypes=[("Danh sách TXT", "*.txt"), ("Tất cả file", "*")])
        if not filename:
            return
        try:
            with open(filename, "rb") as stream:
                raw = stream.read(MAX_TEXT_BYTES + 1)
            if len(raw) > MAX_TEXT_BYTES:
                raise ApiError("File TXT không được vượt quá 1 MiB.", 400)
            try:
                text = raw.decode("utf-16") if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw.decode("utf-8-sig")
            except UnicodeError:
                raise ApiError("Hãy lưu TXT bằng UTF-8 hoặc UTF-16 có BOM rồi nhập lại.", 400) from None
            result = catalog.import_text(text)
            refresh(selected["id"])
            notice.set(f'Đã thêm {result["added"]} kênh, cập nhật {result["updated"]} tên theo mã. Kênh cũ giữ ID và trạng thái.')
            changed()
        except Exception as exc:
            report_error(exc)

    ttk.Button(actions, text="Tạo kênh mới", command=new).pack(side="left")
    save_button = ttk.Button(actions, text="Thêm kênh", command=save)
    save_button.pack(side="left", padx=8)
    toggle_button = ttk.Button(actions, text="Bật / tắt", state="disabled", command=toggle)
    toggle_button.pack(side="left")
    ttk.Button(actions, text="Nhập danh sách TXT", command=import_txt).pack(side="right")
    tree.bind("<<TreeviewSelect>>", selection_changed)

    preview_box = ttk.LabelFrame(frame, text="Xem trước đường dẫn (không kết nối Google)", padding=12)
    preview_box.pack(fill="x")
    filename = tk.StringVar(value="KL1_001.mp4")
    route_text = tk.StringVar(value="Nhập tên video hoặc đường dẫn ảnh, ví dụ KL1_001/anh_001.jpg.")
    entry_row = ttk.Frame(preview_box)
    entry_row.pack(fill="x")
    ttk.Entry(entry_row, textvariable=filename).pack(side="left", fill="x", expand=True, padx=(0, 10))

    def preview():
        candidate = filename.get()
        parts = candidate.replace("\\", "/").split("/")
        suffix = Path(candidate).suffix.lower()
        kind = "ANH" if suffix in IMAGE_SUFFIXES else "VIDEO" if suffix in VIDEO_SUFFIXES else None
        if kind is None:
            route_text.set("Hãy nhập tên có đuôi ảnh/video hỗ trợ, ví dụ KL1_001.jpg hoặc RM4_9_002.mp4.")
            return
        try:
            result = catalog.route_context(parts[-1], kind, parts[:-1])
            if result is None:
                route_text.set(f"DriveDrop Inbox / KÊNH / {parts[-1]}\nMã chưa khớp hoặc kênh đang tắt: dùng thư mục chung.")
            else:
                path = ["DriveDrop Inbox", result["folder_name"], result["media_kind"]]
                if result.get("article"):
                    path.extend([result["article"], *result["subfolders"]])
                route_text.set(" / ".join([*path, result["filename"]]))
        except ApiError as exc:
            route_text.set(str(exc))

    ttk.Button(entry_row, text="Xem đường dẫn", command=preview).pack(side="right")
    ttk.Label(preview_box, textvariable=route_text, wraplength=880).pack(anchor="w", pady=(10, 0))
    refresh()
    return window
