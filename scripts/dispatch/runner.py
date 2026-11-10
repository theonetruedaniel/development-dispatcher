"""Serial, verified dispatch. Live isolation must be independently qualified."""
from dataclasses import asdict
from pathlib import Path
import hashlib
import math
import threading
import time
from .contracts import bounded_path, manifest_digest, validate_manifest
from .journal import Journal, JournalError, SAFE
from .policy import POLICY_VERSION, select_route
from .protocol import ProtocolClient, ProtocolError

GRANT_FIELDS = {"schema_version", "manifest_digest", "workspace", "account_fingerprint",
                "expires_at", "allowed_actions", "max_attempts", "dedicated_session"}


def validate_authorization(grant: dict, digest: str, root: str, attempts: int):
    if not isinstance(grant, dict) or set(grant) != GRANT_FIELDS:
        raise ValueError("Missing or unknown authorization fields")
    if type(grant["schema_version"]) is not int or grant["schema_version"] != 1:
        raise ValueError("Unsupported authorization")
    if grant["manifest_digest"] != digest or Path(grant["workspace"]).resolve(strict=True) != Path(root).resolve(strict=True):
        raise ValueError("Authorization does not bind this manifest/workspace")
    if grant["dedicated_session"] is not True or grant["allowed_actions"] != ["read"]:
        raise ValueError("Dedicated read-only session permission required")
    if type(grant["max_attempts"]) is not int or not 1 <= attempts <= grant["max_attempts"] <= 3:
        raise ValueError("Attempt authority insufficient")
    expiry = grant["expires_at"]
    if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= time.time():
        raise ValueError("Authorization expired")
    identity = grant["account_fingerprint"]
    if not isinstance(identity, str) or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("Account fingerprint required")


def verify(manifest, final):
    results = []
    for check in manifest["verification"]:
        passed = False
        if check["type"] == "final_equals":
            passed = final == check["expected"]
        else:
            try:
                path = bounded_path(Path(manifest["workspace"]), check["path"])
                with path.open("rb") as stream:
                    passed = hashlib.file_digest(stream, "sha256").hexdigest() == check["sha256"]
            except (OSError, ValueError):
                passed = False
        results.append({"type": check["type"], "passed": passed})
    return results


def final_text(turn):
    return "\n".join(item["text"] for item in turn.get("items", [])
                     if isinstance(item, dict) and item.get("type") == "agentMessage"
                     and item.get("phase") in (None, "final_answer")
                     and isinstance(item.get("text"), str))


def _wait_turn(client, thread_id, turn_id, deadline, stop):
    while time.monotonic() < deadline:
        if stop.is_set():
            return None, "cancelled"
        try:
            event = client.next_event(min(0.2, max(0.001, deadline-time.monotonic())))
        except TimeoutError:
            continue
        if event.get("method") == "dispatch/awaiting_user":
            return None, "awaiting_user"
        params = event.get("params", {})
        if event.get("method") == "turn/completed":
            turn = params.get("turn", {})
            if params.get("threadId") != thread_id or turn.get("id") != turn_id:
                continue
            if turn.get("status") not in ("completed", "failed", "interrupted"):
                raise ProtocolError("Invalid terminal status")
            return turn, None
    return None, "cancelled"


def _interrupt(client, thread_id, turn_id):
    # Acknowledgement can precede the server's active-turn registration.
    # Retry only this idempotent control request, never a model dispatch.
    deadline = time.monotonic() + 5
    for attempt in range(3):
        try:
            client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id},
                           max(0.01, deadline-time.monotonic()))
            wait_until = deadline
        except ProtocolError:
            wait_until = min(deadline, time.monotonic()+0.25)
        except (TimeoutError, OSError):
            return False
        try:
            turn, _ = _wait_turn(client, thread_id, turn_id, wait_until, threading.Event())
            if turn is not None:
                return True
        except (ProtocolError, TimeoutError, OSError):
            return False
        if time.monotonic() >= deadline:
            break
    try:
        response = client.request("thread/read", {"threadId": thread_id, "includeTurns": True}, 5)
        thread = response.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            return False
        matches = [turn for turn in thread.get("turns", [])
                   if isinstance(turn, dict) and turn.get("id") == turn_id]
        return len(matches) == 1 and matches[0].get("status") in ("completed", "failed", "interrupted")
    except (ProtocolError, TimeoutError, OSError, TypeError):
        return False


def run_package(manifest: dict, client: ProtocolClient, journal: Journal,
                authorization: dict, stop: threading.Event) -> dict:
    try:
        work = validate_manifest(manifest, Path(authorization["workspace"]))
        digest = manifest_digest(work)
        validate_authorization(authorization, digest, work["workspace"], work["max_attempts"])
        if work["allowed_actions"] != ["read"]:
            raise ValueError("Writing is not qualified in v1")
        if getattr(client, "isolation_verified", False) is not True:
            raise ValueError("External-tool isolation has not been qualified")
        old = journal.records()
        if any(r.get("manifest_digest") not in (None, digest) for r in old):
            raise ValueError("Existing run binds another manifest")
        if old:
            if old[-1].get("state") == "completed":
                return old[-1]
            return {"state": "uncertain", "reason": "Existing run requires explicit reconciliation; no replay"}
    except (KeyError, TypeError, ValueError, OSError, JournalError) as exc:
        return {"state": "blocked", "reason": str(exc)}

    base = {"manifest_digest": digest, "package_id": work["package_id"],
            "workspace": work["workspace"], "account_fingerprint": authorization["account_fingerprint"],
            "policy_version": POLICY_VERSION}

    def record(state, **extra):
        row = base | {"state": state} | extra
        journal.append(row)
        return row

    thread_id = turn_id = None
    dispatch_pending = False
    try:
        with journal.lock():
            try:
                if journal.records():
                    return {"state": "uncertain", "reason": "Run changed during acquisition"}
                record("prepared", manifest=work)
                if stop.is_set():
                    return record("cancelled", reason="Stopped before dispatch")
                client.initialize()
                history = []
                for _ in range(work["max_attempts"]):
                    validate_authorization(authorization, digest, work["workspace"], work["max_attempts"])
                    if stop.is_set():
                        return record("cancelled", reason="Stopped before dispatch")
                    if client.account_fingerprint() != authorization["account_fingerprint"]:
                        return record("blocked", reason="Account mismatch")
                    decision = select_route(work, client.catalog(), client.usage(), history)
                    if decision.action != "dispatch":
                        return record("deferred" if decision.action == "defer" else "blocked", decision=asdict(decision))
                    validate_authorization(authorization, digest, work["workspace"], work["max_attempts"])
                    if stop.is_set():
                        return record("cancelled", reason="Stopped during metadata lookup")
                    base["requested_route"] = asdict(decision.route)
                    base.pop("accepted_route", None)
                    record("prepared", decision=asdict(decision))
                    if thread_id is None:
                        record("dispatching", operation="thread/start")
                        dispatch_pending = True
                        started = client.request("thread/start", {
                            "cwd": work["workspace"], "model": decision.route.model,
                            "sandbox": "read-only", "approvalPolicy": "on-request",
                            "approvalsReviewer": "user", "modelProvider": "openai",
                            "config": {"model_reasoning_effort": decision.route.effort}}, 15)
                        thread = started.get("thread")
                        thread_id = thread.get("id") if isinstance(thread, dict) else None
                        if not isinstance(thread_id, str) or not thread_id:
                            raise ProtocolError("Thread acknowledgement missing")
                        base["thread_id"] = thread_id
                        dispatch_pending = False
                        if hasattr(client, "verify_thread"):
                            base["accepted_route"] = client.verify_thread(started, decision.route)
                        record("prepared", operation="thread/acknowledged")
                    # Recheck after thread creation so revocation/expiry cannot start a turn.
                    validate_authorization(authorization, digest, work["workspace"], work["max_attempts"])
                    if stop.is_set():
                        return record("cancelled", reason="Stopped before turn")
                    prompt = work["task"]
                    if history:
                        prompt += "\nPrevious required verification failed. Correct the result within the same scope."
                    params = {"threadId": thread_id, "input": [{"type": "text", "text": prompt}],
                              "model": decision.route.model, "effort": decision.route.effort,
                              "cwd": work["workspace"], "approvalPolicy": "on-request",
                              "sandboxPolicy": {"type": "readOnly"}}
                    # Prior attempt IDs must never stand in for unacknowledged new work.
                    base.pop("turn_id", None)
                    turn_id = None
                    record("dispatching", operation="turn/start")
                    dispatch_pending = True
                    deadline = time.monotonic() + work["timeout_seconds"]
                    response = client.request("turn/start", params, min(15, work["timeout_seconds"]))
                    acknowledged = response.get("turn")
                    turn_id = acknowledged.get("id") if isinstance(acknowledged, dict) else None
                    if not isinstance(turn_id, str) or not turn_id:
                        raise ProtocolError("Turn acknowledgement missing")
                    base["turn_id"] = turn_id
                    record("running", accepted_turn_id=turn_id, observed_route=None)
                    turn, reason = _wait_turn(client, thread_id, turn_id, deadline, stop)
                    if reason:
                        confirmed = _interrupt(client, thread_id, turn_id)
                        dispatch_pending = not confirmed
                        return record(reason if confirmed else "uncertain", interruption_confirmed=confirmed,
                                      reason=reason, observed_route=None)
                    dispatch_pending = False
                    if turn["status"] != "completed":
                        return record("failed" if turn["status"] == "failed" else "cancelled",
                                      reason="Provider terminal state", observed_route=None)
                    final = final_text(turn)
                    checks = verify(work, final)
                    if all(c["passed"] for c in checks):
                        return record("completed", verification=checks, final=final, observed_route=None)
                    failure = {"package_id": work["package_id"], "outcome": "quality_failure",
                               "verified": True, "effort": decision.route.effort}
                    history.append(failure)
                    record("prepared", verification=checks, attempt_outcome=failure, observed_route=None)
                return record("failed", reason="Verification failed; attempt limit reached", observed_route=None)
            except (ValueError, OSError, ProtocolError, TimeoutError) as exc:
                return record("uncertain" if dispatch_pending else "blocked", reason=str(exc), observed_route=None)

    except (ValueError, OSError, JournalError) as exc:
        return {"state": "uncertain" if dispatch_pending else "blocked", "reason": str(exc)}


def reconcile(journal: Journal, client: ProtocolClient, authorization: dict) -> dict:
    try:
        rows = journal.records()
        if not rows:
            raise ValueError("No run to reconcile")
        row = rows[-1]
        validate_authorization(authorization, row["manifest_digest"], row["workspace"], 1)
        if row.get("account_fingerprint") != authorization["account_fingerprint"]:
            raise ValueError("Account ownership mismatch")
        if getattr(client, "isolation_verified", False) is not True:
            raise ValueError("Isolation has not been qualified")
        if not row.get("thread_id") or not row.get("turn_id"):
            return {"state": "uncertain", "reason": "Missing owned IDs; never redispatch"}
        with journal.lock(recover=True):
            client.initialize()
            if client.account_fingerprint() != authorization["account_fingerprint"]:
                raise ValueError("Account mismatch")
            response = client.request("thread/read", {"threadId": row["thread_id"], "includeTurns": True}, 15)
            thread = response.get("thread", {})
            if thread.get("id") != row["thread_id"]:
                raise ValueError("Thread identity mismatch")
            matches = [t for t in thread.get("turns", []) if t.get("id") == row["turn_id"]]
            if len(matches) != 1 or matches[0].get("status") not in ("completed", "failed", "interrupted"):
                return {"state": "uncertain", "reason": "No unique terminal evidence"}
            result = {k: row[k] for k in ("manifest_digest", "workspace", "account_fingerprint", "thread_id", "turn_id")}
            result.update(state="reconciled", provider_status=matches[0]["status"],
                          reason="Terminal state only; task verification not asserted", observed_route=None)
            journal.append(result)
            return result
    except (KeyError, ValueError, TypeError, OSError, ProtocolError, TimeoutError, JournalError) as exc:
        return {"state": "blocked", "reason": str(exc)}
