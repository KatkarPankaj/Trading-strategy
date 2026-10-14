"""Local single-owner paper-worker lease; not a distributed account coordinator."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class PaperWorkerLease:
    def __init__(self, database_path: str) -> None:
        self._file: BinaryIO | None = None
        if database_path == ":memory:":
            return
        path = Path(database_path).resolve()
        handle = path.with_name(path.name + ".paper-worker.lock").open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise ValueError("PAPER_WORKER_ALREADY_OWNED: use a separate database/account") from exc
        self._file = handle

    def close(self) -> None:
        if self._file is None:
            return
        handle, self._file = self._file, None
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
