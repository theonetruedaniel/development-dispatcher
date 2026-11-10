"""Controller-only changeset application. Not exposed to dispatched workers."""
import os
import re
import tempfile
from pathlib import Path
from .workspace import digest
from .write_contracts import safe_path, permits, safe_root, root_identity
from .contracts import manifest_digest


def current_hash(primary, name):
    path = safe_path(primary, name)
    return digest(path.read_bytes()) if path.exists() else None


def validate(primary, record, changes, journal):
    primary = safe_root(primary)
    if (safe_root(Path(record["primary"])) != primary or
            record["primary_identity"] != root_identity(primary)):
        raise ValueError("Primary mismatch")
    expected = primary / ".dispatch-runs"
    if journal.path.parent.parent != expected or expected.is_symlink():
        raise ValueError("Journal must use the primary writer lock")
    safe_path(primary, ".dispatch-runs")
    seen = set()
    for change in changes:
        if set(change) != {"path", "operation", "before_sha256", "after_sha256"}:
            raise ValueError("Invalid change record")
        name = change["path"]
        if not permits(name, record["outputs"]) or name.casefold() in seen:
            raise ValueError("Output scope violation")
        seen.add(name.casefold())
        safe_path(primary, name)
        before, after = change["before_sha256"], change["after_sha256"]
        if before is None and after is None or before == after:
            raise ValueError("Empty change")
        for value in (before, after):
            if value is not None and (not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value)):
                raise ValueError("Invalid change hash")
        expected_op = "create" if before is None else "delete" if after is None else "modify"
        if change["operation"] != expected_op or record["initial_inventory"].get(name) != before:
            raise ValueError("Change disagrees with baseline")


class IntegrationCancelled(ValueError):
    pass


def check_authority(checkpoint):
    if checkpoint is not None and checkpoint():
        raise IntegrationCancelled('Integration stopped or expired')


def apply_one(primary, record, change, *, checkpoint=None):
    name = change["path"]
    if current_hash(primary, name) != change["before_sha256"]:
        raise ValueError("Target changed during application")
    path = safe_path(primary, name)
    after = change["after_sha256"]
    if after is None:
        check_authority(checkpoint)
        path.unlink()
        return
    data = safe_path(Path(record["staging"]), after).read_bytes()
    if digest(data) != after:
        raise ValueError("Staged output corrupted")
    check_authority(checkpoint)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_path(primary, name)
    fd, temp = tempfile.mkstemp(prefix=".dispatch-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if current_hash(primary, name) != change["before_sha256"]:
            raise ValueError("Target changed before replacement")
        check_authority(checkpoint)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def integrate(primary, copy_record, changes, checks, journal, *, checkpoint=None):
    validate(primary, copy_record, changes, journal)
    with journal.lock():
        validate(primary, copy_record, changes, journal)
        expected_checks = copy_record["checks"]
        if (not checks or len(checks) != len(expected_checks) or
                any(not isinstance(c, dict) for c in checks) or
                {c.get("id") for c in checks} != set(expected_checks) or
                any(c.get("status") != "passed" or
                    c.get("manifest_digest") != copy_record["manifest_digest"] or
                    c.get("changes_digest") != manifest_digest({"changes": changes}) for c in checks)):
            journal.append({"state": "blocked", "reason": "verification failed"})
            return {"status": "conflict", "paths": []}
        expected = dict(copy_record["input_hashes"])
        expected.update({c["path"]: c["before_sha256"] for c in changes})
        conflicts = [name for name, value in expected.items() if current_hash(primary, name) != value]
        if conflicts:
            journal.append({"state": "blocked", "conflicts": conflicts})
            return {"status": "conflict", "paths": conflicts}
        # Preserve before-bytes in controller storage before any target mutation.
        for change in changes:
            if change["before_sha256"]:
                data = safe_path(primary, change["path"]).read_bytes()
                if digest(data) != change["before_sha256"]:
                    raise ValueError("Source changed before backup")
                safe_path(Path(copy_record["staging"]), change["before_sha256"]).write_bytes(data)
        journal.append({"state": "integrating", "record": copy_record, "changes": changes})
        attempted=False
        try:
            for change in changes:
                check_authority(checkpoint)
                journal.append({"state": "applying", "path": change["path"],
                                "before": change["before_sha256"], "after": change["after_sha256"]})
                check_authority(checkpoint)
                attempted=True
                if checkpoint is None:apply_one(primary, copy_record, change)
                else:apply_one(primary, copy_record, change,checkpoint=checkpoint)
                journal.append({"state": "applied", "path": change["path"]})
            check_authority(checkpoint)
        except IntegrationCancelled:
            status='uncertain' if attempted else 'cancelled'
            journal.append({'state':status,'reason':'Integration stopped or expired'})
            return {'status':status,'paths':[c['path'] for c in changes] if attempted else []}
        except (OSError, ValueError) as exc:
            journal.append({"state": "uncertain", "reason": type(exc).__name__})
            return {"status": "uncertain", "paths": [c["path"] for c in changes]}
        journal.append({"state": "completed", "result": "integrated"})
        return {"status": "integrated", "paths": [c["path"] for c in changes]}


def recover_integration(primary, journal):
    primary = safe_root(primary)
    if journal.path.parent.parent != primary / ".dispatch-runs":
        raise ValueError("Journal must use the primary writer lock")
    safe_path(primary, ".dispatch-runs")
    with journal.lock(recover=True):
        intent = next((r for r in reversed(journal.records()) if r.get("state") == "integrating"), None)
        if not intent:
            journal.append({"state": "blocked", "reason": "No integration intent"})
            return {"status": "conflict", "paths": []}
        record, changes = intent["record"], intent["changes"]
        validate(primary, record, changes, journal)
        states = {c["path"]: current_hash(primary, c["path"]) for c in changes}
        conflict = [c["path"] for c in changes
                    if states[c["path"]] not in (c["before_sha256"], c["after_sha256"])]
        pending = [c["path"] for c in changes if states[c["path"]] != c["after_sha256"]]
        # Recovery is inspection only; resuming application needs a fresh check.
        status = "conflict" if conflict else "uncertain" if pending else "integrated"
        journal.append({"state": "completed" if status == "integrated" else "uncertain",
                        "result": status, "pending": pending, "conflicts": conflict})
        return {"status": status, "paths": conflict or pending}
