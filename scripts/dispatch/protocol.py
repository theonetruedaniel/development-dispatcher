"""Bounded JSONL transport. This module never grants server approvals."""
import hashlib
from decimal import Decimal, InvalidOperation
from .policy import usage_remaining
import json
import os
import queue
import subprocess
import threading
import time

MAX_LINE = 4 * 1024 * 1024


class ProtocolError(RuntimeError):
    pass


def clean_environment(source):
    excluded = {'OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL', 'CHATGPT_BASE_URL'}
    return {k: v for k, v in source.items()
            if k.upper() not in excluded and not k.upper().startswith('OTEL_')}


class ProtocolClient:
    def __init__(self, command: list[str]):
        env = clean_environment(os.environ)
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, shell=False, env=env,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.incoming = queue.Queue(maxsize=256)
        self.events = queue.Queue(maxsize=256)
        self.fatal = None
        self.counter = 0
        self.completed = set()
        self.closed = False
        self.lock = threading.Lock()
        self.writers = []
        self.readers = [threading.Thread(target=self._read, daemon=True),
                        threading.Thread(target=self._drain_stderr, daemon=True)]
        for reader in self.readers:
            reader.start()

    def _read(self):
        try:
            while not self.closed:
                line = self.proc.stdout.readline(MAX_LINE + 1)
                if not line:
                    raise ProtocolError("Peer closed")
                if len(line) > MAX_LINE:
                    raise ProtocolError("Event exceeds size limit")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ProtocolError("Event must be an object")
                self.incoming.put_nowait(message)
        except (OSError, ValueError, queue.Full, ProtocolError) as exc:
            self.fatal = ProtocolError(str(exc))

    def _drain_stderr(self):
        try:
            while self.proc.stderr.read(4096):
                pass
        except (OSError, ValueError):
            pass

    def _send(self, value, timeout=15):
        if self.closed or self.fatal:
            raise ProtocolError("Client closed")
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        if len(data) > MAX_LINE:
            raise ProtocolError("Request exceeds size limit")
        done = threading.Event()
        errors = []
        def write():
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                errors.append(exc)
            finally:
                done.set()
        writer = threading.Thread(target=write, daemon=True)
        self.writers = [w for w in self.writers if w.is_alive()]
        self.writers.append(writer)
        writer.start()
        if not done.wait(max(0, timeout)):
            self.fatal = ProtocolError("Transport failed during send; delivery uncertain")
            self.proc.terminate()
            raise TimeoutError("Protocol send deadline exceeded; delivery uncertain")
        if errors:
            raise ProtocolError("Peer unavailable") from errors[0]

    def _get(self, deadline):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Protocol deadline exceeded")
            try:
                return self.incoming.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                if self.fatal:
                    raise self.fatal

    def _event(self, msg, timeout=15):
        if "method" not in msg or not isinstance(msg["method"], str):
            raise ProtocolError("Malformed notification")
        if "id" in msg:
            method = msg["method"]
            if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                self._send({"id": msg["id"], "result": {"decision": "decline"}}, timeout)
            elif method == "item/permissions/requestApproval":
                self._send({"id": msg["id"], "result": {"permissions": {}, "scope": "turn"}}, timeout)
            else:
                self._send({"id": msg["id"], "error": {"code": -32601, "message": "Unsupported server request"}}, timeout)
            msg = {"method": "dispatch/awaiting_user", "params": {"request_method": method}}
        try:
            self.events.put_nowait(msg)
        except queue.Full as exc:
            raise ProtocolError("Event backlog exceeded") from exc

    def request(self, method: str, params: dict, timeout: float = 15) -> dict:
        with self.lock:
            self.counter += 1
            request_id = self.counter
            deadline = time.monotonic() + timeout
            self._send({"id": request_id, "method": method, "params": params}, timeout)
            while True:
                msg = self._get(deadline)
                if "method" in msg:
                    self._event(msg, max(0, deadline-time.monotonic()))
                elif msg.get("id") in self.completed:
                    continue
                elif msg.get("id") != request_id:
                    raise ProtocolError("Uncorrelated response")
                else:
                    self.completed.add(request_id)
                    # Bound duplicate tracking for long sessions.
                    if len(self.completed) > 1024:
                        self.completed.remove(min(self.completed))
                    if "error" in msg:
                        raise ProtocolError("Server rejected " + method)
                    if not isinstance(msg.get("result"), dict):
                        raise ProtocolError("Malformed response")
                    return msg["result"]

    def next_event(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.events.get_nowait()
            except queue.Empty:
                msg = self._get(deadline)
                if "method" in msg:
                    self._event(msg, max(0, deadline-time.monotonic()))
                elif msg.get("id") not in self.completed:
                    raise ProtocolError("Uncorrelated response")

    def initialize(self):
        self.request("initialize", {"clientInfo": {"name": "abrams_dispatch",
                     "title": "Abrams dispatch", "version": "0.1"}}, 15)
        self._send({"method": "initialized"})

    def catalog(self) -> dict:
        result, seen, cursor = {}, set(), None
        for _ in range(100):
            params = {"limit": 100, "includeHidden": False}
            if cursor is not None:
                params["cursor"] = cursor
            page = self.request("model/list", params, 15)
            if not isinstance(page.get("data"), list):
                raise ProtocolError("Malformed model catalog")
            for row in page["data"]:
                if not isinstance(row, dict) or not isinstance(row.get("model"), str):
                    raise ProtocolError("Malformed model")
                efforts = row.get("supportedReasoningEfforts")
                if not isinstance(efforts, list) or not all(isinstance(e, dict) and isinstance(e.get("reasoningEffort"), str) for e in efforts):
                    raise ProtocolError("Malformed effort catalog")
                if row["model"] in result:
                    raise ProtocolError("Duplicate model")
                result[row["model"]] = [e["reasoningEffort"] for e in efforts]
            cursor = page.get("nextCursor")
            if cursor is None:
                return result
            if not isinstance(cursor, str) or cursor in seen:
                raise ProtocolError("Catalog cursor cycle")
            seen.add(cursor)
        raise ProtocolError("Too many catalog pages")

    def usage(self) -> dict:
        response = self.request("account/rateLimits/read", {}, 15)
        buckets = response.get("rateLimitsByLimitId")
        bucket = buckets.get("codex") if isinstance(buckets, dict) else response.get("rateLimits")
        if not isinstance(bucket, dict) or bucket.get("limitId") != "codex":
            raise ProtocolError("Current subscription allowance is unavailable")
        if response.get("ordinaryUsageAllowed") is not True:
            raise ProtocolError("Ordinary subscription usage is not confirmed")
        if bucket.get("spendControlReached") is not False:
            raise ProtocolError("Account spend-control status is blocked or unknown")
        credits = bucket.get("credits")
        if not isinstance(credits, dict) or credits.get("hasCredits") is not False or credits.get("unlimited") is not False:
            raise ProtocolError("No-credit subscription-only execution is not confirmed")
        try:
            balance = Decimal(str(credits.get("balance")))
        except InvalidOperation:
            raise ProtocolError("Account credit balance is unknown") from None
        if not balance.is_finite() or balance != 0:
            raise ProtocolError("Account credit fallback requires separate authorization")
        usage = {k: bucket.get(k) for k in ("limitId", "primary", "secondary")} | {"retrieved_at": time.time()}
        if usage_remaining(usage) is None:
            raise ProtocolError("Current subscription allowance windows are unavailable or malformed")
        return usage

    def account_fingerprint(self) -> str:
        account = self.request("account/read", {"refreshToken": False}, 15).get("account")
        if not isinstance(account, dict) or account.get("type") != "chatgpt":
            raise ProtocolError("Authorized ChatGPT authentication is required")
        email = account.get("email")
        if not isinstance(email, str) or not email.strip():
            raise ProtocolError("Account identity unavailable")
        return hashlib.sha256(("chatgpt:" + email.strip().lower()).encode()).hexdigest()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if any(w.is_alive() for w in self.writers) and self.proc.poll() is None:
            self.proc.terminate()
        for writer in self.writers:
            writer.join(timeout=2)
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        for reader in self.readers:
            reader.join(timeout=2)
        self.proc.stdout.close()
        self.proc.stderr.close()
