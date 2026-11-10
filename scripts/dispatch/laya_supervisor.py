"""Linux classifier supervisor. Only its trusted controller supplies profile pins."""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time

if not __package__:
    # The Windows launcher pins this script and its imported controller modules.
    # Python -I otherwise excludes this trusted repository from the import path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.dispatch.laya_process import LayaProcess
    from scripts.dispatch.laya_runtime import load_profile, sandbox_argv, verify_host
else:
    from .laya_process import LayaProcess
    from .laya_runtime import load_profile, sandbox_argv, verify_host

REQUEST_LIMIT = 2*1024*1024


def validate_request(request):
    if (not isinstance(request, dict) or set(request) != {'tasks'} or
            not isinstance(request['tasks'], list) or not 1 <= len(request['tasks']) <= 64):
        raise ValueError('Bounded task batch required')
    tasks = request['tasks']
    if any(not isinstance(task, str) or not task.strip() or
           len(task.encode('utf-8')) > 256*1024 for task in tasks):
        raise ValueError('Invalid classifier task text')
    if len(json.dumps(request,ensure_ascii=False).encode('utf-8')) > REQUEST_LIMIT:
        raise ValueError('Classifier batch exceeds bound')
    return tasks


def evaluate_batch(request, profile_path, profile_sha256, stop):
    result = dict(status='failed', proposals=[], termination_observed=True,
                  profile_sha256=profile_sha256)
    worker = None
    started = time.monotonic()
    try:
        tasks = validate_request(request)
        if stop.is_set():
            return result | {'status':'cancelled'}
        verify_host()
        profile = load_profile(profile_path,profile_sha256,stop=stop)
        if stop.is_set():
            return result | {'status':'cancelled'}
        worker = LayaProcess(sandbox_argv(profile),profile['checkpoint_revision'],stop=stop)
        measurements=dict(cold_seconds=0,warm_seconds=[])
        for task in tasks:
            if stop.is_set():
                result['status']='cancelled'
                break
            if time.monotonic()-started >= 1800:
                result['status']='timeout'
                break
            result['termination_observed']=False
            request_started=time.monotonic()
            previous_startup=worker.startup_seconds
            result['proposals'].append(worker.request(task))
            startup_delta=worker.startup_seconds-previous_startup
            measurements['warm_seconds'].append(max(0,time.monotonic()-request_started-startup_delta))
            measurements['cold_seconds']=worker.startup_seconds
        else:
            result['status']='completed'
            result['measurements']=measurements
    except (OSError,ValueError,TypeError,TimeoutError):
        result['status']='failed'
    finally:
        if worker is not None:
            try:
                worker.close()
                result['termination_observed']=worker.pid is None or worker.reaped
            except Exception:
                result['termination_observed']=False
                result['status']='uncertain'
        if result['status']=='completed':
            try:
                load_profile(profile_path,profile_sha256,stop=stop)
            except (OSError,ValueError,TypeError):
                result['status']='failed'
        if stop.is_set() and result['status']!='uncertain':
            result['status']='cancelled'
        if result['status']!='completed':
            result['proposals']=[]
            result.pop('measurements',None)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',required=True)
    parser.add_argument('--profile-sha256',required=True)
    args=parser.parse_args()
    request=bytearray()
    while len(request)<=REQUEST_LIMIT:
        byte=os.read(0,1)
        if byte==b'\n':break
        if not byte:raise ValueError('Missing request')
        request.extend(byte)
    else:
        raise ValueError('Request exceeds bound')
    stop=threading.Event()
    def control():
        # STOP, malformed control data and EOF all revoke further work.
        data=bytearray()
        while len(data)<5:
            byte=os.read(0,1)
            if not byte:break
            data.extend(byte)
            if not b'STOP\n'.startswith(data):break
        stop.set()
    threading.Thread(target=control,daemon=True).start()
    result=evaluate_batch(json.loads(request),args.profile,args.profile_sha256,stop)
    print(json.dumps(result,allow_nan=False),flush=True)


if __name__=='__main__':
    main()
