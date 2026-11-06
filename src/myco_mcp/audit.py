"""Audit log: one JSON line per decision, to stdout and optionally to a file.

Arguments and replies are never logged, only their shape (argument names), because
they can carry personal data.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("myco")


class AuditLog:
    def __init__(self, file: Path | None = None, *, stream: Any = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._file = file
        self._lock = threading.Lock()
        if file is not None:
            file.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        entry = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "audit": event, **fields}
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            print(line, file=self._stream, flush=True)
            if self._file is not None:
                try:
                    with self._file.open("a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
                except OSError as exc:  # never fail a call because the audit file is unwritable
                    log.error("cannot write audit file %s: %s", self._file, exc)
        return entry
