"""Append-only JSONL audit log. One JSON object per line, never containing the signing secret."""
import json
import threading


class AuditError(Exception):
    pass


class AuditLog:
    def __init__(self, path, now_fn):
        self.path = str(path)
        self._now = now_fn
        self._seq = 0
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        with self._lock:
            self._seq += 1
            line = json.dumps(dict({"seq": self._seq, "ts": self._now()}, **record), sort_keys=True)
            try:
                with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
            except OSError as exc:
                raise AuditError(f"cannot write audit log {self.path}: {exc}") from exc
