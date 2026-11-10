"""Versioned editing authority. These checks do not replace runtime containment."""
import copy
import math
import re
import stat
from pathlib import Path
from .contracts import REQUIRED, bounded_path, manifest_digest, validate_manifest

EXTRA = {"baseline", "inputs", "outputs", "checks", "selector_mode"}
PROTECTED = (".git", ".dispatch-runs", ".superpowers", "scripts/dispatch",
             "docs/architecture", "docs/evidence", "docs/registries", "AGENTS.md")
SECRET_NAMES = {".env", "auth.json", "credentials.json", "config.toml"}


def relative_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("Use a nonempty slash-separated relative path")
    value = name.rstrip("/")
    if not value or name.startswith("/") or "//" in name:
        raise ValueError("Invalid relative path")
    for part in value.split("/"):
        stem = part.split(".")[0].upper()
        if (part in (".", "..") or part.endswith((" ", ".")) or
                any(ord(c) < 32 or c in ':*?"<>|' for c in part) or
                stem in {"CON", "PRN", "AUX", "NUL"} or
                re.fullmatch(r"(COM|LPT)[1-9]", stem)):
            raise ValueError("Invalid Windows path component")
    return value


def protected(name: str) -> bool:
    value = relative_name(name).casefold()
    return (any(value == p.casefold() or value.startswith(p.casefold()+"/") or
                p.casefold().startswith(value+"/") for p in PROTECTED) or
            any(p.casefold() in SECRET_NAMES or p.casefold().startswith(".env.")
                for p in value.split("/")))


def safe_root(root: Path) -> Path:
    root = root.absolute()
    for path in [*reversed(root.parents), root]:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked root or ancestor is not admitted")
    return root.resolve(strict=True)


def root_identity(root: Path) -> list[int]:
    info = safe_root(root).stat()
    return [info.st_dev, info.st_ino]


def safe_path(root: Path, name: str) -> Path:
    value = relative_name(name)
    root = safe_root(root)
    current = root
    for part in value.split("/"):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info, "st_file_attributes", 0) & 0x400 or
                (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
            raise ValueError("Linked paths are not admitted")
    return bounded_path(root, value)


def permits(name: str, outputs: list[str]) -> bool:
    value = relative_name(name).casefold()
    return not protected(name) and any(
        value == out.rstrip("/").casefold() or
        (out.endswith("/") and value.startswith(out.casefold())) for out in outputs)


def validate_write_manifest(raw: dict, primary: Path) -> dict:
    if not isinstance(raw, dict) or set(raw) - REQUIRED - EXTRA - {"mode", "manual_route"} or not REQUIRED | EXTRA <= raw.keys():
        raise ValueError("Missing or unknown write fields")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 2:
        raise ValueError("Expected schema 2")
    legacy = {k: copy.deepcopy(v) for k, v in raw.items() if k not in EXTRA}
    legacy["schema_version"] = 1
    data = validate_manifest(legacy, primary)
    if data["allowed_actions"] != ["read", "write"]:
        raise ValueError("Editing requires read and write")
    data.update({k: copy.deepcopy(raw[k]) for k in EXTRA}, schema_version=2)
    for key in ("inputs", "outputs"):
        names = data[key]
        if not isinstance(names, list) or not names or len(names) > 10000:
            raise ValueError("Explicit path scope required")
        seen = set()
        for name in names:
            value = relative_name(name)
            if value.casefold() in seen or protected(name) or (key == "inputs" and name.endswith("/")):
                raise ValueError("Duplicate or protected path")
            seen.add(value.casefold())
            safe_path(primary, value)
    base = data["baseline"]
    if (not isinstance(base, dict) or set(base) != {"commit", "overlays"} or
            not isinstance(base["commit"], str) or not re.fullmatch("[0-9a-f]{40}", base["commit"]) or
            not isinstance(base["overlays"], list)):
        raise ValueError("Exact baseline required")
    seen = set()
    for overlay in base["overlays"]:
        if not isinstance(overlay, dict) or set(overlay) != {"path", "sha256"}:
            raise ValueError("Invalid overlay")
        name = overlay["path"]
        if name not in data["inputs"] or name.casefold() in seen:
            raise ValueError("Overlay must bind a unique input")
        seen.add(name.casefold())
        if not isinstance(overlay["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", overlay["sha256"]):
            raise ValueError("Invalid overlay hash")
    if (data["selector_mode"] not in ("rules", "shadow", "laya") or
            not isinstance(data["checks"], list) or not data["checks"] or
            any(not isinstance(c, str) or not re.fullmatch("[A-Za-z0-9_-]{1,80}", c) for c in data["checks"]) or
            len(set(data["checks"])) != len(data["checks"])):
        raise ValueError("Invalid selector or check IDs")
    return data


def validate_write_grant(grant: dict, manifest: dict, account: str, now: float) -> dict:
    fields = {"schema_version", "manifest_digest", "workspace", "account_fingerprint",
              "expires_at", "allowed_actions", "max_attempts", "dedicated_session"}
    if not isinstance(grant, dict) or set(grant) != fields:
        raise ValueError("Invalid write grant")
    expiry = grant["expires_at"]
    if (type(grant["schema_version"]) is not int or grant["schema_version"] != 2 or
            grant["manifest_digest"] != manifest_digest(manifest) or
            Path(grant["workspace"]).resolve(strict=True) != Path(manifest["workspace"]).resolve(strict=True) or
            not isinstance(account, str) or not re.fullmatch("[0-9a-f]{64}", account) or
            grant["account_fingerprint"] != account or grant["dedicated_session"] is not True or
            grant["allowed_actions"] != ["read", "write"] or
            type(grant["max_attempts"]) is not int or
            not manifest["max_attempts"] <= grant["max_attempts"] <= 3 or
            type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= now):
        raise ValueError("Write grant does not bind current work/account/time")
    return copy.deepcopy(grant)
