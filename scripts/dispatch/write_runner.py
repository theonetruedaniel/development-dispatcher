"""Serial editing lifecycle. Services and admission remain controller-owned."""
import copy
from dataclasses import asdict
from pathlib import Path
import time

from .contracts import manifest_digest
from .integration import integrate
from .journal import JournalError
from .policy import POLICY_VERSION
from .protocol import ProtocolError
from .runner import _interrupt, _wait_turn, final_text, verify
from .selector import propose_class, resolve_proposal
from .verification import run_checks
from .workspace import prepare_copy, collect_changes
from .write_contracts import safe_path, safe_root, validate_write_manifest, validate_write_grant
from .write_live import TOOL_SPEC
from .write_sandbox import qualify_write_environment
from .write_tools import WriteToolBroker


def run_write_package(manifest: dict, grant: dict, services: dict, stop) -> dict:
    """Run one granted package; an existing journal is never automatically replayed.

    client_factory(worker, environment) is a trusted transport constructor, not
    manifest data. Production admission is independently checked before it runs.
    """
    pending = False
    integration_started = False
    client = broker = None
    try:
        primary = safe_root(Path(grant['workspace']))
        work = validate_write_manifest(manifest, primary)
        authority = validate_write_grant(grant, work, grant['account_fingerprint'], time.time())
        digest = manifest_digest(work)
        journal = services['journal']
        if journal.path.parent.parent != primary / '.dispatch-runs':
            raise ValueError('Journal must use the primary writer lock')
        safe_path(primary, journal.path.relative_to(primary).as_posix())
        old = journal.records()
        if any(row.get('manifest_digest') not in (None, digest) for row in old):
            raise ValueError('Existing run binds another manifest')
        if old:
            if old[-1].get('state') == 'completed' and old[-1].get('manifest_digest') == digest:
                return old[-1]
            return {'state': 'uncertain', 'reason': 'Existing run requires reconciliation; no replay'}
        environment = copy.deepcopy(services['environment'])
        deadline = time.monotonic() + work['timeout_seconds']
        base = dict(manifest_digest=digest, package_id=work['package_id'], workspace=str(primary),
                    account_fingerprint=authority['account_fingerprint'], policy_version=POLICY_VERSION,
                    observed_route=None)

        def record(state, **fields):
            row = base | dict(state=state) | fields
            journal.append(row)
            return row

        def checkpoint():
            validate_write_grant(authority, work, authority['account_fingerprint'], time.time())
            return stop.is_set() or time.monotonic() >= deadline

        def close_client():
            nonlocal client, pending
            if broker is not None:
                broker.deadline = 0  # Revoke callbacks before transport teardown.
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pending = True
                    raise
                client = None

        with journal.lock():
            try:
                if journal.records():
                    return {'state': 'uncertain', 'reason': 'Run changed during lock acquisition'}
                record('prepared', manifest=work, execution_audit_version=1)
                if checkpoint():
                    return record('cancelled', reason='Stopped before preparation')
                admission = qualify_write_environment(environment, {'primary': str(primary)})
                if admission.get('status') != 'qualified' or admission.get('live_writes') is not True:
                    return record('blocked', admission=admission, reason='Editing environment not qualified')
                copy_record = prepare_copy(work, primary, Path(services['storage']))
                record('prepared', copy_record=copy_record, admission=admission)
                history = []
                for attempt in range(work['max_attempts']):
                    if checkpoint():
                        return record('cancelled', reason='Stopped before dispatch')
                    client = services['client_factory'](Path(copy_record['root']), environment)
                    broker = None
                    client.initialize()
                    account = client.account_fingerprint()
                    validate_write_grant(authority, work, account, time.time())
                    catalog, usage = client.catalog(), client.usage()
                    proposal = (propose_class(work, environment.get('selector', {}), stop=stop,
                                deadline_seconds=min(600, max(0, deadline-time.monotonic())))
                                if work['selector_mode'] != 'rules' else {})
                    decision = resolve_proposal(work, proposal, catalog, usage, history)
                    record('prepared', attempt=attempt+1, decision=asdict(decision), selector=proposal)
                    if decision.action != 'dispatch':
                        close_client()
                        return record('deferred' if decision.action == 'defer' else 'blocked',
                                      reason=decision.reason)
                    if checkpoint():
                        close_client()
                        return record('cancelled', reason='Stopped during metadata lookup')
                    base.update(requested_route=asdict(decision.route))
                    for key in ('accepted_route', 'thread_id', 'turn_id'):
                        base.pop(key, None)
                    record('dispatching', operation='thread/start')
                    pending = True
                    started = client.request('thread/start', dict(
                        cwd=copy_record['root'], model=decision.route.model, modelProvider='openai',
                        permissions='dispatch', approvalPolicy='on-request', approvalsReviewer='user',
                        ephemeral=True, environments=[], dynamicTools=[copy.deepcopy(TOOL_SPEC)],
                        config={'model_reasoning_effort': decision.route.effort},
                        baseInstructions='Use only dispatch_tool within the assigned copy. '
                            'Commands run in Linux at /work. You cannot change grants, checks or integration.'),
                        min(15, max(.001, deadline-time.monotonic())))
                    thread = started.get('thread') if isinstance(started, dict) else None
                    thread_id = thread.get('id') if isinstance(thread, dict) else None
                    if not isinstance(thread_id, str) or not thread_id:
                        raise ProtocolError('Thread acknowledgement missing')
                    base['thread_id'] = thread_id
                    pending = False
                    base['accepted_route'] = client.verify_thread(started, decision.route)
                    if checkpoint():
                        close_client()
                        return record('cancelled', reason='Stopped before turn')
                    prompt = work['task']
                    if history:
                        prompt += '\nPrevious required verification failed. Preserve useful edits in this copy and correct the result within the same scope.'
                    record('dispatching', operation='turn/start')
                    pending = True
                    response = client.request('turn/start', dict(threadId=thread_id,
                        model=decision.route.model, effort=decision.route.effort,
                        input=[{'type': 'text', 'text': prompt}]),
                        min(15, max(.001, deadline-time.monotonic())))
                    acknowledgement = response.get('turn') if isinstance(response, dict) else None
                    turn_id = acknowledgement.get('id') if isinstance(acknowledgement, dict) else None
                    if not isinstance(turn_id, str) or not turn_id:
                        raise ProtocolError('Turn acknowledgement missing')
                    base['turn_id'] = turn_id
                    broker = WriteToolBroker(work, authority, copy_record, dict(
                        account_fingerprint=account, thread_id=thread_id, turn_id=turn_id,
                        deadline=deadline), environment['runtime'], audit=record)
                    client.bind_broker(broker, stop)
                    record('running')
                    turn, reason = _wait_turn(client, thread_id, turn_id, deadline, stop)
                    if reason:
                        broker.deadline = 0
                        confirmed = _interrupt(client, thread_id, turn_id)
                        pending = not confirmed or broker.uncertain
                        close_client()
                        if not pending:
                            record('worker_quiescent', terminal_confirmed=True)
                        return record('uncertain' if pending else reason,
                                      interruption_confirmed=confirmed, reason=reason)
                    pending = broker.uncertain
                    close_client()
                    if pending:
                        return record('uncertain', reason='Command termination not confirmed')
                    record('worker_quiescent', provider_status=turn['status'])
                    if turn['status'] != 'completed':
                        return record('failed' if turn['status'] == 'failed' else 'cancelled',
                                      reason='Provider terminal state')
                    if checkpoint():
                        return record('cancelled', reason='Stopped before verification')
                    changes = collect_changes(copy_record)
                    final_checks = verify(dict(work, workspace=copy_record['root']), final_text(turn))
                    check_environment = dict(environment, deadline=min(deadline,
                        time.monotonic()+max(0, authority['expires_at']-time.time())))
                    record('verification_started', changes=changes)
                    checks = run_checks(work['checks'], copy_record, check_environment, stop)
                    record('verifying', changes=changes, verification=final_checks, checks=checks)
                    if any(c.get('termination_observed') is False or
                           (c.get('execution_started') is True and
                            c.get('termination_observed') is not True) for c in checks):
                        pending = True
                        return record('uncertain', reason='Verifier termination not confirmed')
                    if checkpoint():
                        return record('cancelled', reason='Stopped during verification')
                    if collect_changes(copy_record) != changes:
                        return record('blocked', reason='Changes changed after verification')
                    change_digest = manifest_digest({'changes': changes})
                    if (len(checks) != len(work['checks']) or
                            {c.get('id') for c in checks} != set(work['checks']) or
                            any(c.get('manifest_digest') != digest or
                                c.get('changes_digest') != change_digest for c in checks)):
                        return record('blocked', reason='Checks do not bind this changeset')
                    trusted_pass = all(c.get('status') == 'passed' for c in checks)
                    if trusted_pass and all(c['passed'] for c in final_checks):
                        if checkpoint():
                            return record('cancelled', reason='Stopped before integration')
                        integration_started = True
                        result = integrate(primary, copy_record, changes, checks, journal,
                                           checkpoint=checkpoint)
                        integration_started = False
                        state = {'integrated': 'completed', 'conflict': 'blocked',
                                 'uncertain': 'uncertain','cancelled':'cancelled'}[result['status']]
                        return record(state, integration=result, copy_record=copy_record)
                    quality_failure = trusted_pass or all(
                        c.get('status') == 'passed' or
                        (c.get('termination_observed') is True and type(c.get('exit_code')) is int
                         and c['exit_code'] != 0) for c in checks)
                    if not quality_failure or work['mode'] == 'manual':
                        return record('failed', reason='Verification failed; no automatic retry')
                    failure = dict(package_id=work['package_id'], outcome='quality_failure',
                                   verified=True, effort=decision.route.effort)
                    history.append(failure)
                    record('prepared', attempt_outcome=failure)
                return record('failed', reason='Verification failed; attempt limit reached')
            except (KeyError, TypeError, ValueError, OSError, ProtocolError, TimeoutError) as exc:
                try:
                    close_client()
                except Exception:
                    pending = True
                return record('uncertain' if pending or integration_started else 'blocked', reason=str(exc))
            finally:
                close_client()
    except (KeyError, TypeError, ValueError, OSError, JournalError, ProtocolError, TimeoutError) as exc:
        return {'state': 'uncertain' if pending or integration_started else 'blocked', 'reason': str(exc)}
