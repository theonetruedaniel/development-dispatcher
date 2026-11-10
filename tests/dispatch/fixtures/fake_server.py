"""Synthetic protocol peer; no Codex or network access."""
import json
import sys

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

for line in sys.stdin:
    req = json.loads(line)
    method = req["method"]
    if method == "initialized":
        continue
    if method == "close":
        break
    if method == "malformed":
        print("not-json", flush=True)
        continue
    if method == "oversized":
        print("x" * (4 * 1024 * 1024 + 1), flush=True)
        continue
    if method == "noise":
        sys.stderr.write("x" * 100000)
        sys.stderr.flush()
        print(json.dumps({"method": "notice", "params": {"ok": True}}), flush=True)
    if method == "approval":
        print(json.dumps({"id": 900, "method": "item/commandExecution/requestApproval",
                          "params": {"command": "synthetic"}}), flush=True)
        denial = json.loads(sys.stdin.readline())
        print(json.dumps({"id": req["id"], "result": denial}), flush=True)
        continue
    if method == "future":
        print(json.dumps({"id": req["id"] + 100, "result": {}}), flush=True)
        continue
    result = {"id": req["id"], "result": req.get("params", {})}
    print(json.dumps(result), flush=True)
    if method == "duplicate":
        print(json.dumps(result), flush=True)
