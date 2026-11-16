"""Private content-addressed file checkpoints, separate from repository history."""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import stat
import threading
import time
from pathlib import Path
from uuid import uuid4


class SnapshotError(ValueError):
    pass


class SnapshotService:
    def __init__(self, directory, *, max_files=1000, max_file_bytes=8 * 1024 * 1024, max_bytes=64 * 1024 * 1024,
                 max_storage_bytes=512 * 1024 * 1024):
        self.directory = Path(directory).resolve()
        self.max_files, self.max_file_bytes, self.max_bytes = max_files, max_file_bytes, max_bytes
        self._locks = {}
        self.max_storage_bytes = max_storage_bytes
        self._storage_lock = threading.RLock()

    def lock(self, root):
        return self._locks.setdefault(str(Path(root).resolve()), asyncio.Lock())

    def _manifest(self, identifier):
        return self.directory / "records" / (hashlib.sha256(identifier.encode()).hexdigest() + ".json")

    def _usage(self):
        return sum(path.stat().st_size for subdirectory in ("records", "blobs")
                   for path in (self.directory / subdirectory).glob("*") if path.is_file())

    def _check_json_size(self, path, value):
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if self._usage() - (path.stat().st_size if path.exists() else 0) + size > self.max_storage_bytes:
            raise SnapshotError("checkpoint storage budget exceeded")

    def _write_json(self, path, value):
        with self._storage_lock:
            self._check_json_size(path, value)
            self._replace_json(path, value)

    @staticmethod
    def _replace_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _excluded(name):
        return (name in (".git", ".agents", ".codex", ".venv", "venv", "node_modules", "__pycache__")
                or name == ".env" or name.startswith(".env.")
                or name.lower().endswith((".db", ".sqlite", ".sqlite3", ".pem", ".key", ".pfx")))

    @staticmethod
    def _linked(info):
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)

    def _root(self, root):
        root = Path(root)
        if not root.is_absolute() or ".." in root.parts:
            raise SnapshotError("checkpoint root must be a canonical absolute directory")
        current = Path(root.anchor)
        for part in root.parts[1:]:
            current = current / part
            if self._linked(current.lstat()):
                raise SnapshotError("checkpoint root or ancestor was replaced by a link")
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise SnapshotError("checkpoint root is no longer a directory")
        return root

    def _read(self, root, relative):
        root = self._root(root)
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise SnapshotError("invalid checkpoint path")
        candidate = root / relative
        if candidate == root or not candidate.is_relative_to(root):
            raise SnapshotError("invalid checkpoint path")
        current = root
        for part in candidate.relative_to(root).parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                return None
            if self._linked(info):
                raise SnapshotError("linked workspace paths cannot be checkpointed or restored")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SnapshotError("only regular, single-link files can be restored")
        if info.st_size > self.max_file_bytes:
            raise SnapshotError("file exceeds checkpoint budget")
        with candidate.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise SnapshotError("file changed during checkpoint read")
            data = stream.read(self.max_file_bytes + 1)
            after = os.fstat(stream.fileno())
        final = candidate.lstat()
        if (len(data) > self.max_file_bytes or opened.st_mtime_ns != after.st_mtime_ns
                or opened.st_size != after.st_size or after.st_nlink != 1
                or (final.st_dev, final.st_ino, final.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns)):
            raise SnapshotError("file changed during checkpoint read")
        return data, stat.S_IMODE(info.st_mode) & 0o777

    def capture(self, root):
        with self._storage_lock:
            return self._capture(root)

    def _capture(self, root):
        root = self._root(root)
        if self.directory == root or self.directory.is_relative_to(root):
            raise SnapshotError("checkpoint storage must be outside the writable workspace")
        files, excluded, size, count = {}, [], 0, 0
        used = self._usage()
        def enumeration_failed(error):
            raise SnapshotError("workspace could not be fully enumerated") from error
        for directory, directories, names in os.walk(root, followlinks=False, onerror=enumeration_failed):
            retained = []
            for name in directories:
                count += 1
                if count > self.max_files:
                    raise SnapshotError("checkpoint entry budget exceeded")
                path = Path(directory) / name
                if self._excluded(name):
                    excluded.append(path.relative_to(root).as_posix() + "/")
                elif self._linked(path.lstat()):
                    raise SnapshotError("linked directories are not checkpointed")
                else:
                    retained.append(name)
            directories[:] = retained
            for name in names:
                count += 1
                if count > self.max_files:
                    raise SnapshotError("checkpoint file budget exceeded")
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                if self._excluded(name):
                    excluded.append(relative)
                    continue
                entry = self._read(root, relative)
                if entry is None:
                    raise SnapshotError("workspace changed while capturing")
                data, mode = entry
                size += len(data)
                if size > self.max_bytes:
                    raise SnapshotError("checkpoint byte budget exceeded")
                digest = hashlib.sha256(data).hexdigest()
                blob = self.directory / "blobs" / digest
                blob.parent.mkdir(parents=True, exist_ok=True)
                if not blob.exists():
                    if used + len(data) > self.max_storage_bytes:
                        raise SnapshotError("checkpoint storage budget exceeded")
                    temporary = blob.with_name(digest + "." + uuid4().hex + ".tmp")
                    try:
                        with temporary.open("xb") as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, blob)
                        used += len(data)
                    finally:
                        temporary.unlink(missing_ok=True)
                else:
                    self._blob({"hash": digest})
                files[relative] = {"hash": digest, "mode": mode}
        return {"files": files, "excluded": excluded}

    def begin(self, root, identifier, session_id, run_id):
        with self._storage_lock:
            return self._begin(root, identifier, session_id, run_id)

    def _begin(self, root, identifier, session_id, run_id):
        root = self._root(root)
        path = self._manifest(identifier)
        if path.exists():
            raise SnapshotError("checkpoint already exists; operation must not be replayed")
        before = self.capture(root)
        value = {"execution_id": identifier, "session_id": session_id, "run_id": run_id,
                 "root": str(root), "state": "prepared", "before": before}
        self._write_json(path, value)
        return value

    def finish(self, value):
        with self._storage_lock:
            value["after"] = self.capture(Path(value["root"]))
            value["state"] = "ready"
            self._write_json(self._manifest(value["execution_id"]), value)

    def prune(self, completed_runs, days):
        with self._storage_lock:
            cutoff = time.time() - days * 86400
            documents = {}
            for path in (self.directory / "records").glob("*.json"):
                documents[path] = json.loads(path.read_text(encoding="utf-8"))
            candidates = {path for path, value in documents.items() if value["run_id"] in completed_runs
                          and value["state"] in ("ready", "restored") and path.stat().st_mtime < cutoff}
            protected = {member for path, value in documents.items() if path not in candidates
                         for member in value.get("members", [])}
            candidates = {path for path in candidates if documents[path]["execution_id"] not in protected}
            for path in candidates:
                path.unlink()
            referenced = {item["hash"] for path, value in documents.items() if path not in candidates
                          for version in ("before", "after") for item in value.get(version, {}).get("files", {}).values()}
            removed = 0
            for path in (self.directory / "blobs").glob("*"):
                if len(path.name) == 64 and all(c in "0123456789abcdef" for c in path.name) and path.name not in referenced:
                    path.unlink()
                    removed += 1
            return {"records_removed": len(candidates), "blobs_removed": removed}

    def load(self, identifier, session_id, root):
        root = self._root(root)
        path = self._manifest(identifier)
        if not path.is_file():
            raise SnapshotError("checkpoint not found")
        value = json.loads(path.read_text(encoding="utf-8"))
        if value["session_id"] != session_id or value["root"] != str(root):
            raise SnapshotError("checkpoint belongs to another session or workspace")
        return value

    def recent(self, session_id):
        records = []
        directory = self.directory / "records"
        if directory.exists():
            for path in directory.glob("*.json"):
                value = json.loads(path.read_text(encoding="utf-8"))
                if value["session_id"] == session_id:
                    records.append({"execution_id": value["execution_id"], "run_id": value["run_id"],
                                    "state": value["state"], "updated": path.stat().st_mtime_ns})
        return sorted(records, key=lambda value: value["updated"], reverse=True)[:50]

    def _blob(self, entry):
        digest = entry["hash"]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise SnapshotError("invalid checkpoint content reference")
        with (self.directory / "blobs" / digest).open("rb") as stream:
            data = stream.read(self.max_file_bytes + 1)
        if len(data) > self.max_file_bytes:
            raise SnapshotError("checkpoint content exceeds file budget")
        if hashlib.sha256(data).hexdigest() != digest:
            raise SnapshotError("checkpoint content integrity failure")
        return data

    def combine(self, identifiers, run_id, session_id, root):
        with self._storage_lock:
            if len(set(identifiers)) != len(identifiers):
                raise SnapshotError("duplicate file checkpoint in run")
            identifier = "run:" + run_id
            if self._manifest(identifier).exists():
                existing = self.load(identifier, session_id, root)
                if existing.get("members") != identifiers:
                    raise SnapshotError("run checkpoint membership changed")
                return identifier
            before, after, excluded = {}, {}, set()
            for member in identifiers:
                value = self.load(member, session_id, root)
                if value["state"] != "ready":
                    raise SnapshotError("a run member is incomplete or already restored")
                original, final = value["before"]["files"], value["after"]["files"]
                excluded.update(value["before"]["excluded"] + value["after"]["excluded"])
                for path in set(original) | set(final):
                    old, new = original.get(path), final.get(path)
                    if old == new:
                        continue
                    if path in after and old != after[path]:
                        raise SnapshotError("interleaved file changes cannot be attributed to this run")
                    if path not in before:
                        before[path] = old
                    after[path] = new
            self._write_json(self._manifest(identifier), {
                "execution_id": identifier, "session_id": session_id, "run_id": run_id,
                "root": str(Path(root).resolve()), "state": "ready", "members": identifiers,
                "before": {"files": {p: item for p, item in before.items() if item is not None}, "excluded": sorted(excluded)},
                "after": {"files": {p: item for p, item in after.items() if item is not None}, "excluded": sorted(excluded)},
            })
            return identifier

    def preview(self, identifier, session_id, root):
        value = self.load(identifier, session_id, root)
        if value["state"] != "ready":
            raise SnapshotError("checkpoint is incomplete or already restored")
        before, after = value["before"]["files"], value["after"]["files"]
        changes, conflicts = [], []
        for relative in sorted(set(before) | set(after)):
            old, new = before.get(relative), after.get(relative)
            if old == new:
                continue
            try:
                current = self._read(Path(root), relative)
                current_entry = {"hash": hashlib.sha256(current[0]).hexdigest(), "mode": current[1]} if current else None
                conflict = current_entry != new
            except (OSError, SnapshotError):
                conflict = True
            if conflict:
                conflicts.append(relative)
            old_bytes, new_bytes = self._blob(old) if old else b"", self._blob(new) if new else b""
            diff = None
            try:
                if max(len(old_bytes), len(new_bytes)) <= 64000 and b"\0" not in old_bytes + new_bytes:
                    diff = "".join(difflib.unified_diff(old_bytes.decode("utf-8").splitlines(True), new_bytes.decode("utf-8").splitlines(True), fromfile=relative, tofile=relative))[:20000]
            except UnicodeDecodeError:
                pass
            changes.append({"path": relative, "kind": "added" if old is None else "deleted" if new is None else "modified",
                            "conflict": conflict, "diff": diff})
        return {"execution_id": identifier, "changes": changes, "conflicts": conflicts,
                "excluded": sorted(set(value["before"]["excluded"] + value["after"]["excluded"]))}

    def restore(self, identifier, session_id, root):
        with self._storage_lock:
            return self._restore(identifier, session_id, root)

    def _restore(self, identifier, session_id, root):
        value = self.load(identifier, session_id, root)
        if value["state"] == "restored":
            return {"execution_id": identifier, "already_restored": True}
        preview = self.preview(identifier, session_id, root)
        if preview["conflicts"]:
            raise SnapshotError("files changed after the operation; automatic restore refused")
        value.update(state="restoring", restored_paths=[])
        final_size = {**value, "restored_paths": [item["path"] for item in preview["changes"]]}
        self._check_json_size(self._manifest(identifier), final_size)
        self._write_json(self._manifest(identifier), value)
        for member in value.get("members", []):
            item = self.load(member, session_id, root)
            if item["state"] != "ready":
                raise SnapshotError("run member changed before restoration")
            item.update(state="group_reserved", group=identifier)
            self._write_json(self._manifest(member), item)
        root = self._root(root)
        for change in preview["changes"]:
            relative = change["path"]
            expected = value["after"]["files"].get(relative)
            current = self._read(root, relative)
            observed = {"hash": hashlib.sha256(current[0]).hexdigest(), "mode": current[1]} if current else None
            if observed != expected:
                raise SnapshotError("workspace changed during restore; inspect restore journal")
            target = root / relative
            baseline = value["before"]["files"].get(relative)
            if baseline is None:
                target.unlink()
            else:
                data = self._blob(baseline)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(".velora-restore-" + uuid4().hex)
                try:
                    with temporary.open("xb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary, baseline["mode"])
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            value["restored_paths"].append(relative)
            self._write_json(self._manifest(identifier), value)
        value["state"] = "restored"
        self._write_json(self._manifest(identifier), value)
        for member in value.get("members", []):
            item = self.load(member, session_id, root)
            item["state"] = "restored"
            self._write_json(self._manifest(member), item)
        return {"execution_id": identifier, "restored_paths": value["restored_paths"],
                "coverage": "partial" if preview["excluded"] else "covered_workspace_files",
                "excluded": preview["excluded"]}
