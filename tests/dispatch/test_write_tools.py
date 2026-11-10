import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_write_contracts import work
from scripts.dispatch.contracts import manifest_digest
from scripts.dispatch.write_contracts import validate_write_manifest
from scripts.dispatch.write_tools import WriteToolBroker


class WriteTools(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.primary = base/'primary'; self.primary.mkdir()
        self.worker = base/'worker'; (self.worker/'src').mkdir(parents=True)
        (self.worker/'src/a.py').write_text('original')
        self.manifest = validate_write_manifest(work(self.primary), self.primary)
        self.grant = dict(schema_version=2,manifest_digest=manifest_digest(self.manifest),
            workspace=str(self.primary),account_fingerprint='f'*64,expires_at=time.time()+60,
            allowed_actions=['read','write'],max_attempts=1,dedicated_session=True)
        self.record = dict(root=str(self.worker),primary=str(self.primary),
                           manifest_digest=manifest_digest(self.manifest),outputs=['src/'])
        self.session = dict(account_fingerprint='f'*64,thread_id='thread',turn_id='turn',
                            deadline=time.monotonic()+60)
        self.broker = WriteToolBroker(self.manifest,self.grant,self.record,self.session,{})
        self.serial = 0

    def request(self, operation, arguments, **override):
        self.serial += 1
        return dict(account_fingerprint='f'*64,thread_id='thread',turn_id='turn',
                    call_id=str(self.serial),operation=operation,arguments=arguments) | override

    def call(self, operation, arguments, **override):
        return self.broker.handle(self.request(operation,arguments,**override),threading.Event())

    def test_read_write_list_delete_and_primary_unchanged(self):
        self.assertEqual(self.call('read',{'path':'src/a.py'})['text'],'original')
        self.assertTrue(self.call('write',{'path':'src/b.py','text':'new'})['success'])
        self.assertEqual(self.call('list',{'path':'src'})['entries'],['src/a.py','src/b.py'])
        self.assertTrue(self.call('delete',{'path':'src/b.py'})['success'])
        self.assertFalse((self.worker/'src/b.py').exists())
        self.assertEqual(list(self.primary.iterdir()),[])

    def test_identity_expiry_and_duplicate_calls(self):
        for override in ({'thread_id':'other'},{'turn_id':'other'}, {'account_fingerprint':'a'*64}):
            self.assertFalse(self.call('write',{'path':'src/a.py','text':'bad'},**override)['success'])
        request = self.request('write',{'path':'src/a.py','text':'ok'})
        self.assertTrue(self.broker.handle(request,threading.Event())['success'])
        self.assertFalse(self.broker.handle(request,threading.Event())['success'])
        with patch('scripts.dispatch.write_tools.time.time',return_value=self.grant['expires_at']+1):
            self.assertFalse(self.call('write',{'path':'src/a.py','text':'expired'})['success'])
        self.assertEqual((self.worker/'src/a.py').read_text(),'ok')

    def test_scope_and_unknown_fields(self):
        for path in ('../outside','AGENTS.md','.git/config','auth.json','other/file.py'):
            self.assertFalse(self.call('write',{'path':path,'text':'bad'})['success'])
        self.assertFalse(self.call('command',{'argv':['/bin/bash'],'mounts':['/']})['success'])
        self.assertFalse(self.call('unknown',{})['success'])
        self.assertFalse(self.call('read',{'path':'src/a.py','grant':{}})['success'])
        (self.worker/'other').mkdir()
        self.assertFalse(self.call('list',{'path':'other'})['success'])

    def test_command_uses_bound_root_and_uncertain_result_blocks_more_work(self):
        with patch('scripts.dispatch.write_tools.linux_path',return_value='/mnt/c/test/worker'), \
             patch('scripts.dispatch.write_tools.run_command',return_value={
                 'status':'uncertain','termination_observed':False}) as run:
            result=self.call('command',{'argv':['/usr/bin/python3','-c','pass']})
        self.assertFalse(result['success'])
        self.assertEqual(run.call_args.args[0]['root'],'/mnt/c/test/worker')
        self.assertLessEqual(run.call_args.args[0]['timeout_seconds'],60)
        self.assertFalse(self.call('write',{'path':'src/a.py','text':'bad'})['success'])

    def test_stop_and_changed_worker_identity(self):
        stop=threading.Event();stop.set()
        self.assertFalse(self.broker.handle(self.request('write',{'path':'src/a.py','text':'bad'}),stop)['success'])
        self.worker.rename(self.worker.with_name('old'))
        self.worker.mkdir()
        self.assertFalse(self.call('write',{'path':'src/a.py','text':'bad'})['success'])

    def test_hardlink_read_and_write_are_rejected(self):
        import os
        os.link(self.worker/'src/a.py',self.worker/'src/link.py')
        self.assertFalse(self.call('read',{'path':'src/link.py'})['success'])
        self.assertFalse(self.call('write',{'path':'src/link.py','text':'bad'})['success'])

    def test_command_intent_precedes_launch_and_receipt_follows(self):
        events = []
        self.broker.audit = lambda state, **fields: events.append((state, fields))
        def command(*args):
            self.assertEqual(events[0][0], 'worker_command_started')
            return {'status': 'completed', 'termination_observed': True}
        with patch('scripts.dispatch.write_tools.linux_path', return_value='/mnt/c/test/worker'), \
                patch('scripts.dispatch.write_tools.run_command', side_effect=command):
            self.assertTrue(self.call('command', {'argv': ['/usr/bin/python3', '-c', 'pass']})['success'])
        self.assertEqual([event[0] for event in events], ['worker_command_started', 'worker_command_finished'])
        self.assertEqual(events[0][1]['call_id'], events[1][1]['call_id'])
        self.assertTrue(events[1][1]['termination_observed'])

    def test_audit_failure_cannot_launch_or_allow_more_work(self):
        def fail(*args, **kwargs):
            raise OSError('Journal unavailable')
        self.broker.audit = fail
        with patch('scripts.dispatch.write_tools.run_command') as command:
            self.assertFalse(self.call('command', {'argv': ['/usr/bin/python3']})['success'])
        command.assert_not_called()
        self.assertTrue(self.broker.uncertain)
        self.assertFalse(self.call('write', {'path': 'src/a.py', 'text': 'no'})['success'])

    def test_command_exception_retains_uncertainty(self):
        with patch('scripts.dispatch.write_tools.run_command', side_effect=OSError('Lost receipt')):
            self.assertFalse(self.call('command', {'argv': ['/usr/bin/python3']})['success'])
        self.assertTrue(self.broker.uncertain)
