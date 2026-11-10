import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.dispatch.code_host import CodeHost


class CodeHostTests(unittest.TestCase):
    def setUp(self):
        self.folder=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.exe=self.folder/'host.exe';self.exe.write_bytes(b'fixture')
        self.digest=hashlib.sha256(b'fixture').hexdigest()
        self.children=[]
        self.real_popen=subprocess.Popen

    def launcher(self,code):
        def launch(*args,**kwargs):
            child=self.real_popen([sys.executable,'-I','-u','-c',code],**kwargs)
            self.children.append(child)
            return child
        return patch('scripts.dispatch.code_host.subprocess.Popen',side_effect=launch)

    def test_missing_or_wrong_pin_never_launches(self):
        with patch('subprocess.Popen') as launch:
            for pin in (None,'0'*64):
                with self.assertRaises(ValueError):CodeHost(self.exe,pin)
        launch.assert_not_called()

    def test_endpoint_is_process_reported_and_close_reaps(self):
        with self.launcher("import time;print('http://127.0.0.1:12345');time.sleep(30)"):
            host=CodeHost(self.exe,self.digest)
            self.assertEqual(host.url,'http://127.0.0.1:12345')
            host.check();host.close();host.close()
        self.assertIsNotNone(self.children[0].poll())

    def test_bad_endpoint_timeout_and_early_exit_reap(self):
        for code in ("print('http://example.com:12345')", "import time;time.sleep(30)",
                     "print('x'*5000)", "pass"):
            with self.subTest(code=code),self.launcher(code):
                with self.assertRaises((ValueError,TimeoutError)):CodeHost(self.exe,self.digest,timeout=.2)
                self.assertIsNotNone(self.children[-1].poll())

    def test_runtime_drift_is_rejected(self):
        with self.launcher("import time;print('http://127.0.0.1:12345');time.sleep(30)"):
            host=CodeHost(self.exe,self.digest)
            try:
                self.exe.write_bytes(b'changed')
                with self.assertRaises(ValueError):host.check()
            finally:host.close()
