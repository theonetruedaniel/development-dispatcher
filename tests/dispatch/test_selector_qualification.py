import unittest
from unittest.mock import patch
from scripts.dispatch.qualification import evaluate_selector


class Qualification(unittest.TestCase):
    def test_ordinary_threshold_is_not_hidden_by_protected_cases(self):
        corpus=[dict(id='r',task='format',expected=['routine'],floor='routine')]+[
            dict(id=str(i),task='review',expected=['consequential'],floor='consequential') for i in range(19)]
        result=evaluate_selector(corpus,lambda c:'consequential',lambda c:c['floor'])
        self.assertEqual(result['acceptable'],19)
        self.assertEqual(result['ordinary_acceptable'],0)
        self.assertEqual(result['ordinary_cases'],1)
        self.assertFalse(result['quality_pass'])
        self.assertEqual(result['confusion']['routine']['consequential'],1)

    def test_policy_composition_and_shadow_are_measured(self):
        corpus=[dict(id='r',task='format',expected=['routine'],floor='routine'),
                dict(id='s',task='review',expected=['consequential'],floor='consequential')]
        result=evaluate_selector(corpus,lambda c:'routine',lambda c:c['floor'])
        self.assertEqual(result['consequential_violations'],1)
        self.assertEqual(result['policy_consequential_violations'],0)
        self.assertTrue(result['shadow_routes_identical'])
        self.assertEqual(result['rows'][1]['effective_route']['model'],'gpt-6-astra')

    def test_slow_predictions_cannot_pass_latency_threshold(self):
        corpus=[dict(id='r',task='format',expected=['routine'],floor='routine')]
        with patch('scripts.dispatch.qualification.time.monotonic',side_effect=[0,6]):
            result=evaluate_selector(corpus,lambda c:'routine',lambda c:'routine')
        self.assertEqual(result['p95_seconds'],6)
        self.assertFalse(result['latency_pass'])
        self.assertFalse(result['evaluation_pass'])

    def test_malformed_cases_rejected_before_predictor(self):
        from unittest.mock import Mock
        predictor=Mock()
        good=dict(id='r',task='format',expected=['routine'],floor='routine')
        for bad in (None,{},[good|{'id':[]}],[good|{'task':5}],[good|{'expected':'routine'}]):
            with self.subTest(bad=bad),self.assertRaises(ValueError):evaluate_selector(bad,predictor,lambda c:'routine')
        predictor.assert_not_called()

    def test_perfect_and_under_routing_counts(self):
        corpus = [dict(id="r", task="format", expected=["routine"], floor="routine"),
                  dict(id="s", task="review recovery", expected=["consequential"], floor="consequential")]
        correct = lambda case: case["expected"][0]
        perfect = evaluate_selector(corpus, correct, correct)
        self.assertEqual(perfect["acceptable"], 2)
        self.assertEqual(perfect["under_routing"], 0)
        self.assertTrue(perfect["quality_pass"])
        wrong = evaluate_selector(corpus, lambda case: "routine", correct)
        self.assertEqual(wrong["under_routing"], 1)
        self.assertEqual(wrong["consequential_violations"], 1)
        self.assertFalse(wrong["quality_pass"])
        self.assertFalse(wrong["adoption_qualified"])

    def test_errors_and_invalid_classes_are_not_success(self):
        corpus = [dict(id="r", task="format", expected=["routine"], floor="routine")]
        def fail(case):
            raise RuntimeError("offline fixture failure")
        result = evaluate_selector(corpus, fail, lambda case: "routine")
        self.assertEqual(result["fallbacks"], 1)
        self.assertEqual(result["acceptable"], 0)
        self.assertFalse(result["quality_pass"])
        self.assertFalse(evaluate_selector(corpus, lambda case: "root", lambda case: "routine")["quality_pass"])
