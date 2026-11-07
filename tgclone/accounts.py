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
    user_max_jobs: int = Field(1, ge=1, le=10)  # jobs at once on the free trial
    premium_max_jobs: int = Field(3, ge=1, le=20)  # jobs at once with premium (channel + group + topics)
    user_max_batch: int = Field(0, ge=0)  # posts per save job per user (0 = no limit)
    max_total_jobs: int = Field(10, ge=1, le=100)  # all users together
    max_users: int = Field(0, ge=0)  # 0 = unlimited logged-in users
    premium_enabled: bool = True  # plans: new users get a free trial, then need premium days
    trial_hours: int = Field(24, ge=0, le=24 * 30)  # free trial for every new user (0 = none)
    start_photo: str = ""  # path or https URL shown on top of /start
    premium_emoji: bool = True  # animated custom emoji in texts and buttons
    plans: str = "7=₹49, 30=₹149, 90=₹349"  # days=price shown on the Get premium screen
    pay_info: str = ""  # how to pay (UPI id, link...) shown after a plan is chosen
    extra_connections: bool = True  # big files: 4 extra connections (off if your network cuts them)
    start_text: str = ""  # owner's /start message (HTML, {name} {bot} {plan} {id}); "" = default


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
            CREATE TABLE IF NOT EXISTS plans(uid INTEGER PRIMARY KEY, expires REAL, kind TEXT,
                notified INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS blocked(chat_id INTEGER PRIMARY KEY, title TEXT, added REAL);
            CREATE TABLE IF NOT EXISTS botmode(uid INTEGER PRIMARY KEY, name TEXT, created REAL);
            """
        )
        for col in ("folder", "fname"):  # older databases: chats blocked from a folder link
            try:
                with self.db:
                    self.db.execute(f"ALTER TABLE blocked ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # already there
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
        data = json.loads(row[0]) if row else {}
        if data.get("user_max_batch") == 500:  # old default limit -> no limit
            data["user_max_batch"] = 0
        return HubSettings(**data)

    def save_settings(self, s: HubSettings) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES ('hub', ?)", (s.model_dump_json(),))

    # ---------------------------------------------------------- plans
    def plan(self, uid: int) -> Optional[dict]:
        row = self.db.execute("SELECT expires, kind, notified FROM plans WHERE uid=?", (uid,)).fetchone()
        return dict(zip(("expires", "kind", "notified"), row)) if row else None

    def set_plan(self, uid: int, expires: float, kind: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO plans VALUES (?,?,?,0)", (uid, expires, kind))

    def set_notified(self, uid: int, level: int) -> None:
        with self.db:
            self.db.execute("UPDATE plans SET notified=? WHERE uid=?", (level, uid))

    def plans(self) -> List[dict]:
        rows = self.db.execute("SELECT uid, expires, kind, notified FROM plans ORDER BY expires DESC")
        return [dict(zip(("uid", "expires", "kind", "notified"), r)) for r in rows]

    # ---------------------------------------------------------- blocked chats
    def block(self, chat_id: int, title: str, folder: str = "", fname: str = "") -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO blocked(chat_id, title, added, folder, fname) "
                            "VALUES (?,?,?,?,?)", (chat_id, title, time.time(), folder, fname))

    def unblock_folder(self, folder: str) -> int:
        with self.db:
            return self.db.execute("DELETE FROM blocked WHERE folder=?", (folder,)).rowcount

    def unblock(self, chat_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM blocked WHERE chat_id=?", (chat_id,))

    def blocked(self) -> List[dict]:
        rows = self.db.execute("SELECT chat_id, title, folder, fname FROM blocked ORDER BY added DESC")
        return [{"chat_id": c, "title": t, "folder": f or "", "fname": n or ""} for c, t, f, n in rows]

    def blocked_ids(self) -> set:
        return {r[0] for r in self.db.execute("SELECT chat_id FROM blocked")}

    # ---------------------------------------------------------- bot-only users
    def add_botmode(self, uid: int, name: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO botmode VALUES (?,?,?)", (uid, name, time.time()))

    def remove_botmode(self, uid: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM botmode WHERE uid=?", (uid,))

    def botmode_users(self) -> List[int]:
        return [r[0] for r in self.db.execute("SELECT uid FROM botmode")]

    def name_of(self, uid: int) -> str:
        for sql in ("SELECT name FROM sessions WHERE uid=?", "SELECT name FROM botmode WHERE uid=?",
                    "SELECT name FROM access WHERE uid=?"):
            row = self.db.execute(sql, (uid,)).fetchone()
            if row and row[0]:
                return row[0]
        return ""

    def new_since(self, ts: float) -> int:
        return sum(self.db.execute(f"SELECT COUNT(*) FROM {t} WHERE created>?", (ts,)).fetchone()[0]
                   for t in ("sessions", "botmode"))

    def counts(self) -> Dict[str, int]:
        c = {"users": self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]}
        c["botmode"] = self.db.execute("SELECT COUNT(*) FROM botmode").fetchone()[0]
        c["blocked"] = self.db.execute("SELECT COUNT(*) FROM blocked").fetchone()[0]
        c["premium"] = self.db.execute("SELECT COUNT(*) FROM plans WHERE kind='premium' AND expires>?",
                                       (time.time(),)).fetchone()[0]
        for st in ("pending", "approved", "banned"):
            c[st] = self.db.execute("SELECT COUNT(*) FROM access WHERE state=?", (st,)).fetchone()[0]
        return c
