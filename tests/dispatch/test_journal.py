import json
import tempfile
import unittest
import subprocess
import sys
import threading
from pathlib import Path
from scripts.dispatch.journal import Journal, JournalError


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "run" / "journal.jsonl"
        self.j = Journal(self.path)

    def test_roundtrip_checkpoint(self):
        with self.j.lock():
            self.j.append({"state": "prepared"})
            self.j.append({"state": "completed"})
        self.assertEqual([r["seq"] for r in Journal(self.path).records()], [1, 2])
        self.assertEqual(json.loads(self.path.with_suffix(".checkpoint.json").read_text())["state"], "completed")

    def test_integration_can_reenter_without_releasing_writer(self):
        errors=[]
        def contender():
            try:
                with self.j.lock():
                    errors.append('unexpected acquisition')
            except JournalError:
                errors.append('blocked')
        with self.j.lock():
            self.j.append({'state':'prepared'})
            with self.j.lock():
                self.j.append({'state':'completed'})
            self.assertTrue(self.j.lock_path.exists())
            thread=threading.Thread(target=contender);thread.start();thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors,['blocked'])
        self.assertFalse(self.j.lock_path.exists())

    def test_exclusion_and_uncertain_lock_retention(self):
        with self.j.lock():
            self.j.append({"state": "uncertain"})
            with self.assertRaises(JournalError):
                with Journal(self.path.parent.parent / "other" / "journal.jsonl").lock():
                    pass
        self.assertTrue(self.j.lock_path.exists())

    def test_corrupt_tail_preserved(self):
        self.j.append({"state": "prepared"})
        with self.path.open("ab") as f:
            f.write(b'{"incomplete"')
        original = self.path.read_bytes()
        with self.assertRaises(JournalError):
            self.j.records()
        self.assertEqual(original, self.path.read_bytes())

    def test_sequence_mutation_rejected(self):
        self.j.append({"state": "prepared"})
        row = json.loads(self.path.read_text())
        row["seq"] = 99
        self.path.write_text(json.dumps(row) + "\n")
        with self.assertRaises(JournalError):
            self.j.records()

    def test_process_death_releases_writer_but_keeps_active_owner(self):
        code = ("import os,sys; from pathlib import Path; "
                "from scripts.dispatch.journal import Journal; "
                "j=Journal(Path(sys.argv[1])); guard=j.lock(); guard.__enter__(); "
                "j.append({'state':'uncertain'}); os._exit(0)")
        result = subprocess.run([sys.executable, "-c", code, str(self.path)], timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(self.j.lock_path.exists())
        with self.j.lock(recover=True):
            self.j.append({"state": "reconciled"})
        self.assertFalse(self.j.lock_path.exists())
