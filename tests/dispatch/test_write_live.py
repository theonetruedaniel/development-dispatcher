import queue
import copy
import threading
import unittest
from unittest.mock import Mock, patch
from scripts.dispatch.write_live import WriteClient, write_command, validate_write_config
from scripts.dispatch.live import DISABLED_FEATURES, SETTINGS, TELEMETRY


class WriteLive(unittest.TestCase):
    def config(self):
        cfg=dict(SETTINGS,features={k:False for k in DISABLED_FEATURES},otel=TELEMETRY,mcp_servers={})
        cfg['features']['code_mode_host']=True
        cfg['permissions']={'dispatch':{'description':None,'extends':None,'workspace_roots':None,
            'filesystem':{':minimal':'read',':workspace_roots':'read','glob_scan_max_depth':None},
            'network':{'enabled':False,'domains':None}}}
        return cfg

    def test_code_host_is_the_only_feature_exception(self):
        cfg=self.config()
        validate_write_config(cfg)
        for name in DISABLED_FEATURES:
            if name=='code_mode_host':continue
            cfg['features'][name]=True
            with self.subTest(name=name),self.assertRaises(ValueError):validate_write_config(cfg)
            cfg['features'][name]=False
        args=write_command('codex.exe')
        self.assertEqual(args.count('code_mode_host'),1)
        self.assertEqual(args[args.index('code_mode_host')-1],'--enable')

    def test_missing_broadened_or_inherited_profile_is_rejected(self):
        cases=[]
        cfg=self.config();del cfg['permissions'];cases.append(cfg)
        for key,value in (('extends',':danger-full-access'),('workspace_roots',['C:/']),
                          ('unrecognized_policy',True)):
            cfg=self.config();cfg['permissions']['dispatch'][key]=value;cases.append(cfg)
        for key,value in ((':root','read'),(':workspace_roots','write')):
            cfg=self.config();cfg['permissions']['dispatch']['filesystem'][key]=value;cases.append(cfg)
        for key,value in (('enabled',True),('allow_local_binding',True),('domains',{'example.com':'allow'})):
            cfg=self.config();cfg['permissions']['dispatch']['network'][key]=value;cases.append(cfg)
        for cfg in cases:
            with self.subTest(cfg=cfg),self.assertRaises(ValueError):validate_write_config(cfg)

    def test_validation_does_not_modify_effective_config(self):
        cfg=self.config();original=copy.deepcopy(cfg)
        validate_write_config(cfg)
        self.assertEqual(cfg,original)

    def test_failed_host_prevents_request_and_is_closed_on_failure(self):
        client=WriteClient.__new__(WriteClient)
        client.host=Mock(closed=False)
        client.host.check.side_effect=ValueError('host changed')
        with patch('scripts.dispatch.live.LiveClient.request') as request:
            with self.assertRaisesRegex(ValueError,'host changed'):client.request('turn/start',{})
            request.assert_not_called()
        with patch('scripts.dispatch.live.LiveClient.close') as close:
            with self.assertRaisesRegex(ValueError,'host changed'):client.close()
            close.assert_called_once()
            client.host.close.assert_called_once()

    def client(self):
        client=WriteClient.__new__(WriteClient)
        client.broker=Mock()
        client.broker.session={'account_fingerprint':'f'*64}
        client.broker.handle.return_value={'success':True,'text':'ok'}
        client.stop=threading.Event()
        client._send=Mock()
        client.events=queue.Queue()
        return client

    def message(self):
        return dict(id=1,method='item/tool/call',params=dict(threadId='t',turnId='u',callId='c',
            namespace=None,tool='dispatch_tool',arguments={'operation':'read','arguments':{'path':'src/a.py'}}))

    def test_only_controller_supplies_account_identity(self):
        client=self.client();client._event(self.message())
        envelope=client.broker.handle.call_args.args[0]
        self.assertEqual(envelope['account_fingerprint'],'f'*64)
        self.assertEqual(envelope['thread_id'],'t')
        self.assertTrue(client._send.call_args.args[0]['result']['success'])

    def test_unknown_tool_or_injected_envelope_never_reaches_broker(self):
        for mutation in ('tool','arguments'):
            client=self.client();message=self.message()
            if mutation=='tool':message['params']['tool']='exec_command'
            else:message['params']['arguments']['account_fingerprint']='evil'
            client._event(message)
            client.broker.handle.assert_not_called()
            self.assertFalse(client._send.call_args.args[0]['result']['success'])

    def test_native_approvals_still_declined(self):
        client=self.client()
        client._event({'id':2,'method':'item/fileChange/requestApproval','params':{}})
        self.assertEqual(client._send.call_args.args[0]['result'],{'decision':'decline'})
