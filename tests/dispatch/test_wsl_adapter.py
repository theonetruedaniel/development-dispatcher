import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts.dispatch.wsl_adapter import decode_result, run_command


class WslAdapter(unittest.TestCase):
    def receipt(self, **changes):
        return dict(status='completed', exit_code=0, elapsed_seconds=.1,
                    stdout_b64=base64.b64encode(b'ok').decode(), stderr_b64='',
                    termination_observed=True, **changes)

    def test_output_and_termination_are_validated(self):
        result = decode_result(json.dumps(self.receipt()).encode(), 2)
        self.assertEqual(result['stdout'], 'ok')
        for field, value in [('termination_observed', False), ('exit_code', True),
                             ('elapsed_seconds', float('nan')), ('status', 'invented'),
                             ('stdout_b64', '?'), ('stdout_b64', 'eHh4')]:
            receipt = self.receipt()
            receipt[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                decode_result(json.dumps(receipt).encode(), 2)

    def test_nonzero_completed_or_unknown_fields_rejected(self):
        receipt = self.receipt()
        receipt['exit_code'] = 1
        with self.assertRaises(ValueError):
            decode_result(json.dumps(receipt).encode(), 10)
        receipt = self.receipt()
        receipt['qualified'] = True
        with self.assertRaises(ValueError):
            decode_result(json.dumps(receipt).encode(), 10)

    def test_preexisting_stop_never_launches(self):
        stop = threading.Event()
        stop.set()
        with patch('subprocess.Popen') as launch:
            result = run_command({}, {}, stop)
        launch.assert_not_called()
        self.assertEqual(result['status'], 'cancelled')

    def test_caller_flag_is_not_runtime_integrity(self):
        with patch('subprocess.Popen') as launch:
            result = run_command({}, {'qualified':True}, threading.Event())
        launch.assert_not_called()
        self.assertEqual(result['status'], 'blocked')

    @unittest.skipUnless(os.name == 'nt', 'Windows adapter transport')
    def test_missing_or_malformed_acknowledgement_is_uncertain(self):
        from scripts.dispatch.wsl_adapter import linux_path
        real_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as directory:
            worker = Path(directory)/'worker'
            worker.mkdir()
            request = dict(root=linux_path(worker),argv=['/usr/bin/python3','-c','pass'],
                           timeout_seconds=1,output_limit=100)
            for code in ("print('not JSON')", "import time; time.sleep(30)"):
                def fake_launch(args, **kwargs):
                    return real_popen([sys.executable,'-c',code], **kwargs)
                with self.subTest(code=code), patch('scripts.dispatch.wsl_adapter.inspect_runtime',
                     return_value={'status':'integrity_checked','runtime_digest':'test'}), \
                     patch('scripts.dispatch.wsl_adapter.subprocess.Popen',side_effect=fake_launch), \
                     patch('scripts.dispatch.wsl_adapter.STOP_GRACE',.1):
                    result = run_command(request, {}, threading.Event())
                self.assertEqual(result['status'],'uncertain',result)
                self.assertFalse(result['termination_observed'])
