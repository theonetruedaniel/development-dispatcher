"""Owned, pinned Code Mode host. Host binding does not admit native tools."""
import hashlib
import os
from pathlib import Path
import queue
import re
import subprocess
import threading

from .protocol import clean_environment
from .write_contracts import safe_root


class CodeHost:
    def __init__(self, executable, digest, timeout=8):
        self.path=Path(executable)
        self.digest=digest
        self.proc=None
        self.closed=False
        self.failed=threading.Event()
        self.readers=[]
        self._verify()
        self.proc=subprocess.Popen([str(self.path),'--listen','grpc://127.0.0.1:0'],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            env=clean_environment(os.environ),shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        ready=queue.Queue(maxsize=1)

        def stdout():
            try:
                line=self.proc.stdout.readline(4097)
                ready.put_nowait(line)
                if self.proc.stdout.read(1):self.failed.set()
            except (OSError,ValueError,queue.Full):self.failed.set()

        def stderr():
            total=0
            try:
                while chunk:=self.proc.stderr.read(4096):
                    total+=len(chunk)
                    if total>65536:self.failed.set()
            except (OSError,ValueError):self.failed.set()

        try:
            for target in (stdout,stderr):
                reader=threading.Thread(target=target,daemon=True)
                self.readers.append(reader);reader.start()
            try:line=ready.get(timeout=timeout)
            except queue.Empty as exc:raise TimeoutError('Code host startup timed out') from exc
            match=re.fullmatch(rb'http://127\.0\.0\.1:([0-9]{1,5})\r?\n',line)
            if not match or not 0<int(match[1])<65536:
                raise ValueError('Code host did not report a bounded loopback endpoint')
            self.url=line.decode('ascii').strip()
            self.check()
        except BaseException:
            self.close()
            raise

    def _verify(self):
        safe_root(self.path)
        if (not self.path.is_file() or not isinstance(self.digest,str) or
                not re.fullmatch('[0-9a-f]{64}',self.digest)):
            raise ValueError('Exact Code Mode host pin required')
        with self.path.open('rb') as stream:
            actual=hashlib.file_digest(stream,'sha256').hexdigest()
        if actual!=self.digest:raise ValueError('Code Mode host changed; requalification required')

    def check(self):
        if self.closed or self.failed.is_set() or self.proc.poll() is not None:
            raise ValueError('Code Mode host is unavailable or its output exceeded bounds')
        self._verify()

    def close(self):
        if self.closed:return
        if self.proc is not None:
            if self.proc.poll() is None:self.proc.terminate()
            try:self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill();self.proc.wait(timeout=3)
            for reader in self.readers:reader.join(timeout=1)
            self.proc.stdout.close();self.proc.stderr.close()
        self.closed=True
