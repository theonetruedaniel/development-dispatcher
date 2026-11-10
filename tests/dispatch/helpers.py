import copy
import time
import queue


class FakeClient:
    isolation_verified = True

    def __init__(self, answers=None, event_mode="complete"):
        self.answers = list(answers or ["OK"])
        self.event_mode = event_mode
        self.methods = []
        self.turn_params = []
        self.events = queue.Queue()
        self.account = "a" * 64
        self.headroom = usage()
        self.on_start = None
        self.turn_status = "completed"

    def initialize(self):
        pass

    def account_fingerprint(self):
        return self.account

    def catalog(self):
        return catalog()

    def usage(self):
        return self.headroom

    def request(self, method, params, timeout=15):
        self.methods.append(method)
        if method == "thread/start":
            return {"thread": {"id": "thread-owned"}}
        if method == "turn/start":
            self.turn_params.append(params)
            if self.on_start:
                self.on_start()
            turn_id = "turn-" + str(len(self.turn_params))
            answer = self.answers.pop(0) if self.answers else "OK"
            if self.event_mode == "approval":
                self.events.put({"method": "dispatch/awaiting_user", "params": {}})
            elif self.event_mode == "complete":
                self.events.put({"method": "turn/completed", "params": {
                    "threadId": "thread-owned", "turn": {"id": turn_id, "status": self.turn_status,
                        "items": [{"type": "agentMessage", "phase": "final_answer", "text": answer}]}}})
            return {"turn": {"id": turn_id, "status": "inProgress"}}
        if method == "turn/interrupt":
            self.events.put({"method": "turn/completed", "params": {
                "threadId": "thread-owned", "turn": {"id": params["turnId"], "status": "interrupted", "items": []}}})
            return {}
        if method == "thread/read":
            return {"thread": {"id": "thread-owned", "turns": [
                {"id": "turn-1", "status": self.turn_status, "items": []}]}}
        if method == "thread/resume":
            return {"thread": {"id": "thread-owned"}}
        raise AssertionError(method)

    def next_event(self, timeout):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("synthetic deadline")

    def close(self):
        pass


def manifest(**overrides):
    result = dict(schema_version=1, package_id="synthetic", task="Return OK.",
                  task_class="routine", workspace=".", allowed_actions=["read"],
                  required=True, timeout_seconds=10, max_attempts=2,
                  verification=[{"type": "final_equals", "expected": "OK"}])
    result.update(overrides)
    return copy.deepcopy(result)


def catalog():
    return {f"gpt-6-{name}": ["low", "medium", "high", "xhigh", "max"]
            for name in ("luna", "sol", "astra")}


def usage(remaining=80):
    return {"retrieved_at": time.time(), "limitId": "codex",
            "primary": {"usedPercent": 100 - remaining}}
