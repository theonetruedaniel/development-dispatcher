"""Pinned Windows-to-WSL classifier transport. Integrity is not admission."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from .laya_process import decode_proposal
from .laya_runtime import PROFILE_LIMIT, validate_profile, _unique
from .laya_supervisor import validate_request
from .protocol import clean_environment
from .write_contracts import safe_root
from .wsl_adapter import linux_path

MAX_RECEIPT=128*1024
STOP_GRACE=12


def runtime_files():
    folder=Path(__file__).resolve().parent
    files={name:folder/(name+'.py') for name in ('laya_adapter','laya_supervisor','laya_runtime',
        'laya_process','laya_worker','runtime_inventory','wsl_supervisor','__init__')}
    files['launcher']=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/wsl.exe'
    return files


def inspect_runtime(runtime):
    keys={'distro','files','profile_path','profile_sha256','checkpoint_revision'}
    if (not isinstance(runtime,dict) or set(runtime)!=keys or runtime['distro']!='Ubuntu'
            or not isinstance(runtime['files'],dict) or set(runtime['files'])!=set(runtime_files())
            or not isinstance(runtime['profile_path'],str)):
        raise ValueError('Exact controller classifier runtime required')
    for name,size in (('profile_sha256',64),('checkpoint_revision',40)):
        if not isinstance(runtime[name],str) or not re.fullmatch('[0-9a-f]{'+str(size)+'}',runtime[name]):
            raise ValueError('Invalid classifier runtime identity')
    folder=Path(__file__).resolve().parent
    if (folder.parent/'__init__.py').exists():
        raise ValueError('Unpinned parent package initializer')
    paths=runtime_files()|{'profile':Path(runtime['profile_path'])}
    observed={}
    profile=None
    for name,path in paths.items():
        safe_root(path.parent)
        if not path.is_file() or path.is_symlink() or getattr(path.lstat(),'st_file_attributes',0)&0x400:
            raise ValueError('Linked or nonregular classifier component')
        if name=='profile' and path.stat().st_size>PROFILE_LIMIT:
            raise ValueError('Classifier profile exceeds bound')
        raw=path.read_bytes()
        observed[name]=hashlib.sha256(raw).hexdigest()
        if name=='profile':profile=raw
    if observed!={**runtime['files'],'profile':runtime['profile_sha256']}:
        raise ValueError('Classifier runtime changed; requalification required')
    profile=validate_profile(json.loads(profile,object_pairs_hook=_unique))
    if profile['checkpoint_revision']!=runtime['checkpoint_revision']:
        raise ValueError('Classifier checkpoint mismatch')
    script=paths['laya_supervisor']
    return dict(profile_sha256=runtime['profile_sha256'],checkpoint_revision=runtime['checkpoint_revision'],
        runtime_digest=hashlib.sha256(json.dumps(runtime,sort_keys=True).encode()).hexdigest(),
        command=[str(paths['launcher']),'-d','Ubuntu','--exec','/usr/bin/env','-i','PATH=/usr/bin:/bin',
                 '/usr/bin/python3','-B','-I',linux_path(script.parent)+'/'+script.name,
                 '--profile',linux_path(paths['profile'].parent)+'/'+paths['profile'].name,
                 '--profile-sha256',runtime['profile_sha256']])


def validate_measurements(value,count):
    if (not isinstance(value,dict) or set(value)!={'cold_seconds','warm_seconds'} or
            not isinstance(value['warm_seconds'],list) or len(value['warm_seconds'])!=count or
            any(type(number) not in (int,float) or not math.isfinite(number) or number<0
                for number in [value['cold_seconds'],*value['warm_seconds']])):
        raise ValueError('Invalid classifier timing measurements')


def decode_result(raw,profile_sha256,revision,count):
    if len(raw)>MAX_RECEIPT:raise ValueError('Classifier receipt exceeds bound')
    result=json.loads(raw,object_pairs_hook=_unique)
    if (not isinstance(result,dict) or
            set(result)-{'measurements'}!={'status','proposals','termination_observed','profile_sha256'} or
            result['profile_sha256']!=profile_sha256 or type(result['termination_observed']) is not bool or
            result['status'] not in ('completed','failed','cancelled','timeout','uncertain') or
            not isinstance(result['proposals'],list)):
        raise ValueError('Unbound classifier receipt')
    if result['status']=='uncertain':
        if result['termination_observed'] or result['proposals']:
            raise ValueError('Invalid uncertain receipt')
    elif not result['termination_observed']:
        raise ValueError('Missing Linux termination acknowledgement')
    if result['status']=='completed':
        if len(result['proposals'])!=count:raise ValueError('Incomplete classifier batch')
        for proposal in result['proposals']:
            timeout=dict(status='timeout',task_class=None,selector_revision=revision,reason_code='worker_timeout')
            if proposal!=timeout:decode_proposal(json.dumps(proposal).encode(),revision)
    elif result['proposals']:
        raise ValueError('Failed batch contains proposals')
    if 'measurements' in result:
        if result['status']!='completed':raise ValueError('Failed batch contains timing measurements')
        validate_measurements(result['measurements'],count)
    return result


def run_batch(tasks,runtime,stop,timeout_seconds=600):
    result=dict(status='blocked',proposals=[],termination_observed=True,launched=False)
    if stop.is_set():return result|{'status':'cancelled'}
    try:
        if os.name!='nt':raise ValueError('Windows classifier controller required')
        if (type(timeout_seconds) not in (int,float) or not math.isfinite(timeout_seconds)
                or not 0<timeout_seconds<=1800):raise ValueError('Invalid classifier deadline')
        validate_request({'tasks':tasks})
        checked=inspect_runtime(runtime)
        payload=json.dumps({'tasks':tasks},ensure_ascii=False).encode()+b'\n'
        if stop.is_set():return result|{'status':'cancelled'}
        process=subprocess.Popen(checked['command'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW,shell=False,
            env=clean_environment(os.environ))
    except (OSError,ValueError,TypeError) as exc:
        return result|{'reason':str(exc)}
    output={'stdout':bytearray(),'stderr':bytearray()}
    overflow=threading.Event();write_errors=[];writers=[]
    def drain(stream,name,cap):
        try:
            while data:=stream.read(4096):
                room=cap-len(output[name]);output[name].extend(data[:room])
                if len(data)>room:overflow.set()
        except (OSError,ValueError):overflow.set()
    readers=[threading.Thread(target=drain,args=(process.stdout,'stdout',MAX_RECEIPT),daemon=True),
             threading.Thread(target=drain,args=(process.stderr,'stderr',65536),daemon=True)]
    for reader in readers:reader.start()
    def send(data):
        def write():
            try:process.stdin.write(data);process.stdin.flush()
            except (OSError,ValueError) as exc:write_errors.append(exc)
        thread=threading.Thread(target=write,daemon=True);writers.append(thread);thread.start()
    started=time.monotonic();stopping=None;reason=None;stop_sent=False
    try:
        send(payload)
        while process.poll() is None:
            now=time.monotonic()
            if write_errors:raise ValueError('Classifier request delivery uncertain')
            if stopping is None and (stop.is_set() or overflow.is_set() or now-started>=timeout_seconds):
                reason='cancelled' if stop.is_set() else 'output_limit' if overflow.is_set() else 'timeout'
                stopping=now
            if stopping is not None and not stop_sent and not writers[-1].is_alive():
                send(b'STOP\n');stop_sent=True
            if stopping is not None and now-stopping>=STOP_GRACE:
                raise TimeoutError('Linux classifier termination acknowledgement missing')
            time.sleep(.02)
        for reader in readers:reader.join(2)
        if process.returncode!=0 or overflow.is_set() or any(t.is_alive() for t in readers):
            raise ValueError('Incomplete classifier transport')
        receipt=decode_result(bytes(output['stdout']),checked['profile_sha256'],
                              checked['checkpoint_revision'],len(tasks))
        if inspect_runtime(runtime)!=checked:raise ValueError('Classifier runtime drift')
        if receipt['status']!='uncertain' and (reason or stop.is_set()):
            receipt.update(status=reason or 'cancelled',proposals=[])
        return receipt|dict(launched=True,runtime_digest=checked['runtime_digest'])
    except (OSError,ValueError,TypeError,TimeoutError) as exc:
        return dict(status='uncertain',proposals=[],termination_observed=False,launched=True,reason=str(exc))
    finally:
        # Windows launcher cleanup cannot establish that a Linux worker stopped.
        if process.poll() is None:process.kill()
        process.wait(timeout=3)
        for writer in writers:writer.join(2)
        for reader in readers:reader.join(2)
        if not any(t.is_alive() for t in writers):process.stdin.close()
        if not any(t.is_alive() for t in readers):
            process.stdout.close();process.stderr.close()
