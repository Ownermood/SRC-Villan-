"""Shared runtime: clients, config, store, plugins, delivery."""

import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Optional

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl import types
from telethon.tl.custom.message import Message

from tgclone.config import (CONFIG_FILE, DATA_DIR, Config, Forward, Secrets, load_config, norm_ref,
                            save_config)
from tgclone.jobs import Job, JobManager
from tgclone.message import CloneMessage, media_key, send
from tgclone.plugins import apply_plugins, load_plugins
from tgclone.storage import Store
from tgclone.topics import Topics, reply_target, topic_of

log = logging.getLogger("tgclone")


class App:
    """One workspace: a Telegram account with its own forwards, settings, files and jobs.

    App()             -> single-user mode from .env (files in data/, config tgclone.config.json)
    App(home, uid)    -> one user of the multi-user bot (files in data/users/<uid>/)
    """

    def __init__(self, home: Optional[Path] = None, uid: int = 0) -> None:
        self.secrets = Secrets()
        self.secrets.check()
        self.uid = uid
        self.home = Path(home) if home else DATA_DIR
        self.cfg_path = self.home / "config.json" if home else CONFIG_FILE
        self.cfg: Config = load_config(self.cfg_path, self.home)
        self.store = Store(self.home / "tgclone.db")
        self.plugins = load_plugins(self.cfg.plugins, self.home)
        self.topics = Topics(self)  # forum groups: topic-by-topic cloning
        self.caps: Optional[dict] = None  # limits set by the bot owner (multi-user)
        self.paused = False  # plan expired: live forwarding stops until renewed
        self.botmode = False  # no login: the shared bot reads and sends
        self.hub = None  # set by Hub
        self.reader: Optional[TelegramClient] = None  # reads source chats
        self.sender: Optional[TelegramClient] = None  # writes to destinations
        self.control: Optional[TelegramClient] = None  # receives admin commands
        self.user: Optional[TelegramClient] = None
        self.bot: Optional[TelegramClient] = None
        self.from_to: Dict[int, List[int]] = {}
        self.admins: set = set()
        self.jobs = JobManager(self.cfg.max_jobs)
        self.titles: Dict[int, str] = {}  # chat id -> display name (UI)

    # ------------------------------------------------------------ clients
    def _client(self, session) -> TelegramClient:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return TelegramClient(
            session, self.secrets.api_id, self.secrets.api_hash,
            sequential_updates=self.cfg.live.sequential_updates,
            flood_sleep_threshold=60,
        )

    async def start(self, need_user: bool = False) -> None:
        s = self.secrets
        if s.session_string or need_user:
            sess = StringSession(s.session_string) if s.session_string else str(DATA_DIR / "user")
            self.user = self._client(sess)
            await self.user.start()
            await self.user.get_dialogs()  # warm entity cache so numeric ids resolve
        if s.bot_token:
            self.bot = self._client(str(DATA_DIR / "bot"))
            await self.bot.start(bot_token=s.bot_token)
        if not self.user and not self.bot:
            raise SystemExit("Set SESSION_STRING (user) and/or BOT_TOKEN in .env")

        self.reader = self.user or self.bot
        self.sender = self.bot if (self.cfg.send_via_bot and self.bot and self.user) else self.reader
        self.control = self.bot or self.user
        me = await self.reader.get_me()
        log.info("Reader: %s (%s)", me.username or me.first_name, "bot" if me.bot else "user")
        if self.sender is not self.reader:
            log.info("Sending through bot account")

        for a in [*self.cfg.admins, *s.admins]:
            try:
                self.admins.add(await self.control.get_peer_id(norm_ref(a)))
            except Exception as e:
                log.error("Admin %s not resolved: %s", a, e)
        if self.user:  # account owner is always admin
            self.admins.add((await self.user.get_me()).id)
        await self.load_forwards()

    async def attach(self, user: TelegramClient, bot: Optional[TelegramClient]) -> None:
        """Multi-user: use an already logged-in user client, bot is the shared control bot."""
        self.user, self.bot = user, bot
        self.reader = user
        self.sender = bot if (self.cfg.send_via_bot and bot) else user
        self.control = bot or user
        self.admins = {self.uid}
        try:
            await user.get_dialogs()
        except Exception as e:
            log.warning("get_dialogs failed for %s: %s", self.uid, e)
        await self.load_forwards()

    async def attach_bot(self, bot: TelegramClient) -> None:
        """Multi-user without login: the shared bot reads (live only) and sends."""
        self.user, self.bot, self.botmode = None, bot, True
        self.reader = self.sender = self.control = bot
        self.admins = {self.uid}
        await self.load_forwards()

    def blocked(self, chat_id: int) -> bool:
        """Chat on the owner's block list (the owner himself is never blocked)."""
        hub = self.hub
        return bool(hub) and self.uid != hub.owner and chat_id in hub.acc.blocked_ids()

    async def sync_pin(self, src: int, src_ids: List[int], pinned: bool) -> int:
        """Pin / unpin the copies of src messages and delete the "pinned a message" notice."""
        done = 0
        for sid in src_ids:
            for dest, mid in self.store.get(src, sid).items():
                try:
                    if pinned:
                        notice = await self.sender.pin_message(dest, mid, notify=False)
                        if isinstance(notice, Message) or getattr(notice, "id", None):
                            try:  # remove the "X pinned a message" service message right away
                                await self.sender.delete_messages(dest, notice.id)
                            except Exception as e:
                                log.debug("pin notice delete failed: %s", e)
                    else:
                        await self.sender.unpin_message(dest, mid)
                    done += 1
                except Exception as e:
                    log.warning("%s %s:%s failed: %s", "Pin" if pinned else "Unpin", dest, mid, e)
        return done

    @property
    def clients(self) -> List[TelegramClient]:
        return [c for c in (self.user, self.bot) if c]

    # ------------------------------------------------------------ config
    async def resolve(self, ref) -> int:
        return await self.reader.get_peer_id(norm_ref(ref))

    async def title(self, ref) -> str:
        """Human name for a chat ref (cached). Falls back to the ref itself."""
        try:
            cid = await self.resolve(ref)
        except Exception:
            return str(ref)
        if cid not in self.titles:
            try:
                ent = await self.reader.get_entity(cid)
                self.titles[cid] = getattr(ent, "title", None) or " ".join(
                    x for x in (getattr(ent, "first_name", ""), getattr(ent, "last_name", "")) if x
                ) or str(cid)
            except Exception:
                return str(ref)
        return self.titles[cid]

    def reload_plugins(self) -> None:
        self.plugins = load_plugins(self.cfg.plugins, self.home)

    async def load_forwards(self) -> None:
        mapping: Dict[int, List[int]] = {}
        for f in self.cfg.forwards:
            if not f.enabled or f.source in ("", None) or not f.dest:
                continue
            try:
                src = await self.resolve(f.source)
                dests = [await self.resolve(d) for d in f.dest]
            except Exception as e:
                log.error("Forward %s skipped, cannot resolve: %s", f.name or f.source, e)
                continue
            if self.blocked(src):
                log.warning("Forward from %s skipped: blocked by the owner", src)
                continue
            mapping.setdefault(src, [])
            mapping[src] += [d for d in dests if d not in mapping[src] and d != src]
        self.from_to = mapping
        log.info("Active forwards: %s", mapping)

    def save(self) -> None:
        save_config(self.cfg, self.cfg_path)

    # ------------------------------------------------------------ delivery
    async def process(self, messages: List[Message]) -> List[CloneMessage]:
        out = []
        for m in messages:
            tm = await apply_plugins(self.plugins, CloneMessage(m))
            if tm:
                out.append(tm)
        return out

    async def deliver(self, src: int, tms: List[CloneMessage], dests: List[int],
                      skip_done: bool = False, progress=None) -> int:
        """Send message/album to every dest, map ids. Returns number of dests delivered."""
        if not tms:
            return 0
        first = tms[0].message
        src_topic = topic_of(first)
        replied = reply_target(first)
        done = 0
        try:
            for d in dests:
                if skip_done and self.store.get_one(src, first.id, d) is not None:
                    continue  # already cloned (resume after crash)
                topic = None
                if src_topic:
                    try:
                        topic = await self.topics.dest_topic(src, src_topic, d)
                    except Exception as e:
                        log.warning("Topic for %s:%s -> %s failed: %s", src, src_topic, d, e)
                reply_to = self.store.get_one(src, replied, d) if replied else None
                try:
                    sent = await send(self.sender, d, tms, self.cfg, reply_to or topic,
                                      same_client=self.sender is self.reader,
                                      progress=progress, topic=topic)
                except Exception as e:
                    log.error("Send %s:%s -> %s failed: %s", src, first.id, d, e)
                    continue
                for tm, s in zip(tms, sent):
                    self.store.add(src, tm.message.id, d, s.id, media_key(tm.message))
                done += 1
        finally:
            for tm in tms:
                tm.cleanup()
        return done


async def run_forward(app: App, fwd: Forward, fresh: bool = False,
                      job: Optional[Job] = None) -> int:
    """Past mode for one forward: clone history oldest -> newest, resumable."""
    if app.reader is not app.user:
        raise RuntimeError("Copying old posts needs a login (👤 My profile → 🔐 Login). "
                           "Without login only new posts are forwarded.")
    if app.blocked(await app.resolve(fwd.source)):
        raise ValueError("This chat can't be cloned (blocked by the owner).")
    src = await app.resolve(fwd.source)
    dests = [await app.resolve(d) for d in fwd.dest]
    key = app.store.key(src, dests)
    if fresh:
        app.store.clear(src, dests)
        app.store.set_progress(key, 0)
    start = max(fwd.offset, app.store.get_progress(key))
    p = app.cfg.past
    log.info("Cloning %s -> %s from id > %s", src, dests, start)
    try:  # forum group: create all topics first, in the source's order
        made = await app.topics.prepare(src, dests)
        if made:
            log.info("Topics ready: %s", made)
    except Exception as e:
        log.warning("Preparing topics failed (messages still go out): %s", e)

    count = 0
    buf: List[Message] = []

    async def flush() -> None:
        nonlocal count
        msgs = list(buf)
        buf.clear()
        tms = await app.process(msgs)
        ok = await app.deliver(src, tms, dests, skip_done=True,
                               progress=job.file_progress if job else None)
        app.store.set_progress(key, msgs[-1].id)
        count += len(msgs)
        if job:
            job.last_id = msgs[-1].id
            if tms and dests and ok == 0 and any(
                    app.store.get_one(src, msgs[0].id, d) is None for d in dests):
                job.failed += len(msgs)
            else:
                job.done += len(msgs)
        if p.delay:
            await asyncio.sleep(p.delay)
        if p.batch_size and p.batch_sleep and count % p.batch_size < len(msgs):
            await asyncio.sleep(p.batch_sleep)

    if job:
        try:  # range for the progress bar (1 cheap request)
            last = await app.reader.get_messages(src, limit=1)
            top = last[0].id if last else 0
            job.start_id, job.end_id = start, min(fwd.end, top) if fwd.end else top
        except Exception:
            pass
    async for m in app.reader.iter_messages(src, reverse=True, min_id=start,
                                            max_id=fwd.end + 1 if fwd.end else 0):
        if job and job.cancelled:
            break
        if isinstance(m.action, types.MessageActionPinMessage) and m.reply_to_msg_id:
            if buf:
                await flush()
            if app.cfg.live.pin_sync:
                await app.sync_pin(src, [m.reply_to_msg_id], True)  # pins copied in order
            continue
        if m.action is not None or (not m.message and not m.media):
            continue  # service / empty
        if buf and m.grouped_id != buf[0].grouped_id:
            await flush()
        buf.append(m)
        if not m.grouped_id:
            await flush()
    if buf:
        await flush()
    log.info("Done %s -> %s: %s messages", src, dests, count)
    return count
