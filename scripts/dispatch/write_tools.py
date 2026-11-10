"""Controller-owned file and command operations for one granted editing turn."""
import copy
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time

from .contracts import manifest_digest
from .write_contracts import (permits, protected, relative_name, root_identity,
                              safe_path, safe_root, validate_write_grant, validate_write_manifest)
from .wsl_adapter import linux_path, run_command

READ_LIMIT = 64 * 1024
WRITE_LIMIT = 256 * 1024


class WriteToolBroker:
    def __init__(self, manifest: dict, grant: dict, copy_record: dict,
                 session: dict, runtime: dict, audit=None):
        self.manifest = validate_write_manifest(manifest, Path(manifest['workspace']))
        if (set(session) != {'account_fingerprint','thread_id','turn_id','deadline'}
                or any(not isinstance(session[k], str) or not session[k]
                       for k in ('account_fingerprint','thread_id','turn_id'))
                or type(session['deadline']) not in (int,float)
                or not math.isfinite(session['deadline'])):
            raise ValueError('Exact controller session required')
        self.session = copy.deepcopy(session)
        self.grant = validate_write_grant(grant, self.manifest, session['account_fingerprint'], time.time())
        if (copy_record['manifest_digest'] != manifest_digest(self.manifest)
                or Path(copy_record['primary']).resolve() != Path(self.manifest['workspace']).resolve()
                or copy_record['outputs'] != self.manifest['outputs']):
            raise ValueError('Copy does not bind authorized work')
        self.root = safe_root(Path(copy_record['root']))
        primary = safe_root(Path(self.manifest['workspace']))
        if self.root.is_relative_to(primary) or primary.is_relative_to(self.root):
            raise ValueError('Worker must be separate from primary')
        self.root_id = root_identity(self.root)
        self.runtime = copy.deepcopy(runtime)
        self.deadline = min(session['deadline'],time.monotonic()+self.manifest['timeout_seconds'])
        self.calls = set()
        self.lock = threading.Lock()
        self.uncertain = False
        self.audit = audit

    def _audit(self, state, **fields):
        if self.audit is not None:
            try:
                self.audit(state, **fields)
            except Exception:
                self.uncertain = True
                raise

    def _path(self, name, writing=False):
        name = relative_name(name)
        allowed = permits(name,self.manifest['outputs'])
        if not writing:
            allowed = allowed or name in self.manifest['inputs']
        if protected(name) or not allowed:
            raise ValueError('Path outside granted scope')
        return safe_path(self.root,name)

    def handle(self, request: dict, stop: threading.Event) -> dict:
        with self.lock:
            try:
                if self.uncertain:
                    raise ValueError('Prior command termination uncertain; reconciliation required')
                if stop.is_set() or time.monotonic() >= self.deadline:
                    raise ValueError('Package stopped or expired')
                validate_write_grant(self.grant,self.manifest,self.session['account_fingerprint'],time.time())
                if root_identity(self.root) != self.root_id:
                    raise ValueError('Worker identity changed')
                expected = {'account_fingerprint','thread_id','turn_id','call_id','operation','arguments'}
                if not isinstance(request,dict) or set(request) != expected:
                    raise ValueError('Invalid tool envelope')
                if any(request[k] != self.session[k] for k in ('account_fingerprint','thread_id','turn_id')):
                    raise ValueError('Tool request belongs to another session')
                call = request['call_id']
                if (not isinstance(call,str) or not 1 <= len(call) <= 200
                        or call in self.calls or len(self.calls) >= 1000):
                    raise ValueError('Duplicate or excessive tool calls')
                arguments = request['arguments']
                if not isinstance(arguments,dict) or len(json.dumps(arguments).encode()) > 512*1024:
                    raise ValueError('Tool arguments exceed bound')
                self.calls.add(call)
                result = self._operate(request['operation'],arguments,stop,call)
                return {'success':True} | result
            except (OSError, ValueError, TypeError, KeyError) as exc:
                return {'success':False,'error':str(exc)}

    def _operate(self, operation, args, stop, call_id):
        fields = {'read':{'path'},'write':{'path','text'},'delete':{'path'},
                  'list':{'path'},'command':{'argv'}}
        if not isinstance(operation,str) or operation not in fields or set(args) != fields[operation]:
            raise ValueError('Unsupported operation or arguments')
        if operation == 'command':
            remaining = min(self.deadline-time.monotonic(), self.grant['expires_at']-time.time())
            if remaining < 1:
                raise ValueError('No command time remains')
            request = dict(root=linux_path(self.root),argv=args['argv'],
                           timeout_seconds=min(1800,int(remaining)),output_limit=128*1024)
            self._audit('worker_command_started', call_id=call_id)
            self.uncertain = True
            result = run_command(request,self.runtime,stop)
            self.uncertain = result.get('termination_observed') is not True
            self._audit('worker_command_finished', call_id=call_id,
                        termination_observed=result.get('termination_observed') is True,
                        command_status=result.get('status'))
            result = {k:v for k,v in result.items() if k not in ('stdout_b64','stderr_b64')}
            return {'success':result.get('status')=='completed', 'command':result}
        if operation == 'list':
            prefix = '' if args['path']=='' else relative_name(args['path'])
            if prefix and not (permits(prefix,self.manifest['outputs']) or any(
                    p.rstrip('/') == prefix or p.startswith(prefix+'/')
                    for p in self.manifest['inputs']+self.manifest['outputs'])):
                raise ValueError('Directory outside granted scope')
            folder = self.root if not prefix else safe_path(self.root,prefix)
            entries=[]
            for path in sorted(folder.iterdir()):
                relative = path.relative_to(self.root).as_posix()
                allowed = permits(relative,self.manifest['outputs']) or any(
                    p == relative or p.startswith(relative+'/') for p in self.manifest['inputs'])
                allowed = allowed or any(p.startswith(relative+'/') for p in self.manifest['outputs'])
                if allowed and not protected(relative):
                    safe_path(self.root,relative)
                    entries.append(relative)
                if len(entries)>1000:
                    raise ValueError('Directory listing exceeds bound')
            return {'entries':entries}
        path = self._path(args['path'],writing=operation!='read')
        if operation == 'read':
            with path.open('rb') as stream:
                before = path.stat()
                opened = os.fstat(stream.fileno())
                if (before.st_dev,before.st_ino) != (opened.st_dev,opened.st_ino):
                    raise ValueError('File changed while opening')
                data=stream.read(READ_LIMIT+1)
            self._path(args['path'])
            after=path.stat()
            if (before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_ino,after.st_size,after.st_mtime_ns):
                raise ValueError('File changed while reading')
            if len(data)>READ_LIMIT:
                raise ValueError('Read exceeds bound')
            return {'text':data.decode('utf-8')}
        if operation == 'delete':
            if not path.is_file():
                raise ValueError('Delete requires a regular file')
            path.unlink()
            return {}
        if not isinstance(args['text'],str) or len(args['text'].encode('utf-8'))>WRITE_LIMIT:
            raise ValueError('Write exceeds bound')
        path.parent.mkdir(parents=True,exist_ok=True)
        self._path(args['path'],writing=True)
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.dispatch-write-',delete=False) as stream:
                temporary=Path(stream.name)
                stream.write(args['text'].encode('utf-8'));stream.flush();os.fsync(stream.fileno())
            self._path(args['path'],writing=True)
            os.replace(temporary,path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return {}
