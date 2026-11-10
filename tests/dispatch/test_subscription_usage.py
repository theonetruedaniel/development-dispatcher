import unittest
from scripts.dispatch.protocol import ProtocolClient, ProtocolError


class SubscriptionUsageTests(unittest.TestCase):
    def response(self):
        return {"ordinaryUsageAllowed": True, "rateLimitsByLimitId": {"codex": {
            "limitId": "codex", "primary": {"usedPercent": 5}, "secondary": None,
            "spendControlReached": False,
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"}}}}

    def read(self, response):
        client = ProtocolClient.__new__(ProtocolClient)
        client.request = lambda *args, **kwargs: response
        return client.usage()

    def test_available_subscription_allowance_is_preserved(self):
        self.assertEqual(self.read(self.response())["primary"]["usedPercent"], 5)

    def test_nonordinary_usage_blocks_before_dispatch(self):
        for value in (False, None, "true"):
            response = self.response(); response["ordinaryUsageAllowed"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ProtocolError, "subscription"):
                self.read(response)

    def test_available_or_unknown_credit_fallback_blocks(self):
        for credits in (None, {}, {"hasCredits": True, "unlimited": False, "balance": "1"},
                        {"hasCredits": False, "unlimited": True, "balance": "0"},
                        {"hasCredits": False, "unlimited": False, "balance": "1"}):
            response = self.response(); response["rateLimitsByLimitId"]["codex"]["credits"] = credits
            with self.subTest(credits=credits), self.assertRaisesRegex(ProtocolError, "credit"):
                self.read(response)

    def test_spend_control_stop_is_respected(self):
        response = self.response(); response["rateLimitsByLimitId"]["codex"]["spendControlReached"] = True
        with self.assertRaisesRegex(ProtocolError, "spend"):
            self.read(response)

    def test_missing_or_malformed_allowance_blocks(self):
        for primary, secondary in ((None, None), ({}, None), ({"usedPercent": "5"}, None),
                                   ({"usedPercent": float("nan")}, None),
                                   ({"usedPercent": 5}, {"usedPercent": "unknown"})):
            response = self.response(); bucket = response["rateLimitsByLimitId"]["codex"]
            bucket.update(primary=primary, secondary=secondary)
            with self.subTest(primary=primary, secondary=secondary), self.assertRaisesRegex(ProtocolError, "allowance"):
                self.read(response)

    def test_missing_spend_control_blocks(self):
        response = self.response(); del response["rateLimitsByLimitId"]["codex"]["spendControlReached"]
        with self.assertRaisesRegex(ProtocolError, "spend"):
            self.read(response)

    def test_unknown_or_nonfinite_balance_blocks(self):
        for balance in (None, "bad", "NaN", "Infinity", False):
            response = self.response(); response["rateLimitsByLimitId"]["codex"]["credits"]["balance"] = balance
            with self.subTest(balance=balance), self.assertRaisesRegex(ProtocolError, "credit"):
                self.read(response)
