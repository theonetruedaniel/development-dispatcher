"""Bounded Windows transport to the trusted WSL supervisor; not runtime admission."""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time

from .write_contracts import safe_root, root_identity
from .wsl_supervisor import validate_request

MAX_RECEIPT = 6 * 1024 * 1024
STOP_GRACE = 12


def runtime_files():
    folder = Path(__file__).resolve().parent
    return {'supervisor': folder / 'wsl_supervisor.py',
            'inventory_code': folder / 'runtime_inventory.py',
            'registry': folder.parents[1] / 'docs/registries/dispatch-wsl-runtime.json',
            'launcher': Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/wsl.exe'}


def inspect_runtime(expected: dict) -> dict:
    if (not isinstance(expected, dict) or set(expected) != {'distro', 'files'}
            or expected['distro'] != 'Ubuntu' or not isinstance(expected['files'], dict)
            or set(expected['files']) != set(runtime_files())):
        raise ValueError('Exact controller runtime identity required')
    observed = {}
    for name, path in runtime_files().items():
        safe_root(path.parent)
        if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked controller component')
        observed[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if observed != expected['files']:
        raise ValueError('Controller runtime changed; requalification required')
    digest = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
    return {'status':'integrity_checked', 'runtime_digest':digest}


def linux_path(path):
    path = safe_root(Path(path))
    if path.drive.casefold() != 'c:':
        raise ValueError('Only the admitted C drive is supported')
    return '/mnt/c/' + path.as_posix()[3:]


def decode_result(raw: bytes, limit: int) -> dict:
    if len(raw) > MAX_RECEIPT:
        raise ValueError('Supervisor receipt exceeds limit')
    value = json.loads(raw)
    keys = {'status','exit_code','elapsed_seconds','stdout_b64','stderr_b64','termination_observed'}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError('Malformed supervisor receipt')
    if (value['termination_observed'] is not True or type(value['exit_code']) is not int
            or type(value['elapsed_seconds']) not in (int, float)
            or not math.isfinite(value['elapsed_seconds']) or value['elapsed_seconds'] < 0
            or value['status'] not in {'completed','failed','cancelled','timeout',
                                       'output_limit','controller_disconnected','invalid_control'}
            or value['status'] == 'completed' and value['exit_code'] != 0):
        raise ValueError('Unconfirmed supervisor outcome')
    output = {}
    try:
        for key in ('stdout','stderr'):
            output[key] = base64.b64decode(value[key+'_b64'], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('Invalid supervisor output encoding') from exc
    if sum(map(len, output.values())) > limit:
        raise ValueError('Command output exceeds grant')
    return value | {k:v.decode('utf-8', errors='replace') for k,v in output.items()}


def run_command(request: dict, runtime: dict, stop: threading.Event) -> dict:
    if stop.is_set():
        return {'status':'cancelled','termination_observed':True,'launched':False}
    try:
        if os.name != 'nt':
            raise ValueError('Windows controller required')
        checked = validate_request(request)
        identity = inspect_runtime(runtime)
        worker = Path('C:/' + checked['root'][7:])
        if linux_path(worker) != checked['root']:
            raise ValueError('Worker path changed')
        worker_id = root_identity(worker)
        payload = json.dumps(checked).encode() + b'\n'
        if len(payload) > 65536:
            raise ValueError('Request exceeds transport bound')
        supervisor = runtime_files()['supervisor']
        script = linux_path(supervisor.parent) + '/' + supervisor.name
    except (OSError, ValueError, TypeError) as exc:
        return {'status':'blocked','reason':str(exc),'termination_observed':True,'launched':False}
    if stop.is_set():
        return {'status':'cancelled','termination_observed':True,'launched':False}
    try:
        process = subprocess.Popen([str(runtime_files()['launcher']), '-d', 'Ubuntu', '--',
            '/usr/bin/env', '-i', 'PATH=/usr/bin:/bin', '/usr/bin/python3', '-I', script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW, shell=False)
    except OSError as exc:
        return {'status':'blocked','reason':str(exc),'termination_observed':True,'launched':False}
    output = {'stdout':bytearray(), 'stderr':bytearray()}
    overflow = threading.Event()
    def drain(stream, name, cap):
        while True:
            data = stream.read(4096)
            if not data:
                return
            room = cap-len(output[name])
            output[name].extend(data[:room])
            if len(data) > room:
                overflow.set()
    readers = [threading.Thread(target=drain, args=(process.stdout,'stdout',MAX_RECEIPT), daemon=True),
               threading.Thread(target=drain, args=(process.stderr,'stderr',65536), daemon=True)]
    for reader in readers:
        reader.start()
    started = time.monotonic()
    stopping = None
    reason = None
    writes = []
    write_errors = []
    def send(data):
        def write():
            try:
                process.stdin.write(data)
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                write_errors.append(exc)
        writer = threading.Thread(target=write, daemon=True)
        writes.append(writer)
        writer.start()
    stop_sent = False
    try:
        send(payload)
        while process.poll() is None:
            now = time.monotonic()
            if write_errors:
                raise ValueError('Supervisor request delivery uncertain')
            if stopping is None and (stop.is_set() or overflow.is_set() or now-started >= checked['timeout_seconds']):
                reason = 'cancelled' if stop.is_set() else 'output_limit' if overflow.is_set() else 'timeout'
                stopping = now
            if stopping is not None and not stop_sent and not writes[-1].is_alive():
                send(b'STOP\n')
                stop_sent = True
            if stopping is not None and now-stopping >= STOP_GRACE:
                raise TimeoutError('Supervisor termination acknowledgement missing')
            time.sleep(.02)
        for reader in readers:
            reader.join(timeout=2)
        if any(reader.is_alive() for reader in readers) or overflow.is_set() or process.returncode != 0:
            raise ValueError('Supervisor transport incomplete or failed')
        result = decode_result(bytes(output['stdout']), checked['output_limit'])
        if inspect_runtime(runtime) != identity or root_identity(worker) != worker_id:
            raise ValueError('Runtime or workspace changed during command')
        if reason is not None or stop.is_set():
            result['status'] = reason or 'cancelled'
        return result | identity | {'status':result['status'],'launched':True}
    except (OSError, ValueError, TimeoutError) as exc:
        return {'status':'uncertain','reason':str(exc),'termination_observed':False,'launched':True}
    finally:
        # Launcher death is cleanup only, never proof of Linux termination.
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        for writer in writes:
            writer.join(timeout=2)
        if not any(writer.is_alive() for writer in writes):
            try:
                process.stdin.close()
            except OSError:
                pass
        for reader in readers:
            reader.join(timeout=2)
        for stream in (process.stdout, process.stderr):
            if not any(reader.is_alive() for reader in readers):
                stream.close()
