from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from scripts.dispatch.workspace import inventory
from scripts.dispatch.contracts import manifest_digest
from scripts.dispatch.verification import run_checks


class Verification(unittest.TestCase):
    def test_worker_test_file_cannot_be_the_registered_verifier(self):
        (self.worker/'check.py').write_text('print("OK")')
        self.record['initial_inventory']['check.py']='0'*64
        self.record['outputs'].append('check.py')
        self.env['checks']['unit']['argv']=['/usr/bin/python3','-I','/work/check.py']
        with patch('scripts.dispatch.verification.run_command') as command:
            result=run_checks(['unit'],self.record,self.env,threading.Event())
        command.assert_not_called()
        self.assertEqual(result[0]['status'],'failed')

    def test_controller_harness_is_separate_from_worker_test(self):
        (self.worker/'check.py').write_text('print("OK")')
        self.record['initial_inventory']['check.py']='0'*64
        self.record['outputs'].append('check.py')
        harness='raise AssertionError("independent check fails")'
        self.env['checks']['unit'].update(argv=['/usr/bin/python3','-I',
            '/work/.dispatch-verifier/check.py'],harness={'check.py':harness})
        observed=[]
        def independent(request,runtime,stop):
            observed.append(True)
            self.assertEqual((Path(request['root'])/'.dispatch-verifier/check.py').read_text(),harness)
            self.assertEqual((Path(request['root'])/'check.py').read_text(),'print("OK")')
            return self.fake(request,runtime,stop)|{'exit_code':1,'stdout':''}
        self.assertEqual(self.execute(independent)[0]['status'],'failed')
        self.assertEqual(observed,[True])

    def test_harness_collision_traversal_and_mutation_fail(self):
        definition=self.env['checks']['unit']
        definition.update(argv=['/usr/bin/python3','-I','/work/.dispatch-verifier/check.py'],
                          harness={'check.py':'print("OK")'})
        def mutate_harness(request,runtime,stop):
            (Path(request['root'])/'.dispatch-verifier/check.py').write_text('tampered')
            return self.fake(request,runtime,stop)
        self.assertEqual(self.execute(mutate_harness)[0]['status'],'failed')
        for harness in ({'../check.py':'pass'},{'check.py':'pass','CHECK.py':'pass'}):
            definition['harness']=harness
            with patch('scripts.dispatch.verification.run_command') as command:
                self.assertEqual(run_checks(['unit'],self.record,self.env,threading.Event())[0]['status'],'failed')
            command.assert_not_called()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name);self.worker=base/'worker';self.worker.mkdir()
        self.stage=base/'staged';self.stage.mkdir()
        (self.worker/'a.txt').write_text('before')
        self.record=dict(root=str(self.worker),staging=str(self.stage),checks=['unit'],
            manifest_digest='f'*64,outputs=['a.txt'],initial_inventory=inventory(self.worker))
        (self.worker/'a.txt').write_text('after')
        self.env=dict(runtime={},deadline=time.monotonic()+30,checks={
            'unit':dict(platform='linux',argv=['/usr/bin/python3','-I','-c','print("OK")'],
                        expected_stdout='OK\n')})

    def fake(self,request,runtime,stop):
        self.assertNotEqual(request['root'],str(self.worker))
        return dict(status='completed',exit_code=0,termination_observed=True,
                    stdout='OK\n',stderr='',elapsed_seconds=.1)

    def execute(self,effect=None,stop=None):
        with patch('scripts.dispatch.verification.run_command',side_effect=effect or self.fake), \
             patch('scripts.dispatch.verification.linux_path',side_effect=lambda p:str(p)):
            return run_checks(['unit'],self.record,self.env,stop or threading.Event())

    def test_results_bind_exact_changes_and_manifest(self):
        from scripts.dispatch.workspace import collect_changes
        changes=collect_changes(self.record)
        result=self.execute()[0]
        self.assertEqual(result['status'],'passed')
        self.assertEqual(result['manifest_digest'],'f'*64)
        self.assertEqual(result['changes_digest'],manifest_digest({'changes':changes}))

    def test_source_or_verification_mutation_invalidates_result(self):
        def mutate_source(request,runtime,stop):
            (self.worker/'a.txt').write_text('tampered')
            return self.fake(request,runtime,stop)
        self.assertEqual(self.execute(mutate_source)[0]['status'],'failed')
        (self.worker/'a.txt').write_text('after')
        def mutate_copy(request,runtime,stop):
            (Path(request['root'])/'a.txt').write_text('tampered')
            return self.fake(request,runtime,stop)
        self.assertEqual(self.execute(mutate_copy)[0]['status'],'failed')

    def test_wrong_output_uncertain_and_stop_never_pass(self):
        for update in ({'stdout':'wrong'},{'termination_observed':False},{'exit_code':1}):
            def changed(request,runtime,stop):return self.fake(request,runtime,stop)|update
            self.assertEqual(self.execute(changed)[0]['status'],'failed')
        stop=threading.Event();stop.set()
        with patch('scripts.dispatch.verification.run_command') as command:
            result=run_checks(['unit'],self.record,self.env,stop)
        command.assert_not_called();self.assertEqual(result[0]['status'],'failed')

    def test_unregistered_or_windows_check_does_not_execute(self):
        for checks in ({},{'unit':dict(self.env['checks']['unit'],platform='windows')}):
            self.env['checks']=checks
            with patch('scripts.dispatch.verification.run_command') as command:
                result=run_checks(['unit'],self.record,self.env,threading.Event())
            command.assert_not_called();self.assertEqual(result[0]['status'],'failed')

    def test_adapter_exception_preserves_launch_uncertainty(self):
        def failed(*args):
            raise OSError('Receipt pipe failed')
        result = self.execute(failed)[0]
        self.assertIs(result.get('execution_started'), True)
        self.assertIsNot(result.get('termination_observed'), True)
