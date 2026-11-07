"""Save restricted content from t.me links (single post or id range)."""

import asyncio
import logging
import re
from typing import List, Optional, Tuple

from telethon.tl.custom.message import Message

from tgclone.app import App
from tgclone.message import CloneMessage, send

log = logging.getLogger("tgclone")

# t.me/user/5  t.me/c/123/5  t.me/c/123/<topic>/5  t.me/user/5-20
LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.(?:me|dog))/(?:(c)/)?([A-Za-z0-9_]+)/(?:\d+/)?(\d+)(?:-(\d+))?"
)


def parse_link(text: str) -> Optional[Tuple[object, int, int]]:
    """Returns (chat, first_id, last_id) or None."""
    m = LINK_RE.search(text or "")
    if not m:
        return None
    private, chat, first, last = m.groups()
    chat = int(f"-100{chat}") if private else chat
    first = int(first)
    last = int(last) if last else first
    if last < first:
        first, last = last, first
    return chat, first, last


async def save_range(app: App, chat, first: int, last: int, target: int,
                     progress=None, is_cancelled=lambda: False) -> Tuple[int, int]:
    """Copy messages first..last of chat to target. Returns (sent, failed)."""
    if not app.user:
        raise RuntimeError("Saving needs a user account (SESSION_STRING).")
    s = app.cfg.save
    if last - first + 1 > s.max_batch:
        raise ValueError(f"Range too big ({last - first + 1} > max_batch={s.max_batch})")
    sender = app.control  # deliver to the admin through the bot if present
    sent = failed = 0
    buf: List[Message] = []

    async def flush() -> None:
        nonlocal sent, failed
        msgs = list(buf)
        buf.clear()
        tms = await app.process(msgs) if s.use_plugins else [CloneMessage(m) for m in msgs]
        if not tms:
            return
        try:
            await send(sender, target, tms, app.cfg, same_client=sender is app.user)
            sent += len(tms)
        except Exception as e:
            failed += len(tms)
            log.error("Save %s:%s failed: %s", chat, msgs[0].id, e)
        finally:
            for tm in tms:
                tm.cleanup()
        if progress:
            await progress(sent, failed, msgs[-1].id)
        if s.delay:
            await asyncio.sleep(s.delay)

    for start in range(first, last + 1, 100):
        ids = list(range(start, min(start + 100, last + 1)))
        msgs = await app.user.get_messages(chat, ids=ids)
        for m in msgs:
            if is_cancelled():
                return sent, failed
            if m is None or m.action is not None or (not m.message and not m.media):
                continue
            if buf and m.grouped_id != buf[0].grouped_id:
                await flush()
            buf.append(m)
            if not m.grouped_id:
                await flush()
    if buf:
        await flush()
    return sent, failed
