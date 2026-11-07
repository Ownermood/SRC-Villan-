"""Forum groups: clone topic by topic.

Source topic -> destination topic is created on demand (same title, colour and icon),
remembered in the store, and every message is posted inside its topic.
"""

import asyncio
import logging
import random
from typing import Dict, List, Optional

from telethon.errors import RPCError
from telethon.tl import functions, types

log = logging.getLogger("tgclone")

GENERAL = 1  # the "General" topic: posting without a topic lands there


def topic_of(msg) -> Optional[int]:
    """Topic id of a message in a forum group (None = General / not a forum)."""
    r = getattr(msg, "reply_to", None)
    if not r or not getattr(r, "forum_topic", False):
        return None
    return r.reply_to_top_id or r.reply_to_msg_id


def reply_target(msg) -> Optional[int]:
    """Message id this is a real reply to (a top-level post inside a topic isn't a reply)."""
    r = getattr(msg, "reply_to", None)
    if not r or not getattr(r, "reply_to_msg_id", None):
        return None
    if getattr(r, "forum_topic", False) and not r.reply_to_top_id:
        return None
    return r.reply_to_msg_id


def _new_topic_id(result) -> Optional[int]:
    """CreateForumTopic returns Updates; the topic id = id of its service message."""
    for upd in getattr(result, "updates", []) or []:
        msg = getattr(upd, "message", None)
        if isinstance(getattr(msg, "action", None), types.MessageActionTopicCreate):
            return msg.id
    for upd in getattr(result, "updates", []) or []:
        if isinstance(upd, types.UpdateMessageID):
            return upd.id
    return None


class Topics:
    def __init__(self, app) -> None:
        self.app = app
        self._locks: Dict[tuple, asyncio.Lock] = {}
        self._forum: Dict[int, bool] = {}  # chat id -> is a forum (after our checks)
        self._info: Dict[tuple, Optional[types.ForumTopic]] = {}
        self.warned: set = set()

    @property
    def on(self) -> bool:
        return self.app.cfg.topics.enabled

    # ------------------------------------------------------------ chats
    async def is_forum(self, client, chat_id: int) -> bool:
        try:
            ent = await client.get_entity(chat_id)
        except Exception:
            return False
        return bool(getattr(ent, "forum", False))

    async def dest_ready(self, dest: int) -> bool:
        """Destination supports topics (turns them on when allowed)."""
        if dest in self._forum:
            return self._forum[dest]
        client = self.app.sender
        ok = await self.is_forum(client, dest)
        if not ok and self.app.cfg.topics.auto_enable_forum:
            try:
                await client(functions.channels.ToggleForumRequest(
                    await client.get_input_entity(dest), enabled=True, tabs=False))
                ok = True
                log.info("Enabled Topics in %s", dest)
            except Exception as e:
                log.warning("Can't enable Topics in %s (%s) - posting without topics", dest, e)
        if not ok and dest not in self.warned:
            self.warned.add(dest)
        self._forum[dest] = ok
        return ok

    # ------------------------------------------------------------ source topics
    async def list_source(self, src: int) -> List[types.ForumTopic]:
        """All topics of a source forum, oldest first."""
        client = self.app.reader
        peer = await client.get_input_entity(src)
        out, offset_date, offset_id, offset_topic = [], None, 0, 0
        while True:
            res = await client(functions.messages.GetForumTopicsRequest(
                peer=peer, offset_date=offset_date, offset_id=offset_id,
                offset_topic=offset_topic, limit=100))
            topics = [t for t in res.topics if isinstance(t, types.ForumTopic)]
            out += topics
            if len(res.topics) < 100 or not topics:
                break
            last = topics[-1]
            offset_date, offset_id, offset_topic = last.date, last.top_message, last.id
            if len(out) >= res.count:
                break
        for t in out:
            self._info[(src, t.id)] = t
        return sorted(out, key=lambda t: t.id)

    async def source_info(self, src: int, topic: int) -> Optional[types.ForumTopic]:
        if (src, topic) not in self._info:
            try:
                client = self.app.reader
                res = await client(functions.messages.GetForumTopicsByIDRequest(
                    peer=await client.get_input_entity(src), topics=[topic]))
                found = [t for t in res.topics if isinstance(t, types.ForumTopic)]
                self._info[(src, topic)] = found[0] if found else None
            except Exception as e:
                log.warning("Topic %s of %s not readable: %s", topic, src, e)
                return None
        return self._info[(src, topic)]

    # ------------------------------------------------------------ mapping
    async def dest_topic(self, src: int, src_topic: Optional[int], dest: int) -> Optional[int]:
        """Destination topic for a source topic (created if missing). None = General/no topics."""
        if not self.on or not src_topic or src_topic == GENERAL:
            return None
        known = self.app.store.get_topic(src, src_topic, dest)
        if known:
            return known
        if not await self.dest_ready(dest):
            return None
        lock = self._locks.setdefault((src, src_topic, dest), asyncio.Lock())
        async with lock:  # live + clone may ask at the same time: create only once
            known = self.app.store.get_topic(src, src_topic, dest)
            if known:
                return known
            info = await self.source_info(src, src_topic)
            new_id = await self._create(dest, info, src_topic)
            if new_id:
                self.app.store.set_topic(src, src_topic, dest, new_id)
                log.info("Topic %s -> %s:%s (%s)", src_topic, dest, new_id,
                         getattr(info, "title", "?"))
            return new_id

    async def _create(self, dest: int, info, src_topic: int) -> Optional[int]:
        from tgclone.message import retry
        client = self.app.sender
        peer = await client.get_input_entity(dest)
        title = (getattr(info, "title", None) or f"Topic {src_topic}")[:128]
        kw = {"icon_color": getattr(info, "icon_color", None)}
        emoji = getattr(info, "icon_emoji_id", None)
        for attempt in ((emoji,) if emoji else ()) + (None,):
            try:
                res = await retry(client, functions.messages.CreateForumTopicRequest(
                    peer=peer, title=title, icon_emoji_id=attempt,
                    random_id=random.randrange(-2 ** 63, 2 ** 63), **kw))
                new_id = _new_topic_id(res)
                if new_id and getattr(info, "closed", False):
                    await self._safe_edit(dest, new_id, closed=True)
                return new_id
            except RPCError as e:
                if attempt is None:  # premium emoji refused -> retried without; still failing
                    log.error("Can't create topic '%s' in %s: %s", title, dest, e)
                    return None
        return None

    async def _safe_edit(self, dest: int, topic: int, **kw) -> None:
        try:
            client = self.app.sender
            await client(functions.messages.EditForumTopicRequest(
                peer=await client.get_input_entity(dest), topic_id=topic, **kw))
        except Exception as e:
            log.warning("Edit topic %s in %s failed: %s", topic, dest, e)

    async def prepare(self, src: int, dests: List[int]) -> int:
        """Before a history clone: create every source topic in order. Returns count."""
        if not self.on or not await self.is_forum(self.app.reader, src):
            return 0
        made = 0
        for t in await self.list_source(src):
            if t.id == GENERAL:
                continue
            for d in dests:
                if await self.dest_topic(src, t.id, d):
                    made += 1
        return made

    async def sync_edit(self, src: int, src_topic: int, action) -> None:
        """Live: topic renamed / closed / reopened in the source."""
        if not self.on or not self.app.cfg.topics.sync_edits:
            return
        kw = {}
        if action.title:
            kw["title"] = action.title[:128]
        if action.closed is not None:
            kw["closed"] = action.closed
        if not kw:
            return
        self._info.pop((src, src_topic), None)
        for dest, topic in self.app.store.topics_of(src, src_topic).items():
            await self._safe_edit(dest, topic, **kw)
