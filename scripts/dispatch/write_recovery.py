"""Inspect editing journals and primary bytes without replaying any operation."""
from pathlib import Path
import time

from .contracts import manifest_digest
from .integration import current_hash, validate
from .journal import JournalError
from .write_contracts import safe_root, safe_path, validate_write_manifest, validate_write_grant


def reconcile_write(journal, grant: dict) -> dict:
    try:
        primary = safe_root(Path(grant['workspace']))
        if journal.path.parent.parent != primary / '.dispatch-runs':
            raise ValueError('Journal must use the primary writer lock')
        safe_path(primary, journal.path.relative_to(primary).as_posix())
        rows = journal.records()
        source = next((r for r in rows if 'manifest' in r), None)
        if source is None:
            raise ValueError('Original editing manifest missing')
        work = validate_write_manifest(source['manifest'], primary)
        digest = manifest_digest(work)
        validate_write_grant(grant, work, grant['account_fingerprint'], time.time())
        binding = dict(manifest_digest=digest, workspace=str(primary),
                       account_fingerprint=grant['account_fingerprint'])
        if any(any(row.get(k) not in (None, value) for k, value in binding.items()) for row in rows):
            raise ValueError('Journal or account ownership mismatch')
        if source.get('account_fingerprint') != grant['account_fingerprint']:
            raise ValueError('Original account ownership missing')
        with journal.lock(recover=True):
            if journal.records() != rows:
                raise ValueError('Journal changed during lock acquisition')

            def finish(state, **fields):
                result = binding | dict(state=state, dispatched=False, replayed=False,
                                        task_verified=False, observed_route=None) | fields
                journal.append(result)
                return result

            if source.get('execution_audit_version') != 1:
                return finish('uncertain', reason='Legacy run lacks complete execution audit')
            commands = {}
            dispatch_pending = False
            active_thread = active_turn = None
            verifier_pending = False
            verified = None
            for row in rows:
                state = row.get('state')
                if state == 'dispatching':
                    dispatch_pending = True
                    active_thread, active_turn = row.get('thread_id'), row.get('turn_id')
                if state == 'running':
                    active_thread, active_turn = row.get('thread_id'), row.get('turn_id')
                if state in ('worker_command_started', 'worker_command_finished'):
                    identity = tuple(row.get(k) for k in ('thread_id', 'turn_id', 'call_id'))
                    if any(not isinstance(v, str) or not v for v in identity):
                        return finish('uncertain', reason='Command identity missing')
                    if state == 'worker_command_started':
                        if identity in commands:
                            return finish('uncertain', reason='Duplicate command intent')
                        commands[identity] = False
                    else:
                        if identity not in commands or commands[identity]:
                            return finish('uncertain', reason='Unmatched command receipt')
                        commands[identity] = row.get('termination_observed') is True
                if state == 'worker_quiescent':
                    if (row.get('thread_id') == active_thread and row.get('turn_id') == active_turn
                            and (row.get('provider_status') in ('completed', 'failed', 'interrupted')
                                 or row.get('terminal_confirmed') is True)):
                        dispatch_pending = False
                if state == 'verification_started':
                    verifier_pending, verified = True, None
                if state == 'verifying':
                    checks = row.get('checks')
                    verifier_pending = (not isinstance(checks, list) or not checks or
                                        any(c.get('execution_started') is not False and
                                            c.get('termination_observed') is not True for c in checks))
                    verified = row
            if dispatch_pending or not all(commands.values()) or verifier_pending:
                return finish('uncertain', reason='Execution termination evidence incomplete',
                              turn_pending=dispatch_pending, verifier_pending=verifier_pending,
                              pending_commands=[list(k) for k, value in commands.items() if not value])
            intent = next((row for row in reversed(rows) if row.get('state') == 'integrating'), None)
            if intent is None:
                return finish('reconciled', reason='Execution quiescent; isolated edits not integrated or verified')
            record, changes = intent['record'], intent['changes']
            validate(primary, record, changes, journal)
            if (record['manifest_digest'] != digest or record['checks'] != work['checks'] or
                    record['outputs'] != work['outputs'] or verified is None or
                    verified['seq'] >= intent['seq'] or verified.get('changes') != changes):
                return finish('uncertain', reason='Integration intent lacks matching verification')
            checks = verified['checks']
            changes_digest = manifest_digest({'changes': changes})
            if (len(checks) != len(work['checks']) or {c.get('id') for c in checks} != set(work['checks']) or
                    any(c.get('status') != 'passed' or c.get('manifest_digest') != digest or
                        c.get('changes_digest') != changes_digest for c in checks) or
                    not verified.get('verification') or
                    not all(c.get('passed') is True for c in verified['verification'])):
                return finish('uncertain', reason='Integration checks are incomplete or stale')
            states = {c['path']: current_hash(primary, c['path']) for c in changes}
            conflicts = [c['path'] for c in changes
                         if states[c['path']] not in (c['before_sha256'], c['after_sha256'])]
            pending = [c['path'] for c in changes if states[c['path']] != c['after_sha256']]
            changed = {c['path'] for c in changes}
            conflicts += [name for name, expected in record['input_hashes'].items()
                          if name not in changed and current_hash(primary, name) != expected]
            complete = not conflicts and not pending
            return finish('completed' if complete else 'uncertain', task_verified=complete,
                          integration={'status': 'integrated' if complete else 'uncertain',
                                       'pending': pending, 'conflicts': conflicts},
                          reason='Inspected current bytes; no writes or dispatch replayed')
    except (KeyError, TypeError, ValueError, OSError, JournalError) as exc:
        return {'state': 'blocked', 'reason': str(exc), 'dispatched': False, 'replayed': False}
