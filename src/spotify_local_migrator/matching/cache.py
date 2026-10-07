import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..errors import StateError


class SearchCache:
    def __init__(self, path: Path, *, ttl_days: float = 7):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ttl = ttl_days * 86400
        self.path = path
        self.connection = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS searches "
            "(key TEXT PRIMARY KEY, created REAL NOT NULL, response TEXT NOT NULL)"
        )
        self.connection.commit()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT created, response FROM searches WHERE key = ?", (key,)
        ).fetchone()
        if row and row[0] >= time.time() - self.ttl:
            try:
                response = json.loads(row[1])
                if isinstance(response, dict):
                    self.hits += 1
                    return response
            except ValueError:
                pass
        self.misses += 1
        return None

    def put(self, key: str, response: dict[str, Any]) -> None:
        try:
            self.connection.execute(
                "INSERT OR REPLACE INTO searches VALUES (?, ?, ?)",
                (key, time.time(), json.dumps(response, ensure_ascii=False, allow_nan=False)),
            )
            self.connection.commit()
        except (sqlite3.Error, ValueError) as exc:
            raise StateError("Cannot persist the search cache.") from exc

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
