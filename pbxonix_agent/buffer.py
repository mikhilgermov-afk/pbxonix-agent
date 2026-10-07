"""Local SQLite buffer.

When the cloud is unreachable the agent keeps collecting and writes samples
here, then replays them once the connection returns. The buffer is bounded in
two independent ways -- row count and file size -- because a monitoring agent
that fills the disk of the PBX it is monitoring has caused the exact outage it
was installed to prevent.

Oldest rows are dropped first: during a long outage, recent telemetry is worth
more than the beginning of the gap.
"""

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_kind_id ON samples (kind, id);
"""

# Refuse to buffer if the filesystem is nearly full, whatever our own limits say.
MIN_FREE_BYTES = 128 * 1024 * 1024


class BufferFull(Exception):
    pass


class Buffer:
    def __init__(self, path: str, *, max_rows: int = 50_000, max_bytes: int = 64 * 1024 * 1024):
        self.path = path
        self.max_rows = max_rows
        self.max_bytes = max_bytes

        directory = os.path.dirname(path) or "."
        os.makedirs(directory, mode=0o750, exist_ok=True)

        self._conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # WAL survives an unclean shutdown without losing committed samples.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        try:
            os.chmod(path, 0o640)
        except OSError:
            pass

    # ------------------------------------------------------------- writing --
    def append(
        self, kind: str, payload: Dict[str, Any], recorded_at: Optional[datetime] = None
    ) -> None:
        when = recorded_at or datetime.now(timezone.utc)
        if self._free_bytes() < MIN_FREE_BYTES:
            # Drop the oldest half rather than the sample we just took, then
            # give up for this cycle if that did not help.
            self._trim_to(self.max_rows // 2)
            if self._free_bytes() < MIN_FREE_BYTES:
                raise BufferFull("less than 128 MB free on {}".format(self.path))

        self._conn.execute(
            "INSERT INTO samples (kind, recorded_at, payload) VALUES (?, ?, ?)",
            (kind, when.isoformat(), json.dumps(payload)),
        )
        self.enforce_limits()

    # ------------------------------------------------------------- reading --
    def take(self, kind: str, limit: int = 200) -> List[Tuple[int, Dict[str, Any]]]:
        rows = self._conn.execute(
            "SELECT id, payload FROM samples WHERE kind = ? ORDER BY id LIMIT ?",
            (kind, limit),
        ).fetchall()
        out: List[Tuple[int, Dict[str, Any]]] = []
        for row in rows:
            try:
                out.append((row["id"], json.loads(row["payload"])))
            except ValueError:
                # A corrupt row must not stall the queue behind it forever.
                self.delete([row["id"]])
        return out

    def delete(self, ids: Sequence[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        # Not injectable: `placeholders` is a run of "?" marks sized to `ids`,
        # and every value is bound as a parameter.
        self._conn.execute(
            "DELETE FROM samples WHERE id IN ({})".format(placeholders),  # noqa: S608
            tuple(ids),
        )

    def count(self, kind: Optional[str] = None) -> int:
        if kind is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM samples WHERE kind = ?", (kind,)
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------- limits --
    def enforce_limits(self) -> None:
        if self.count() > self.max_rows:
            self._trim_to(self.max_rows)
        if self._file_bytes() > self.max_bytes:
            self._trim_to(max(1, self.count() // 2))
            self._conn.execute("VACUUM")

    def _trim_to(self, keep: int) -> int:
        total = self.count()
        excess = total - keep
        if excess <= 0:
            return 0
        self._conn.execute(
            "DELETE FROM samples WHERE id IN (SELECT id FROM samples ORDER BY id LIMIT ?)",
            (excess,),
        )
        return excess

    def _file_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total

    def _free_bytes(self) -> int:
        try:
            return shutil.disk_usage(os.path.dirname(self.path) or "/").free
        except OSError:
            return MIN_FREE_BYTES

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Buffer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
