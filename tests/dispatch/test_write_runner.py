from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from helpers import FakeClient
import test_workspace
from scripts.dispatch.contracts import manifest_digest
from scripts.dispatch.journal import Journal
from scripts.dispatch.write_contracts import validate_write_manifest
from scripts.dispatch.workspace import collect_changes
from scripts.dispatch.write_runner import run_write_package


class EditingClient(FakeClient):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.closed = False
        self.broker = None

    def verify_thread(self, response, route):
        return asdict(route)

    def bind_broker(self, broker, stop):
        self.broker = broker
        self.stop = stop
        self.owner.roots.append(broker.root)
        self.owner.before.append((broker.root / 'src/a.py').read_text())

    def next_event(self, timeout):
        if self.broker and not self.stop.is_set():
            self.owner.assertEqual((self.owner.root / 'src/a.py').read_text(), 'original')
            result = self.broker.handle(dict(
                account_fingerprint=self.account, thread_id='thread-owned', turn_id='turn-1',
                call_id='write', operation='write',
                arguments={'path': 'src/a.py', 'text': 'changed'}), self.stop)
            self.owner.assertTrue(result['success'], result)
            self.broker.uncertain = self.owner.command_uncertain
        return super().next_event(timeout)

    def close(self):
        self.closed = True


class WriteRunner(unittest.TestCase):
    def test_stop_on_integration_intent_prevents_primary_mutation(self):
        append=self.journal.append
        def stop_on_intent(row):
            append(row)
            if row.get('state')=='integrating':self.stop.set()
        with patch.object(self.journal,'append',side_effect=stop_on_intent):
            self.assertEqual(self.run_it()['state'],'cancelled')
        self.assertEqual((self.root/'src/a.py').read_text(),'original')

    def test_grant_expiry_on_integration_intent_prevents_primary_mutation(self):
        append=self.journal.append
        expired=False
        real_time=time.time
        def expire_on_intent(row):
            nonlocal expired
            append(row)
            if row.get('state')=='integrating':expired=True
        with patch.object(self.journal,'append',side_effect=expire_on_intent), \
             patch('scripts.dispatch.write_runner.time.time',side_effect=lambda:real_time()+(1000 if expired else 0)):
            self.assertEqual(self.run_it()['state'],'uncertain')
        self.assertEqual((self.root/'src/a.py').read_text(),'original')

    git = test_workspace.Workspace.git

    def setUp(self):
        test_workspace.Workspace.setUp(self)
        self.work = validate_write_manifest(self.raw, self.root)
        self.grant = dict(schema_version=2, manifest_digest=manifest_digest(self.work),
            workspace=str(self.root), account_fingerprint='a'*64, expires_at=time.time()+600,
            allowed_actions=['read', 'write'], max_attempts=3, dedicated_session=True)
        self.journal = Journal(self.root / '.dispatch-runs/one/journal.jsonl')
        self.stop = threading.Event()
        self.clients, self.roots, self.before = [], [], []
        self.command_uncertain = False
        self.configure = lambda client: None
        self.check_count = 0
        self.services = dict(journal=self.journal, storage=self.storage,
            environment={'runtime': {}, 'checks': {}}, client_factory=self.factory)
        self.admission = patch('scripts.dispatch.write_runner.qualify_write_environment',
            return_value={'status': 'qualified', 'live_writes': True})
        self.admission.start()
        self.addCleanup(self.admission.stop)
        self.check_mock = patch('scripts.dispatch.write_runner.run_checks', side_effect=self.checks)
        self.check_mock.start()
        self.addCleanup(self.check_mock.stop)

    def factory(self, worker, environment):
        client = EditingClient(self)
        self.configure(client)
        self.clients.append(client)
        return client

    def checks(self, ids, record, environment, stop):
        self.check_count += 1
        self.assertTrue(self.clients[-1].closed)
        self.assertTrue(self.journal.lock_path.exists())
        self.assertEqual((self.root / 'src/a.py').read_text(), 'original')
        return [dict(id='unit', status='passed', manifest_digest=record['manifest_digest'],
                     changes_digest=manifest_digest({'changes': collect_changes(record)}),
                     termination_observed=True)]

    def run_it(self):
        self.grant['manifest_digest'] = manifest_digest(self.work)
        return run_write_package(self.work, self.grant, self.services, self.stop)

    def test_complete_integrates_only_after_checks_and_never_replays(self):
        (self.root / 'unrelated').write_text('dirty')
        result = self.run_it()
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual((self.root / 'src/a.py').read_text(), 'changed')
        self.assertEqual((self.root / 'unrelated').read_text(), 'dirty')
        self.assertEqual(self.check_count, 1)
        self.assertEqual(result['requested_route'], result['accepted_route'])
        self.assertIsNone(result['observed_route'])
        self.assertFalse(self.journal.lock_path.exists())
        self.assertEqual(self.run_it()['state'], 'completed')
        self.assertEqual(len(self.clients), 1)

    def test_successful_lifecycle_has_ordered_recovery_evidence(self):
        self.assertEqual(self.run_it()['state'], 'completed')
        rows = self.journal.records()
        self.assertEqual(rows[0].get('execution_audit_version'), 1)
        states = [r['state'] for r in rows]
        self.assertLess(states.index('worker_quiescent'), states.index('verification_started'))
        self.assertLess(states.index('verification_started'), states.index('verifying'))
        from scripts.dispatch.write_recovery import reconcile_write
        self.assertEqual(reconcile_write(self.journal, self.grant)['state'], 'completed')

    def test_unqualified_environment_never_creates_client(self):
        self.admission.stop()
        self.assertEqual(self.run_it()['state'], 'blocked')
        self.assertEqual(self.clients, [])

    def test_stop_before_dispatch(self):
        self.stop.set()
        self.assertEqual(self.run_it()['state'], 'cancelled')
        self.assertEqual(self.clients, [])

    def test_lost_turn_ack_is_uncertain_no_checks_no_replay(self):
        def configure(client):
            original = client.request
            def request(method, params, timeout=15):
                if method == 'turn/start':
                    raise TimeoutError('Lost acknowledgement')
                return original(method, params, timeout)
            client.request = request
        self.configure = configure
        self.assertEqual(self.run_it()['state'], 'uncertain')
        self.assertEqual(self.check_count, 0)
        self.assertTrue(self.journal.lock_path.exists())
        self.assertTrue(self.clients[0].closed)
        self.assertEqual(self.run_it()['state'], 'uncertain')
        self.assertEqual(len(self.clients), 1)

    def test_uncertain_command_never_checks_or_integrates(self):
        self.command_uncertain = True
        self.assertEqual(self.run_it()['state'], 'uncertain')
        self.assertEqual(self.check_count, 0)
        self.assertEqual((self.root / 'src/a.py').read_text(), 'original')
        self.assertTrue(self.journal.lock_path.exists())

    def test_stop_in_flight_interrupts_and_preserves_primary(self):
        self.configure = lambda client: setattr(client, 'on_start', self.stop.set)
        self.assertEqual(self.run_it()['state'], 'cancelled')
        self.assertIn('turn/interrupt', self.clients[0].methods)
        self.assertTrue(self.clients[0].closed)
        self.assertEqual(self.check_count, 0)

    def test_stale_check_binding_blocks_integration(self):
        with patch('scripts.dispatch.write_runner.run_checks', return_value=[
                dict(id='unit', status='passed', manifest_digest='0'*64, changes_digest='0'*64)]):
            self.assertEqual(self.run_it()['state'], 'blocked')
        self.assertEqual((self.root / 'src/a.py').read_text(), 'original')

    def test_mutation_after_check_blocks_integration(self):
        def checks(*args):
            rows = self.checks(*args)
            (Path(args[1]['root']) / 'src/a.py').write_text('unverified')
            return rows
        with patch('scripts.dispatch.write_runner.run_checks', side_effect=checks):
            self.assertEqual(self.run_it()['state'], 'blocked')
        self.assertEqual((self.root / 'src/a.py').read_text(), 'original')

    def test_quality_retry_preserves_copy_and_escalates(self):
        self.work['max_attempts'] = 2
        def configure(client):
            if not self.clients:
                client.answers = ['WRONG']
        self.configure = configure
        self.assertEqual(self.run_it()['state'], 'completed')
        self.assertEqual(self.before, ['original', 'changed'])
        self.assertEqual(self.roots[0], self.roots[1])
        self.assertEqual([c.turn_params[0]['effort'] for c in self.clients], ['medium', 'high'])
        self.assertTrue(all(c.closed for c in self.clients))

    def test_manual_failure_does_not_escalate(self):
        self.work.update(mode='manual', manual_route={'model': 'gpt-6-sol', 'effort': 'medium'}, max_attempts=3)
        self.configure = lambda client: setattr(client, 'answers', ['WRONG'])
        self.assertEqual(self.run_it()['state'], 'failed')
        self.assertEqual(len(self.clients), 1)

    def test_conflicting_primary_preserved(self):
        def checks(*args):
            rows = self.checks(*args)
            (self.root / 'src/a.py').write_text('user change')
            return rows
        with patch('scripts.dispatch.write_runner.run_checks', side_effect=checks):
            self.assertEqual(self.run_it()['state'], 'blocked')
        self.assertEqual((self.root / 'src/a.py').read_text(), 'user change')

    def test_account_mismatch_never_starts_thread(self):
        self.configure = lambda client: setattr(client, 'account', 'b'*64)
        self.assertEqual(self.run_it()['state'], 'blocked')
        self.assertEqual(self.clients[0].methods, [])
        self.assertTrue(self.clients[0].closed)

    def test_competing_writer_prevents_dispatch(self):
        other = Journal(self.root / '.dispatch-runs/other/journal.jsonl')
        with other.lock():
            self.assertEqual(self.run_it()['state'], 'blocked')
            other.append({'state': 'completed'})
        self.assertEqual(self.clients, [])

    def test_malformed_ack_keeps_uncertain_run(self):
        def configure(client):
            original = client.request
            def request(method, params, timeout=15):
                if method == 'turn/start':
                    return {'turn': None}
                return original(method, params, timeout)
            client.request = request
        self.configure = configure
        self.assertEqual(self.run_it()['state'], 'uncertain')
        self.assertTrue(self.clients[0].closed)
        self.assertTrue(self.journal.lock_path.exists())

    def test_verifier_unconfirmed_termination_keeps_lock(self):
        def checks(*args):
            rows = self.checks(*args)
            rows[0].update(status='failed', termination_observed=False, exit_code=None)
            return rows
        with patch('scripts.dispatch.write_runner.run_checks', side_effect=checks):
            self.assertEqual(self.run_it()['state'], 'uncertain')
        self.assertTrue(self.journal.lock_path.exists())
        self.assertEqual((self.root / 'src/a.py').read_text(), 'original')

    def test_verifier_exception_after_launch_cannot_clear_lock(self):
        def checks(*args):
            rows = self.checks(*args)
            rows[0].pop('termination_observed')
            rows[0].update(status='failed', execution_started=True, exit_code=None)
            return rows
        with patch('scripts.dispatch.write_runner.run_checks', side_effect=checks):
            self.assertEqual(self.run_it()['state'], 'uncertain')
        self.assertTrue(self.journal.lock_path.exists())

    def test_expiry_during_metadata_never_starts_thread(self):
        def configure(client):
            original = client.usage
            def usage():
                self.grant['expires_at'] = 0
                return original()
            client.usage = usage
        # A wall-clock expiry applies to the immutable authority snapshot too.
        with patch('scripts.dispatch.write_runner.time.time', side_effect=lambda:
                   1e20 if self.grant['expires_at'] == 0 else 1):
            self.configure = configure
            self.assertEqual(self.run_it()['state'], 'blocked')
        self.assertEqual(self.clients[0].methods, [])

    @unittest.skipUnless(os.name == 'nt' and os.environ.get('ABRAMS_WSL_QUALIFY') == '1',
                         'Explicit Windows/WSL synthetic qualification only')
    def test_live_wsl_check_then_real_integration(self):
        from scripts.dispatch.wsl_adapter import runtime_files
        self.check_mock.stop()
        self.services['environment'] = dict(
            runtime={'distro': 'Ubuntu', 'files': {
                key: hashlib.sha256(path.read_bytes()).hexdigest()
                for key, path in runtime_files().items()}},
            checks={'unit': {'platform': 'linux', 'argv': ['/usr/bin/python3', '-I', '-c',
                'from pathlib import Path; assert Path("/work/src/a.py").read_text()=="changed"; print("VERIFIED")'],
                'expected_stdout': 'VERIFIED\n'}})
        result = self.run_it()
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual((self.root / 'src/a.py').read_text(), 'changed')
        verification = next(row for row in self.journal.records() if row['state'] == 'verifying')
        self.assertTrue(verification['checks'][0]['termination_observed'])
        self.assertEqual(verification['checks'][0]['status'], 'passed')
