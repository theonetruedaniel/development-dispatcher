import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from helpers import manifest, catalog
from scripts.dispatch.__main__ import main
from scripts.dispatch.contracts import validate_manifest, manifest_digest
import time


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "manifest.json"
        self.catalog = self.root / "catalog.json"
        self.work.write_text(json.dumps(manifest(workspace=str(self.root))), encoding="utf-8")
        self.catalog.write_text(json.dumps(catalog()), encoding="utf-8")

    def test_preview_never_launches(self):
        output = io.StringIO()
        with mock.patch("subprocess.Popen", side_effect=AssertionError("spawn")), contextlib.redirect_stdout(output):
            code = main(["preview", "--manifest", str(self.work), "--catalog", str(self.catalog)])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["decision"]["route"]["model"], "gpt-6-luna")
        self.assertEqual(result["evidence"], "offline preview; supplied catalog not live verified")
        self.assertFalse(result["dispatched"])

    def test_default_preview(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--manifest", str(self.work), "--catalog", str(self.catalog)]), 0)

    def test_execute_no_grant_no_process(self):
        with mock.patch("subprocess.Popen", side_effect=AssertionError("spawn")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["execute", "--manifest", str(self.work),
                                   "--authorization", str(self.root / "absent.json")]), 2)

    def test_run_id_rejects_traversal(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["status", "--run", "../outside", "--workspace", str(self.root)]), 2)

    def test_invalid_catalog(self):
        self.catalog.write_text('{"gpt-6-luna": "low"}')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["preview", "--manifest", str(self.work), "--catalog", str(self.catalog)]), 2)

    def test_valid_grant_cannot_enable_unqualified_live_adapter(self):
        work = validate_manifest(json.loads(self.work.read_text()), self.root)
        grant = dict(schema_version=1, manifest_digest=manifest_digest(work),
                     workspace=str(self.root), account_fingerprint="a"*64,
                     expires_at=time.time()+600, allowed_actions=["read"],
                     max_attempts=2, dedicated_session=True)
        grant_path = self.root / "grant.json"
        grant_path.write_text(json.dumps(grant))
        output = io.StringIO()
        with mock.patch("scripts.dispatch.__main__.qualified_client", side_effect=ValueError("isolation unqualified")), contextlib.redirect_stdout(output):
            self.assertEqual(main(["execute", "--manifest", str(self.work),
                                   "--authorization", str(grant_path)]), 2)
        self.assertIn("isolation", json.loads(output.getvalue())["reason"])


    def test_execute_uses_qualified_client_and_returns_verified_result(self):
        from helpers import FakeClient
        work = validate_manifest(json.loads(self.work.read_text()), self.root)
        grant = dict(schema_version=1, manifest_digest=manifest_digest(work),
                     workspace=str(self.root), account_fingerprint="a"*64,
                     expires_at=time.time()+600, allowed_actions=["read"],
                     max_attempts=2, dedicated_session=True)
        path=self.root/'grant.json';path.write_text(json.dumps(grant))
        client=FakeClient()
        output=io.StringIO()
        with mock.patch('scripts.dispatch.__main__.qualified_client',return_value=client), contextlib.redirect_stdout(output):
            self.assertEqual(main(['execute','--manifest',str(self.work),'--authorization',str(path)]),0)
        self.assertEqual(json.loads(output.getvalue())['state'],'completed')
        self.assertEqual(len(client.turn_params),1)
