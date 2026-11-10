import json
import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from scripts.dispatch.laya_adapter import decode_result, run_batch, inspect_runtime

PIN='b'*64
REV='a'*40


def receipt():
    return dict(status='completed',termination_observed=True,profile_sha256=PIN,
        proposals=[dict(status='ok',task_class='routine',selector_revision=REV,reason_code='classified')])


class LayaAdapter(unittest.TestCase):
    def test_timing_receipt_is_bound_to_complete_batch(self):
        value=receipt()|{'measurements':{'cold_seconds':2.0,'warm_seconds':[.2]}}
        self.assertEqual(decode_result(json.dumps(value).encode(),PIN,REV,1),value)
        for timings in ({'cold_seconds':2,'warm_seconds':[]},
                        {'cold_seconds':True,'warm_seconds':[.2]},
                        {'cold_seconds':2,'warm_seconds':[float('nan')]},
                        {'cold_seconds':2,'warm_seconds':[-1]}):
            with self.subTest(timings=timings),self.assertRaises(ValueError):
                decode_result(json.dumps(receipt()|{'measurements':timings}).encode(),PIN,REV,1)

    def test_receipt_requires_exact_binding_count_and_termination(self):
        self.assertEqual(decode_result(json.dumps(receipt()).encode(),PIN,REV,1),receipt())
        for change in ({'profile_sha256':'c'*64},{'termination_observed':False},
                       {'proposals':[]},{'qualified':True},{'status':'invented'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                decode_result(json.dumps(receipt()|change).encode(),PIN,REV,1)
        value=receipt();value['proposals'][0]['selector_revision']='c'*40
        with self.assertRaises(ValueError):decode_result(json.dumps(value).encode(),PIN,REV,1)

    def test_worker_timeout_is_valid_fallback_not_successful_classification(self):
        value=receipt()
        value['proposals']=[dict(status='timeout',task_class=None,selector_revision=REV,reason_code='worker_timeout')]
        self.assertEqual(decode_result(json.dumps(value).encode(),PIN,REV,1),value)
        value['proposals'][0]['task_class']='routine'
        with self.assertRaises(ValueError):decode_result(json.dumps(value).encode(),PIN,REV,1)

    def test_uncertain_receipt_cannot_smuggle_proposals(self):
        value=receipt()|dict(status='uncertain',termination_observed=False,proposals=[])
        self.assertEqual(decode_result(json.dumps(value).encode(),PIN,REV,1),value)
        value['proposals']=receipt()['proposals']
        with self.assertRaises(ValueError):decode_result(json.dumps(value).encode(),PIN,REV,1)

    def test_stop_and_unqualified_input_never_launch(self):
        stop=threading.Event();stop.set()
        with patch('subprocess.Popen') as launch:
            self.assertEqual(run_batch(['one'],{},stop)['status'],'cancelled')
            self.assertEqual(run_batch(['one'],{'qualified':True},threading.Event())['status'],'blocked')
        launch.assert_not_called()
        with self.assertRaises(ValueError):inspect_runtime({'qualified':True})

    @unittest.skipUnless(os.name=='nt','Windows launcher transport')
    def test_actual_pipe_transport_and_missing_acknowledgement(self):
        real_popen=subprocess.Popen
        checked=dict(command=['ignored'],profile_sha256=PIN,checkpoint_revision=REV,runtime_digest='test')
        for code,expected in (("import sys;sys.stdin.readline();print("+repr(json.dumps(receipt()))+")",'completed'),
                              ("print('not-json')",'uncertain'),
                              ("import time;time.sleep(30)",'uncertain')):
            def launch(*args,**kwargs):
                return real_popen([sys.executable,'-I','-u','-c',code],**kwargs)
            with self.subTest(expected=expected),patch('scripts.dispatch.laya_adapter.inspect_runtime',return_value=checked),\
                 patch('scripts.dispatch.laya_adapter.subprocess.Popen',side_effect=launch),\
                 patch('scripts.dispatch.laya_adapter.STOP_GRACE',.1):
                result=run_batch(['one'],{},threading.Event(),timeout_seconds=.15)
                self.assertEqual(result['status'],expected)
                self.assertEqual(result['termination_observed'],expected=='completed')

    @unittest.skipUnless(os.name=='nt','Windows launcher transport')
    def test_stop_after_receipt_discards_proposals(self):
        real_popen=subprocess.Popen;stop=threading.Event()
        checked=dict(command=['ignored'],profile_sha256=PIN,checkpoint_revision=REV,runtime_digest='test')
        def inspect(*args):
            if inspect.calls:stop.set()
            inspect.calls+=1
            return checked
        inspect.calls=0
        def launch(*args,**kwargs):
            return real_popen([sys.executable,'-I','-u','-c',
                "import sys;sys.stdin.readline();print("+repr(json.dumps(receipt()))+")"],**kwargs)
        with patch('scripts.dispatch.laya_adapter.inspect_runtime',side_effect=inspect),\
             patch('scripts.dispatch.laya_adapter.subprocess.Popen',side_effect=launch):
            result=run_batch(['one'],{},stop)
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(result['proposals'],[])
