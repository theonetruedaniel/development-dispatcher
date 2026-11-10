import tempfile
from pathlib import Path
import unittest
import threading
from unittest.mock import patch

from scripts.dispatch.runtime_inventory import capture_inventory, verify_inventory


class RuntimeInventory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        (self.runtime / 'lib.py').write_bytes(b'original')

    def test_exact_inventory_detects_change_addition_and_removal(self):
        receipt = capture_inventory([str(self.runtime)])
        self.assertTrue(verify_inventory(receipt))
        (self.runtime / 'lib.py').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            verify_inventory(receipt)
        (self.runtime / 'lib.py').write_bytes(b'original')
        extra = self.runtime / 'extra.py'
        extra.write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, 'changed'):
            verify_inventory(receipt)
        extra.unlink()
        (self.runtime / 'lib.py').unlink()
        with self.assertRaisesRegex(ValueError, 'changed'):
            verify_inventory(receipt)

    def test_permissions_are_part_of_identity(self):
        receipt = capture_inventory([str(self.runtime)])
        path = self.runtime / 'lib.py'
        previous = path.stat().st_mode
        path.chmod(0o444)
        self.addCleanup(path.chmod, previous)
        with self.assertRaisesRegex(ValueError, 'changed'):
            verify_inventory(receipt)

    def test_unknown_fields_and_false_qualification_rejected(self):
        receipt = capture_inventory([str(self.runtime)])
        with self.assertRaises(ValueError):
            verify_inventory(dict(receipt, qualified=True))

    def test_overlapping_roots_and_linked_children_rejected(self):
        with self.assertRaises(ValueError):
            capture_inventory([str(self.runtime), str(self.runtime / 'lib.py')])
        # Hard links are permitted runtime aliases only when explicitly inventoried;
        # symlink descendants must not widen a directory mount.
        try:
            (self.runtime / 'linked').symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest('Host cannot create symlinks')
        with self.assertRaises(ValueError):
            capture_inventory([str(self.runtime)])

    def test_stop_before_verification_never_reads_runtime(self):
        receipt=capture_inventory([str(self.runtime)])
        stop=threading.Event();stop.set()
        with patch('pathlib.Path.open') as opened, self.assertRaises(InterruptedError):
            verify_inventory(receipt,stop=stop)
        opened.assert_not_called()

    def test_stop_during_file_hash_returns_no_partial_inventory(self):
        import hashlib
        path=self.runtime/'large.bin';path.write_bytes(b'x'*(4*1024*1024))
        stop=threading.Event();actual=hashlib.sha256
        class CancellingHash:
            def __init__(self):self.inner=actual();self.bytes=0
            def update(self,data):
                self.bytes+=len(data);self.inner.update(data);stop.set()
            def hexdigest(self):return self.inner.hexdigest()
        hashed=CancellingHash()
        with patch('scripts.dispatch.runtime_inventory.hashlib.sha256',return_value=hashed),\
             self.assertRaises(InterruptedError):
            capture_inventory([str(path)],stop=stop)
        self.assertLess(hashed.bytes,path.stat().st_size)
