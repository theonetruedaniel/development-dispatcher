import tempfile
import threading
import time
import unittest
from pathlib import Path
from helpers import manifest, FakeClient
from scripts.dispatch.contracts import validate_manifest, manifest_digest
from scripts.dispatch.journal import Journal
from scripts.dispatch.runner import run_package, reconcile
from scripts.dispatch.protocol import ProtocolError


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = validate_manifest(manifest(workspace=str(self.root)), self.root)
        self.j = Journal(self.root / ".dispatch-runs" / "one" / "journal.jsonl")
        self.fake = FakeClient()
        self.stop = threading.Event()
        self.grant = dict(schema_version=1, manifest_digest=manifest_digest(self.work),
                          workspace=str(self.root), account_fingerprint="a"*64,
                          expires_at=time.time()+600, allowed_actions=["read"],
                          max_attempts=2, dedicated_session=True)

    def run_it(self):
        return run_package(self.work, self.fake, self.j, self.grant, self.stop)

    def test_completed_and_no_replay(self):
        self.assertEqual(self.run_it()["state"], "completed")
        self.assertEqual(self.fake.turn_params[0]["model"], "gpt-6-luna")
        self.assertEqual(self.fake.turn_params[0]["effort"], "low")
        self.assertEqual(self.run_it()["state"], "completed")
        self.assertEqual(len(self.fake.turn_params), 1)
        self.assertIsNone(self.j.records()[-1]["observed_route"])

    def test_completed_text_is_returned_and_reused_without_redispatch(self):
        first = self.run_it()
        self.assertEqual(first.get("final"), "OK")
        self.assertEqual(self.run_it().get("final"), "OK")
        self.assertEqual(len(self.fake.turn_params), 1)

    def test_uncertain_intent_never_replayed(self):
        self.j.append({"state": "dispatching", "manifest_digest": manifest_digest(self.work)})
        self.assertEqual(self.run_it()["state"], "uncertain")
        self.assertEqual(self.fake.methods, [])

    def test_invalid_auth_before_start(self):
        for changes in ({"expires_at": 0}, {"manifest_digest": "bad"},
                        {"dedicated_session": False}, {"max_attempts": True}):
            self.grant.update(changes)
            self.assertEqual(self.run_it()["state"], "blocked")
        self.assertEqual(self.fake.methods, [])

    def test_account_and_isolation_fail_closed(self):
        self.fake.account = "b"*64
        self.assertEqual(self.run_it()["state"], "blocked")
        self.fake.account = "a"*64
        self.fake.isolation_verified = False
        self.assertEqual(self.run_it()["state"], "blocked")
        self.assertNotIn("thread/start", self.fake.methods)

    def test_quality_failure_escalates_once(self):
        self.fake.answers = ["WRONG", "OK"]
        self.assertEqual(self.run_it()["state"], "completed")
        self.assertEqual([p["effort"] for p in self.fake.turn_params], ["low", "medium"])

    def test_stop_before_dispatch(self):
        self.stop.set()
        self.assertEqual(self.run_it()["state"], "cancelled")
        self.assertEqual(self.fake.methods, [])

    def test_stop_in_flight_and_approval(self):
        self.fake.event_mode = "wait"
        self.fake.on_start = self.stop.set
        self.assertEqual(self.run_it()["state"], "cancelled")
        self.assertIn("turn/interrupt", self.fake.methods)

    def test_approval_stops_without_retry(self):
        self.fake.event_mode = "approval"
        self.assertEqual(self.run_it()["state"], "awaiting_user")
        self.assertEqual(len(self.fake.turn_params), 1)
        self.assertIn("turn/interrupt", self.fake.methods)

    def test_timeout_reconciles_interrupt(self):
        self.work["timeout_seconds"] = 1
        self.grant["manifest_digest"] = manifest_digest(self.work)
        self.fake.event_mode = "wait"
        self.assertEqual(self.run_it()["state"], "cancelled")

    def test_failed_provider_never_quality_retry(self):
        self.fake.turn_status = "failed"
        self.assertEqual(self.run_it()["state"], "failed")
        self.assertEqual(len(self.fake.turn_params), 1)

    def test_changed_manifest_no_dispatch(self):
        self.assertEqual(self.run_it()["state"], "completed")
        self.work["task"] = "Changed"
        self.grant["manifest_digest"] = manifest_digest(self.work)
        self.assertEqual(self.run_it()["state"], "blocked")
        self.assertEqual(len(self.fake.turn_params), 1)

    def test_reconcile_does_not_redispatch(self):
        self.j.append({"state": "uncertain", "manifest_digest": manifest_digest(self.work),
                       "thread_id": "thread-owned", "turn_id": "turn-1",
                       "workspace": str(self.root), "account_fingerprint": "a"*64})
        result = reconcile(self.j, self.fake, self.grant)
        self.assertEqual(result["state"], "reconciled")
        self.assertNotIn("turn/start", self.fake.methods)

    def test_malformed_ack_is_uncertain_and_keeps_lock(self):
        original = self.fake.request
        def malformed(method, params, timeout=15):
            if method == "turn/start":
                return {"turn": None}
            return original(method, params, timeout)
        self.fake.request = malformed
        self.assertEqual(self.run_it()["state"], "uncertain")
        self.assertTrue(self.j.lock_path.exists())

    def test_interrupt_error_keeps_uncertainty(self):
        original = self.fake.request
        def lost(method, params, timeout=15):
            if method == "turn/interrupt":
                raise TimeoutError("lost")
            return original(method, params, timeout)
        self.fake.request = lost
        self.fake.event_mode = "approval"
        self.assertEqual(self.run_it()["state"], "uncertain")
        self.assertTrue(self.j.lock_path.exists())

    def test_expiry_during_metadata_blocks_turn(self):
        original = self.fake.usage
        def expire():
            self.grant["expires_at"] = 0
            return original()
        self.fake.usage = expire
        self.assertEqual(self.run_it()["state"], "blocked")
        self.assertNotIn("thread/start", self.fake.methods)

    def test_stop_during_metadata_does_not_create_thread(self):
        original = self.fake.usage
        def stop():
            self.stop.set()
            return original()
        self.fake.usage = stop
        self.assertEqual(self.run_it()["state"], "cancelled")
        self.assertNotIn("thread/start", self.fake.methods)

    def test_lost_second_ack_cannot_reconcile_first_turn(self):
        original = self.fake.request
        self.fake.answers = ["WRONG"]
        def lost_second(method, params, timeout=15):
            if method == "turn/start" and self.fake.turn_params:
                raise TimeoutError("second acknowledgement lost")
            return original(method, params, timeout)
        self.fake.request = lost_second
        self.assertEqual(self.run_it()["state"], "uncertain")
        self.assertNotIn("turn_id", self.j.records()[-1])
        result = reconcile(self.j, self.fake, self.grant)
        self.assertEqual(result["state"], "uncertain")
        self.assertNotIn("thread/read", self.fake.methods)

    def test_metadata_failure_releases_workspace(self):
        def fail():
            raise ProtocolError("metadata failure")
        self.fake.catalog = fail
        self.assertEqual(self.run_it()["state"], "blocked")
        self.assertFalse(self.j.lock_path.exists())
        self.assertNotIn("thread/start", self.fake.methods)

    def test_live_thread_validation_before_model_work(self):
        def rejected(response, route):
            raise ValueError('Inventory not isolated')
        self.fake.verify_thread = rejected
        self.assertEqual(self.run_it()['state'], 'blocked')
        self.assertEqual(self.fake.turn_params, [])
        self.assertEqual(self.j.records()[-1]['thread_id'], 'thread-owned')

    def test_thread_route_acceptance_is_recorded_separately(self):
        self.fake.verify_thread = lambda response,route: {'model': route.model, 'effort': route.effort}
        result=self.run_it()
        self.assertEqual(result['accepted_route'], {'model':'gpt-6-luna','effort':'low'})
        self.assertIsNone(result['observed_route'])

    def test_stop_handles_ack_before_server_turn_becomes_interruptible(self):
        original=self.fake.request
        calls=[]
        self.fake.event_mode='pending'
        self.fake.on_start=self.stop.set
        def request(method,params,timeout=15):
            if method=='turn/interrupt':
                calls.append(method)
                if len(calls)==1:raise ProtocolError('no active turn to interrupt')
            return original(method,params,timeout)
        self.fake.request=request
        result=self.run_it()
        self.assertEqual(result['state'],'cancelled')
        self.assertTrue(result['interruption_confirmed'])
        self.assertEqual(len(self.fake.turn_params),1)
