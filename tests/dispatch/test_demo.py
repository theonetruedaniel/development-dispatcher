import contextlib
import io
import unittest
from unittest import mock

from scripts.dispatch.demo import main, run_demo


class DemoTests(unittest.TestCase):
    def test_demo_checks_real_preview_without_network_or_child_processes(self):
        with mock.patch('subprocess.Popen', side_effect=AssertionError('child process')), \
             mock.patch('socket.socket', side_effect=AssertionError('network')):
            rows = run_demo()
        self.assertEqual([(r['model'], r['effort'], r['action']) for r in rows], [
            ('gpt-6-luna', 'low', 'dispatch'),
            ('gpt-6-astra', 'high', 'dispatch'),
            ('gpt-6-luna', 'low', 'dispatch'),
            ('gpt-6-sol', 'high', 'dispatch'),
            ('gpt-6-sol', 'high', 'blocked'),
        ])
        self.assertTrue(all(r['passed'] for r in rows))
        self.assertTrue(all(r['dispatched'] is False for r in rows))
        self.assertTrue(all(r['usage_band'] == 'unknown' for r in rows))

    def test_demo_returns_failure_for_wrong_preview(self):
        bad = {'decision': {'route': {'model': 'wrong', 'effort': 'low'},
                           'action': 'dispatch', 'usage_band': 'unknown'},
               'dispatched': False,
               'evidence': 'offline preview; supplied catalog not live verified'}
        import json
        def incorrect_preview(argv):
            print(json.dumps(bad))
            return 0
        with mock.patch('scripts.dispatch.demo.preview_main', side_effect=incorrect_preview), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 2)

    def test_transcript_explains_decisions_are_not_execution(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertIn('5/5 checks passed', output.getvalue())
        self.assertIn('No model calls', output.getvalue())
        self.assertIn('unknown', output.getvalue())


if __name__ == '__main__':
    unittest.main()
