import contextlib
import io
import json
import unittest
from unittest.mock import patch
from scripts.dispatch.__main__ import main


class ClaudeEntrypointTests(unittest.TestCase):
    def test_missing_input_blocks_without_process(self):
        output = io.StringIO()
        with patch("subprocess.Popen", side_effect=AssertionError("must not launch")), contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = main(["execute-claude", "--manifest", "missing-input.json", "--authorization", "missing-grant.json", "--subscription-proof", "missing-proof.json"])
            except SystemExit:
                self.fail("Guarded Claude entry point is missing")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["state"], "blocked")

    def test_executor_error_is_uncertain_not_falsely_undispatched(self):
        from pathlib import Path
        work = {"workspace": str(Path.cwd()), "package_id": "fixture"}
        args = ["execute-claude", "--manifest", "work", "--authorization", "grant", "--subscription-proof", "proof"]
        output = io.StringIO()
        with patch("scripts.dispatch.__main__.load_json", side_effect=[work, work, {}, {"execution_verified": True}]), patch("scripts.dispatch.__main__.validate_manifest", return_value=work), patch("scripts.dispatch.__main__.manifest_digest", return_value="a"*64), patch("scripts.dispatch.__main__.run_journal"), patch("scripts.dispatch.__main__.run_readonly", side_effect=OSError("post-launch journal write failure")), contextlib.redirect_stdout(output):
            code=main(args)
        result=json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(result["state"], "uncertain")
        self.assertIsNone(result["dispatched"])

    def test_claude_reconciliation_does_not_launch_codex(self):
        from pathlib import Path
        from unittest.mock import MagicMock
        work={"workspace": str(Path.cwd()), "manifest_digest": "a"*64, "account_fingerprint": "b"*64, "provider": "claude_code", "state": "uncertain"}
        journal=MagicMock();journal.records.return_value=[work]
        output=io.StringIO()
        with patch("scripts.dispatch.__main__.load_json",return_value=work), patch("scripts.dispatch.__main__.run_journal",return_value=journal), patch("scripts.dispatch.__main__.validate_authorization"), patch("scripts.dispatch.__main__.qualified_client") as client, contextlib.redirect_stdout(output):
            code=main(["reconcile","--run","fixture","--authorization","grant"])
        client.assert_not_called()
        self.assertEqual(code,2)
        self.assertIn("Claude",json.loads(output.getvalue())["reason"])
