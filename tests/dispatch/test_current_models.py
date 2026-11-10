import unittest
from helpers import manifest, catalog, usage
from scripts.dispatch.contracts import Route
from scripts.dispatch.policy import select_route


class CurrentModelTests(unittest.TestCase):
    def current_catalog(self):
        return catalog() | {name: ["low", "medium", "high", "xhigh", "max"]
                            for name in ("gpt-6.1-sol", "gpt-6.1-terra")}

    def test_implementation_prefers_available_codex_terra(self):
        decision = select_route(manifest(task_class="implementation"), self.current_catalog(), usage(), [])
        self.assertEqual(decision.action, "dispatch")
        self.assertEqual(decision.route, Route("gpt-6.1-terra", "medium"))

    def test_complex_analysis_prefers_current_sol(self):
        decision = select_route(manifest(task_class="complex"), self.current_catalog(), usage(), [])
        self.assertEqual(decision.action, "dispatch")
        self.assertEqual(decision.route, Route("gpt-6.1-sol", "high"))

    def test_known_legacy_routes_remain_supported(self):
        decision = select_route(manifest(task_class="implementation"), catalog(), usage(), [])
        self.assertEqual(decision.route, Route("gpt-6-sol", "medium"))

    def test_current_model_must_offer_the_selected_effort(self):
        models = self.current_catalog()
        models["gpt-6.1-terra"] = ["low"]
        decision = select_route(manifest(task_class="implementation"), models, usage(), [])
        self.assertEqual(decision.route, Route("gpt-6.1-sol", "medium"))

    def test_manual_pin_never_changes_generation(self):
        work = manifest(mode="manual", manual_route={"model": "gpt-6-sol", "effort": "high"})
        self.assertEqual(select_route(work, self.current_catalog(), usage(), []).route,
                         Route("gpt-6-sol", "high"))

    def test_consequential_floor_stays_astra(self):
        decision = select_route(manifest(task_class="consequential"), self.current_catalog(), usage(), [])
        self.assertEqual(decision.route, Route("gpt-6-astra", "high"))

    def test_new_generation_high_effort_remains_expensive(self):
        decision = select_route(manifest(task_class="complex", required=False), self.current_catalog(), usage(40), [])
        self.assertEqual(decision.action, "defer")

    def test_generation_does_not_bypass_exhaustion(self):
        decision = select_route(manifest(task_class="implementation"), self.current_catalog(), usage(0), [])
        self.assertEqual(decision.action, "defer")

    def test_unadvertised_model_names_are_never_invented(self):
        decision = select_route(manifest(task_class="routine"), {"gpt-6.1-luna": ["low"]}, usage(), [])
        self.assertEqual(decision.action, "blocked")
