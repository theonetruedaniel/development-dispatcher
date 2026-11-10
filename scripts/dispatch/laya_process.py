"""Linux-side bounded warm-worker transport, not a runtime admission mechanism.

The trusted launcher must supply verified Bubblewrap arguments with a private PID
namespace. Killing an arbitrary process group alone cannot contain detached
descendants. This module neither selects mounts nor accepts model-supplied argv.
"""
import json
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time

from .laya_worker import INPUT_LIMIT, OUTPUT_LIMIT, QUESTION, proposal


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate protocol field')
        result[key] = value
    return result


def _decode(raw):
    if len(raw) > OUTPUT_LIMIT:
        raise ValueError('Worker response exceeds bound')
    return json.loads(raw, object_pairs_hook=_unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def decode_proposal(raw, revision):
    value = _decode(raw)
    if (not isinstance(value, dict) or
            set(value) != {'status', 'task_class', 'selector_revision', 'reason_code'} or
            value['selector_revision'] != revision or
            not isinstance(value['reason_code'], str) or not 1 <= len(value['reason_code']) <= 128):
        raise ValueError('Unbound or malformed worker response')
    if value['status'] == 'ok':
        if (not isinstance(value['task_class'], str) or value['task_class'] not in QUESTION['criteria']
                or value['reason_code'] != 'classified'):
            raise ValueError('Invalid classification')
    elif value['status'] not in ('overflow', 'unavailable') or value['task_class'] is not None:
        raise ValueError('Invalid fallback')
    return value


def _limit(value, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError('Invalid transport deadline')


class LayaProcess:
    def __init__(self, argv, revision, *, startup_timeout=180, stop=None):
        _limit(startup_timeout, 180)
        if (not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{40}', revision)
                or not isinstance(argv, list) or not argv or not os.path.isabs(argv[0])
                or any(not isinstance(arg, str) or '\0' in arg for arg in argv)):
            raise ValueError('Trusted argv and exact checkpoint revision required')
        self.argv, self.revision = list(argv), revision
        self.startup_timeout = startup_timeout
        self.stop = stop if stop is not None else threading.Event()
        self.process = None
        self.closed = False
        self.reaped = False
        self.stderr_bytes = 0
        self.startup_seconds = 0
        self.lock = threading.Lock()

    @property
    def pid(self):
        return self.process.pid if self.process is not None else None

    def _exchange(self, payload, timeout):
        process = self.process
        deadline = time.monotonic()+timeout
        offset = 0
        output = bytearray()
        with selectors.DefaultSelector() as events:
            events.register(process.stdout, selectors.EVENT_READ, 'stdout')
            events.register(process.stderr, selectors.EVENT_READ, 'stderr')
            if payload:
                events.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
            while True:
                if self.stop.is_set():
                    raise ValueError('Stopped')
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Worker response deadline')
                for key, _ in events.select(min(.05, remaining)):
                    try:
                        if key.data == 'stdin':
                            offset += os.write(key.fd, payload[offset:offset+4096])
                            if offset == len(payload):
                                events.unregister(key.fileobj)
                            continue
                        chunk = os.read(key.fd, 4096)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        if key.data == 'stdout':
                            raise ValueError('Worker EOF')
                        events.unregister(key.fileobj)
                        continue
                    if key.data == 'stderr':
                        self.stderr_bytes += len(chunk)
                        if self.stderr_bytes > OUTPUT_LIMIT:
                            raise ValueError('Worker diagnostic output exceeds bound')
                        continue
                    output.extend(chunk)
                    if len(output) > OUTPUT_LIMIT:
                        raise ValueError('Worker response exceeds bound')
                    if b'\n' in output:
                        if not output.endswith(b'\n') or output.count(b'\n') != 1 or offset != len(payload):
                            raise ValueError('Unsolicited worker output')
                        return bytes(output)
                if process.poll() is not None:
                    raise ValueError('Worker exited')

    def request(self, task, timeout_seconds=5):
        _limit(timeout_seconds, 5)
        with self.lock:
            if self.closed or self.stop.is_set():
                self.close()
                return proposal('unavailable', self.revision, 'worker_closed')
            try:
                if not isinstance(task, str) or not task.strip():
                    raise ValueError('Task text required')
                payload = (json.dumps({'task': task}, ensure_ascii=False)+'\n').encode('utf-8')
                if len(payload) > INPUT_LIMIT:
                    return proposal('overflow', self.revision, 'request_size_exceeded')
                if self.process is None:
                    if sys.platform != 'linux':
                        raise ValueError('Linux-side transport required')
                    startup_started=time.monotonic()
                    try:
                        self.process = subprocess.Popen(self.argv, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                            start_new_session=True, env={'PATH': '/usr/bin:/bin'}, shell=False)
                        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                            os.set_blocking(stream.fileno(), False)
                        ready = _decode(self._exchange(b'', self.startup_timeout))
                        if ready != {'status': 'ready', 'selector_revision': self.revision}:
                            raise ValueError('Worker readiness not bound to checkpoint')
                    finally:self.startup_seconds=time.monotonic()-startup_started
                return decode_proposal(self._exchange(payload, timeout_seconds), self.revision)
            except (OSError, ValueError, TypeError, TimeoutError) as exc:
                self.close()
                return proposal('timeout' if isinstance(exc, TimeoutError) else 'unavailable',
                                self.revision, 'worker_timeout' if isinstance(exc, TimeoutError)
                                else 'worker_protocol_or_process_failure')

    def close(self):
        self.closed = True
        if self.process is None or self.reaped:
            return
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.process.wait(timeout=3)
        self.reaped = True
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
