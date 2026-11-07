"""SQLite store: source->dest message map (reply/edit/delete sync) + past progress."""

import sqlite3
from pathlib import Path
from typing import Dict, Iterable, Optional


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS msg_map(
                src_chat INTEGER, src_id INTEGER, dest_chat INTEGER, dest_id INTEGER,
                PRIMARY KEY (src_chat, src_id, dest_chat));
            CREATE TABLE IF NOT EXISTS progress(key TEXT PRIMARY KEY, last_id INTEGER);
            CREATE TABLE IF NOT EXISTS topic_map(
                src_chat INTEGER, src_topic INTEGER, dest_chat INTEGER, dest_topic INTEGER,
                PRIMARY KEY (src_chat, src_topic, dest_chat));
            """
        )
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(msg_map)")]
        if "media_key" not in cols:  # migrate v1 databases
            self.db.execute("ALTER TABLE msg_map ADD COLUMN media_key INTEGER")
            self.db.commit()

    def add(self, src_chat: int, src_id: int, dest_chat: int, dest_id: int,
            media_key: Optional[int] = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO msg_map(src_chat, src_id, dest_chat, dest_id, media_key)"
                " VALUES (?,?,?,?,?)",
                (src_chat, src_id, dest_chat, dest_id, media_key),
            )

    def media_key(self, src_chat: int, src_id: int) -> Optional[int]:
        row = self.db.execute(
            "SELECT media_key FROM msg_map WHERE src_chat=? AND src_id=? LIMIT 1",
            (src_chat, src_id),
        ).fetchone()
        return row[0] if row else None

    def set_media_key(self, src_chat: int, src_id: int, media_key: Optional[int]) -> None:
        with self.db:
            self.db.execute("UPDATE msg_map SET media_key=? WHERE src_chat=? AND src_id=?",
                            (media_key, src_chat, src_id))

    def get(self, src_chat: int, src_id: int) -> Dict[int, int]:
        rows = self.db.execute(
            "SELECT dest_chat, dest_id FROM msg_map WHERE src_chat=? AND src_id=?",
            (src_chat, src_id),
        )
        return dict(rows.fetchall())

    def get_one(self, src_chat: int, src_id: int, dest_chat: int) -> Optional[int]:
        row = self.db.execute(
            "SELECT dest_id FROM msg_map WHERE src_chat=? AND src_id=? AND dest_chat=?",
            (src_chat, src_id, dest_chat),
        ).fetchone()
        return row[0] if row else None

    def remove(self, src_chat: int, src_id: int) -> None:
        with self.db:
            self.db.execute(
                "DELETE FROM msg_map WHERE src_chat=? AND src_id=?", (src_chat, src_id)
            )

    def clear(self, src_chat: int, dests: Iterable[int]) -> None:
        with self.db:
            for d in dests:
                self.db.execute(
                    "DELETE FROM msg_map WHERE src_chat=? AND dest_chat=?", (src_chat, d)
                )

    def get_topic(self, src_chat: int, src_topic: int, dest_chat: int) -> Optional[int]:
        row = self.db.execute(
            "SELECT dest_topic FROM topic_map WHERE src_chat=? AND src_topic=? AND dest_chat=?",
            (src_chat, src_topic, dest_chat)).fetchone()
        return row[0] if row else None

    def set_topic(self, src_chat: int, src_topic: int, dest_chat: int, dest_topic: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO topic_map VALUES (?,?,?,?)",
                            (src_chat, src_topic, dest_chat, dest_topic))

    def topics_of(self, src_chat: int, src_topic: int) -> Dict[int, int]:
        rows = self.db.execute(
            "SELECT dest_chat, dest_topic FROM topic_map WHERE src_chat=? AND src_topic=?",
            (src_chat, src_topic))
        return dict(rows.fetchall())

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM msg_map").fetchone()[0]

    @staticmethod
    def key(src: int, dests: Iterable[int]) -> str:
        return f"{src}:{','.join(map(str, sorted(dests)))}"

    def get_progress(self, key: str) -> int:
        row = self.db.execute("SELECT last_id FROM progress WHERE key=?", (key,)).fetchone()
        return row[0] if row else 0

    def set_progress(self, key: str, last_id: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO progress VALUES (?,?)", (key, last_id))
