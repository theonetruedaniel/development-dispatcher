import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from scripts.dispatch.journal import Journal
from scripts.dispatch.write_services import build_write_services, qualified_write_client


class WriteServices(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'environment.json'
        self.journal = Journal(self.root / '.dispatch-runs/one/journal.jsonl')

    def test_missing_environment_never_launches_or_creates_storage(self):
        with patch('scripts.dispatch.write_services.ENVIRONMENT_PATH', self.path), \
                patch('subprocess.Popen', side_effect=AssertionError('spawn')):
            with self.assertRaisesRegex(ValueError, 'write environment'):
                build_write_services(self.root, self.journal)
        self.assertFalse(self.path.exists())

    def test_fixed_controller_environment_and_separate_storage(self):
        environment = {'schema_version': 1, 'pin': {}, 'runtime': {}, 'checks': {}, 'selector': {}}
        self.path.write_text(json.dumps(environment))
        with patch('scripts.dispatch.write_services.ENVIRONMENT_PATH', self.path):
            services = build_write_services(self.root, self.journal)
        self.assertEqual(services['environment'], environment)
        self.assertIs(services['client_factory'], qualified_write_client)
        self.assertFalse(services['storage'].is_relative_to(self.root))

    def test_caller_flag_cannot_admit_factory(self):
        with patch('subprocess.Popen', side_effect=AssertionError('spawn')):
            with self.assertRaisesRegex(ValueError, 'not qualified'):
                qualified_write_client(self.root, {'qualified': True, 'live_writes': True})

    def test_unknown_environment_fields_rejected(self):
        self.path.write_text(json.dumps({'schema_version': 1, 'pin': {}, 'runtime': {},
                                        'checks': {}, 'selector': {}, 'command': 'anything'}))
        with patch('scripts.dispatch.write_services.ENVIRONMENT_PATH', self.path):
            with self.assertRaisesRegex(ValueError, 'environment'):
                build_write_services(self.root, self.journal)

    def test_factory_binds_owned_host_and_cleans_failed_client(self):
        host=Mock(url='http://127.0.0.1:12345')
        probe=Mock();probe.request.return_value={'config':{'mcp_servers':{}}}
        client=Mock();client.initialize.side_effect=ValueError('configuration rejected')
        with patch('scripts.dispatch.write_services.qualify_write_environment',
                   return_value={'status':'qualified','live_writes':True}), \
             patch('scripts.dispatch.write_services.shutil.which',return_value=str(self.root/'codex.exe')), \
             patch('scripts.dispatch.write_services.verify_pin'), \
             patch('scripts.dispatch.write_services.inspect_runtime'), \
             patch('scripts.dispatch.write_services.ProtocolClient',return_value=probe), \
             patch('scripts.dispatch.write_services.CodeHost',return_value=host) as factory, \
             patch('scripts.dispatch.write_services.WriteClient',return_value=client) as create:
            with self.assertRaisesRegex(ValueError,'configuration rejected'):
                qualified_write_client(self.root,{'pin':{'code_host_sha256':'a'*64},'runtime':{}})
            factory.assert_called_once_with(self.root/'codex-code-mode-host.exe','a'*64)
            args=create.call_args.args[0]
            self.assertEqual(args[args.index('--code-mode-host')+1],host.url)
            self.assertIs(create.call_args.kwargs['host'],host)
            host.close.assert_called_once()
            client.close.assert_called_once()
