import tempfile
import threading
import time
import unittest
from pathlib import Path
from helpers import manifest, FakeClient
from scripts.dispatch.contracts import validate_manifest, manifest_digest
from scripts.dispatch.journal import Journal
from scripts.dispatch.runner import run_package


class IntegrationTests(unittest.TestCase):
    def test_routine_complex_routine_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fake = FakeClient(["OK", "OK", "OK"])
            for index, kind in enumerate(("routine", "consequential", "routine")):
                work = validate_manifest(manifest(workspace=str(root), package_id=f"work-{index}", task_class=kind), root)
                grant = dict(schema_version=1, manifest_digest=manifest_digest(work),
                             workspace=str(root), account_fingerprint="a"*64,
                             expires_at=time.time()+600, allowed_actions=["read"],
                             max_attempts=2, dedicated_session=True)
                journal = Journal(root / ".dispatch-runs" / f"run-{index}" / "journal.jsonl")
                result = run_package(work, fake, journal, grant, threading.Event())
                self.assertEqual(result["state"], "completed")
            self.assertEqual([(p["model"], p["effort"]) for p in fake.turn_params],
                             [("gpt-6-luna", "low"), ("gpt-6-astra", "high"), ("gpt-6-luna", "low")])
