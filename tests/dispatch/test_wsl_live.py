"""Opt-in synthetic qualification; never runs model inference or real project code."""
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import unittest


@unittest.skipUnless(os.name == "nt" and os.environ.get("ABRAMS_WSL_QUALIFY") == "1",
                     "Explicit Windows/WSL synthetic qualification only")
class WslLive(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.worker = Path(self.temp.name) / "worker"
        self.worker.mkdir()

    @staticmethod
    def linux(path):
        path = str(Path(path).resolve())
        if not path.startswith("C:"):
            raise ValueError("Qualification expects C drive")
        return "/mnt/c/" + path[3:].replace("\\", "/")

    def launch(self, code, timeout=5, limit=65536):
        script = Path(__file__).resolve().parents[2] / "scripts/dispatch/wsl_supervisor.py"
        process = subprocess.Popen(
            ["wsl.exe", "-d", "Ubuntu", "--", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin",
             "/usr/bin/python3", "-I", self.linux(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.cleanup_process, process)
        results = queue.Queue()
        def read():
            results.put(process.stdout.readline(6*1024*1024))
        threading.Thread(target=read, daemon=True).start()
        request = dict(root=self.linux(self.worker), argv=["/usr/bin/python3", "-c", code],
                       timeout_seconds=timeout, output_limit=limit)
        process.stdin.write(json.dumps(request).encode()+b"\n")
        process.stdin.flush()
        return process, results

    def cleanup_process(self, process):
        if process.poll() is None:
            try:
                process.stdin.write(b"STOP\n")
                process.stdin.flush()
                process.wait(timeout=3)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()

    def result(self, process, results):
        raw = results.get(timeout=15)
        process.wait(timeout=3)
        self.assertEqual(process.returncode, 0, process.stderr.read().decode(errors="replace"))
        return json.loads(raw)

    def test_persistent_edit_and_denials(self):
        code = """import pathlib,json,socket,errno
pathlib.Path('/work/result').write_text('ok')
result={'home_hidden':not pathlib.Path('/home').exists(),
        'host_hidden':not pathlib.Path('/mnt/c').exists()}
try:
 pathlib.Path('/usr/probe').write_text('forbidden')
 result['runtime_readonly']=False
except OSError as e:
 result['runtime_readonly']=e.errno in (errno.EROFS,errno.EACCES)
s=socket.socket(); s.settimeout(1)
result['network_errno']=s.connect_ex(('1.1.1.1',443))
print(json.dumps(result))
"""
        process, output = self.launch(code)
        result = self.result(process, output)
        self.assertEqual(result["status"], "completed")
        checks = json.loads(base64.b64decode(result["stdout_b64"]))
        self.assertEqual(checks, dict(home_hidden=True, host_hidden=True,
                                    runtime_readonly=True, network_errno=101))
        self.assertEqual((self.worker / "result").read_text(), "ok")

    def test_stop_kills_child_before_deadline(self):
        self.check_stop_child(detached=False)

    def test_stop_kills_detached_child_before_deadline(self):
        self.check_stop_child(detached=True)

    def check_stop_child(self, detached):
        child = "import pathlib,time,os\n" + ("os.setsid()\n" if detached else "")
        child += "p=pathlib.Path('/work/beat')\nwhile True:\n p.write_text(str(time.time()))\n time.sleep(.05)"
        code = f"import subprocess,time\nsubprocess.Popen(['/usr/bin/python3','-c',{child!r}])\ntime.sleep(30)"
        process, output = self.launch(code, timeout=10)
        beat = self.worker / "beat"
        deadline = time.monotonic()+8
        while not beat.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(beat.exists(), "Child never started; cancellation is unproven")
        started = time.monotonic()
        process.stdin.write(b"STOP\n")
        process.stdin.flush()
        result = self.result(process, output)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["termination_observed"])
        self.assertLess(time.monotonic()-started, 3)
        before = beat.read_bytes()
        time.sleep(.3)
        self.assertEqual(beat.read_bytes(), before)

    def test_parent_exit_terminates_detached_child(self):
        child = "import os,time,pathlib; os.setsid(); pathlib.Path('/work/ready').write_text('ready'); time.sleep(1); pathlib.Path('/work/late').write_text('escaped')"
        code = (f"import pathlib,subprocess,time\nsubprocess.Popen(['/usr/bin/python3','-c',{child!r}])\n"
                "while not pathlib.Path('/work/ready').exists(): time.sleep(.01)\n")
        process, output = self.launch(code, timeout=10)
        result = self.result(process, output)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue((self.worker / 'ready').exists())
        time.sleep(1.2)
        self.assertFalse((self.worker / 'late').exists())

    def test_timeout_and_output_limit(self):
        process, output = self.launch("import time; time.sleep(30)", timeout=1)
        self.assertEqual(self.result(process, output)["status"], "timeout")
        process, output = self.launch("import os; os.write(1,b'x'*10000)", limit=100)
        result = self.result(process, output)
        self.assertEqual(result["status"], "output_limit")
        self.assertEqual(len(base64.b64decode(result["stdout_b64"])), 100)

    def test_controller_disconnect_stops_job(self):
        process, output = self.launch("import time; time.sleep(30)")
        process.stdin.close()
        result = self.result(process, output)
        self.assertEqual(result["status"], "controller_disconnected")
        self.assertTrue(result["termination_observed"])

    def adapter_runtime(self):
        from scripts.dispatch.wsl_adapter import runtime_files
        return {'distro':'Ubuntu','files':{
            name:hashlib.sha256(path.read_bytes()).hexdigest()
            for name,path in runtime_files().items()}}

    def test_adapter_persists_edit_and_rejects_drift(self):
        from scripts.dispatch.wsl_adapter import run_command
        runtime = self.adapter_runtime()
        request = dict(root=self.linux(self.worker),
                       argv=['/usr/bin/python3','-c',"from pathlib import Path; Path('/work/adapter').write_text('ok'); print('done')"],
                       timeout_seconds=30,output_limit=4096)
        result = run_command(request, runtime, threading.Event())
        self.assertEqual(result['status'], 'completed', result)
        self.assertTrue(result['termination_observed'])
        self.assertEqual((self.worker/'adapter').read_text(), 'ok')
        self.assertEqual(result['stdout'].strip(), 'done')
        runtime['files']['supervisor'] = '0'*64
        result = run_command(request, runtime, threading.Event())
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(result['launched'])

    def test_adapter_stop_observes_termination(self):
        from scripts.dispatch.wsl_adapter import run_command
        stop = threading.Event()
        result_box = []
        request = dict(root=self.linux(self.worker), argv=['/usr/bin/python3','-c',
            "import time,pathlib\np=pathlib.Path('/work/adapter-beat')\nwhile True:\n p.write_text(str(time.time()))\n time.sleep(.03)"],
            timeout_seconds=30,output_limit=4096)
        thread = threading.Thread(target=lambda: result_box.append(run_command(request,self.adapter_runtime(),stop)))
        thread.start()
        self.addCleanup(thread.join, 20)
        self.addCleanup(stop.set)
        beat = self.worker/'adapter-beat'
        deadline = time.monotonic()+20
        while not beat.exists() and time.monotonic()<deadline:
            time.sleep(.05)
        self.assertTrue(beat.exists(), 'Adapter command did not start')
        stop.set()
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box[0]['status'], 'cancelled', result_box)
        self.assertTrue(result_box[0]['termination_observed'])
        before = beat.read_bytes()
        time.sleep(.2)
        self.assertEqual(before, beat.read_bytes())
