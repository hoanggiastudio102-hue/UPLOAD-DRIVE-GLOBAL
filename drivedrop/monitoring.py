"""Bounded authenticated worker status; no credentials or upload URLs."""
import json
import time
from .common import ApiError
from .alerts import sync_alert

STATES = {"starting", "scanning", "hashing", "uploading", "verifying", "waiting", "error", "stopped"}
TEXT = {"state": 20, "file": 500, "destination": 1000, "error": 300, "version": 30}
COUNTS = {"sent", "size", "queued", "verified", "failed"}

def validate_status(payload):
    if not isinstance(payload, dict) or set(payload) - (set(TEXT) | COUNTS | {'inventory'}):
        raise ApiError("Trạng thái thiết bị không hợp lệ.", 400)
    if payload.get("state") not in STATES:
        raise ApiError("Trạng thái thiết bị không hợp lệ.", 400)
    result = {}
    for name, limit in TEXT.items():
        value = payload.get(name, "")
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
            raise ApiError("Nội dung trạng thái vượt giới hạn.", 400)
        result[name] = value
    for name in COUNTS:
        value = payload.get(name, 0)
        if type(value) is not int or not 0 <= value <= 10**15:
            raise ApiError("Số liệu trạng thái không hợp lệ.", 400)
        result[name] = value
    if result["sent"] > result["size"]:
        raise ApiError("Tiến độ không hợp lệ.", 400)
    if 'inventory' in payload:
        from .inventory import validate_inventory
        result['inventory'] = validate_inventory(payload['inventory'])
    return result

def save_status(broker, device, payload):
    payload = validate_status(payload)
    with broker.lock:
        broker.db.execute("INSERT OR REPLACE INTO device_status VALUES(?,?,?)",
                          (device, time.time(), json.dumps(payload, ensure_ascii=False)))
        row = broker.db.execute('SELECT revoked FROM devices WHERE id=?', (device,)).fetchone()
        sync_alert(broker, device, payload, time.time(), bool(row and row['revoked']))
        broker.db.commit()
    return {"ok": True}

def list_status(broker):
    with broker.lock:
        rows = broker.db.execute("""SELECT d.id,d.name,d.revoked,s.received,s.payload,
            (SELECT count(*) FROM uploads u WHERE u.device=d.id AND u.state='verified') confirmed
            FROM devices d LEFT JOIN device_status s ON s.device=d.id ORDER BY d.created DESC""").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["status"] = json.loads(item.pop("payload")) if item["payload"] else {}
            item["online"] = item["received"] is not None and time.time() - item["received"] <= 60
            item['alert'] = sync_alert(broker, item['id'], item['status'], item['received'], item['revoked'])
            result.append(item)
        broker.db.commit()
    return result

def list_articles(broker):
    with broker.lock:
        return [dict(r) for r in broker.db.execute("""SELECT d.name,
            substr(r.destination,1,length(r.destination)-length(r.remote_name)-1) folder,
            count(*) issued, sum(u.state='verified') verified,
            sum(u.state='verified' AND r.media_kind='ANH') images,
            sum(u.state='verified' AND r.media_kind='VIDEO') videos,
            sum(CASE WHEN u.state='verified' THEN u.size ELSE 0 END) bytes, max(u.created) recent
            FROM uploads u JOIN upload_routes r ON r.upload_id=u.id JOIN devices d ON d.id=u.device
            WHERE d.revoked=0 GROUP BY d.id,folder ORDER BY recent DESC LIMIT 100""")]

def show_monitor(root, broker):
    import tkinter as tk
    from tkinter import ttk
    window = tk.Toplevel(root)
    window.title("DriveDrop — Theo dõi nhân viên")
    window.geometry("1150x550")
    ttk.Label(window, text="Tự làm mới 5 giây • Mac báo 15 giây/lần • Quá 60 giây không báo: mất liên lạc",
              padding=10).pack(anchor="w")
    columns = ("name", "state", "seen", "file", "progress", "queue", "verified")
    tree = ttk.Treeview(window, columns=columns, show="headings", height=6)
    for key, label, width in zip(columns, ("Máy", "Trạng thái", "Báo gần nhất", "File / bài", "Tiến độ file", "Đang chờ", "Đã xác minh"),
                                  (110, 180, 110, 330, 110, 90, 110)):
        tree.heading(key, text=label)
        tree.column(key, width=width)
    tree.pack(fill="both", expand=True, padx=10)
    detail = tk.StringVar(value="Chọn một máy để xem đích Drive và lỗi gần nhất.")
    ttk.Label(window, textvariable=detail, wraplength=1100, padding=10).pack(fill="x")
    ttk.Label(window, text="100 bài/đích gần nhất — số đã cấp phiên không bao gồm file Mac chưa gửi lên", padding=8).pack(anchor="w")
    articles = ttk.Treeview(window, columns=("device","folder","issued","verified"), show="headings", height=6)
    for key, label, width in (("device","Máy",120),("folder","Thư mục bài trên Drive",680),
                              ("issued","Đã cấp phiên",130),("verified","Đã xác minh",130)):
        articles.heading(key,text=label)
        articles.column(key,width=width)
    articles.pack(fill="both",expand=True,padx=10,pady=(0,10))
    labels = dict(starting="Đang mở", scanning="Đang quét", hashing="Đang kiểm tra file", uploading="Đang upload",
                  verifying="Đang xác minh", waiting="Đang theo dõi", error="Có lỗi", stopped="Đã dừng")
    cache = {}
    def selected(event=None):
        ids = tree.selection()
        if ids and ids[0] in cache:
            state = cache[ids[0]]["status"]
            detail.set("Đích: " + (state.get("destination") or "Chưa có") + "\nLỗi: " + (state.get("error") or "Không")
                       + "\nPhiên bản nhân viên: " + (state.get("version") or "Chưa gửi trạng thái"))
    tree.bind("<<TreeviewSelect>>", selected)
    def refresh():
        if not window.winfo_exists():
            return
        cache.clear()
        for row in list_status(broker):
            if row["revoked"]:
                continue
            cache[row["id"]] = row
            s = row["status"]
            state = labels.get(s.get("state"), "Cần bản Mac chạy nền")
            if row["received"] and not row["online"]:
                state = "Mất liên lạc >60 giây"
            progress = f'{s.get("sent",0)/s["size"]:.0%}' if s.get("size") else "—"
            values = (row["name"], state, time.strftime("%H:%M:%S", time.localtime(row["received"])) if row["received"] else "Chưa có",
                      s.get("file", ""), progress, s.get("queued", "—"), row["confirmed"])
            if tree.exists(row["id"]):
                tree.item(row["id"], values=values)
            else:
                tree.insert("", "end", iid=row["id"], values=values)
        for item in tree.get_children():
            if item not in {r["id"] for r in cache.values() if not r["revoked"]}:
                tree.delete(item)
        selected()
        for item in articles.get_children():
            articles.delete(item)
        for article in list_articles(broker):
            articles.insert("","end",values=(article["name"],article["folder"],article["issued"],article["verified"]))
        window.after(5000, refresh)
    refresh()
    return window
