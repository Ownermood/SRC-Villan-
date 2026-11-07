"""Multi-user storage: encrypted Telegram sessions, access list and owner settings."""

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field

log = logging.getLogger("tgclone")


class HubSettings(BaseModel):
    mode: str = "open"  # open = anyone | approve = owner approves | owner = only the owner
    user_max_jobs: int = Field(2, ge=1, le=10)  # parallel jobs per user (owner: unlimited by this)
    user_max_batch: int = Field(500, ge=10, le=10000)  # posts per save job per user
    max_total_jobs: int = Field(10, ge=1, le=100)  # all users together
    max_users: int = Field(0, ge=0)  # 0 = unlimited logged-in users


def load_key(secret_key: str, key_file: Path) -> bytes:
    """SECRET_KEY from .env, else data/secret.key (created once, chmod 600)."""
    if secret_key:
        return secret_key.encode()
    if key_file.exists():
        return key_file.read_bytes().strip()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    log.warning("Created %s - back it up: without it saved logins can't be decrypted.", key_file)
    return key


class Accounts:
    def __init__(self, path: Path, key: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fernet = Fernet(key)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions(uid INTEGER PRIMARY KEY, session BLOB,
                name TEXT, phone TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS access(uid INTEGER PRIMARY KEY, state TEXT, name TEXT,
                updated REAL);
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
            """
        )
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    # ---------------------------------------------------------- sessions
    def save_session(self, uid: int, session: str, name: str, phone: str) -> None:
        token = self.fernet.encrypt(session.encode())
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?)",
                            (uid, token, name, phone, time.time()))

    def sessions(self) -> List[Tuple[int, Optional[str]]]:
        """[(uid, session or None if it can't be decrypted)]"""
        out = []
        for uid, token in self.db.execute("SELECT uid, session FROM sessions"):
            try:
                out.append((uid, self.fernet.decrypt(token).decode()))
            except InvalidToken:
                log.error("Session of %s can't be decrypted (secret key changed?)", uid)
                out.append((uid, None))
        return out

    def info(self, uid: int) -> Optional[dict]:
        row = self.db.execute("SELECT name, phone, created FROM sessions WHERE uid=?",
                              (uid,)).fetchone()
        return dict(zip(("name", "phone", "created"), row)) if row else None

    def users(self) -> List[dict]:
        rows = self.db.execute("SELECT uid, name, phone, created FROM sessions ORDER BY created")
        return [dict(zip(("uid", "name", "phone", "created"), r)) for r in rows]

    def delete_session(self, uid: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE uid=?", (uid,))

    # ---------------------------------------------------------- access
    def access(self, uid: int) -> Optional[str]:
        row = self.db.execute("SELECT state FROM access WHERE uid=?", (uid,)).fetchone()
        return row[0] if row else None

    def set_access(self, uid: int, state: Optional[str], name: str = "") -> None:
        with self.db:
            if state is None:
                self.db.execute("DELETE FROM access WHERE uid=?", (uid,))
            else:
                self.db.execute(
                    "INSERT INTO access VALUES (?,?,?,?) ON CONFLICT(uid) DO UPDATE SET "
                    "state=excluded.state, name=COALESCE(NULLIF(excluded.name,''), access.name), "
                    "updated=excluded.updated", (uid, state, name, time.time()))

    def by_state(self, state: str) -> List[dict]:
        rows = self.db.execute("SELECT uid, name FROM access WHERE state=? ORDER BY updated", (state,))
        return [{"uid": u, "name": n} for u, n in rows]

    # ---------------------------------------------------------- settings
    def settings(self) -> HubSettings:
        row = self.db.execute("SELECT value FROM kv WHERE key='hub'").fetchone()
        return HubSettings(**json.loads(row[0])) if row else HubSettings()

    def save_settings(self, s: HubSettings) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES ('hub', ?)", (s.model_dump_json(),))

    def counts(self) -> Dict[str, int]:
        c = {"users": self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]}
        for st in ("pending", "approved", "banned"):
            c[st] = self.db.execute("SELECT COUNT(*) FROM access WHERE state=?", (st,)).fetchone()[0]
        return c
