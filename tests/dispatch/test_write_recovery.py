from pathlib import Path
import time
import unittest

import test_workspace
from scripts.dispatch.contracts import manifest_digest
from scripts.dispatch.journal import Journal
from scripts.dispatch.workspace import prepare_copy, collect_changes
from scripts.dispatch.write_contracts import validate_write_manifest
from scripts.dispatch.write_recovery import reconcile_write


class WriteRecovery(unittest.TestCase):
    git = test_workspace.Workspace.git

    def setUp(self):
        test_workspace.Workspace.setUp(self)
        self.work = validate_write_manifest(self.raw, self.root)
        self.digest = manifest_digest(self.work)
        self.grant = dict(schema_version=2, manifest_digest=self.digest,
            workspace=str(self.root), account_fingerprint='a'*64, expires_at=time.time()+600,
            allowed_actions=['read', 'write'], max_attempts=1, dedicated_session=True)
        self.journal = Journal(self.root / '.dispatch-runs/recovery/journal.jsonl')
        self.record = prepare_copy(self.work, self.root, self.storage)
        self.base_row = dict(manifest_digest=self.digest, workspace=str(self.root),
                             account_fingerprint='a'*64, thread_id='thread', turn_id='turn')
        self.row('prepared', manifest=self.work, copy_record=self.record, execution_audit_version=1)
        self.row('dispatching', operation='turn/start')
        self.row('running')

    def row(self, state, **fields):
        self.journal.append(self.base_row | dict(state=state) | fields)

    def integrate_intent(self):
        (Path(self.record['root']) / 'src/a.py').write_text('changed')
        changes = collect_changes(self.record)
        self.row('worker_quiescent', provider_status='completed')
        self.row('verification_started')
        self.row('verifying', changes=changes, verification=[{'passed': True}], checks=[dict(
            id='unit', status='passed', manifest_digest=self.digest,
            changes_digest=manifest_digest({'changes': changes}), termination_observed=True)])
        self.row('integrating', record=self.record, changes=changes)

    def test_no_quiescence_keeps_uncertain_lock_and_does_not_edit(self):
        result = reconcile_write(self.journal, self.grant)
        self.assertEqual(result['state'], 'uncertain', result)
        self.assertTrue(self.journal.lock_path.exists())
        self.assertEqual((self.root / 'src/a.py').read_text(), 'original')

    def test_quiescent_worker_can_release_without_claiming_completion(self):
        self.row('worker_quiescent', provider_status='completed')
        result = reconcile_write(self.journal, self.grant)
        self.assertEqual(result['state'], 'reconciled', result)
        self.assertFalse(self.journal.lock_path.exists())
        self.assertEqual((self.root / 'src/a.py').read_text(), 'original')
        self.assertFalse(result['task_verified'])

    def test_unconfirmed_command_overrides_turn_completion(self):
        self.row('worker_command_started', call_id='one')
        self.row('worker_quiescent', provider_status='completed')
        self.assertEqual(reconcile_write(self.journal, self.grant)['state'], 'uncertain')
        self.assertTrue(self.journal.lock_path.exists())

    def test_confirmed_command_is_bound_to_exact_session(self):
        self.row('worker_command_started', call_id='one')
        self.row('worker_command_finished', call_id='one', termination_observed=True, turn_id='other')
        self.row('worker_quiescent', provider_status='completed')
        self.assertEqual(reconcile_write(self.journal, self.grant)['state'], 'uncertain')

    def test_unfinished_verifier_remains_uncertain(self):
        self.row('worker_quiescent', provider_status='completed')
        self.row('verification_started')
        self.assertEqual(reconcile_write(self.journal, self.grant)['state'], 'uncertain')

    def test_partial_application_never_replays(self):
        self.integrate_intent()
        result = reconcile_write(self.journal, self.grant)
        self.assertEqual(result['state'], 'uncertain')
        self.assertEqual(result['integration']['pending'], ['src/a.py'])
        self.assertEqual((self.root / 'src/a.py').read_text(), 'original')

    def test_completed_application_confirmed_from_actual_bytes(self):
        self.integrate_intent()
        (self.root / 'src/a.py').write_text('changed')
        result = reconcile_write(self.journal, self.grant)
        self.assertEqual(result['state'], 'completed', result)
        self.assertFalse(self.journal.lock_path.exists())

    def test_user_change_preserved_and_reported(self):
        self.integrate_intent()
        (self.root / 'src/a.py').write_text('user version')
        result = reconcile_write(self.journal, self.grant)
        self.assertEqual(result['state'], 'uncertain')
        self.assertEqual(result['integration']['conflicts'], ['src/a.py'])
        self.assertEqual((self.root / 'src/a.py').read_text(), 'user version')

    def test_foreign_grant_does_not_mutate_journal(self):
        before = self.journal.path.read_bytes()
        self.grant['account_fingerprint'] = 'b'*64
        self.assertEqual(reconcile_write(self.journal, self.grant)['state'], 'blocked')
        self.assertEqual(self.journal.path.read_bytes(), before)
