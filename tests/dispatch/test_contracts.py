import tempfile
import unittest
import subprocess
import os
from pathlib import Path
from helpers import manifest
from scripts.dispatch.contracts import validate_manifest, manifest_digest


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_defaults_and_digest(self):
        raw = manifest(workspace=str(self.root))
        normalized = validate_manifest(raw, self.root)
        self.assertEqual(normalized["mode"], "auto")
        self.assertEqual(manifest_digest(normalized), manifest_digest(dict(reversed(list(normalized.items())))))

    def test_rejects_bad_fields(self):
        for overrides in ({"extra": True}, {"task": ""}, {"max_attempts": True},
                          {"max_attempts": 4}, {"timeout_seconds": 0},
                          {"verification": []}, {"required": 1},
                          {"task_class": "anything"}, {"schema_version": True},
                          {"allowed_actions": ["send"]}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                validate_manifest(manifest(workspace=str(self.root), **overrides), self.root)
        for key in ("task", "workspace", "allowed_actions", "verification"):
            raw = manifest(workspace=str(self.root))
            del raw[key]
            with self.assertRaises(ValueError):
                validate_manifest(raw, self.root)

    def test_workspace_escape(self):
        for path in (str(self.root.parent), str(self.root / "..")):
            with self.assertRaises(ValueError):
                validate_manifest(manifest(workspace=path), self.root)

    def test_manual_requires_exact_pair(self):
        with self.assertRaises(ValueError):
            validate_manifest(manifest(workspace=str(self.root), mode="manual"), self.root)
        with self.assertRaises(ValueError):
            validate_manifest(manifest(workspace=str(self.root), manual_route={"model": "x", "effort": "low"}), self.root)

    def test_verification_paths_and_schema(self):
        for check in ({"type": "command", "command": "echo hi"},
                      {"type": "artifact_sha256", "path": "../x", "sha256": "a"*64},
                      {"type": "artifact_sha256", "path": "C:/x", "sha256": "a"*64},
                      {"type": "final_equals", "expected": "OK", "grant": True}):
            with self.assertRaises(ValueError):
                validate_manifest(manifest(workspace=str(self.root), verification=[check]), self.root)

    def test_symlink_escape(self):
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        link = self.root / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Creating a directory symlink requires Windows privilege.")
        self.addCleanup(link.unlink)
        with self.assertRaises(ValueError):
            validate_manifest(manifest(workspace=str(self.root), verification=[
                {"type": "artifact_sha256", "path": "link/file", "sha256": "a"*64}]), self.root)

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_junction_escape(self):
        outside = self.root.parent / (self.root.name + "-junction-target")
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        link = self.root / "junction"
        env = dict(os.environ, DISPATCH_TEST_LINK=str(link), DISPATCH_TEST_TARGET=str(outside))
        proc = subprocess.run(["powershell", "-NoProfile", "-Command",
            "New-Item -ItemType Junction -Path $env:DISPATCH_TEST_LINK -Target $env:DISPATCH_TEST_TARGET | Out-Null"],
            capture_output=True, env=env)
        if proc.returncode:
            self.skipTest("Windows denied junction fixture creation")
        self.addCleanup(link.rmdir)
        with self.assertRaises(ValueError):
            validate_manifest(manifest(workspace=str(self.root), verification=[
                {"type": "artifact_sha256", "path": "junction/file", "sha256": "a"*64}]), self.root)
