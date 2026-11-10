"""Classifier proposals are advisory inputs to the deterministic route policy."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
from .contracts import CLASSES
from .laya_adapter import run_batch
from .laya_process import decode_proposal
from .laya_runtime import _unique
from .policy import select_route
from .write_contracts import safe_root

SELECTOR_PIN_PATH = Path(__file__).with_name('selector-compatibility.json')
SELECTOR_STATE_PATH = Path(__file__).with_name('selector-uncertain.json')


def run_reserved_batch(tasks,runtime,stop,deadline_seconds=600):
    """Serialize evaluation and production classifier launches across processes."""
    if stop.is_set():
        return dict(status='cancelled',proposals=[],termination_observed=True)
    digest=hashlib.sha256(json.dumps(runtime,sort_keys=True).encode()).hexdigest()
    safe_root(SELECTOR_STATE_PATH.parent)
    with SELECTOR_STATE_PATH.open('x',encoding='utf-8') as stream:
        json.dump(dict(status='pending',runtime_digest=digest),stream)
        stream.flush();os.fsync(stream.fileno())
    result=run_batch(tasks,runtime,stop,timeout_seconds=deadline_seconds)
    if isinstance(result,dict) and result.get('termination_observed') is True:
        SELECTOR_STATE_PATH.unlink()
    return result

PROPOSAL_FIELDS = {"status", "task_class", "selector_revision", "reason_code"}


def resolve_proposal(manifest: dict, proposal: dict, catalog: dict, usage: dict | None,
                     history: list[dict]):
    original = select_route(manifest, catalog, usage, history)
    if (manifest.get("selector_mode", "rules") != "laya" or
            manifest.get("mode") == "manual" or
            manifest["task_class"] in ("consequential", "exceptional") or history):
        return original
    if (not isinstance(proposal, dict) or set(proposal) != PROPOSAL_FIELDS or
            proposal["status"] != "ok" or
            not isinstance(proposal["task_class"], str) or proposal["task_class"] not in CLASSES or
            not isinstance(proposal["selector_revision"], str) or not proposal["selector_revision"] or
            proposal["reason_code"] != "classified"):
        return original
    candidate = copy.deepcopy(manifest)
    candidate["task_class"] = proposal["task_class"]
    # A learned label must not undo an existing usage/quality/availability block.
    if original.action != "dispatch":
        return original
    return select_route(candidate, catalog, usage, history)


def propose_class(task: dict, runtime: dict, timeout_seconds: float = 5, *,
                  stop=None, deadline_seconds: float = 600) -> dict:
    """Use only a controller-admitted runtime; uncertain launches stay reserved.

    The worker has a fixed five-second warm-request limit. deadline_seconds
    separately bounds inventory, startup and the complete batch transport.
    """
    unavailable = dict(status='unavailable', task_class=None, selector_revision=None,
                       reason_code='local_runtime_not_qualified')
    stop = stop if stop is not None else threading.Event()
    try:
        if (stop.is_set() or task.get('mode') != 'auto' or
                task.get('selector_mode') not in ('shadow', 'laya') or
                timeout_seconds != 5 or type(deadline_seconds) not in (int, float) or
                not math.isfinite(deadline_seconds) or not 0 < deadline_seconds <= 1800):
            return unavailable
        safe_root(SELECTOR_PIN_PATH.parent)
        info = SELECTOR_PIN_PATH.lstat()
        if (SELECTOR_PIN_PATH.is_symlink() or not SELECTOR_PIN_PATH.is_file() or
                getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_size > 4096):
            return unavailable
        with SELECTOR_PIN_PATH.open('rb') as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            return unavailable
        pin = json.loads(raw, object_pairs_hook=_unique)
        digest = hashlib.sha256(json.dumps(runtime, sort_keys=True).encode()).hexdigest()
        if (not isinstance(pin, dict) or set(pin) != {'schema_version', 'runtime_digest',
                'checkpoint_revision', 'modes', 'evidence_sha256'} or
                type(pin['schema_version']) is not int or pin['schema_version'] != 1 or
                pin['runtime_digest'] != digest or not isinstance(pin['modes'], list) or
                not pin['modes'] or any(mode not in ('shadow', 'laya') for mode in pin['modes']) or
                task['selector_mode'] not in pin['modes'] or
                any(not isinstance(pin[key], str) or not re.fullmatch('[0-9a-f]{'+str(size)+'}', pin[key])
                    for key, size in (('checkpoint_revision', 40), ('evidence_sha256', 64)))):
            return unavailable
        result = run_reserved_batch([task['task']], runtime, stop, deadline_seconds)
        if not isinstance(result, dict) or result.get('termination_observed') is not True:
            return unavailable
        if stop.is_set() or result.get('status') != 'completed':
            return unavailable
        proposals = result.get('proposals')
        if not isinstance(proposals, list) or len(proposals) != 1:
            return unavailable
        decode_proposal(json.dumps(proposals[0]).encode(), pin['checkpoint_revision'])
        return proposals[0]
    except Exception:
        # Never infer worker termination from a controller exception.
        return unavailable
