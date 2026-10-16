"""Save the result of each finished unit of work as it completes, so an interrupted run
can resume instead of starting over.

A checkpoint is a JSON-lines file: a header line (what the run was for and when), then one line
per finished item. Opening it with a different signature, or after it has gone stale, starts a
new one. A line cut off by a crash is ignored.
"""

import json
import os
import threading
from datetime import datetime, timedelta, timezone


class Checkpoint:
    def __init__(self, path, signature, max_age_hours=None):
        self.path = path
        self.signature = signature
        self.items = {}
        self._lock = threading.Lock()
        if not self._load(max_age_hours):
            self._start()

    def _load(self, max_age_hours):
        """True when an existing file matches this run and is still fresh (its items are loaded)."""
        if not self.path.exists():
            return False
        try:
            with self.path.open(encoding="utf-8") as file:
                lines = file.read().splitlines()
            header = json.loads(lines[0])["header"]
        except (OSError, ValueError, KeyError, IndexError):
            return False
        if header.get("signature") != self.signature:
            return False
        if max_age_hours is not None:
            created = datetime.fromisoformat(header["created"])
            if datetime.now(timezone.utc) - created > timedelta(hours=max_age_hours):
                return False
        for line in lines[1:]:
            try:
                record = json.loads(line)
                self.items[record["key"]] = record["payload"]
            except (ValueError, KeyError, TypeError):
                continue  # a line cut off by a crash
        return True

    def _start(self):
        self.items = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = {"header": {"signature": self.signature, "created": datetime.now(timezone.utc).isoformat()}}
        self.path.write_text(json.dumps(header) + "\n", encoding="utf-8")

    def get(self, key):
        return self.items.get(key)

    def add(self, key, payload):
        """Append one finished item (safe to call from several threads)."""
        line = json.dumps({"key": key, "payload": payload}, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(line + "\n")
                file.flush()
                os.fsync(file.fileno())
            self.items[key] = payload

    def snapshot(self):
        """The payloads of all finished items so far, in the order they finished."""
        with self._lock:
            return list(self.items.values())

    def clear(self):
        """Remove the file once the run's real output has been written."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
