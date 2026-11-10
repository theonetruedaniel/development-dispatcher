import time
import unittest
from helpers import manifest, catalog, usage
try:
    from scripts.dispatch import development_policy
except ImportError:
    development_policy = None


class DevelopmentPolicyTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(development_policy, "Development provider selector is missing")
    def choose(self, work=None, purpose="work", proof=None, history=None):
        return development_policy.select_development_route(work or manifest(), catalog() | {"gpt-6.1-terra": ["medium"]}, usage(),
            {"claude-opus-5-5": ["high"], "claude-sonnet-5-5": ["medium"], "claude-haiku-5-5": ["low"]},
            proof or {"checked_at": time.time(), "source": "claude_settings_usage", "account_fingerprint": "a"*64,
                      "extra_usage_enabled": False, "session_remaining_percent": 38, "weekly_remaining_percent": 94},
            "a"*64, purpose=purpose, history=history or [])
    def test_normal_implementation_uses_codex_terra(self):
        result = self.choose(manifest(task_class="implementation"))
        self.assertEqual((result["provider"], result["route"]["model"], result["action"]), ("codex", "gpt-6.1-terra", "dispatch"))
    def test_consequential_independent_review_uses_qualified_opus(self):
        result = self.choose(manifest(task_class="consequential"), purpose="independent_review")
        self.assertEqual((result["provider"], result["route"]), ("claude_code", {"model": "claude-opus-5-5", "effort": "high"}))
    def test_stale_claude_proof_does_not_switch_to_another_provider(self):
        result = self.choose(purpose="independent_review", proof={"checked_at": 0})
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(result["provider"], "claude_code")
    def test_no_provider_hopping_after_attempt(self):
        result = self.choose(purpose="independent_review", history=[{"outcome": "quota"}])
        self.assertEqual(result["action"], "blocked")
    def test_manual_codex_pin_overrides_review_preference(self):
        work = manifest(mode="manual", manual_route={"model": "gpt-6-sol", "effort": "high"})
        self.assertEqual(self.choose(work, purpose="independent_review")["provider"], "codex")

    def test_codex_does_not_ignore_prior_claude_attempt(self):
        result = self.choose(history=[{"provider": "claude_code", "state": "uncertain"}])
        self.assertEqual(result["action"], "blocked")
