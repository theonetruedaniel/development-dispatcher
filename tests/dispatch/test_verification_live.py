import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from scripts.dispatch.verification import run_checks
from scripts.dispatch.workspace import inventory, collect_changes
from scripts.dispatch.contracts import manifest_digest
from scripts.dispatch.wsl_adapter import runtime_files


@unittest.skipUnless(os.name=='nt' and os.environ.get('ABRAMS_WSL_QUALIFY')=='1',
                     'Explicit Windows/WSL synthetic qualification only')
class LiveVerification(unittest.TestCase):
    def test_worker_replacement_cannot_replace_failing_controller_harness(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);worker=root/'worker';worker.mkdir();stage=root/'staged';stage.mkdir()
            original='raise AssertionError("broken implementation")'
            (worker/'check.py').write_text(original)
            record=dict(root=str(worker),staging=str(stage),checks=['independent'],
                manifest_digest='a'*64,outputs=['check.py'],initial_inventory=inventory(worker))
            (worker/'check.py').write_text('print("OK")')
            runtime={'distro':'Ubuntu','files':{k:hashlib.sha256(p.read_bytes()).hexdigest()
                                              for k,p in runtime_files().items()}}
            env=dict(runtime=runtime,deadline=time.monotonic()+60,checks={
                'independent':dict(platform='linux',argv=['/usr/bin/python3','-I',
                    '/work/.dispatch-verifier/check.py'],harness={'check.py':original},expected_stdout='OK\n')})
            checks=run_checks(['independent'],record,env,threading.Event())
            self.assertEqual(checks[0]['status'],'failed',checks)
            self.assertNotEqual(checks[0]['exit_code'],0)
            self.assertTrue(checks[0]['termination_observed'])
            self.assertEqual((worker/'check.py').read_text(),'print("OK")')

    def test_contained_check_binds_exact_changed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);worker=root/'worker';worker.mkdir();stage=root/'staged';stage.mkdir()
            (worker/'value.txt').write_text('before')
            record=dict(root=str(worker),staging=str(stage),checks=['assert-value'],
                manifest_digest='a'*64,outputs=['value.txt'],initial_inventory=inventory(worker))
            (worker/'value.txt').write_text('after')
            runtime={'distro':'Ubuntu','files':{k:hashlib.sha256(p.read_bytes()).hexdigest()
                                              for k,p in runtime_files().items()}}
            env=dict(runtime=runtime,deadline=time.monotonic()+60,checks={
                'assert-value':dict(platform='linux',argv=['/usr/bin/python3','-I','-c',
                    "from pathlib import Path; assert Path('/work/value.txt').read_text()=='after'; print('PASS')"],
                    expected_stdout='PASS\n')})
            checks=run_checks(['assert-value'],record,env,threading.Event())
            self.assertEqual(checks[0]['status'],'passed',checks)
            self.assertEqual(checks[0]['changes_digest'],manifest_digest({'changes':collect_changes(record)}))
            self.assertEqual((worker/'value.txt').read_text(),'after')
