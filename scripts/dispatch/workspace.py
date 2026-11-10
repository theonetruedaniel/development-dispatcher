"""Independent file copies with explicit provenance; never a sandbox by themselves."""
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from .contracts import manifest_digest
from .write_contracts import validate_write_manifest, safe_path, permits, safe_root, root_identity


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inventory(root: Path) -> dict:
    result = {}
    seen = set()
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            rel = path.relative_to(root).as_posix()
            checked = safe_path(root, rel)
            if rel.casefold() in seen:
                raise ValueError("Case collision")
            seen.add(rel.casefold())
            if checked.is_file():
                result[rel] = digest(checked.read_bytes())
            elif not checked.is_dir():
                raise ValueError("Nonordinary file")
    return result


def prepare_copy(manifest: dict, primary: Path, storage: Path) -> dict:
    work = validate_write_manifest(manifest, primary)
    primary = safe_root(primary)
    storage = storage.resolve()
    if storage.is_relative_to(primary) or primary.is_relative_to(storage):
        raise ValueError("Scratch storage must be separate from the primary checkout")
    base = work["baseline"]["commit"]
    def git(*args):
        return subprocess.check_output(["git", "-C", str(primary), *args],
                                       stderr=subprocess.PIPE, timeout=30)
    try:
        if git("rev-parse", base+"^{commit}").decode().strip() != base:
            raise ValueError("Baseline is not an exact commit")
    except subprocess.SubprocessError as exc:
        raise ValueError("Baseline unavailable") from exc
    overlays = {x["path"]: x["sha256"] for x in work["baseline"]["overlays"]}
    contents = {}
    hashes = {}
    for rel in work["inputs"]:
        path = safe_path(primary, rel)
        data = path.read_bytes()
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("Input exceeds copy limit")
        actual = digest(data)
        if rel in overlays:
            if actual != overlays[rel]:
                raise ValueError("Overlay changed")
        else:
            entries = git("ls-tree", "-z", base, "--", rel).split(b"\0")
            if len(entries) != 2 or not entries[0].startswith((b"100644 blob ", b"100755 blob ")):
                raise ValueError("Input must be an ordinary baseline blob or explicit overlay")
            object_id = entries[0].split(b"\t")[0].split()[2].decode("ascii")
            if digest(git("cat-file", "blob", object_id)) != actual:
                raise ValueError("Unrecorded dirty input")
        contents[rel] = data
        hashes[rel] = actual
    # Reject an input that changed while the remaining inputs were read.
    for rel, expected in hashes.items():
        if digest(safe_path(primary, rel).read_bytes()) != expected:
            raise ValueError("Input changed during preparation")
    storage.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="edit-", dir=storage))
    target = run_root / "worker"
    stage = run_root / "staged"
    target.mkdir()
    stage.mkdir()
    for rel, data in contents.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return dict(root=str(target), staging=str(stage), primary=str(primary),
                primary_identity=root_identity(primary), checks=work["checks"],
                manifest_digest=manifest_digest(work), input_hashes=hashes,
                initial_inventory=inventory(target), outputs=work["outputs"])


def collect_changes(copy_record: dict) -> list[dict]:
    root = Path(copy_record["root"]).resolve(strict=True)
    stage = Path(copy_record["staging"]).resolve(strict=True)
    if stage.is_relative_to(root):
        raise ValueError("Staging must be outside worker writes")
    initial = copy_record["initial_inventory"]
    current = inventory(root)
    changes = []
    for rel in sorted(initial.keys() | current.keys()):
        before, after = initial.get(rel), current.get(rel)
        if before == after:
            continue
        if not permits(rel, copy_record["outputs"]):
            raise ValueError("Out-of-scope output")
        if after is not None:
            data = safe_path(root, rel).read_bytes()
            if digest(data) != after:
                raise ValueError("Output changed while collecting")
            path = stage / after
            if path.exists() and digest(path.read_bytes()) != after:
                raise ValueError("Staging collision")
            path.write_bytes(data)
        changes.append(dict(path=rel, operation="create" if before is None else
                            "delete" if after is None else "modify",
                            before_sha256=before, after_sha256=after))
    return changes
