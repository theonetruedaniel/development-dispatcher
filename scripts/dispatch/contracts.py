"""Strict work-package contracts. Task content cannot grant authority."""
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import copy
import hashlib
import json
import re


@dataclass(frozen=True)
class Route:
    model: str
    effort: str


@dataclass(frozen=True)
class Decision:
    route: Route
    action: str
    reason: str
    usage_band: str


CLASSES = {"routine", "screening", "implementation", "complex", "consequential", "exceptional"}
REQUIRED = {"schema_version", "package_id", "task", "task_class", "workspace",
            "allowed_actions", "required", "timeout_seconds", "max_attempts", "verification"}


def manifest_digest(raw: dict) -> str:
    return hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def bounded_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise ValueError("Invalid relative path")
    win = PureWindowsPath(relative)
    if win.drive or win.root or ".." in win.parts or ":" in relative:
        raise ValueError("Path escapes workspace")
    root = root.resolve(strict=True)
    result = (root / relative).resolve()
    if not result.is_relative_to(root):
        raise ValueError("Path escapes workspace")
    return result


def validate_manifest(raw: dict, workspace: Path) -> dict:
    if not isinstance(raw, dict) or not REQUIRED <= raw.keys() or raw.keys() - REQUIRED - {"mode", "manual_route"}:
        raise ValueError("Missing or unknown manifest fields")
    data = copy.deepcopy(raw)
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("Unsupported manifest version")
    for name in ("package_id", "task", "task_class", "workspace"):
        if not isinstance(data[name], str) or not data[name].strip() or "\x00" in data[name]:
            raise ValueError("Invalid " + name)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", data["package_id"]):
        raise ValueError("Invalid package ID")
    if len(data["task"]) > 100_000 or data["task_class"] not in CLASSES:
        raise ValueError("Invalid task")
    if type(data["required"]) is not bool:
        raise ValueError("required must be boolean")
    for key, limit in (("timeout_seconds", 1800), ("max_attempts", 3)):
        if type(data[key]) is not int or not 1 <= data[key] <= limit:
            raise ValueError("Invalid " + key)
    if data["allowed_actions"] not in (["read"], ["write"], ["read", "write"]):
        raise ValueError("Invalid action scope")
    root = workspace.resolve(strict=True)
    path = Path(data["workspace"]).resolve(strict=True)
    if path != root or not path.is_dir():
        raise ValueError("Workspace does not match authorized root")
    data["workspace"] = str(root)
    mode = data.setdefault("mode", "auto")
    route = data.get("manual_route")
    if mode not in ("auto", "manual"):
        raise ValueError("Invalid mode")
    if mode == "manual":
        if not isinstance(route, dict) or set(route) != {"model", "effort"}:
            raise ValueError("Manual requires model and effort")
        if not all(isinstance(v, str) and v.strip() for v in route.values()):
            raise ValueError("Invalid manual route")
    elif route is not None:
        raise ValueError("Auto cannot contain a manual route")
    checks = data["verification"]
    if not isinstance(checks, list) or not checks or len(checks) > 20:
        raise ValueError("Verification is required")
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("Invalid verification")
        if check.get("type") == "final_equals":
            if set(check) != {"type", "expected"} or not isinstance(check["expected"], str):
                raise ValueError("Invalid final_equals")
        elif check.get("type") == "artifact_sha256":
            if set(check) != {"type", "path", "sha256"}:
                raise ValueError("Invalid artifact check")
            bounded_path(root, check["path"])
            if not isinstance(check["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", check["sha256"]):
                raise ValueError("Invalid artifact digest")
        else:
            raise ValueError("Unsupported verification")
    return data
