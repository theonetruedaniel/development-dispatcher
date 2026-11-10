from unittest.mock import patch
from pathlib import Path
from scripts.dispatch.integration import integrate, recover_integration
from scripts.dispatch.journal import Journal, JournalError
from scripts.dispatch.workspace import prepare_copy, collect_changes
from test_workspace import Workspace
from scripts.dispatch.contracts import manifest_digest


def passed(record, changes):
    return [{"id": "unit", "status": "passed",
             "manifest_digest": record["manifest_digest"],
             "changes_digest": manifest_digest({"changes": changes})}]


class Integration(Workspace):
    def test_stop_after_one_applied_file_keeps_remaining_and_lock(self):
        rec,changes,journal=self.prepared()
        stopped=False
        append=journal.append
        def stop_after_applied(row):
            nonlocal stopped
            append(row)
            if row.get('state')=='applied':stopped=True
        with patch.object(journal,'append',side_effect=stop_after_applied):
            result=integrate(self.root,rec,changes,passed(rec,changes),journal,
                             checkpoint=lambda:stopped)
        self.assertEqual(result['status'],'uncertain')
        self.assertEqual((self.root/'src/a.py').read_bytes(),b'changed')
        self.assertFalse((self.root/'src/b.py').exists())
        self.assertTrue(journal.lock_path.exists())

    def prepared(self):
        record = prepare_copy(self.raw, self.root, self.storage)
        target = Path(record["root"])
        (target / "src/a.py").write_bytes(b"changed")
        (target / "src/b.py").write_bytes(b"new")
        changes = collect_changes(record)
        journal = Journal(self.root / ".dispatch-runs" / "integration" / "journal.jsonl")
        return record, changes, journal

    def test_verified_integration_and_idempotent_recovery(self):
        rec, changes, journal = self.prepared()
        self.assertEqual(integrate(self.root, rec, changes, passed(rec, changes), journal)["status"], "integrated")
        self.assertEqual((self.root / "src/a.py").read_bytes(), b"changed")
        self.assertEqual(recover_integration(self.root, journal)["status"], "integrated")

    def test_user_edit_or_failing_check_prevents_any_apply(self):
        rec, changes, journal = self.prepared()
        self.assertEqual(integrate(self.root, rec, changes, [{"status": "failed"}], journal)["status"], "conflict")
        self.assertEqual((self.root / "src/a.py").read_bytes(), b"original")
        (self.root / "src/a.py").write_bytes(b"user")
        self.assertEqual(integrate(self.root, rec, changes, passed(rec, changes), journal)["status"], "conflict")
        self.assertFalse((self.root / "src/b.py").exists())
        self.assertEqual((self.root / "src/a.py").read_bytes(), b"user")

    def test_partial_failure_and_user_change_survive_recovery(self):
        rec, changes, journal = self.prepared()
        import scripts.dispatch.integration as implementation
        real = implementation.apply_one
        def fail_second(primary, record, change):
            if change["path"] == "src/b.py":
                raise OSError("simulated disk failure")
            return real(primary, record, change)
        with patch.object(implementation, "apply_one", side_effect=fail_second):
            self.assertEqual(integrate(self.root, rec, changes, passed(rec, changes), journal)["status"], "uncertain")
        (self.root / "src/a.py").write_bytes(b"user after interruption")
        self.assertEqual(recover_integration(self.root, journal)["status"], "conflict")
        self.assertEqual((self.root / "src/a.py").read_bytes(), b"user after interruption")
        self.assertFalse((self.root / "src/b.py").exists())

    def test_scope_and_writer_lock(self):
        rec, changes, journal = self.prepared()
        changes[0]["path"] = "../escape"
        with self.assertRaises(ValueError):
            integrate(self.root, rec, changes, [{"status": "passed"}], journal)
        changes = collect_changes(rec)
        other = Journal(self.root / ".dispatch-runs" / "other" / "journal.jsonl")
        with other.lock():
            with self.assertRaises(JournalError):
                integrate(self.root, rec, changes, [{"status": "passed"}], journal)
            other.append({"state": "completed"})

    def test_unrelated_passing_check_does_not_authorize_integration(self):
        rec, changes, journal = self.prepared()
        result = integrate(self.root, rec, changes, [{"id": "unrelated", "status": "passed"}], journal)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual((self.root / "src/a.py").read_bytes(), b"original")

    def test_replaced_primary_root_is_rejected(self):
        import os
        import shutil
        import subprocess
        rec, changes, journal = self.prepared()
        original = self.base / "original"
        victim = self.base / "victim"
        self.assertTrue(original.resolve().is_relative_to(self.base.resolve()))
        self.root.rename(original)
        shutil.copytree(original, victim)
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(self.root), str(victim)],
                           check=True, capture_output=True)
        else:
            self.root.symlink_to(victim, target_is_directory=True)
        journal = Journal(self.root / ".dispatch-runs" / "replacement" / "journal.jsonl")
        with self.assertRaises(ValueError):
            integrate(self.root, rec, changes, passed(rec, changes), journal)
        self.assertEqual((victim / "src/a.py").read_bytes(), b"original")
        with self.assertRaises(ValueError):
            recover_integration(self.root, journal)
        self.assertFalse((victim / ".dispatch-runs/active.json").exists())

    def test_stale_check_binding_is_rejected(self):
        rec, changes, journal = self.prepared()
        checks = passed(rec, changes)
        checks[0]["changes_digest"] = "0"*64
        self.assertEqual(integrate(self.root, rec, changes, checks, journal)["status"], "conflict")
        self.assertEqual((self.root / "src/a.py").read_bytes(), b"original")
