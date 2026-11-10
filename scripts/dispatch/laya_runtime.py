"""Integrity-checked, read-only classifier launch profiles; not selector admission."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

from .runtime_inventory import verify_inventory, _identity
from .wsl_supervisor import BWRAP_SHA256, PYTHON_SHA256

PROFILE_LIMIT = 16*1024*1024
SPECIAL_TARGETS = {
    '/runtime/bin/python': 'file', '/runtime/pyvenv.cfg': 'file',
    '/runtime/lib/python3.14/site-packages': 'directory',
    '/checkpoint': 'directory', '/worker.py': 'file'}


def _path(value):
    return (isinstance(value, str) and value.startswith('/')
            and str(PurePosixPath(value)) == value and '..' not in value.split('/')
            and not any(c in value for c in ('\0','\\','\n','\r')))


def validate_profile(profile):
    if (not isinstance(profile, dict) or
            set(profile) != {'schema','checkpoint_revision','mounts','inventory'} or
            type(profile['schema']) is not int or profile['schema'] != 1 or
            not isinstance(profile['checkpoint_revision'], str) or
            not re.fullmatch('[0-9a-f]{40}', profile['checkpoint_revision']) or
            not isinstance(profile['mounts'], list) or not 1 <= len(profile['mounts']) <= 4096):
        raise ValueError('Invalid classifier runtime profile')
    inventory = profile['inventory']
    if (not isinstance(inventory, dict) or set(inventory) != {'schema','roots','entries'} or
            type(inventory['schema']) is not int or inventory['schema'] != 1 or
            not isinstance(inventory['roots'], list) or not isinstance(inventory['entries'], dict)):
        raise ValueError('Invalid classifier inventory')
    entries, targets, sources = inventory['entries'], set(), set()
    target_paths, target_ancestors = set(), set()
    for mount in profile['mounts']:
        if not isinstance(mount, dict) or set(mount) != {'source','target'}:
            raise ValueError('Invalid classifier mount')
        source, target = mount['source'], mount['target']
        if (not _path(source) or not _path(target) or target in targets or
                not isinstance(entries.get(source), dict)):
            raise ValueError('Redirected, duplicate or uninventoried mount')
        kind = entries[source].get('type')
        if target in SPECIAL_TARGETS:
            if kind != SPECIAL_TARGETS[target]:
                raise ValueError('Wrong classifier component type')
        elif (kind != 'file' or
              not target.startswith(('/usr/bin/','/usr/lib/','/lib64/'))):
            raise ValueError('System runtime mounts must be individual files')
        target_path = PurePosixPath(target)
        if target_path in target_ancestors or any(parent in target_paths for parent in target_path.parents):
            raise ValueError('Overlapping classifier mount targets')
        target_paths.add(target_path)
        target_ancestors.update(target_path.parents)
        targets.add(target)
        sources.add(source)
    if not set(SPECIAL_TARGETS) <= targets or inventory['roots'] != sorted(sources):
        raise ValueError('Incomplete classifier profile')
    source_paths = {PurePosixPath(source) for source in sources}
    if any(parent in source_paths for path in source_paths for parent in path.parents):
        raise ValueError('Overlapping runtime sources')
    for entry in entries:
        path = PurePosixPath(entry) if isinstance(entry, str) else None
        if not _path(entry) or not (path in source_paths or any(parent in source_paths for parent in path.parents)):
            raise ValueError('Inventory contains unmounted data')
    return profile


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate profile key')
        value[key] = item
    return value


def load_profile(path, expected_sha256, *, stop=None):
    path = Path(path)
    if (not isinstance(expected_sha256, str) or not re.fullmatch('[0-9a-f]{64}', expected_sha256)
            or not path.is_absolute() or path != path.resolve(strict=True)):
        raise ValueError('Exact profile path and digest required')
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked classifier profile')
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > PROFILE_LIMIT:
        raise ValueError('Classifier profile exceeds bound or is not regular')
    with path.open('rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError('Classifier profile changed while opening')
        raw = stream.read(PROFILE_LIMIT+1)
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError('Classifier profile changed while reading')
    if (_identity(path.lstat()) != _identity(before) or len(raw) > PROFILE_LIMIT or
            hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise ValueError('Classifier profile changed; requalification required')
    profile = validate_profile(json.loads(raw, object_pairs_hook=_unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite profile value'))))
    if stop is None:
        verify_inventory(profile['inventory'])
    else:
        verify_inventory(profile['inventory'], stop=stop)
    return profile


def verify_host():
    if sys.platform != 'linux' or os.getuid() != 1000:
        raise ValueError('Pinned non-root Linux host required')
    for filename, digest in (('/usr/bin/bwrap', BWRAP_SHA256),
                             (sys.executable, PYTHON_SHA256)):
        with open(filename, 'rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                raise ValueError('Classifier host changed; requalification required')


def sandbox_argv(profile):
    validate_profile(profile)
    args = ['/usr/bin/bwrap','--unshare-all','--die-with-parent','--new-session','--cap-drop','ALL']
    for mount in profile['mounts']:
        args += ['--ro-bind',mount['source'],mount['target']]
    for target, link in (('usr/bin','/bin'),('usr/lib','/lib'),
                         ('python3.14','/usr/bin/python3'),('bash','/usr/bin/sh')):
        args += ['--symlink',target,link]
    args += ['--proc','/proc','--dev','/dev','--tmpfs','/tmp','--chdir','/tmp','--clearenv']
    environment = {'HOME':'/tmp','PATH':'/runtime/bin:/usr/bin',
        'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1',
        'TOKENIZERS_PARALLELISM':'false','OMP_NUM_THREADS':'2','MKL_NUM_THREADS':'2',
        'PYTHONDONTWRITEBYTECODE':'1'}
    for name, value in environment.items():
        args += ['--setenv',name,value]
    return args + ['--remount-ro','/','/runtime/bin/python','-I','/worker.py',
                   '--checkpoint','/checkpoint','--revision',profile['checkpoint_revision']]
