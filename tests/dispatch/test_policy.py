import time
import unittest
from helpers import manifest, catalog, usage
from scripts.dispatch.contracts import Route
from scripts.dispatch.policy import select_route


class PolicyTests(unittest.TestCase):
    def choose(self, raw=None, headroom=80, history=None):
        return select_route(raw or manifest(), catalog(), usage(headroom), history or [])

    def test_initial_routes(self):
        cases = {"routine": ("luna", "low"), "screening": ("luna", "medium"),
                 "implementation": ("sol", "medium"), "complex": ("sol", "high"),
                 "consequential": ("astra", "high")}
        for kind, pair in cases.items():
            with self.subTest(kind=kind):
                d = self.choose(manifest(task_class=kind))
                self.assertEqual(d.route, Route("gpt-6-" + pair[0], pair[1]))
                self.assertEqual(d.action, "dispatch")
        self.assertEqual(self.choose(manifest(task_class="exceptional")).action, "blocked")

    def test_thresholds(self):
        for n, band in ((40.01, "normal"), (40, "conserve"), (20.01, "conserve"),
                        (20, "low"), (10.01, "low"), (10, "critical"), (0, "critical")):
            self.assertEqual(self.choose(headroom=n).usage_band, band)
        self.assertEqual(self.choose(headroom=0).action, "defer")
        self.assertEqual(self.choose(manifest(task_class="consequential"), 10).action, "defer")

    def test_unknown_stale_invalid_usage(self):
        for value in (None, {}, {"retrieved_at": 1, "limitId": "codex"},
                      usage(float("nan")), usage(-1), usage(101),
                      dict(usage(), limitId="other"), dict(usage(), retrieved_at=time.time()+999)):
            self.assertEqual(select_route(manifest(), catalog(), value, []).usage_band, "unknown")
        known = usage(0)
        known["secondary"] = None
        self.assertEqual(select_route(manifest(), catalog(), known, []).action, "defer")

    def test_manual_supported_and_unavailable(self):
        m = manifest(mode="manual", manual_route={"model": "gpt-6-sol", "effort": "high"})
        self.assertEqual(self.choose(m).route, Route("gpt-6-sol", "high"))
        self.assertEqual(select_route(m, {}, usage(), []).action, "blocked")

    def test_bounded_quality_escalation_and_stepdown(self):
        fail = {"package_id": "synthetic", "outcome": "quality_failure", "verified": True}
        self.assertEqual(self.choose(history=[fail]).route, Route("gpt-6-luna", "medium"))
        self.assertEqual(self.choose(history=[fail, fail]).action, "blocked")
        self.assertEqual(self.choose(history=[dict(fail, package_id="other")]).route,
                         Route("gpt-6-luna", "low"))
        for outcome in ("safety", "permission", "account", "ambiguous"):
            self.assertEqual(self.choose(history=[dict(fail, outcome=outcome)]).action, "blocked")

    def test_no_manual_escalation_and_optional_throttle(self):
        fail = {"package_id": "synthetic", "outcome": "quality_failure", "verified": True}
        m = manifest(mode="manual", manual_route={"model": "gpt-6-sol", "effort": "high"})
        self.assertEqual(self.choose(m, history=[fail]).action, "blocked")
        self.assertEqual(self.choose(manifest(task_class="complex", required=False), 40).action, "defer")
        self.assertEqual(select_route(manifest(), catalog(), None, [fail]).action, "defer")

    def test_absent_window_uses_available_window_without_inventing_capacity(self):
        one_window = usage(80)
        one_window["secondary"] = None
        self.assertEqual(select_route(manifest(), catalog(), one_window, []).usage_band, "normal")
        one_window["primary"] = None
        self.assertEqual(select_route(manifest(), catalog(), one_window, []).usage_band, "unknown")
