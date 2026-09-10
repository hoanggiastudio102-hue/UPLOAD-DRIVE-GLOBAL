"""Employee assignments and upload aggregates computed from server records."""
from datetime import datetime, timedelta, timezone
import re
import time
from .common import ApiError
from .inventory import FIELDS

REPORT_TZ = timezone(timedelta(hours=7))

def save_profile(broker, payload):
    if not isinstance(payload, dict) or set(payload) != {'id','machine_name','employee_code','employee_name'}:
        raise ApiError('Thông tin nhân viên không hợp lệ.', 400)
    values = {}
    for key, limit in (('id',80),('machine_name',60),('employee_code',40),('employee_name',100)):
        value = payload[key]
        if not isinstance(value,str) or len(value)>limit or (value and not value.isprintable()):
            raise ApiError('Tên hoặc mã vượt giới hạn.', 400)
        values[key] = value.strip()
    code = values['employee_code'].upper()
    if not values['machine_name'] or bool(code) != bool(values['employee_name']) or (code and not re.fullmatch(r'[A-Z0-9_-]+',code)):
        raise ApiError('Nhập tên máy; mã nhân viên dùng chữ, số, dấu gạch. Điền cả mã và tên nhân viên hoặc để trống cả hai.',400)
    with broker.lock:
        if not broker.db.execute('SELECT 1 FROM devices WHERE id=?',(values['id'],)).fetchone():
            raise ApiError('Không tìm thấy máy.',404)
        with broker.db:
            broker.db.execute('UPDATE devices SET name=? WHERE id=?',(values['machine_name'],values['id']))
            if code:
                broker.db.execute('INSERT INTO employees VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name',(code,values['employee_name']))
                broker.db.execute('INSERT OR REPLACE INTO device_profiles VALUES(?,?)',(values['id'],code))
            else:
                broker.db.execute('DELETE FROM device_profiles WHERE device=?',(values['id'],))
    return {'ok':True}

def add_reports(broker, devices):
    now = time.time()
    today = datetime.fromtimestamp(now, REPORT_TZ).replace(hour=0,minute=0,second=0,microsecond=0)
    start = (today-timedelta(days=6)).timestamp()
    trend = [{'date':(today-timedelta(days=6-i)).strftime('%d/%m'),'files':0,'bytes':0} for i in range(7)]
    with broker.lock:
        profiles = {r['device']:dict(r) for r in broker.db.execute('SELECT p.device,e.id employee_code,e.name employee_name FROM device_profiles p JOIN employees e ON e.id=p.employee_id')}
        totals = {r['device']:dict(r) for r in broker.db.execute('''SELECT u.device,
            sum(CASE WHEN v.verified_at>=? THEN 1 ELSE 0 END) today_files,
            sum(CASE WHEN v.verified_at>=? THEN u.size ELSE 0 END) today_bytes,
            sum(CASE WHEN v.verified_at>=? AND r.media_kind='ANH' THEN 1 ELSE 0 END) today_images,
            sum(CASE WHEN v.verified_at>=? AND r.media_kind='VIDEO' THEN 1 ELSE 0 END) today_videos,
            max(v.verified_at) last_verified
            FROM uploads u LEFT JOIN upload_verified v ON v.upload_id=u.id
            LEFT JOIN upload_routes r ON r.upload_id=u.id WHERE u.state='verified' GROUP BY u.device''', (today.timestamp(),)*4)}
        history = broker.db.execute('''SELECT CAST((v.verified_at-?)/86400 AS INTEGER) day_index,count(*) files,sum(u.size) bytes
            FROM upload_verified v JOIN uploads u ON u.id=v.upload_id JOIN devices d ON d.id=u.device
            WHERE v.verified_at>=? AND v.verified_at<? AND d.revoked=0 AND u.state='verified' GROUP BY day_index''', (start,start,now+1)).fetchall()
    for row in history:
        if 0<=row['day_index']<7:
            trend[row['day_index']].update(files=row['files'], bytes=row['bytes'])
    for device in devices:
        device.update(profiles.get(device['id'], {'employee_code':'','employee_name':''}))
        device.update(totals.get(device['id'], dict(today_files=0,today_bytes=0,today_images=0,today_videos=0,last_verified=None)))
        inventory = device['status'].get('inventory')
        device['inventory_totals'] = {key:sum(row[key] for row in inventory['folders']) for key in FIELDS} if inventory else None
        device['inventory_fresh'] = bool(inventory and device['online'] and 0 <= now-inventory['captured'] <= 180)
    active = [d for d in devices if not d['revoked']]
    snapshots = [d for d in active if d['inventory_totals'] is not None]
    summary = dict(employees=len({d['employee_code'] for d in active if d['employee_code']}),
        devices=len(active), unassigned=sum(not d['employee_code'] for d in active),
        online=sum(d['online'] for d in active),
        attention=sum(not d['online'] or d['status'].get('state') in ('error','stopped') or d['status'].get('failed',0)>0 for d in active),
        today_files=sum(d['today_files'] for d in active),today_bytes=sum(d['today_bytes'] for d in active),
        today_images=sum(d['today_images'] for d in active),today_videos=sum(d['today_videos'] for d in active),
        inventory_machines=len(snapshots),inventory_fresh=sum(d['inventory_fresh'] for d in snapshots),
        inventory_partial=sum(not d['status']['inventory']['complete'] for d in snapshots),
        folders=sum(len(d['status']['inventory']['folders'])-1 for d in snapshots),
        **{key:sum(d['inventory_totals'][key] for d in snapshots) for key in FIELDS})
    return {'summary':summary,'trend':trend,'timezone':'Asia/Ho_Chi_Minh','generated_at':now}
