"""Select a development tool at a new task boundary, without executing it."""
from dataclasses import asdict
import time
from .claude_cli import validate_subscription_proof
from .policy import select_route


CLAUDE_REVIEW_ROUTES = {
    "routine": ("claude-haiku-5-5", "low"),
    "screening": ("claude-sonnet-5-5", "medium"),
    "implementation": ("claude-sonnet-5-5", "medium"),
    "complex": ("claude-opus-5-5", "high"),
    "consequential": ("claude-opus-5-5", "high"),
}


def select_development_route(work, codex_catalog, codex_usage, claude_catalog,
                             claude_proof, claude_account, *, purpose="work", history=()):
    if purpose not in ("work", "independent_review"):
        raise ValueError("Unknown development task purpose")
    manual = work.get("mode") == "manual"
    pin = work.get("manual_route") or {}
    use_claude = (str(pin.get("model", "")).startswith("claude-") if manual
                  else purpose == "independent_review")
    if history and (use_claude or any(not isinstance(row, dict) or row.get("provider") == "claude_code" for row in history)):
        return {"provider": "claude_code" if use_claude else "codex", "route": None,
                "action": "blocked", "usage_band": "unknown",
                "reason": "Existing provider attempt requires inspection; no automatic provider hopping"}
    if not use_claude:
        return {"provider": "codex"} | asdict(select_route(work, codex_catalog, codex_usage, list(history)))
    pair = (pin.get("model"), pin.get("effort")) if manual else CLAUDE_REVIEW_ROUTES.get(work["task_class"], (None, None))
    decision = {"provider": "claude_code", "route": {"model": pair[0], "effort": pair[1]},
                "action": "blocked", "usage_band": "unknown", "reason": "Unqualified Claude route"}
    if pair[1] not in claude_catalog.get(pair[0], []):
        return decision
    try:
        validate_subscription_proof(claude_proof, claude_account, time.time())
    except ValueError as error:
        return decision | {"reason": str(error)}
    remaining = min(claude_proof["session_remaining_percent"], claude_proof["weekly_remaining_percent"])
    band = "low" if remaining <= 20 else "conserve" if remaining <= 40 else "normal"
    if not work["required"] and remaining <= 40 and (pair[0] == "claude-opus-5-5" or remaining <= 20):
        return decision | {"action": "defer", "usage_band": band, "reason": "Optional review deferred to preserve included allowance"}
    return decision | {"action": "dispatch", "usage_band": band,
                       "reason": "Explicit manual pin" if manual else "Recommended independent-review route; execution admission required"}
