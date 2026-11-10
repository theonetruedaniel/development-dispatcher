"""Personal official Claude Code commands; no token handling or API fallback."""
import hashlib
import math


def subscription_environment(source):
    prefixes = ("ANTHROPIC_", "CLAUDE_CODE_", "AWS_", "AZURE_", "GOOGLE_", "GCLOUD_", "VERTEX_", "OTEL_")
    excluded = {"CLAUDE_CONFIG_DIR", "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS", "CLAUDE_API_KEY", "API_TIMEOUT_MS"}
    env = {key: value for key, value in source.items()
           if not key.upper().startswith(prefixes) and key.upper() not in excluded}
    env.update(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", CLAUDE_CODE_DISABLE_FAST_MODE="1")
    return env


def account_fingerprint(auth):
    if (not isinstance(auth, dict) or auth.get("loggedIn") is not True or
            auth.get("authMethod") != "claude.ai" or auth.get("apiProvider") != "firstParty" or
            auth.get("subscriptionType") not in ("pro", "max", "ultra")):
        raise ValueError("Signed-in personal Claude subscription is required")
    email = auth.get("email")
    if not isinstance(email, str) or not email.strip():
        raise ValueError("Claude account identity is unavailable")
    return hashlib.sha256(("claude:" + email.strip().lower()).encode()).hexdigest()


def validate_subscription_proof(proof, fingerprint, now):
    if not isinstance(proof, dict):
        raise ValueError("Fresh subscription-only billing evidence is required")
    checked = proof.get("checked_at")
    if type(checked) not in (int, float) or not math.isfinite(checked) or not 0 <= now - checked <= 300:
        raise ValueError("Claude billing evidence is stale or invalid")
    if proof.get("source") not in ("claude_settings_usage", "claude_cli_usage"):
        raise ValueError("Unrecognized Claude billing evidence source")
    if proof.get("account_fingerprint") != fingerprint or proof.get("extra_usage_enabled") is not False:
        raise ValueError("Usage credits must be verified disabled for this account")
    for field in ("session_remaining_percent", "weekly_remaining_percent"):
        value = proof.get(field)
        if type(value) not in (int, float) or not math.isfinite(value) or not 10 < value <= 100:
            raise ValueError("Claude included allowance is low or unknown")


def command(executable, model, effort):
    if not isinstance(model, str) or not model.startswith("claude-") or effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ValueError("Explicit Claude model and supported effort required")
    return [str(executable), "--safe-mode", "--restricted", "--no-chrome", "--strict-mcp-config",
            "--tools", "", "--setting-sources", "", "--settings", '{"fastMode":false}',
            "--no-session-persistence", "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--disable-slash-commands", "--model", model, "--effort", effort,
            "--max-turns", "1", "--print", "--output-format", "stream-json", "--verbose"]


def validate_result(events, model):
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise ValueError("Malformed CLI event collection")
    initial = [event for event in events if event.get("type") == "system" and event.get("subtype") == "init"]
    results = [event for event in events if event.get("type") == "result"]
    if len(initial) != 1 or len(results) != 1:
        raise ValueError("Missing unique CLI initialization or terminal result")
    init, result = initial[0], results[0]
    if (init.get("model") != model or init.get("tools") != [] or init.get("mcp_servers") != [] or
            init.get("apiKeySource") != "none" or init.get("permissionMode") != "dontAsk"):
        raise ValueError("CLI did not accept the isolated subscription route")
    if (result.get("subtype") != "success" or result.get("is_error") is not False or
            result.get("fast_mode_state") != "off" or result.get("permission_denials") != [] or
            result.get("num_turns") != 1 or
            set(result.get("modelUsage", {})) != {model}):
        raise ValueError("CLI terminal result or model route is unqualified")
    if result.get("usage") is not None and not isinstance(result["usage"], dict):
        raise ValueError("Malformed CLI usage record")
    if not isinstance(result.get("result"), str):
        raise ValueError("CLI final text is unavailable")
    return result["result"]



def reap_process(proc):
    import subprocess
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def collect_process(proc, payload, stop, timeout_seconds, stdout_limit=4*1024*1024, stderr_limit=1024*1024):
    import threading
    import time
    chunks = [bytearray(), bytearray()]
    overflow = threading.Event()
    errors = []
    def drain(stream, index, limit):
        try:
            while True:
                data = stream.read(4096)
                if not data:
                    return
                if len(chunks[index]) + len(data) > limit:
                    overflow.set()
                elif not overflow.is_set():
                    chunks[index].extend(data)
        except (OSError, ValueError) as error:
            errors.append(error)
    def send():
        try:
            proc.stdin.write(payload)
            proc.stdin.close()
        except (OSError, ValueError) as error:
            errors.append(error)
    workers = [threading.Thread(target=drain, args=(proc.stdout, 0, stdout_limit), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, 1, stderr_limit), daemon=True),
               threading.Thread(target=send, daemon=True)]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + timeout_seconds
    try:
        while proc.poll() is None:
            if overflow.is_set():
                raise ValueError("CLI output exceeded bounded result size")
            if errors:
                raise ValueError("CLI stream failed; provider completion unconfirmed")
            if stop.is_set() or time.monotonic() >= deadline:
                raise ValueError("CLI stopped; provider completion unconfirmed; no replay")
            time.sleep(.05)
        for worker in workers:
            worker.join(timeout=2)
        if any(worker.is_alive() for worker in workers):
            raise ValueError("CLI stream closure unconfirmed")
        if overflow.is_set():
            raise ValueError("CLI output exceeded bounded result size")
        if errors:
            raise ValueError("CLI stream failed; provider completion unconfirmed")
        return bytes(chunks[0]), bytes(chunks[1])
    finally:
        reap_process(proc)
        for worker in workers:
            worker.join(timeout=2)
        if not any(worker.is_alive() for worker in workers):
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()


def run_readonly(manifest, pin, proof, authorization, journal, stop=None):
    import json
    import os
    from pathlib import Path
    import subprocess
    import threading
    import time

    from .contracts import validate_manifest, manifest_digest
    from .runner import validate_authorization, verify

    expected_account = authorization["account_fingerprint"]
    validate_subscription_proof(proof, expected_account, time.time())
    work = validate_manifest(manifest, Path(authorization["workspace"]))
    validate_authorization(authorization, manifest_digest(work), work["workspace"], work["max_attempts"])
    if work["allowed_actions"] != ["read"] or work["max_attempts"] != 1 or work["mode"] != "manual":
        raise ValueError("Claude qualification requires a single scoped read-only route")
    task, workspace, route = work["task"], work["workspace"], work["manual_route"]
    timeout_seconds = work["timeout_seconds"]
    if not isinstance(task, str) or not task.strip() or len(task) > 100000:
        raise ValueError("Bounded supplied-text task required")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 180:
        raise ValueError("Claude task deadline must be within 180 seconds")
    root = Path(workspace).resolve(strict=True)
    executable = Path(pin["executable_path"])
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError("Pinned official Claude executable is unavailable")
    with executable.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != pin.get("executable_sha256"):
            raise ValueError("Claude executable changed; requalification required")
    model, effort = route["model"], route["effort"]
    if model not in pin.get("models", {}) or effort not in pin["models"][model]:
        raise ValueError("Claude route is not in the reviewed candidate catalog")
    args = command(executable, model, effort)
    env = subscription_environment(os.environ)
    options = dict(cwd=str(root), env=env, capture_output=True, timeout=15,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    version = subprocess.run([str(executable), "--version"], check=True, **options).stdout.decode().strip()
    if version != pin.get("cli_version"):
        raise ValueError("Claude version changed; requalification required")
    auth_args = args[:args.index("--no-session-persistence")] + ["auth", "status", "--json"]
    auth = json.loads(subprocess.run(auth_args, check=True, **options).stdout)
    fingerprint = account_fingerprint(auth)
    if fingerprint != expected_account:
        raise ValueError("Claude signed-in account changed")
    validate_subscription_proof(proof, fingerprint, time.time())
    stop = stop or threading.Event()
    task_digest = hashlib.sha256(task.encode()).hexdigest()
    base = {"provider": "claude_code", "manifest_digest": manifest_digest(work), "task_digest": task_digest, "workspace": str(root),
            "account_fingerprint": fingerprint, "requested_route": route, "billing_checked_at": proof["checked_at"],
            "supplied_text_only": True, "live_writes": False}
    def record(state, **fields):
        result = base | {"state": state} | fields
        journal.append(result)
        return result
    with journal.lock():
        old = journal.records()
        if old:
            previous = old[-1]
            identity = ("provider", "manifest_digest", "workspace", "account_fingerprint", "requested_route")
            if previous.get("state") == "completed" and all(previous.get(key) == base[key] for key in identity):
                return previous
            raise ValueError("Existing Claude run requires inspection; no replay")
        record("prepared")
        if stop.is_set():
            return record("cancelled", reason="Stopped before launch")
        validate_subscription_proof(proof, fingerprint, time.time())
        validate_authorization(authorization, manifest_digest(work), work["workspace"], 1)
        record("dispatching")
        proc = subprocess.Popen(args, cwd=str(root), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, shell=False,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            record("running", process_id=proc.pid)
            stdout, stderr = collect_process(proc, task.encode(), stop, timeout_seconds)
        except BaseException as error:
            try:
                reap_process(proc)
            finally:
                record("uncertain", reason=type(error).__name__ + ": " + str(error))
            if not isinstance(error, Exception):
                raise
            return journal.records()[-1]
        try:
            events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            if proc.returncode != 0:
                raise ValueError("Official CLI returned nonzero status")
            final = validate_result(events, model)
            result = next(event for event in events if event.get("type") == "result")
            if (result.get("usage") or {}).get("fallback_credit") is not None:
                raise ValueError("Unexpected usage-credit reporting requires inspection")
        except (ValueError, TypeError, StopIteration) as error:
            return record("uncertain", reason=str(error), exit_code=proc.returncode)
        checks = verify(work, final)
        return record("completed" if all(item["passed"] for item in checks) else "failed", accepted_route={"model": model, "effort_requested": effort},
                      observed_effort=None, final=final, estimated_provider_cost_usd=result.get("total_cost_usd"),
                      paid_usage_authorized=False, verification=checks)
