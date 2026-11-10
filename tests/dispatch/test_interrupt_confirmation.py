import unittest
from unittest.mock import patch
from scripts.dispatch.runner import _interrupt


class InterruptConfirmationTests(unittest.TestCase):
    def client(self, thread="owned", turn="turn-owned", status="interrupted"):
        class Client:
            methods = []
            def request(self, method, params, timeout=15):
                self.methods.append((method, params))
                if method == "thread/read":
                    return {"thread": {"id": thread, "turns": [{"id": turn, "status": status}]}}
                return {}
        return Client()

    def test_owned_terminal_read_confirms_delayed_notification(self):
        client = self.client()
        with patch("scripts.dispatch.runner._wait_turn", return_value=(None, "cancelled")):
            self.assertTrue(_interrupt(client, "owned", "turn-owned"))
        self.assertEqual(sum(method == "thread/read" for method, _ in client.methods), 1)
        self.assertFalse(any(method in ("thread/start", "turn/start") for method, _ in client.methods))

    def test_unowned_or_nonterminal_turn_stays_uncertain(self):
        for values in ({"thread": "other"}, {"turn": "other"}, {"status": "inProgress"}):
            with self.subTest(values=values), patch("scripts.dispatch.runner._wait_turn", return_value=(None, "cancelled")):
                self.assertFalse(_interrupt(self.client(**values), "owned", "turn-owned"))
