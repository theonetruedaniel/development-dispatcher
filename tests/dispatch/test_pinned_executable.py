import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.dispatch import live


class PinnedExecutableTests(unittest.TestCase):
    def resolve(self, pin):
        self.assertTrue(callable(getattr(live, "resolve_executable", None)), "Pinned executable resolver is missing")
        return live.resolve_executable(pin)

    def test_explicit_pinned_executable_works_without_path(self):
        with tempfile.TemporaryDirectory() as root:
            exe = Path(root) / "codex.exe"
            exe.write_bytes(b"fixture")
            with patch("scripts.dispatch.live.shutil.which", return_value=None):
                self.assertEqual(self.resolve({"executable_path": str(exe)}), str(exe))

    def test_relative_pinned_executable_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            self.resolve({"executable_path": "codex.exe"})

    def test_missing_explicit_path_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("scripts.dispatch.live.shutil.which", return_value="other.exe"):
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    self.resolve({"executable_path": str(Path(root) / "missing.exe")})

    def test_legacy_path_discovery_still_works(self):
        with patch("scripts.dispatch.live.shutil.which", return_value="legacy.exe"):
            self.assertEqual(self.resolve({}), "legacy.exe")
