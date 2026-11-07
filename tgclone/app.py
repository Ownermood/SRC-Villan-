"""Shared runtime: clients, config, store, plugins, delivery."""

import asyncio
import logging
from typing import Dict, List, Optional

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.custom.message import Message

from tgclone.config import DATA_DIR, Config, Forward, Secrets, load_config, norm_ref, save_config
from tgclone.jobs import Job, JobManager
from tgclone.message import CloneMessage, media_key, send
from tgclone.plugins import apply_plugins, load_plugins
from tgclone.storage import Store

log = logging.getLogger("tgclone")


class App:
    def __init__(self) -> None:
        self.secrets = Secrets()
        self.secrets.check()
        self.cfg: Config = load_config()
        self.store = Store(DATA_DIR / "tgclone.db")
        self.plugins = load_plugins(self.cfg.plugins)
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
        self.plugins = load_plugins(self.cfg.plugins)

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
            mapping.setdefault(src, [])
            mapping[src] += [d for d in dests if d not in mapping[src] and d != src]
        self.from_to = mapping
        log.info("Active forwards: %s", mapping)

    def save(self) -> None:
        save_config(self.cfg)

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
        done = 0
        try:
            for d in dests:
                if skip_done and self.store.get_one(src, first.id, d) is not None:
                    continue  # already cloned (resume after crash)
                reply_to = None
                if first.reply_to_msg_id:
                    reply_to = self.store.get_one(src, first.reply_to_msg_id, d)
                try:
                    sent = await send(self.sender, d, tms, self.cfg, reply_to,
                                      same_client=self.sender is self.reader,
                                      progress=progress)
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
        raise RuntimeError("Cloning history needs a user account (SESSION_STRING); bots cannot read history.")
    src = await app.resolve(fwd.source)
    dests = [await app.resolve(d) for d in fwd.dest]
    key = app.store.key(src, dests)
    if fresh:
        app.store.clear(src, dests)
        app.store.set_progress(key, 0)
    start = max(fwd.offset, app.store.get_progress(key))
    p = app.cfg.past
    log.info("Cloning %s -> %s from id > %s", src, dests, start)

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
