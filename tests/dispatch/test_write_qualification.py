import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.dispatch import qualification
from scripts.dispatch.__main__ import main


class WriteQualification(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.output = self.root / 'report.json'

    def qualify(self):
        self.assertTrue(hasattr(qualification, 'qualify_writes'), 'missing planned runner')
        return qualification.qualify_writes({'mode': 'synthetic'}, self.root / 'fixtures')

    def test_real_controller_outcomes_without_process_or_network(self):
        with patch('subprocess.Popen', side_effect=AssertionError('process forbidden')), \
             patch('socket.socket', side_effect=AssertionError('network forbidden')):
            result = self.qualify()
        self.assertEqual(result['state'], 'evaluated', result)
        self.assertTrue(result['synthetic_pass'])
        self.assertFalse(result['live_writes'])
        self.assertFalse(result['dispatched'])
        rows = {row['id']: row for row in result['probes']}
        self.assertEqual(len(rows), 9)
        self.assertEqual(rows['integration']['observed']['files']['src/a.py'], 'changed')
        self.assertEqual(rows['user_conflict']['observed']['files']['src/a.py'], 'user edit')
        self.assertEqual(rows['interrupted_recovery']['observed']['status'], 'uncertain')
        self.assertTrue(rows['missing_termination']['observed']['lock_retained'])
        self.assertTrue(all(row['passed'] for row in rows.values()))
        self.assertTrue(all(row['status'] == 'not_run' for row in result['live_probes']))
        self.assertEqual(json.loads((self.root / 'fixtures/result.json').read_text()), result)

    def test_existing_storage_is_preserved(self):
        (self.root / 'fixtures').mkdir()
        (self.root / 'fixtures/keep').write_text('preserve')
        self.assertTrue(hasattr(qualification, 'qualify_writes'), 'missing planned runner')
        with self.assertRaises(FileExistsError):
            qualification.qualify_writes({'mode': 'synthetic'}, self.root / 'fixtures')
        self.assertEqual((self.root / 'fixtures/keep').read_text(), 'preserve')

    def test_caller_cannot_enable_a_live_environment(self):
        self.assertTrue(hasattr(qualification, 'qualify_writes'), 'missing planned runner')
        for environment in ({'mode': 'live'}, {'mode': 'synthetic', 'live_writes': True}):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                qualification.qualify_writes(environment, self.root / 'fixtures')
        self.assertFalse((self.root / 'fixtures').exists())

    def test_cli_persists_report_and_refuses_overwrite(self):
        args = ['qualify-writes', '--output', str(self.output)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
            before = self.output.read_bytes()
            self.assertEqual(main(args), 2)
        self.assertEqual(self.output.read_bytes(), before)
        self.assertTrue(json.loads(before)['synthetic_pass'])

    def test_wrong_controller_result_cannot_pass(self):
        self.assertTrue(hasattr(qualification, 'qualify_writes'), 'missing planned runner')
        with patch('scripts.dispatch.integration.integrate', return_value={'status': 'integrated'}):
            result = self.qualify()
        self.assertFalse(result['synthetic_pass'])
        self.assertFalse(result['probes'][0]['passed'])
        self.assertFalse(result['live_writes'])

    def test_unexpected_exception_keeps_durable_uncertain_result(self):
        self.assertTrue(hasattr(qualification, 'qualify_writes'), 'missing planned runner')
        with patch('scripts.dispatch.integration.integrate', side_effect=OSError('injected fault')):
            result = self.qualify()
        self.assertEqual(result['state'], 'uncertain')
        self.assertFalse(result['synthetic_pass'])
        self.assertEqual(result['probes'][0]['status'], 'uncertain')
        self.assertEqual(json.loads((self.root / 'fixtures/result.json').read_text()), result)

    def test_pending_report_exists_before_first_probe(self):
        self.assertTrue(hasattr(qualification, 'qualify_writes'), 'missing planned runner')
        from scripts.dispatch.integration import integrate
        seen = []
        def observe(*args, **kwargs):
            seen.append(json.loads((self.root / 'fixtures/result.json').read_text())['state'])
            return integrate(*args, **kwargs)
        with patch('scripts.dispatch.integration.integrate', side_effect=observe):
            self.qualify()
        self.assertEqual(seen[0], 'pending')

    def test_cli_failure_returns_nonzero_and_preserves_evidence(self):
        with patch('scripts.dispatch.integration.integrate', side_effect=OSError('injected fault')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['qualify-writes', '--output', str(self.output)]), 2)
        result = json.loads(self.output.read_text())
        self.assertEqual(result['state'], 'uncertain')
        self.assertFalse(result['synthetic_pass'])
        self.assertTrue(result['not_run'])

    def test_interruption_preserves_pending_record_without_replay(self):
        with patch('scripts.dispatch.integration.integrate', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.qualify()
        result = json.loads((self.root / 'fixtures/result.json').read_text())
        self.assertEqual(result['state'], 'pending')
        self.assertFalse(result['synthetic_pass'])
        self.assertEqual(len(result['probes']), 1)
        self.assertEqual(result['probes'][0]['status'], 'pending')
        with self.assertRaises(FileExistsError):
            self.qualify()

    def test_recovery_does_not_mask_wrong_integration_result(self):
        from scripts.dispatch.integration import integrate
        def wrong_status(*args, **kwargs):
            result = integrate(*args, **kwargs)
            return result | {'status': 'incorrect'}
        with patch('scripts.dispatch.integration.integrate', side_effect=wrong_status):
            result = self.qualify()
        self.assertFalse(result['synthetic_pass'])
        self.assertFalse(result['probes'][0]['passed'])

    def test_completed_receipt_survives_later_snapshot_failure(self):
        from scripts.dispatch.write_qualification import persist
        def fail_later(stream, result):
            if len(result.get('probes', [])) >= 2:
                stream.seek(0)
                stream.write('{incomplete')
                stream.truncate()
                stream.flush()
                raise OSError('snapshot write interrupted')
            return persist(stream, result)
        with patch('scripts.dispatch.write_qualification.persist', side_effect=fail_later):
            with self.assertRaises(OSError):
                self.qualify()
        receipt_path = self.root / 'fixtures/receipts/integration.json'
        self.assertTrue(receipt_path.is_file(), 'completed independent receipt was lost')
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt['id'], 'integration')
        self.assertTrue(receipt['passed'])
        self.assertEqual(receipt['observed']['files']['src/a.py'], 'changed')
