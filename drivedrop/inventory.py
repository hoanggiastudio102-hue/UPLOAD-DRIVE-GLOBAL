"""Bounded metadata-only inventory of the configured watch folder. Never opens media."""
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import time

IMAGES = {'.jpg','.jpeg','.png','.gif','.webp','.bmp','.tif','.tiff','.heic','.heif','.avif','.dng'}
VIDEOS = {'.mp4','.mov','.m4v','.avi','.mkv','.webm','.mpeg','.mpg','.3gp','.mts','.m2ts'}
FIELDS = ('images', 'videos', 'other', 'bytes', 'queued')
ACTIVE = ('claiming','claimed','uploading','verified','finalizing_delete','finalizing_preserve')
MAX_FOLDERS = 300

def media_kind(name):
    suffix = Path(name).suffix.lower()
    return 'images' if suffix in IMAGES else 'videos' if suffix in VIDEOS else 'other'

def validate_inventory(value):
    from .common import ApiError
    def bad():
        raise ApiError('Báo cáo thư mục không hợp lệ.', 400)
    if not isinstance(value, dict) or set(value) != {'root','captured','complete','skipped','folders'}:
        bad()
    if not isinstance(value['root'], str) or not 1 <= len(value['root']) <= 200 or not value['root'].isprintable():
        bad()
    if type(value['captured']) not in (int,float) or not math.isfinite(value['captured']) or not 0 < value['captured'] <= time.time()+300:
        bad()
    if type(value['complete']) is not bool or type(value['skipped']) is not int or not 0 <= value['skipped'] <= 10**9:
        bad()
    rows = value['folders']
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FOLDERS:
        bad()
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'path', *FIELDS}:
            bad()
        path = row['path']
        if not isinstance(path, str) or not 1 <= len(path) <= 500 or not path.isprintable() or '\\' in path or ':' in path:
            bad()
        if path != '.' and (path.startswith('/') or any(p in ('', '.', '..') for p in path.split('/'))):
            bad()
        if path in seen or (path != '.' and str(PurePosixPath(path).parent) not in seen):
            bad()
        seen.add(path)
        for key in FIELDS:
            if type(row[key]) is not int or not 0 <= row[key] <= 10**15:
                bad()
    if rows[0]['path'] != '.' or len(json.dumps(value, ensure_ascii=False).encode()) > 400000:
        bad()
    return value

def scan_inventory(root, jobs=(), max_entries=100000, max_seconds=2):
    root = Path(root)
    start = time.monotonic()
    result = {'root':root.name[:200] or 'Thư mục', 'captured':time.time(), 'complete':True, 'skipped':0, 'folders':[]}
    mapping = {}
    observed_sources = set()
    path_bytes = 0
    def row_for(path):
        nonlocal path_bytes
        key = path.relative_to(root).as_posix()
        encoded_size = len(key.encode('utf-8', errors='replace'))
        if len(key) > 500 or len(mapping) >= MAX_FOLDERS or path_bytes+encoded_size > 300000 or len(path.relative_to(root).parts) > 32 or not key.isprintable() or ':' in key:
            result['complete'] = False
            result['skipped'] += 1
            return None
        row = {'path':key, **dict.fromkeys(FIELDS, 0)}
        mapping[key] = row
        path_bytes += encoded_size
        result['folders'].append(row)
        return row
    def safe_info(path):
        # Reject symlinks / Windows junctions in every component before metadata access.
        current = path
        while True:
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise OSError('Linked path')
            if current == root:
                break
            current = current.parent
        return path.lstat()
    def count_file(row, path, name=None):
        info = safe_info(path)
        if stat.S_ISREG(info.st_mode):
            row[media_kind(name or path.name)] += 1
            row['bytes'] += info.st_size
    row_for(root)
    stack = [root]
    visited = 0
    while stack:
        directory = stack.pop()
        row = mapping[directory.relative_to(root).as_posix()]
        try:
            safe_info(directory)
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > max_entries or time.monotonic()-start > max_seconds:
                        result['complete'] = False
                        stack.clear()
                        break
                    info = entry.stat(follow_symlinks=False)
                    if entry.name.startswith('.') or getattr(info, 'st_file_attributes', 0) & 2:
                        continue
                    if entry.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                        result['skipped'] += 1
                        continue
                    path = Path(entry.path)
                    if stat.S_ISDIR(info.st_mode):
                        child = row_for(path)
                        if child is not None:
                            stack.append(path)
                    elif stat.S_ISREG(info.st_mode):
                        # Windows DirEntry.stat may omit the file identity. Obtain the
                        # real identity so a concurrent rename into the queue is deduplicated.
                        if not info.st_ino or not info.st_dev:
                            info = path.lstat()
                            if not stat.S_ISREG(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
                                continue
                        observed_sources.add((str(path),info.st_dev,info.st_ino))
                        row[media_kind(entry.name)] += 1
                        row['bytes'] += info.st_size
        except OSError:
            result['complete'] = False
            result['skipped'] += 1
    # Claimed files are temporarily inside the private queue; attribute each to its source folder.
    for job in jobs:
        try:
            source = Path(job['source_path'])
            key = source.parent.relative_to(root).as_posix()
            row = mapping.get(key)
            if row is None:
                result['complete'] = False
                continue
            if job['status'] in ACTIVE:
                row['queued'] += 1
            claimed = Path(job['claimed_path'])
            if claimed.parent.parent != root / '.drivedrop-pending':
                continue
            if claimed.exists():
                info = safe_info(claimed)
                if (str(source),info.st_dev,info.st_ino) not in observed_sources:
                    count_file(row, claimed, job['name'])
        except (OSError, ValueError):
            result['complete'] = False
            result['skipped'] += 1
    # Parents precede descendants for deterministic validation/rendering.
    result['folders'].sort(key=lambda r:(r['path'].count('/')+(r['path']!='.'), r['path'].casefold()))
    return result
