import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from helpers import catalog
from test_write_contracts import work
from scripts.dispatch.__main__ import main
from scripts.dispatch.contracts import manifest_digest
from scripts.dispatch.write_contracts import validate_write_manifest


class WriteCli(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.work = validate_write_manifest(work(self.root), self.root)
        self.grant = dict(schema_version=2, manifest_digest=manifest_digest(self.work),
            workspace=str(self.root), account_fingerprint='a'*64, expires_at=time.time()+600,
            allowed_actions=['read', 'write'], max_attempts=1, dedicated_session=True)
        self.manifest_path = self.root / 'manifest.json'
        self.grant_path = self.root / 'grant.json'
        self.catalog_path = self.root / 'catalog.json'
        self.catalog_path.write_text(json.dumps(catalog()))

    def invoke(self, command='execute'):
        self.manifest_path.write_text(json.dumps(self.work))
        self.grant_path.write_text(json.dumps(self.grant))
        args = [command, '--manifest', str(self.manifest_path)]
        args += (['--catalog', str(self.catalog_path)] if command == 'preview' else
                 ['--authorization', str(self.grant_path)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(args)
        return code, json.loads(out.getvalue())

    def test_schema_two_preview_is_offline(self):
        with patch('subprocess.Popen', side_effect=AssertionError('spawn')):
            code, result = self.invoke('preview')
        self.assertEqual(code, 0, result)
        self.assertEqual(result['decision']['route']['model'], 'gpt-6-sol')
        self.assertFalse(result['dispatched'])

    def test_write_execution_uses_separate_services_and_lifecycle(self):
        services = {'controller': 'fixture'}
        with patch('scripts.dispatch.__main__.build_write_services', return_value=services) as build, \
                patch('scripts.dispatch.__main__.run_write_package', return_value={'state': 'completed'}) as run, \
                patch('scripts.dispatch.__main__.qualified_client', side_effect=AssertionError('read lane')):
            code, result = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertEqual(run.call_args.args[:3], (self.work, self.grant, services))
        self.assertEqual(build.call_args.args[0], self.root)
        self.assertTrue(result['run_id'].startswith('edit-'))

    def test_invalid_grant_never_builds_services(self):
        self.grant['expires_at'] = 0
        with patch('scripts.dispatch.__main__.build_write_services') as build:
            code, result = self.invoke()
        self.assertEqual(code, 2)
        build.assert_not_called()

    def test_missing_admission_cannot_launch(self):
        with patch('subprocess.Popen', side_effect=AssertionError('spawn')):
            code, result = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn('write environment', result['reason'].lower())

    def test_non_object_manifest_reports_blocked(self):
        self.work = []
        for command in ('preview', 'execute'):
            with self.subTest(command=command):
                code, result = self.invoke(command)
                self.assertEqual(code, 2)
                self.assertEqual(result['state'], 'blocked')

    def test_editing_reconciliation_uses_local_receipts_without_launch(self):
        from scripts.dispatch.journal import Journal
        journal = Journal(self.root / '.dispatch-runs/fixture/journal.jsonl')
        journal.append(dict(state='prepared', manifest=self.work, manifest_digest=manifest_digest(self.work),
            workspace=str(self.root), account_fingerprint='a'*64, execution_audit_version=1))
        self.grant_path.write_text(json.dumps(self.grant))
        out = io.StringIO()
        with patch('subprocess.Popen', side_effect=AssertionError('spawn')), contextlib.redirect_stdout(out):
            code = main(['reconcile', '--run', 'fixture', '--workspace', str(self.root),
                         '--authorization', str(self.grant_path)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())['state'], 'reconciled')
