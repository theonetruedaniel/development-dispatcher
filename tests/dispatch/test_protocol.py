import sys
import unittest
import time
from pathlib import Path
from scripts.dispatch.protocol import ProtocolClient, ProtocolError


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.client = ProtocolClient([sys.executable, str(Path(__file__).parent / "fixtures/fake_server.py")])
        self.addCleanup(self.client.close)

    def test_literal_prompt(self):
        value = '$(Write-Output nope) "quote"\nUnicode λ'
        self.assertEqual(self.client.request("echo", {"text": value}, 2), {"text": value})

    def test_interleaving_and_stderr(self):
        self.assertEqual(self.client.request("noise", {"x": 1}, 3), {"x": 1})
        self.assertEqual(self.client.next_event(1)["method"], "notice")

    def test_duplicate_reply_is_not_next_response(self):
        self.client.request("duplicate", {"old": True}, 2)
        self.assertEqual(self.client.request("echo", {"new": True}, 2), {"new": True})

    def test_bad_and_unexpected_responses(self):
        for method in ("malformed", "oversized", "future"):
            client = ProtocolClient([sys.executable, str(Path(__file__).parent / "fixtures/fake_server.py")])
            with self.subTest(method=method), self.assertRaises(ProtocolError):
                client.request(method, {}, 2)
            client.close()

    def test_approval_denied_and_reported(self):
        reply = self.client.request("approval", {}, 2)
        self.assertEqual(reply["result"]["decision"], "decline")
        self.assertEqual(self.client.next_event(1)["method"], "dispatch/awaiting_user")

    def test_closed_peer_fails(self):
        with self.assertRaises(ProtocolError):
            self.client.request("close", {}, 2)

    def test_deadline_includes_blocked_stdin(self):
        client = ProtocolClient([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(client.close)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            client.request("echo", {"text": "x"*200000}, .1)
        self.assertLess(time.monotonic()-started, 2)

    def test_child_environment_excludes_endpoint_and_telemetry_overrides(self):
        from scripts.dispatch import protocol
        original={'CODEX_HOME':'preserved', 'PATH':'preserved','OPENAI_API_KEY':'synthetic',
                  'CODEX_API_KEY':'synthetic','OPENAI_BASE_URL':'https://example.invalid',
                  'CHATGPT_BASE_URL':'https://example.invalid','OTEL_EXPORTER_OTLP_ENDPOINT':'https://example.invalid'}
        self.assertEqual(protocol.clean_environment(original), {'CODEX_HOME':'preserved','PATH':'preserved'})
