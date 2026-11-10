import time
import unittest
try:
    from scripts.dispatch import claude_cli
except ImportError:
    claude_cli = None


class ClaudeCliTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(claude_cli, "Guarded official Claude adapter is missing")

    def proof(self):
        return {"checked_at": time.time(), "source": "claude_settings_usage", "account_fingerprint": "a" * 64,
                "extra_usage_enabled": False, "session_remaining_percent": 38, "weekly_remaining_percent": 94}

    def test_subscription_environment_has_no_api_provider_overrides(self):
        env = {"PATH": "path", "HOME": "home", "ANTHROPIC_API_KEY": "test",
               "ANTHROPIC_AUTH_TOKEN": "test", "CLAUDE_CODE_USE_BEDROCK": "1", "AWS_PROFILE": "test",
               "NODE_OPTIONS": "test", "CLAUDE_CONFIG_DIR": "other", "OTEL_EXPORTER_OTLP_ENDPOINT": "test"}
        result = claude_cli.subscription_environment(env)
        self.assertEqual(result["PATH"], "path")
        self.assertEqual(result["CLAUDE_CODE_DISABLE_FAST_MODE"], "1")
        self.assertFalse(any(key in result for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AWS_PROFILE", "NODE_OPTIONS", "CLAUDE_CONFIG_DIR")))

    def test_metadata_requires_subscription_auth(self):
        for auth in ({}, {"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty", "subscriptionType": "pro"}):
            with self.assertRaises(ValueError): claude_cli.account_fingerprint(auth)
        self.assertEqual(len(claude_cli.account_fingerprint({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro", "email": "example@example.com"})), 64)

    def test_billing_evidence_must_be_fresh_exact_and_disabled(self):
        claude_cli.validate_subscription_proof(self.proof(), "a" * 64, time.time())
        for change in ({"checked_at": time.time()-301}, {"checked_at": time.time()+5},
                       {"extra_usage_enabled": True}, {"extra_usage_enabled": None},
                       {"account_fingerprint": "b" * 64}, {"session_remaining_percent": 0},
                       {"weekly_remaining_percent": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                claude_cli.validate_subscription_proof(self.proof() | change, "a" * 64, time.time())

    def test_command_removes_tools_customizations_fast_mode_and_persistence(self):
        args = claude_cli.command("claude.exe", "claude-opus-5-5", "high")
        for flag in ("--safe-mode", "--restricted", "--strict-mcp-config", "--no-chrome", "--no-session-persistence", "--disable-slash-commands"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--tools")+1], "")
        self.assertIn("--max-turns", args)
        self.assertEqual(args[args.index("--max-turns")+1], "1")
        self.assertNotIn("--bare", args)
        self.assertNotIn("--dangerously-skip-permissions", args)

    def test_result_requires_expected_model_empty_tools_and_terminal_success(self):
        init = {"type": "system", "subtype": "init", "model": "claude-opus-5-5", "tools": [], "mcp_servers": [], "apiKeySource": "none", "permissionMode": "dontAsk"}
        result = {"type": "result", "subtype": "success", "is_error": False, "result": "OK", "num_turns": 1, "fast_mode_state": "off", "modelUsage": {"claude-opus-5-5": {}}, "permission_denials": []}
        self.assertEqual(claude_cli.validate_result([init, result], "claude-opus-5-5"), "OK")
        for change in ({"tools": ["Bash"]}, {"apiKeySource": "env"}, {"model": "other"}):
            with self.assertRaises(ValueError):claude_cli.validate_result([init | change, result], "claude-opus-5-5")
        with self.assertRaises(ValueError):claude_cli.validate_result([init], "claude-opus-5-5")

    def test_expired_evidence_prevents_any_process(self):
        from unittest.mock import patch
        proof = self.proof() | {"checked_at": time.time()-301}
        self.assertTrue(callable(getattr(claude_cli, "run_readonly", None)), "Bounded launcher is missing")
        with patch("subprocess.run", side_effect=AssertionError("must not launch")), patch("subprocess.Popen", side_effect=AssertionError("must not launch")):
            with self.assertRaises(ValueError):
                claude_cli.run_readonly({}, {}, proof, {"account_fingerprint": "a"*64}, None)

    def test_malformed_events_are_rejected_without_traceback(self):
        for events in ([None], [{"type": "result"}, []]):
            with self.assertRaises(ValueError):
                claude_cli.validate_result(events, "claude-opus-5-5")

    def test_scoped_readonly_lifecycle_uses_journal_and_final_check(self):
        import hashlib, json, tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from helpers import manifest
        from scripts.dispatch.contracts import validate_manifest, manifest_digest
        from scripts.dispatch.journal import Journal
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); exe = root / "claude.exe"; exe.write_bytes(b"fake-binary")
            auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro", "email": "example@example.com"}
            fingerprint = claude_cli.account_fingerprint(auth)
            work = validate_manifest(manifest(workspace=str(root), timeout_seconds=10, max_attempts=1, mode="manual", manual_route={"model": "claude-opus-5-5", "effort": "high"}), root)
            grant = {"schema_version": 1, "manifest_digest": manifest_digest(work), "workspace": str(root), "account_fingerprint": fingerprint, "expires_at": time.time()+60, "allowed_actions": ["read"], "max_attempts": 1, "dedicated_session": True}
            pin = {"executable_path": str(exe), "executable_sha256": hashlib.sha256(b"fake-binary").hexdigest(), "cli_version": "fixture", "models": {"claude-opus-5-5": ["high"]}}
            proof = self.proof() | {"account_fingerprint": fingerprint}
            journal = Journal(root / ".dispatch-runs" / "test" / "journal.jsonl")
            events = [{"type": "system", "subtype": "init", "model": "claude-opus-5-5", "tools": [], "mcp_servers": [], "apiKeySource": "none", "permissionMode": "dontAsk"}, {"type": "result", "subtype": "success", "is_error": False, "result": "OK", "num_turns": 1, "fast_mode_state": "off", "modelUsage": {"claude-opus-5-5": {}}, "permission_denials": [], "usage": {"fallback_credit": None}}]
            process = SimpleNamespace(pid=123, returncode=0, communicate=lambda **kwargs: (("\n".join(json.dumps(row) for row in events)).encode(), b""))
            def metadata(args, **kwargs):
                return SimpleNamespace(stdout=b"fixture" if "--version" in args else json.dumps(auth).encode())
            with patch("subprocess.run", side_effect=metadata), patch("subprocess.Popen", return_value=process) as launch, patch("scripts.dispatch.claude_cli.collect_process", return_value=process.communicate()):
                result = claude_cli.run_readonly(work, pin, proof, grant, journal)
                self.assertEqual(result["state"], "completed")
                self.assertEqual(result["verification"], [{"type": "final_equals", "passed": True}])
                self.assertEqual(launch.call_count, 1)
                self.assertFalse(launch.call_args.kwargs["shell"])
                self.assertFalse(journal.lock_path.exists())
                again = claude_cli.run_readonly(work, pin, proof, grant, journal)
                self.assertEqual(again["state"], "completed")
                self.assertEqual(launch.call_count, 1)

    def test_bounded_collector_reaps_on_output_overflow(self):
        import subprocess, sys, threading
        self.assertTrue(callable(getattr(claude_cli, "collect_process", None)), "Bounded collector is missing")
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdout.write('x'*20000)"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self.assertRaisesRegex(ValueError, "output"):
            claude_cli.collect_process(proc, b"", threading.Event(), 5, stdout_limit=1000)
        self.assertIsNotNone(proc.poll())

    def test_bounded_collector_reaps_after_stop(self):
        import subprocess, sys, threading
        self.assertTrue(callable(getattr(claude_cli, "collect_process", None)), "Bounded collector is missing")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stop = threading.Event(); stop.set()
        with self.assertRaisesRegex(ValueError, "completion unconfirmed"):
            claude_cli.collect_process(proc, b"", stop, 5)
        self.assertIsNotNone(proc.poll())

    def test_malformed_result_usage_is_rejected(self):
        initial = {"type": "system", "subtype": "init", "model": "claude-opus-5-5", "tools": [], "mcp_servers": [], "apiKeySource": "none", "permissionMode": "dontAsk"}
        result = {"type": "result", "subtype": "success", "is_error": False, "result": "OK", "num_turns": 1, "fast_mode_state": "off", "modelUsage": {"claude-opus-5-5": {}}, "permission_denials": [], "usage": ["bad"]}
        with self.assertRaises(ValueError):claude_cli.validate_result([initial, result], "claude-opus-5-5")
