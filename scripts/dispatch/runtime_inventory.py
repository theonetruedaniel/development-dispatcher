"""Content and metadata receipts for controller-selected runtime mounts.

An inventory detects drift; it does not grant authority or qualify a sandbox.
The same-user host administrator remains outside the worker threat boundary.
"""
import hashlib
import os
from pathlib import Path
import stat


def _identity(info):
    # Python 3.13 Windows lstat/fstat disagree on creation versus change time.
    # Linux runtime qualification retains ctime; Windows still checks file ID,
    # mode, length, mtime and content rather than rejecting an unchanged file.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns if os.name != 'nt' else None)


def _check_stop(stop):
    if stop is not None and stop.is_set():
        raise InterruptedError('Runtime verification cancelled')


def _record(path, stop=None):
    _check_stop(stop)
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Linked runtime path is not admitted')
    record = {'mode': stat.S_IMODE(before.st_mode),
              'uid': getattr(before, 'st_uid', 0), 'gid': getattr(before, 'st_gid', 0)}
    if stat.S_ISDIR(before.st_mode):
        return dict(record, type='directory')
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('Runtime contains a nonordinary file')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if _identity(opened) != _identity(before):
            raise ValueError('Runtime changed while opening')
        hasher = hashlib.sha256()
        while True:
            _check_stop(stop)
            chunk = stream.read(1024*1024)
            if not chunk:
                break
            hasher.update(chunk)
        digest = hasher.hexdigest()
        if _identity(os.fstat(stream.fileno())) != _identity(opened):
            raise ValueError('Runtime changed while reading')
    if _identity(path.lstat()) != _identity(before):
        raise ValueError('Runtime changed after reading')
    return dict(record, type='file', size=before.st_size, sha256=digest)


def capture_inventory(roots: list[str], *, stop=None) -> dict:
    _check_stop(stop)
    if not isinstance(roots, list) or not roots or any(not isinstance(r, str) for r in roots):
        raise ValueError('Explicit runtime roots required')
    paths = [Path(r) for r in roots]
    for path in paths:
        _check_stop(stop)
        if not path.is_absolute() or path != path.resolve(strict=True):
            raise ValueError('Runtime roots must be canonical absolute paths')
        for ancestor in path.parents:
            if ancestor.is_symlink() or getattr(ancestor.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('Linked runtime ancestor')
    if any(a.is_relative_to(b) for i, a in enumerate(paths) for j, b in enumerate(paths) if i != j):
        raise ValueError('Overlapping runtime roots')
    records = {}
    for root in sorted(paths):
        records[str(root)] = _record(root, stop)
        if root.is_dir():
            def fail(error):
                raise error
            for parent, dirs, files in os.walk(root, followlinks=False, onerror=fail):
                _check_stop(stop)
                for name in sorted(dirs + files):
                    child = Path(parent) / name
                    records[str(child)] = _record(child, stop)
    return {'schema': 1, 'roots': sorted(str(p) for p in paths), 'entries': records}


def verify_inventory(expected: dict, *, stop=None) -> bool:
    if (not isinstance(expected, dict) or set(expected) != {'schema', 'roots', 'entries'}
            or type(expected['schema']) is not int or expected['schema'] != 1
            or not isinstance(expected['entries'], dict)):
        raise ValueError('Invalid runtime inventory')
    try:
        observed = capture_inventory(expected['roots'], stop=stop)
    except InterruptedError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError('Runtime changed or unavailable') from exc
    if observed != expected:
        raise ValueError('Runtime changed; requalification required')
    return True
