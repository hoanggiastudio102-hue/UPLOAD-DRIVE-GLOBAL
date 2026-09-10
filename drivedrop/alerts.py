"""One persistent current alert per device; acknowledgements never alter worker data."""
import json
import time
import uuid
from .common import ApiError


def sync_alert(broker, device, status, received, revoked=False, now=None):
    # Caller holds broker.lock and commits. Transient scans must not erase a retrying error.
    now = time.time() if now is None else now
    online = received is not None and now - received <= 60
    old = broker.db.execute('SELECT * FROM device_alerts WHERE device=?', (device,)).fetchone()
    state = status.get('state')
    reason = ('offline' if not online else 'error' if state == 'error' or status.get('failed', 0) else
              'stopped' if state == 'stopped' else '')
    if revoked or (not reason and (state == 'waiting' or old and old['kind'] != 'error')):
        broker.db.execute('DELETE FROM device_alerts WHERE device=?', (device,))
        return None
    if not reason:
        return dict(old) if old else None
    message = (status.get('error') or 'Máy báo lỗi xử lý file.') if reason == 'error' else (
        'Máy chưa kết nối hoặc quá 60 giây không gửi trạng thái.' if reason == 'offline' else 'Ứng dụng trên máy đã dừng.')
    queued, failed = status.get('queued', 0), status.get('failed', 0)
    changed = not old or old['kind'] != reason or old['message'] != message
    escalated = old and old['acknowledged'] and (queued > old['ack_queued'] or failed > old['ack_failed'])
    if changed:
        broker.db.execute('INSERT OR REPLACE INTO device_alerts VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (device, uuid.uuid4().hex, reason, message, now, now, 0, 0, 0, queued, failed))
    else:
        broker.db.execute('UPDATE device_alerts SET last_seen=?,queued=?,failed=? WHERE device=?',
            (now if reason == 'offline' else received, queued, failed, device))
        if escalated:
            broker.db.execute('UPDATE device_alerts SET token=?,acknowledged=0 WHERE device=?', (uuid.uuid4().hex, device))
    return dict(broker.db.execute('SELECT * FROM device_alerts WHERE device=?', (device,)).fetchone())


def acknowledge(broker, payload):
    if (not isinstance(payload, dict) or set(payload) != {'id', 'token', 'hidden'}
            or not isinstance(payload['id'], str) or not isinstance(payload['token'], str)
            or type(payload['hidden']) is not bool):
        raise ApiError('Thông tin cảnh báo không hợp lệ.', 400)
    with broker.lock:
        row = broker.db.execute('SELECT d.revoked,s.received,s.payload FROM devices d LEFT JOIN device_status s ON s.device=d.id WHERE d.id=?', (payload['id'],)).fetchone()
        if not row:
            raise ApiError('Không tìm thấy máy.', 404)
        alert = sync_alert(broker, payload['id'], json.loads(row['payload']) if row['payload'] else {}, row['received'], row['revoked'])
        broker.db.commit()
        if not alert or alert['token'] != payload['token']:
            raise ApiError('Cảnh báo đã thay đổi. Làm mới và kiểm tra lại.', 409)
        broker.db.execute('UPDATE device_alerts SET acknowledged=?,ack_queued=queued,ack_failed=failed WHERE device=?',
                          (int(payload['hidden']), payload['id']))
        broker.db.commit()
    return {'ok': True}
