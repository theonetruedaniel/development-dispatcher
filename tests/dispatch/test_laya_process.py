import json
import os
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scripts.dispatch.laya_process import LayaProcess, decode_proposal

REVISION = 'a'*40


class ProposalProtocol(unittest.TestCase):
    def test_exact_revision_and_schema_are_required(self):
        valid = dict(status='ok', task_class='routine', selector_revision=REVISION,
                     reason_code='classified')
        self.assertEqual(decode_proposal(json.dumps(valid).encode(), REVISION), valid)
        for change in ({'selector_revision': 'b'*40}, {'task_class': 'root'},
                       {'grant': 'all'}, {'reason_code': 'other'}, {'status': 'ready'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                decode_proposal(json.dumps(valid | change).encode(), REVISION)
        with self.assertRaises(ValueError):
            decode_proposal(b'{"status":"ok","status":"overflow"}', REVISION)


@unittest.skipUnless(sys.platform == 'linux', 'Linux-side subprocess transport')
class ProcessTransport(unittest.TestCase):
    def make(self, body, **kwargs):
        prelude = "import sys,json,time,os\nrev='"+REVISION+"'\n"
        prelude += "print(json.dumps(dict(status='ready',selector_revision=rev)),flush=True)\n"
        return LayaProcess([sys.executable, '-I', '-u', '-c', prelude+body], REVISION,
                           startup_timeout=.5, **kwargs)

    def test_reuses_one_worker_and_bounds_valid_requests(self):
        body = "for line in sys.stdin:\n print(json.dumps(dict(status='ok',task_class='routine',selector_revision=rev,reason_code='classified')),flush=True)"
        with self.make(body) as worker:
            self.assertEqual(worker.request('one')['status'], 'ok')
            pid = worker.pid
            self.assertEqual(worker.request('two')['task_class'], 'routine')
            self.assertEqual(worker.pid, pid)
            self.assertEqual(worker.request('x'*300000)['status'], 'overflow')
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_timeout_kills_worker_and_prevents_reuse(self):
        with self.make("sys.stdin.readline();time.sleep(30)") as worker:
            started = time.monotonic()
            self.assertEqual(worker.request('one', timeout_seconds=.1)['status'], 'timeout')
            self.assertLess(time.monotonic()-started, 2)
            pid = worker.pid
            self.assertEqual(worker.request('two')['status'], 'unavailable')
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_malformed_oversized_and_stderr_flood_kill_worker(self):
        for code in ("print('bad',flush=True)", "print('x'*65537,flush=True)",
                     "os.write(2,b'x'*100000);time.sleep(30)"):
            with self.subTest(code=code), self.make('sys.stdin.readline();'+code) as worker:
                self.assertEqual(worker.request('one')['status'], 'unavailable')
                with self.assertRaises(ProcessLookupError):
                    os.kill(worker.pid, 0)

    def test_stop_during_wait_and_preexisting_stop(self):
        stop = threading.Event()
        with self.make('sys.stdin.readline();time.sleep(30)', stop=stop) as worker:
            timer = threading.Timer(.1, stop.set)
            timer.start()
            try:
                self.assertEqual(worker.request('one')['status'], 'unavailable')
            finally:
                timer.join()
            with self.assertRaises(ProcessLookupError):
                os.kill(worker.pid, 0)
        with self.make('time.sleep(30)', stop=stop) as worker:
            self.assertEqual(worker.request('one')['status'], 'unavailable')
            self.assertIsNone(worker.pid)

    def test_cold_start_timeout_and_exited_worker(self):
        worker = LayaProcess([sys.executable, '-I', '-c', 'import time;time.sleep(30)'],
                             REVISION, startup_timeout=.1)
        with worker:
            self.assertEqual(worker.request('one')['status'], 'timeout')
            with self.assertRaises(ProcessLookupError):
                os.kill(worker.pid, 0)
        with self.make('sys.exit(0)') as worker:
            self.assertEqual(worker.request('one')['status'], 'unavailable')

    def test_blocked_request_write_has_same_deadline(self):
        with self.make('time.sleep(30)') as worker:
            started = time.monotonic()
            self.assertEqual(worker.request('x'*200000, timeout_seconds=.1)['status'], 'timeout')
            self.assertLess(time.monotonic()-started, 2)

    def test_limits_cannot_be_relaxed(self):
        with self.make('time.sleep(30)') as worker:
            for limit in (0, -1, 6, float('nan'), True):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    worker.request('one', timeout_seconds=limit)
            self.assertIsNone(worker.pid)

    def test_close_does_not_signal_reaped_pid_again(self):
        worker = self.make('sys.stdin.readline();time.sleep(30)')
        worker.request('one', timeout_seconds=.05)
        with patch('os.killpg') as kill:
            worker.close()
            worker.close()
        kill.assert_not_called()

    def test_close_does_not_signal_worker_already_reaped_by_poll(self):
        worker = self.make('sys.exit(0)')
        # Start and reap a fixture directly to cover the exit/cleanup race.
        import subprocess
        worker.process = subprocess.Popen([sys.executable, '-c', 'pass'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        worker.process.wait(timeout=3)
        with patch('os.killpg') as kill:
            worker.close()
        kill.assert_not_called()


@unittest.skipUnless(sys.platform == 'linux' and os.environ.get('ABRAMS_WSL_QUALIFY') == '1',
                     'Explicit Linux/Bubblewrap synthetic qualification')
class NamespaceTermination(unittest.TestCase):
    def test_timeout_kills_detached_descendant_after_observed_heartbeat(self):
        from scripts.dispatch.wsl_supervisor import sandbox_argv, verify_host
        with tempfile.TemporaryDirectory() as directory:
            verify_host(directory)
            child = ("import os,time,pathlib;os.setsid();p=pathlib.Path('/work/beat')\n"
                     "while True:\n p.write_text(str(time.time_ns()))\n time.sleep(.02)")
            code = ("import subprocess,time,pathlib,json,sys\n"
                    f"subprocess.Popen(['/usr/bin/python3','-c',{child!r}])\n"
                    "while not pathlib.Path('/work/beat').exists():time.sleep(.01)\n"
                    f"print(json.dumps(dict(status='ready',selector_revision={REVISION!r})),flush=True)\n"
                    "sys.stdin.readline();time.sleep(30)")
            argv = sandbox_argv(dict(root=directory, argv=['/usr/bin/python3','-c',code]))
            with LayaProcess(argv, REVISION, startup_timeout=5) as worker:
                self.assertEqual(worker.request('one', timeout_seconds=.1)['status'], 'timeout')
                self.assertTrue(worker.reaped)
            beat = Path(directory)/'beat'
            self.assertTrue(beat.exists(), 'No observed child startup')
            before = beat.read_bytes()
            time.sleep(.4)
            self.assertEqual(beat.read_bytes(), before, 'Detached child survived cleanup')
