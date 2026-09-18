"""SQLite catalogue and snapshot archive. Credentials belong in the OS keyring."""

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .models import Profile


def data_dir() -> Path:
    return (
        Path(os.environ.get("SDLC_DATA_DIR", "~/.local/share/sdlc-builder"))
        .expanduser()
        .resolve()
    )


def workspace(task_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", task_id):
        raise ValueError("Invalid task ID")
    root = data_dir() / "workspaces"
    path = root / task_id
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise ValueError("Invalid workspace")
    return path


class Store:
    def __init__(self, root: Path | None = None):
        self.root = root or data_dir()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "app.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1');
                CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, value TEXT NOT NULL, snapshot TEXT,
                    version INTEGER NOT NULL DEFAULT -1, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_reviews (
                    task_id TEXT PRIMARY KEY, value TEXT NOT NULL
                );
            """)
            db.execute(
                "INSERT OR IGNORE INTO profiles VALUES (?, ?)",
                (
                    "demo",
                    Profile(
                        id="demo",
                        label="Demo · no API key",
                        provider="demo",
                        max_steps=12,
                    ).model_dump_json(),
                ),
            )
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def all(self, table: str) -> list[dict]:
        assert table in {"profiles", "projects", "tasks"}
        with self.connect() as db:
            rows = db.execute(f"SELECT * FROM {table} ORDER BY rowid DESC").fetchall()
        return [self.decode(row) for row in rows]

    @staticmethod
    def decode(row) -> dict:
        value = json.loads(row["value"])
        if "snapshot" in row.keys():
            value.update(
                snapshot=json.loads(row["snapshot"]) if row["snapshot"] else None,
                version=row["version"],
                updated=row["updated"],
            )
        return value

    def get(self, table: str, identity: str) -> dict:
        assert table in {"profiles", "projects", "tasks"}
        with self.connect() as db:
            row = db.execute(
                f"SELECT * FROM {table} WHERE id=?", (identity,)
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown {table.rstrip('s')}")
        return self.decode(row)

    def put(self, table: str, value: dict):
        assert table in {"profiles", "projects", "tasks"}
        with self.connect() as db:
            if table == "tasks":
                value = {
                    k: v
                    for k, v in value.items()
                    if k not in {"snapshot", "version", "updated"}
                }
                db.execute(
                    """INSERT INTO tasks(id,value,updated) VALUES (?,?,?)
                    ON CONFLICT(id) DO UPDATE SET value=excluded.value, updated=excluded.updated""",
                    (value["id"], json.dumps(value), time.time()),
                )
            else:
                db.execute(
                    f"INSERT INTO {table} VALUES (?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                    (value["id"], json.dumps(value)),
                )

    def archive(self, task_id: str, envelope: dict):
        # Full authoritative snapshots deliberately avoid patch gaps on reconnect.
        with self.connect() as db:
            db.execute(
                """UPDATE tasks SET snapshot=?,version=?,updated=? WHERE id=? AND
                (version<? OR (version=? AND
                COALESCE(json_extract(snapshot, '$.conversation.version'), -1)<?))""",
                (
                    json.dumps(envelope),
                    envelope["version"],
                    time.time(),
                    task_id,
                    envelope["version"],
                    envelope["version"],
                    envelope.get("conversation", {}).get("version", -1),
                ),
            )

    def delete_profile(self, identity: str):
        with self.connect() as db:
            db.execute("DELETE FROM profiles WHERE id=?", (identity,))

    def delete_task(self, identity: str):
        with self.connect() as db:
            db.execute("DELETE FROM task_reviews WHERE task_id=?", (identity,))
            db.execute("DELETE FROM tasks WHERE id=?", (identity,))

    def review(self, identity: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM task_reviews WHERE task_id=?", (identity,)
            ).fetchone()
        return (
            json.loads(row["value"])
            if row
            else {"version": 0, "checkpoints": {}, "comments": []}
        )

    def save_review(self, identity: str, value: dict):
        # Called under the same task lock as deletion and other review writes.
        value["version"] += 1
        with self.connect() as db:
            db.execute(
                "INSERT INTO task_reviews VALUES (?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET value=excluded.value",
                (identity, json.dumps(value)),
            )


def resolve_credential(ref: str) -> str:
    if ref.startswith("env:"):
        key = os.environ.get(ref[4:], "")
    elif ref.startswith("keyring:"):
        import keyring

        key = keyring.get_password("sdlc-builder", ref[8:]) or ""
    else:
        key = ""
    if not key:
        raise ValueError(
            "Provider credential is missing. Update Settings and start a new task."
        )
    return key
