import threading
import unittest
from unittest.mock import patch

from scripts.dispatch.laya_supervisor import evaluate_batch, validate_request


class Worker:
    def __init__(self,*args,**kwargs):
        self.pid=None;self.reaped=False;self.startup_seconds=0
    def request(self,text):
        self.pid=123
        return dict(status='ok',task_class='routine',selector_revision='a'*40,reason_code='classified')
    def close(self):self.reaped=True


class LayaSupervisor(unittest.TestCase):
    def test_lazy_startup_is_not_counted_as_warm_inference(self):
        class Lazy(Worker):
            def request(self,text):
                self.startup_seconds=10
                return super().request(text)
        self.services(Lazy)
        with patch('scripts.dispatch.laya_supervisor.time.monotonic',side_effect=[0,0,0,11,11,11]):
            result=evaluate_batch({'tasks':['one']},'/controller/profile','b'*64,threading.Event())
        self.assertEqual(result['measurements'],{'cold_seconds':10,'warm_seconds':[1]})

    def test_request_cannot_select_paths_or_limits(self):
        self.assertEqual(validate_request({'tasks':['one','two']}),['one','two'])
        for value in ({'tasks':[]},{'tasks':['one'],'profile':'/private'},
                      {'tasks':['one'],'timeout_seconds':100},{'tasks':['x']*65},
                      {'tasks':['x'*300000]},{'tasks':[{}]}):
            with self.subTest(value=str(value)[:70]),self.assertRaises(ValueError):validate_request(value)

    def services(self,worker=Worker):
        self.enterContext(patch('scripts.dispatch.laya_supervisor.verify_host'))
        load=self.enterContext(patch('scripts.dispatch.laya_supervisor.load_profile',
            return_value={'checkpoint_revision':'a'*40}))
        self.enterContext(patch('scripts.dispatch.laya_supervisor.sandbox_argv',return_value=['/pinned/bwrap']))
        self.enterContext(patch('scripts.dispatch.laya_supervisor.LayaProcess',worker))
        return load

    def test_batch_rechecks_profile_after_worker_reaped(self):
        load=self.services()
        result=evaluate_batch({'tasks':['one','two']},'/controller/profile','b'*64,threading.Event())
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(result['proposals']),2)
        self.assertTrue(result['termination_observed'])
        self.assertEqual(load.call_count,2)
        self.assertEqual(len(result['measurements']['warm_seconds']),2)
        self.assertGreaterEqual(result['measurements']['cold_seconds'],0)

    def test_stop_before_launch_does_not_load_or_spawn(self):
        load=self.services()
        stop=threading.Event();stop.set()
        result=evaluate_batch({'tasks':['one']},'/controller/profile','b'*64,stop)
        self.assertEqual(result['status'],'cancelled')
        self.assertTrue(result['termination_observed'])
        load.assert_not_called()

    def test_post_run_drift_invalidates_proposals(self):
        load=self.services()
        load.side_effect=[{'checkpoint_revision':'a'*40},ValueError('drift')]
        result=evaluate_batch({'tasks':['one']},'/controller/profile','b'*64,threading.Event())
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['proposals'],[])
        self.assertTrue(result['termination_observed'])

    def test_failed_cleanup_never_claims_termination(self):
        class Uncertain(Worker):
            def close(self):raise TimeoutError('unacknowledged')
        self.services(Uncertain)
        result=evaluate_batch({'tasks':['one']},'/controller/profile','b'*64,threading.Event())
        self.assertEqual(result['status'],'uncertain')
        self.assertFalse(result['termination_observed'])
        self.assertEqual(result['proposals'],[])

    def test_stop_during_last_request_discards_batch(self):
        stop=threading.Event()
        class Stopped(Worker):
            def request(self,text):
                answer=super().request(text);stop.set();return answer
        self.services(Stopped)
        result=evaluate_batch({'tasks':['one']},'/controller/profile','b'*64,stop)
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(result['proposals'],[])
