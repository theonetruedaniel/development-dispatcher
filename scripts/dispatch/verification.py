"""Trusted checks on independent snapshots of the exact proposed changes."""
import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import threading
import time

from .contracts import manifest_digest
from .workspace import collect_changes, digest, inventory
from .write_contracts import safe_path, safe_root, root_identity
from .wsl_adapter import linux_path, run_command
from .write_contracts import relative_name


def trusted_harness(definition):
    if (not isinstance(definition,dict) or
            set(definition)-{'harness'}!={'platform','argv','expected_stdout'} or
            definition['platform']!='linux' or not isinstance(definition['expected_stdout'],str)):
        raise ValueError('Unregistered or unsupported trusted check')
    argv=definition['argv']
    if (not isinstance(argv,list) or len(argv)<3 or
            any(not isinstance(arg,str) for arg in argv) or
            argv[:2]!=['/usr/bin/python3','-I']):
        raise ValueError('Trusted checks require an isolated controller Python harness')
    files=definition.get('harness')
    if files is None:
        if len(argv)!=4 or argv[2]!='-c':
            raise ValueError('Worker files cannot supply the trusted verifier')
        return {}
    if not isinstance(files,dict) or not files or len(files)>128:
        raise ValueError('Invalid trusted harness bundle')
    payload={}
    for name,text in files.items():
        name=relative_name(name)
        if not isinstance(text,str):raise ValueError('Trusted harness must contain text files')
        payload[name]=text.encode('utf-8')
    if len({name.casefold() for name in payload})!=len(payload) or sum(map(len,payload.values()))>4*1024*1024:
        raise ValueError('Trusted harness collides or exceeds size limit')
    prefix='/work/.dispatch-verifier/'
    if not argv[2].startswith(prefix) or argv[2][len(prefix):] not in payload:
        raise ValueError('Verifier entry point must belong to controller harness')
    return payload


def run_checks(check_ids: list[str], copy_record: dict, environment: dict,
               stop: threading.Event) -> list[dict]:
    rows=[]
    binding={'manifest_digest':copy_record['manifest_digest'], 'changes_digest':None}
    try:
        if (check_ids != copy_record['checks'] or not check_ids
                or len(check_ids)!=len(set(check_ids))):
            raise ValueError('Checks do not match the manifest')
        source=safe_root(Path(copy_record['root']))
        source_id=root_identity(source)
        stage=safe_root(Path(copy_record['staging']))
        if stage.is_relative_to(source) or source.is_relative_to(stage):
            raise ValueError('Verification storage must be outside worker')
        changes=collect_changes(copy_record)
        binding['changes_digest']=manifest_digest({'changes':changes})
        baseline=inventory(source)
        definitions=copy.deepcopy(environment['checks'])
        deadline=environment['deadline']
        if type(deadline) not in (int,float) or not math.isfinite(deadline):
            raise ValueError('Verification deadline required')
        for identifier in check_ids:
            started=time.monotonic()
            row=dict(binding,id=identifier,status='failed',exit_code=None,output_digest=None,
                     execution_started=False)
            rows.append(row)
            definition=definitions.get(identifier)
            harness=trusted_harness(definition)
            if any(name.casefold()=='.dispatch-verifier' or
                   name.casefold().startswith('.dispatch-verifier/') for name in baseline):
                raise ValueError('Worker contains reserved verifier paths')
            if stop.is_set() or deadline-time.monotonic()<1:
                raise ValueError('Verification stopped or expired')
            # Every check gets a fresh copy, so generated files cannot affect later checks.
            folder=Path(tempfile.mkdtemp(prefix='verification-',dir=stage.parent))
            worker=folder/'worker';worker.mkdir()
            size=0
            for name,expected in baseline.items():
                path=safe_path(source,name)
                if path.stat().st_size>16*1024*1024:
                    raise ValueError('Verification input too large')
                data=path.read_bytes();size+=len(data)
                if size>512*1024*1024 or digest(data)!=expected:
                    raise ValueError('Verification snapshot changed or exceeds bound')
                target=worker/name;target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(data)
            if inventory(worker)!=baseline or inventory(source)!=baseline:
                raise ValueError('Source changed during verification preparation')
            verified_inventory=dict(baseline)
            for name,data in harness.items():
                relative='.dispatch-verifier/'+name
                target=safe_path(worker,relative)
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(data)
                verified_inventory[relative]=digest(data)
            if inventory(worker)!=verified_inventory:
                raise ValueError('Trusted harness snapshot changed')
            if stop.is_set() or deadline-time.monotonic()<1:
                raise ValueError('Verification stopped or expired')
            row['execution_started']=True
            result=run_command(dict(root=linux_path(worker),argv=definition['argv'],
                timeout_seconds=min(1800,int(deadline-time.monotonic())),output_limit=4*1024*1024),
                environment['runtime'],stop)
            row.update(exit_code=result.get('exit_code'),elapsed_seconds=time.monotonic()-started,
                       termination_observed=result.get('termination_observed') is True,
                       verifier_digest=manifest_digest(definition),snapshot=str(worker),
                       output_digest=hashlib.sha256(json.dumps({k:result.get(k) for k in
                           ('status','stdout','stderr')},sort_keys=True).encode()).hexdigest())
            if (stop.is_set() or time.monotonic()>=deadline
                    or result.get('status')!='completed' or result.get('exit_code')!=0
                    or result.get('termination_observed') is not True
                    or result.get('stdout')!=definition['expected_stdout']):
                raise ValueError('Trusted check failed or termination unconfirmed')
            current=inventory(worker)
            if any(current.get(name)!=expected for name,expected in verified_inventory.items()):
                raise ValueError('Check modified its source snapshot')
            if root_identity(source)!=source_id or inventory(source)!=baseline:
                raise ValueError('Worker changed during verification')
            row['status']='passed'
        if collect_changes(copy_record)!=changes or stop.is_set():
            raise ValueError('Changes changed after verification')
    except (OSError,ValueError,KeyError,TypeError) as exc:
        for row in rows:
            row.update(status='failed',reason=str(exc))
        for identifier in check_ids[len(rows):]:
            rows.append(dict(binding,id=identifier,status='failed',reason=str(exc),
                             exit_code=None,output_digest=None,execution_started=False))
    return rows
