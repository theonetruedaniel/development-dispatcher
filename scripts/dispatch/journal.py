"""Append-only journal and workspace-wide dispatch exclusion."""
from contextlib import contextmanager
from pathlib import Path
import json
import os
import time
import uuid
import threading

SAFE = {"completed", "failed", "cancelled", "blocked", "deferred", "reconciled"}


class JournalError(RuntimeError):
    pass


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.lock_path = self.path.parent.parent / "active.json"
        self.guard_path = self.path.parent.parent / "writer.lock"
        self._lock_owner = None

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        try:
            with self.path.open("rb") as stream:
                for seq, line in enumerate(stream, 1):
                    if len(line) > 4*1024*1024 or not line.endswith(b"\n"):
                        raise JournalError("Journal tail incomplete or oversized")
                    row = json.loads(line)
                    if not isinstance(row, dict) or type(row.get("seq")) is not int or row["seq"] != seq:
                        raise JournalError("Journal sequence invalid")
                    rows.append(row)
        except (OSError, ValueError) as exc:
            raise JournalError("Journal unreadable; preserve it for reconciliation") from exc
        return rows

    def append(self, record: dict):
        rows = self.records()
        row = dict(record, seq=len(rows)+1, recorded_at=time.time())
        data = json.dumps(row, ensure_ascii=False, allow_nan=False).encode() + b"\n"
        if len(data) > 4*1024*1024:
            raise JournalError("Journal record too large")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        checkpoint = self.path.with_suffix(".checkpoint.json")
        temp = checkpoint.with_name(checkpoint.name + "." + uuid.uuid4().hex + ".tmp")
        with temp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, checkpoint)

    @contextmanager
    def lock(self, recover=False):
        if self._lock_owner == threading.get_ident():
            if recover:
                raise JournalError('Cannot recover an active writer')
            yield
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        guard = self.guard_path.open("a+b")
        if guard.seek(0, os.SEEK_END) == 0:
            guard.write(b"0")
            guard.flush()
        guard.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            guard.close()
            raise JournalError("Another writer owns this workspace") from exc
        owns_active = False
        try:
            if self.lock_path.exists():
                owner = json.loads(self.lock_path.read_text(encoding="utf-8"))
                if not recover or owner.get("journal") != str(self.path):
                    raise JournalError("Workspace has an active or uncertain dispatch")
            else:
                with self.lock_path.open("x", encoding="utf-8") as stream:
                    json.dump({"journal": str(self.path)}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
            owns_active = True
            self._lock_owner = threading.get_ident()
            yield
        finally:
            if owns_active:
                self._lock_owner = None
            if owns_active:
                try:
                    rows = self.records()
                    if rows and rows[-1].get("state") in SAFE:
                        self.lock_path.unlink()
                except (JournalError, OSError):
                    pass
            # Keep the guard inode stable. The OS releases ownership on process death.
            guard.seek(0)
            if os.name == "nt":
                msvcrt.locking(guard.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
            guard.close()
