import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from scripts.dispatch.workspace import prepare_copy, collect_changes
from test_write_contracts import work


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src/a.py").write_bytes(b"original")
        (self.root / "unrelated").write_bytes(b"keep")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "baseline")
        self.raw = work(self.root)
        self.raw["baseline"]["commit"] = self.git("rev-parse", "HEAD").strip()
        self.storage = self.base / "copies"

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True)

    def test_selective_copy_and_changes(self):
        (self.root / "unrelated").write_bytes(b"dirty")
        record = prepare_copy(self.raw, self.root, self.storage)
        target = Path(record["root"])
        self.assertFalse((target / "unrelated").exists())
        self.assertFalse((target / ".git").exists())
        (target / "src/a.py").write_bytes(b"changed")
        (target / "src/new.py").write_bytes(b"new")
        changes = collect_changes(record)
        self.assertEqual([(c["path"], c["operation"]) for c in changes],
                         [("src/a.py", "modify"), ("src/new.py", "create")])
        self.assertEqual((self.root / "src/a.py").read_bytes(), b"original")
        self.assertEqual((self.root / "unrelated").read_bytes(), b"dirty")

    def test_overlay_and_stale_source(self):
        source = self.root / "src/a.py"
        source.write_bytes(b"dirty")
        with self.assertRaises(ValueError):
            prepare_copy(self.raw, self.root, self.storage)
        self.raw["baseline"]["overlays"] = [
            dict(path="src/a.py", sha256=hashlib.sha256(b"dirty").hexdigest())]
        record = prepare_copy(self.raw, self.root, self.storage)
        self.assertEqual((Path(record["root"]) / "src/a.py").read_bytes(), b"dirty")
        source.write_bytes(b"newer")
        with self.assertRaises(ValueError):
            prepare_copy(self.raw, self.root, self.storage)

    def test_deletion_out_of_scope_and_hardlink(self):
        record = prepare_copy(self.raw, self.root, self.storage)
        target = Path(record["root"])
        (target / "src/a.py").unlink()
        self.assertEqual(collect_changes(record)[0]["operation"], "delete")
        (target / "outside").write_bytes(b"bad")
        with self.assertRaises(ValueError):
            collect_changes(record)
        (target / "outside").unlink()
        os.link(self.root / "src/a.py", target / "src/linked.py")
        with self.assertRaises(ValueError):
            collect_changes(record)
