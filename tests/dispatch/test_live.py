import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.dispatch import live


class LiveTests(unittest.TestCase):
    def config(self):
        return {'otel': {'exporter':'none','trace_exporter':'none','metrics_exporter':'none','log_user_prompt':False},
                'features': {k: False for k in live.DISABLED_FEATURES},
                'mcp_servers': {'example': {'enabled': False}},
                'web_search': 'disabled', 'notify': [], 'model_provider': 'openai',
                'forced_login_method': 'chatgpt', 'approvals_reviewer': 'user',
                'sandbox_mode': 'read-only', 'approval_policy': 'on-request'}

    def test_config_blocks_each_inherited_capability(self):
        live.validate_config(self.config())
        for flag in live.DISABLED_FEATURES:
            cfg=self.config(); cfg['features'][flag]=True
            with self.subTest(flag=flag), self.assertRaises(ValueError): live.validate_config(cfg)
        for key,value in [('mcp_servers',{'other':{}}),('notify',['command']),
                          ('web_search','live'),('openai_base_url','https://other.invalid'),
                          ('model_providers',{'openai': {'base_url':'https://other.invalid'}}),
                          ('approvals_reviewer','auto_review')]:
            cfg=self.config(); cfg[key]=value
            with self.subTest(key=key), self.assertRaises(ValueError): live.validate_config(cfg)

    def test_missing_or_malformed_config_blocks(self):
        for cfg in [None, {}, {'features': []}]:
            with self.assertRaises(ValueError): live.validate_config(cfg)

    def test_thread_acceptance_requires_route_sandbox_and_user_reviewer(self):
        from scripts.dispatch.contracts import Route
        route=Route('gpt-6-luna','low')
        response={'model':route.model,'reasoningEffort':route.effort,'modelProvider':'openai',
                  'sandbox':{'type':'readOnly','networkAccess':False},
                  'approvalPolicy':'on-request','approvalsReviewer':'user'}
        self.assertEqual(live.validate_thread(response,route),{'model':route.model,'effort':route.effort})
        for key,value in [('model','other'),('reasoningEffort','high'),('sandbox',{'type':'dangerFullAccess'}),
                          ('sandbox',{'type':'readOnly','networkAccess':True}),('approvalsReviewer','auto_review')]:
            changed=copy.deepcopy(response);changed[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):live.validate_thread(changed,route)

    def test_changed_executable_rejected_before_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            exe=Path(directory)/'codex.exe';exe.write_bytes(b'changed')
            with patch('subprocess.run',side_effect=AssertionError('spawn')):
                with self.assertRaisesRegex(ValueError,'executable'):
                    live.verify_pin(exe,{'executable_sha256':'a'*64})

    def test_mcp_inventory_must_be_empty_and_complete(self):
        for value in [{'data':[{'name':'external','tools':{}}],'nextCursor':None},
                      {'data':[],'nextCursor':'more'}, {}, None]:
            with self.assertRaises(ValueError):live.validate_inventory(value)
        live.validate_inventory({'data':[],'nextCursor':None})



    def test_disabled_mcp_descriptor_is_not_an_active_tool(self):
        entry={'name':'node_repl','tools':{},'resources':[],'resourceTemplates':[],
               'runtimeStatus':'disabled','toolsError':None,'serverInfo':None,'serverCapabilities':None}
        live.validate_inventory({'data':[entry],'nextCursor':None}, {'node_repl'})
        entry['tools']={'exec':{}}
        with self.assertRaises(ValueError):
            live.validate_inventory({'data':[entry],'nextCursor':None}, {'node_repl'})

    def test_schema_drift_rejected_before_server_start(self):
        import hashlib
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            exe=Path(directory)/'codex.exe';exe.write_bytes(b'known')
            pin={'executable_sha256':hashlib.sha256(b'known').hexdigest(),
                 'cli_version':'codex-test','files':{'v2/TurnStartParams.json':'a'*64}}
            def run(args,**kwargs):
                if args[-1]=='--version':return SimpleNamespace(stdout=b'codex-test')
                root=Path(args[-1]);(root/'v2').mkdir();(root/'v2/TurnStartParams.json').write_bytes(b'drift')
                return SimpleNamespace(stdout=b'')
            with patch('subprocess.run',side_effect=run):
                with self.assertRaisesRegex(ValueError,'protocol changed'):live.verify_pin(exe,pin)

    def test_unsafe_mcp_names_fail_before_launch(self):
        for name in ['a.b','"quoted"','']:
            with self.assertRaises(ValueError):live.command('codex',[name])


    def test_builtin_image_reading_is_blocked(self):
        cfg=self.config();cfg['features']['view_image']=True
        with self.assertRaises(ValueError):live.validate_config(cfg)
        self.assertIn('view_image',live.command('codex'))

    def test_telemetry_cannot_export_prompt_or_traces(self):
        for key,value in [('exporter',{'otlp-http':{'endpoint':'https://collector.example.invalid','protocol':'json'}}),
                          ('trace_exporter','otlp-grpc'),('metrics_exporter','statsig'),('log_user_prompt',True)]:
            cfg=self.config();cfg['otel'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):live.validate_config(cfg)
        cfg=self.config();del cfg['otel']
        with self.assertRaises(ValueError):live.validate_config(cfg)

    def test_configuration_rechecked_before_thread_start(self):
        client=live.LiveClient.__new__(live.LiveClient)
        client.workspace='.';client.disabled_servers={'example'};client.ready=True
        changed=self.config();changed['mcp_servers']['new']={}
        calls=[]
        def request(instance,method,params,timeout=15):
            calls.append(method)
            if method=='configRequirements/read':return {'requirements':None}
            return {'config':changed}
        with patch.object(live.ProtocolClient,'request',request):
            with self.assertRaises(ValueError):client.request('thread/start',{})
        self.assertNotIn('thread/start',calls)
