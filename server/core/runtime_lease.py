"""Keep one service owner for a local runtime database."""
import hashlib
import os
from pathlib import Path

from sqlalchemy.engine import make_url


class RuntimeLease:
    def __init__(self, database_url, directory):
        url = make_url(database_url)
        self.path = (Path(url.database).resolve().with_suffix(".runtime.lock") if url.get_backend_name() == "sqlite" and url.database not in (None, ":memory:")
                     else Path(directory) / (hashlib.sha256(database_url.encode()).hexdigest() + ".lock"))
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise RuntimeError("runtime ownership path must not be a link")
        stream = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeError("Another service owns this runtime database; stop it before starting another worker") from exc
        self.stream = stream
        return self

    def __exit__(self, *args):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
