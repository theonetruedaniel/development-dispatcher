"""Offline controller probes. This module cannot admit or launch a live backend."""
import hashlib
import json
import os
from pathlib import Path
import time

from . import integration
from .contracts import manifest_digest
from .journal import Journal
from .workspace import collect_changes, digest
from .write_contracts import root_identity, safe_root, validate_write_manifest
from .write_recovery import reconcile_write
from .write_sandbox import qualify_write_environment

FIXTURES = Path(__file__).resolve().parents[2] / 'tests/dispatch/fixtures/write/cases.json'
CASE_IDS = ('integration', 'verification_denied', 'user_conflict', 'stop_before_apply',
            'interrupted_recovery', 'recovery_user_edit', 'missing_termination',
            'scope_denial', 'admission_denied')
LIVE_PROBES = ('current_build_tool_mediation', 'protected_read_and_write_denial',
               'network_denial', 'contained_verifier', 'descendant_stop',
               'production_prepare_edit_check_integrate', 'model_routes_and_escalation')


def persist(stream, result):
    stream.seek(0)
    json.dump(result, stream, indent=2, allow_nan=False)
    stream.write('\n')
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def _manifest(primary):
    # Synthetic baseline label only: these probes do not claim Git preparation.
    return validate_write_manifest(dict(
        schema_version=2, package_id='synthetic-write-probe', task='Synthetic controller probe',
        task_class='implementation', workspace=str(primary), allowed_actions=['read', 'write'],
        required=True, timeout_seconds=60, max_attempts=1,
        verification=[dict(type='final_equals', expected='OK')],
        baseline=dict(commit='a'*40, overlays=[]), inputs=['src/a.py'],
        outputs=['src/'], checks=['synthetic'], selector_mode='rules'), primary)


def _probe(case_id, directory):
    directory.mkdir()
    primary, worker, stage = (directory / name for name in ('primary', 'worker', 'staged'))
    for root in (primary, worker):
        (root / 'src').mkdir(parents=True)
        (root / 'src/a.py').write_bytes(b'original')
    stage.mkdir()
    (primary / 'unrelated.txt').write_bytes(b'keep')
    (primary / 'docs/evidence').mkdir(parents=True)
    (primary / 'docs/evidence/sentinel.txt').write_bytes(b'protected')
    work = _manifest(primary)
    initial = {'src/a.py': digest(b'original')}
    record = dict(root=str(worker), staging=str(stage), primary=str(primary),
                  primary_identity=root_identity(primary), checks=work['checks'],
                  manifest_digest=manifest_digest(work), input_hashes=initial,
                  initial_inventory=initial, outputs=work['outputs'])
    (worker / 'src/a.py').write_bytes(b'changed')
    (worker / 'src/b.py').write_bytes(b'new')
    changes = collect_changes(record)
    # Controller-authored synthetic receipt, not execution of a contained check.
    checks = [dict(id='synthetic', status='passed', manifest_digest=manifest_digest(work),
                   changes_digest=manifest_digest({'changes': changes}))]
    journal = Journal(primary / '.dispatch-runs/probe/journal.jsonl')
    integration_status = recovery_status = None
    if case_id == 'admission_denied':
        admission = qualify_write_environment({}, {})
        status = admission['status'] if admission.get('live_writes') is False else 'unsafe_admission'
    elif case_id == 'scope_denial':
        rejected = []
        for target in ('../outside.txt', 'docs/evidence/sentinel.txt', 'scripts/dispatch/x.py'):
            try:
                validate_write_manifest(work | {'outputs': [target]}, primary)
            except ValueError:
                rejected.append(target)
        status = 'denied' if len(rejected) == 3 else 'scope_accepted'
    elif case_id == 'missing_termination':
        binding = dict(manifest_digest=manifest_digest(work), workspace=str(primary),
                       account_fingerprint='a'*64, thread_id='synthetic', turn_id='synthetic')
        journal.append(binding | dict(state='prepared', manifest=work, execution_audit_version=1))
        journal.append(binding | dict(state='dispatching'))
        journal.append(binding | dict(state='worker_command_started', call_id='unconfirmed'))
        journal.append(binding | dict(state='worker_quiescent', provider_status='completed'))
        grant = dict(schema_version=2, manifest_digest=manifest_digest(work), workspace=str(primary),
                     account_fingerprint='a'*64, expires_at=time.time()+60,
                     allowed_actions=['read', 'write'], max_attempts=1, dedicated_session=True)
        status = reconcile_write(journal, grant)['state']
    else:
        if case_id == 'verification_denied':
            checks[0]['status'] = 'failed'
        if case_id == 'user_conflict':
            (primary / 'src/a.py').write_bytes(b'user edit')
        checkpoint = None
        if case_id == 'stop_before_apply':
            checkpoint = lambda: True
        elif case_id in ('interrupted_recovery', 'recovery_user_edit'):
            checkpoint = lambda: any(row['state'] == 'applied' for row in journal.records())
        integration_status = integration.integrate(primary, record, changes, checks, journal,
                                                    checkpoint=checkpoint)['status']
        status = integration_status
        if case_id in ('integration', 'interrupted_recovery', 'recovery_user_edit'):
            if case_id == 'recovery_user_edit':
                (primary / 'src/a.py').write_bytes(b'user edit')
            recovery_status = integration.recover_integration(primary, journal)['status']
            status = recovery_status
    files = {}
    for name in ('src/a.py', 'src/b.py', 'unrelated.txt', 'docs/evidence/sentinel.txt'):
        path = primary / name
        files[name] = path.read_text(encoding='utf-8') if path.exists() else None
    return dict(status=status, integration_status=integration_status, recovery_status=recovery_status,
                files=files, lock_retained=journal.lock_path.exists(),
                journal_states=[row['state'] for row in journal.records()])


def qualify_writes(environment: dict, storage: Path) -> dict:
    """Run fixed offline fixtures in a new directory; preserve reports and journals.

    The only admitted environment is {'mode': 'synthetic'}. Storage must not
    exist. No preparation subprocess, model client, verifier process or network
    transport is constructed. Simulated receipts do not establish containment.
    """
    if environment != {'mode': 'synthetic'}:
        raise ValueError('Only the fixed synthetic qualification environment is supported')
    raw = FIXTURES.read_bytes()
    fixture = json.loads(raw)
    cases = fixture['cases']
    if (fixture.get('schema_version') != 1 or
            [case['id'] for case in cases] != list(CASE_IDS) or
            any(set(case) != {'id', 'status', 'a', 'b', 'lock_retained',
                             'integration_status', 'recovery_status'} for case in cases)):
        raise ValueError('Unexpected synthetic fixture set')
    storage = Path(storage).absolute()
    safe_root(storage.parent)
    storage.mkdir()
    receipts = storage / 'receipts'
    receipts.mkdir()
    result = dict(schema_version=1, state='pending', mode='synthetic', synthetic_pass=False,
                  live_writes=False, dispatched=False, adoption_qualified=False,
                  fixture_sha256=hashlib.sha256(raw).hexdigest(),
                  storage=str(storage), probes=[],
                  limitations=['Controller probes with constructed copy records and synthetic check receipts',
                               'No Git preparation, model execution or contained verifier execution',
                               'No current-build admission or OS containment evidence'],
                  live_probes=[dict(id=name, status='not_run', reason='Live admission remains blocked')
                               for name in LIVE_PROBES])
    with (storage / 'result.json').open('x+', encoding='utf-8') as stream:
        persist(stream, result)
        for case in cases:
            row = dict(id=case['id'], status='pending', passed=False)
            result['probes'].append(row)
            persist(stream, result)
            try:
                observed = _probe(case['id'], storage / case['id'])
                expected = dict(status=case['status'], integration_status=case['integration_status'],
                    recovery_status=case['recovery_status'], lock_retained=case['lock_retained'], files={
                    'src/a.py': case['a'], 'src/b.py': case['b'], 'unrelated.txt': 'keep',
                    'docs/evidence/sentinel.txt': 'protected'})
                passed = all(observed[key] == value for key, value in expected.items())
                row.update(status='passed' if passed else 'failed', passed=passed,
                           expected=expected, observed=observed)
            except Exception as exc:
                row.update(status='uncertain', error_type=type(exc).__name__)
                result['state'] = 'uncertain'
            # Each completed observation has its own exclusively created receipt.
            # A later aggregate rewrite cannot destroy these earlier outcomes.
            with (receipts / (case['id'] + '.json')).open('x', encoding='utf-8') as receipt:
                json.dump(row | dict(fixture_sha256=result['fixture_sha256'], mode='synthetic',
                                     live_writes=False, dispatched=False), receipt, indent=2)
                receipt.write('\n')
                receipt.flush()
                os.fsync(receipt.fileno())
            persist(stream, result)
            if result['state'] == 'uncertain':
                break
        if result['state'] != 'uncertain':
            result['synthetic_pass'] = all(row['passed'] for row in result['probes'])
            result['state'] = 'evaluated' if result['synthetic_pass'] else 'failed'
        result['not_run'] = list(CASE_IDS[len(result['probes']):])
        persist(stream, result)
    return result


def qualify_writes_output(output_path):
    """Reserve requested evidence before running probes; never replace old output."""
    path = Path(output_path).absolute()
    safe_root(path.parent)
    storage = path.with_name(path.name + '.fixtures')
    with path.open('x+', encoding='utf-8') as stream:
        pending = dict(state='pending', mode='synthetic', storage=str(storage),
                       synthetic_pass=False, live_writes=False, dispatched=False)
        persist(stream, pending)
        try:
            result = qualify_writes({'mode': 'synthetic'}, storage)
        except Exception as exc:
            result = pending | dict(state='uncertain', error_type=type(exc).__name__)
        persist(stream, result)
    return result
