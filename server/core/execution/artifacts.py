"""Bounded private execution output pages; reading never replays a workload."""
import hashlib
import json
import os
from pathlib import Path
import threading
from uuid import uuid4

_locks = {}
_registry_lock = threading.Lock()


class OutputStore:
    def __init__(self, directory, max_bytes=128 * 1024 * 1024):
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        with _registry_lock:
            self.lock = _locks.setdefault(str(self.directory.resolve()), threading.RLock())

    def _path(self, execution_id):
        return self.directory / (hashlib.sha256(execution_id.encode()).hexdigest() + ".json")

    def put(self, execution_id, session_id, output):
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self._path(execution_id)
            if path.exists():
                raise ValueError("execution output already exists")
            data = json.dumps({"execution_id": execution_id, "session_id": session_id, "output": output}, ensure_ascii=False).encode()
            used = sum(p.stat().st_size for p in self.directory.glob("*.json"))
            if used + len(data) > self.max_bytes:
                raise ValueError("execution output storage budget exceeded")
            temporary = path.with_suffix("." + uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def read(self, execution_id, session_id, channel="stdout", offset=0, limit=12000):
        if channel not in ("stdout", "stderr") or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16000:
            raise ValueError("invalid execution output page")
        path = self._path(execution_id)
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("execution output unavailable")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["session_id"] != session_id or data["execution_id"] != execution_id:
            raise ValueError("execution output belongs to another session")
        text = data["output"][channel]
        end = min(len(text), offset + limit)
        return {"ok": True, "execution_id": execution_id, "channel": channel, "offset": offset,
                "content": text[offset:end], "next_offset": end if end < len(text) else None,
                "retained_characters": len(text), "capture_truncated": data["output"].get(channel + "_truncated", False)}

    def receipt(self, execution_id, session_id):
        path = self._path(execution_id)
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("execution receipt unavailable")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["session_id"] != session_id or data["execution_id"] != execution_id:
            raise ValueError("execution receipt identity mismatch")
        return data["output"]
