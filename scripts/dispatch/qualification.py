"""Offline classifier scoring; scores alone never admit a live runtime."""
import math
import time
import hashlib
import json
import os
from pathlib import Path
from dataclasses import asdict
from .contracts import CLASSES
from .policy import LADDERS, select_route
from .selector import resolve_proposal
from .selector import run_reserved_batch
from .laya_adapter import validate_measurements
from .write_contracts import safe_root
from .write_qualification import qualify_writes

ORDER = ["routine", "screening", "implementation", "complex", "consequential", "exceptional"]


def validate_corpus(corpus):
    if not isinstance(corpus,list) or not corpus or any(
            not isinstance(c,dict) or set(c)!={'id','task','expected','floor'} or
            not isinstance(c['id'],str) or not c['id'] or
            not isinstance(c['task'],str) or not c['task'].strip() or
            not isinstance(c['floor'],str) or c['floor'] not in CLASSES or
            not isinstance(c['expected'],list) or not c['expected'] or
            any(not isinstance(v,str) or v not in CLASSES for v in c['expected']) for c in corpus):
        raise ValueError("Invalid frozen corpus")
    if len({c["id"] for c in corpus}) != len(corpus):
        raise ValueError("Duplicate case IDs")


def evaluate_selector(corpus: list[dict], predict, rules, *, measurements=None) -> dict:
    validate_corpus(corpus)
    if measurements is not None:validate_measurements(measurements,len(corpus))
    catalog={}
    for ladder in LADDERS.values():
        for model,effort in ladder:
            catalog.setdefault('gpt-6-'+model,[])
            if effort not in catalog['gpt-6-'+model]:catalog['gpt-6-'+model].append(effort)
    confusion={category:{label:0 for label in ORDER+['fallback']} for category in ORDER}
    rows = []
    for case in corpus:
        baseline = rules(case)
        if not isinstance(baseline,str) or baseline not in CLASSES:
            raise ValueError("Invalid rules result")
        started = time.monotonic()
        try:
            proposed = predict(case)
            valid = isinstance(proposed, str) and proposed in CLASSES
        except Exception:
            proposed, valid = None, False
        elapsed = (time.monotonic()-started if measurements is None else
                   measurements['warm_seconds'][len(rows)])
        work=dict(package_id=case['id'],task_class=baseline,mode='auto',selector_mode='laya',
                  max_attempts=1,required=True)
        proposal=dict(status='ok' if valid else 'unavailable',task_class=proposed if valid else None,
                      selector_revision='offline-evaluation',reason_code='classified')
        original=select_route(work,catalog,None,[])
        effective=resolve_proposal(work,proposal,catalog,None,[])
        shadow=resolve_proposal(work|{'selector_mode':'shadow'},proposal,catalog,None,[])
        rank={('gpt-6-'+LADDERS[c][0][0],LADDERS[c][0][1]):i for i,c in enumerate(ORDER)}
        policy_under=(effective.action=='dispatch' and
                      rank[(effective.route.model,effective.route.effort)]<ORDER.index(case['floor']))
        confusion[case['floor']][proposed if valid else 'fallback']+=1
        rows.append({"id": case["id"], "proposed": proposed if valid else None,
                     "acceptable": valid and proposed in case["expected"],
                     "under": valid and ORDER.index(proposed) < ORDER.index(case["floor"]),
                     "rules_under": ORDER.index(baseline) < ORDER.index(case["floor"]),
                     "consequential": case["floor"] in ("consequential", "exceptional"),
                     "fallback": not valid, "elapsed_seconds": elapsed,
                     "effective_route":asdict(effective.route),"effective_action":effective.action,
                     "policy_under":policy_under,"shadow_identical":shadow==original})
    acceptable = sum(r["acceptable"] for r in rows)
    under = sum(r["under"] for r in rows)
    baseline_under = sum(r["rules_under"] for r in rows)
    violations = sum(r["under"] and r["consequential"] for r in rows)
    times = sorted(r["elapsed_seconds"] for r in rows)
    ordinary=[r for r in rows if not r['consequential']]
    ordinary_acceptable=sum(r['acceptable'] for r in ordinary)
    agreement=ordinary_acceptable/len(ordinary) if ordinary else None
    policy_violations=sum(r['policy_under'] and r['consequential'] for r in rows)
    quality=agreement is not None and agreement>=.9 and under<=baseline_under and policy_violations==0
    p95=times[math.ceil(len(times)*0.95)-1]
    return {"cases": len(rows), "acceptable": acceptable, "under_routing": under,
            "rules_under_routing": baseline_under, "consequential_violations": violations,
            "fallbacks": sum(r["fallback"] for r in rows),
            "p95_seconds":p95,"latency_pass":p95<=5,
            "ordinary_cases":len(ordinary),"ordinary_acceptable":ordinary_acceptable,
            "ordinary_agreement":agreement,"confusion":confusion,
            "confusion_reference":"trusted corpus floor; expected may contain multiple acceptable classes",
            "policy_consequential_violations":policy_violations,
            "policy_under_routing":sum(r['policy_under'] for r in rows),
            "shadow_routes_identical":all(r['shadow_identical'] for r in rows),
            "quality_pass":quality,"evaluation_pass":quality and p95<=5,
            "policy_context":"offline synthetic catalog, unknown usage, no retry history; blocked routes are not executed",
            "adoption_qualified": False, "rows": rows}


def qualify_selector(runtime,corpus,output_path,stop):
    """Evaluate an explicitly supplied pinned candidate, never install admission."""
    validate_corpus(corpus)
    if not 60<=len(corpus)<=64 or sum(c['floor']=='consequential' for c in corpus)<15:
        raise ValueError('Qualification requires 60-64 cases including 15 consequential cases')
    path=Path(output_path).absolute()
    safe_root(path.parent)
    base=dict(adoption_qualified=False,dispatched=False,
        corpus_digest=hashlib.sha256(json.dumps(corpus,sort_keys=True).encode()).hexdigest(),
        runtime_digest=hashlib.sha256(json.dumps(runtime,sort_keys=True).encode()).hexdigest(),
        digest_encoding='sorted-key JSON of parsed input',
        baseline='trusted corpus floor, not a measured natural-language rules classifier')
    # Exclusive output creation preserves old evidence and records interrupted runs.
    with path.open('x+',encoding='utf-8') as stream:
        json.dump(base|{'state':'pending'},stream);stream.flush();os.fsync(stream.fileno())
        try:
            receipt=run_reserved_batch([c['task'] for c in corpus],runtime,stop)
            if (not isinstance(receipt,dict) or receipt.get('termination_observed') is not True):
                result=base|dict(state='uncertain',receipt=receipt)
            elif stop.is_set() or receipt.get('status')!='completed':
                result=base|dict(state='blocked',receipt=receipt)
            else:
                proposals=receipt.get('proposals')
                if not isinstance(proposals,list) or len(proposals)!=len(corpus):
                    raise ValueError('Incomplete classifier receipt')
                validate_measurements(receipt.get('measurements'),len(corpus))
                by_id={case['id']:proposal for case,proposal in zip(corpus,proposals)}
                def predicted(case):
                    proposal=by_id[case['id']]
                    return proposal.get('task_class') if isinstance(proposal,dict) and proposal.get('status')=='ok' else None
                evaluation=evaluate_selector(corpus,predicted,lambda c:c['floor'],
                                             measurements=receipt['measurements'])
                result=base|dict(state='evaluated',receipt=receipt,evaluation=evaluation,
                    timing_source='Linux supervisor monotonic cold-start and warm-request durations',
                    memory_qualified=False,paired_tasks_qualified=False)
        except FileExistsError:
            result=base|dict(state='blocked',reason='Classifier reservation exists; reconcile before retry')
        except Exception as exc:
            result=base|dict(state='uncertain',reason=str(exc))
        stream.seek(0);json.dump(result,stream,indent=2,allow_nan=False)
        stream.truncate();stream.flush();os.fsync(stream.fileno())
    return result
