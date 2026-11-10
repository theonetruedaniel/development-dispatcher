import unittest
from scripts.dispatch.write_sandbox import inspect_schema, qualify_write_environment


class Sandbox(unittest.TestCase):
    def test_workspace_write_without_read_scope_is_not_qualification(self):
        schema = {"definitions": {"SandboxPolicy": {"anyOf": [
            {"properties": {"type": {"enum": ["workspaceWrite"]},
                            "writableRoots": {}, "networkAccess": {}}}]}}}
        result = inspect_schema(schema)
        self.assertFalse(result["scoped_reads"])
        self.assertFalse(result["qualified"])

    def test_caller_assertions_cannot_enable_execution(self):
        result = qualify_write_environment({"execution_verified": True},
                                          {"qualified": True, "networkAccess": False})
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["live_writes"])
