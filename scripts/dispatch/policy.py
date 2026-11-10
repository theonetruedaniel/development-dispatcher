"""Deterministic route choices; never changes accounts or grants."""
import math
import time
from .contracts import Decision, Route

POLICY_VERSION = "2026-11-10-v2"
LADDERS = {
    "routine": [("luna", "low"), ("luna", "medium"), ("sol", "medium")],
    "screening": [("luna", "medium"), ("sol", "medium"), ("sol", "high")],
    "implementation": [("sol", "medium"), ("sol", "high"), ("astra", "high")],
    "complex": [("sol", "high"), ("astra", "high"), ("astra", "xhigh")],
    "consequential": [("astra", "high"), ("astra", "xhigh"), ("astra", "max")],
    "exceptional": [("astra", "max")],
}


def supported_auto_route(task_class, role, effort, catalog):
    candidates = {
        "luna": ("gpt-6-luna",),
        "sol": ("gpt-6.1-sol", "gpt-6-sol"),
        "astra": ("gpt-6-astra",),
    }[role]
    if task_class == "implementation" and role == "sol":
        candidates = ("gpt-6.1-terra",) + candidates
    for model in candidates:
        if effort in catalog.get(model, []):
            return Route(model, effort)
    return Route(candidates[0], effort)


def usage_remaining(usage: dict | None) -> float | None:
    if not isinstance(usage, dict) or usage.get("limitId") != "codex":
        return None
    retrieved = usage.get("retrieved_at")
    if type(retrieved) not in (float, int) or not math.isfinite(retrieved) or not 0 <= time.time() - retrieved <= 300:
        return None
    windows = []
    malformed = False
    for name in ("primary", "secondary"):
        window = usage.get(name)
        if window is None:
            continue
        used = window.get("usedPercent") if isinstance(window, dict) else None
        if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
            malformed = True
        else:
            windows.append(100 - used)
    if windows and min(windows) == 0:
        return 0
    return min(windows) if windows and not malformed else None


def select_route(manifest: dict, catalog: dict, usage: dict | None,
                 history: list[dict]) -> Decision:
    remaining = usage_remaining(usage)
    band = ("unknown" if remaining is None else "critical" if remaining <= 10
            else "low" if remaining <= 20 else "conserve" if remaining <= 40 else "normal")
    prior = [h for h in history if h.get("package_id") == manifest["package_id"]]
    ladder = LADDERS[manifest["task_class"]]
    index = min(len(prior), len(ladder)-1)
    route = (Route(**manifest["manual_route"]) if manifest.get("mode") == "manual"
             else supported_auto_route(manifest["task_class"], *ladder[index], catalog))

    def result(action, reason):
        return Decision(route, action, reason, band)

    if route.effort not in catalog.get(route.model, []):
        return result("blocked", "Model/effort unavailable; no substitution")
    if manifest["task_class"] == "exceptional" and not any(
        h.get("verified") is True and h.get("effort") in ("high", "xhigh")
        and h.get("outcome") in ("quality_failure", "capability_gap") for h in history
    ):
        return result("blocked", "Exceptional work requires prior high/xhigh evidence")
    if prior and (manifest.get("mode") == "manual" or any(
        h.get("verified") is not True or h.get("outcome") not in ("quality_failure", "capability_gap") for h in prior
    )):
        return result("blocked", "Retry not authorized by quality evidence")
    if len(prior) >= min(manifest["max_attempts"], len(ladder)):
        return result("blocked", "Attempt limit reached")
    if remaining == 0:
        return result("defer", "Account allowance exhausted")
    if band == "unknown" and (route.effort == "max" or prior):
        return result("defer", "Unknown headroom permits only a bounded initial attempt")
    expensive = route.model == "gpt-6-astra" or route.effort in ("high", "xhigh", "max")
    if remaining is not None:
        if remaining <= 10 and manifest["task_class"] != "routine":
            return result("defer", "Preserve quality floor until allowance resets")
        if remaining <= 20 and (route.effort == "max" or (not manifest["required"] and manifest["task_class"] != "routine")):
            return result("defer", "Low headroom")
        if remaining <= 40 and expensive and not manifest["required"]:
            return result("defer", "Optional expensive work deferred")
    return result("dispatch", "Explicit manual pin" if manifest.get("mode") == "manual"
                  else "Task class and verified attempt history")
