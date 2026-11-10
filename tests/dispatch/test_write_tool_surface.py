"""Synthetic inspection fixtures; none is live admission evidence."""
import copy
import hashlib
import json
import unittest
from scripts.dispatch.write_sandbox import inspect_write_tool_surface
from scripts.dispatch.live import SETTINGS, TELEMETRY, DISABLED_FEATURES


class ToolSurface(unittest.TestCase):
    def setUp(self):
        self.schema={'fixture':'offline-only','dynamicTools':True}
        self.config=dict(SETTINGS,otel=TELEMETRY,mcp_servers={},
            features={name:False for name in DISABLED_FEATURES},
            permissions={'dispatch':{'filesystem':{':minimal':'read',':workspace_roots':'read'},
                                     'network':{'enabled':False}}})
        self.config['features']['code_mode_host']=True
        self.inventory={'scope':'nested_code_mode','schema_digest':hashlib.sha256(
            json.dumps(self.schema,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            'tools':['clock__curr_time','dispatch_tool']}

    def inspect(self):
        return inspect_write_tool_surface(self.schema,self.config,self.inventory)

    def test_exact_nested_fixture_cannot_admit_direct_execution(self):
        result=self.inspect()
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['reason'],'direct_dispatch_enforcement_unverified')
        self.assertEqual(result['allowed_tools'],[])
        self.assertEqual(result['schema_digest'],self.inventory['schema_digest'])

    def test_enabled_capabilities_rejected(self):
        for name in ('shell_tool','unified_exec','view_image','apps','plugins','hooks',
                     'browser_use','skill_mcp_dependency_install'):
            with self.subTest(name=name):
                self.config['features'][name]=True
                self.assertEqual(self.inspect()['reason'],'configuration_not_isolated')
                self.config['features'][name]=False
        self.config['permissions']['dispatch']['network']['enabled']=True
        self.assertEqual(self.inspect()['reason'],'configuration_not_isolated')

    def test_unknown_native_or_connected_tools_rejected(self):
        for tool in ('exec_command','apply_patch','view_image','web_search','mcp__service__tool',
                     'request_plugin_install','unknown_tool'):
            with self.subTest(tool=tool):
                self.inventory['tools']=['clock__curr_time','dispatch_tool',tool]
                self.assertEqual(self.inspect()['reason'],'unexpected_tool_surface')

    def test_schema_drift_and_asserted_authority_rejected(self):
        self.schema['new_handler']=True
        self.assertEqual(self.inspect()['reason'],'schema_inventory_mismatch')
        self.schema.pop('new_handler')
        self.inventory['qualified']=True
        self.assertEqual(self.inspect()['reason'],'invalid_inventory')

    def test_missing_duplicate_or_invented_scope_rejected_without_mutation(self):
        for change in ({'tools':[]},{'tools':['dispatch_tool','dispatch_tool']},
                       {'scope':'all_tools_enforced'}):
            inventory=self.inventory|change
            before=copy.deepcopy(inventory)
            self.assertEqual(inspect_write_tool_surface(self.schema,self.config,inventory)['status'],'blocked')
            self.assertEqual(inventory,before)
