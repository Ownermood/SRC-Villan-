"""Shared runtime: clients, config, store, plugins, delivery."""

import asyncio
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl import types
from telethon.tl.custom.message import Message

from tgclone.config import (CONFIG_FILE, DATA_DIR, Config, Forward, Secrets, load_config, norm_ref,
                            save_config)
from tgclone import jobs as jobs_mod
from tgclone.jobs import Job, JobManager
from tgclone.message import CloneMessage, _is_premium, has_custom_emoji, media_key, send
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
        self.preload_peers()  # chats we used before: usable at once, no dialog download
        self._dialogs = asyncio.ensure_future(self._warm_dialogs())  # titles etc., in background
        await self.load_forwards()

    async def _warm_dialogs(self) -> None:
        try:
            await self.user.get_dialogs()
        except Exception as e:
            log.warning("get_dialogs failed for %s: %s", self.uid, e)

    def preload_peers(self) -> None:
        session = getattr(self.reader, "session", None)
        rows = getattr(session, "_entities", None)
        if rows is None:
            return
        for pid, h in self.store.peers():
            rows.add((pid, h, None, None, None))

    async def remember(self, *peer_ids) -> None:
        """Save chats' access hashes so they work right after a restart."""
        for pid in peer_ids:
            try:
                ip = await self.reader.get_input_entity(pid)
            except Exception:
                continue
            h = getattr(ip, "access_hash", None)
            if h is not None:
                self.store.save_peer(int(pid), h)

    async def ensure_peers(self, *peer_ids) -> None:
        """A chat not cached yet: wait for the background dialog list once, then go on."""
        for pid in peer_ids:
            try:
                await self.reader.get_input_entity(pid)
            except Exception:
                task = getattr(self, "_dialogs", None)
                if task is not None:
                    await asyncio.wait([task], timeout=120)
                break
        await self.remember(*peer_ids)

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
            asyncio.ensure_future(self.remember(src, *dests))
            mapping.setdefault(src, [])
            mapping[src] += [d for d in dests if d not in mapping[src] and d != src]
        self.from_to = mapping
        log.info("Active forwards: %s", mapping)

    def save(self) -> None:
        save_config(self.cfg, self.cfg_path)

    async def _send_premium_via_bot(self, dest, tms, reply_to, progress, topic):
        """Account without Premium can't post premium emoji; the bot may (Telegram decides).

        Returns the sent messages, or None to send normally (plain emoji from the account).
        """
        bot = self.bot
        if (not self.cfg.premium_emoji_via_bot or bot is None or self.sender is bot
                or getattr(bot, "_tgc_no_emoji", False) or not has_custom_emoji(tms)
                or dest in getattr(bot, "_tgc_bad_dests", ()) or await _is_premium(self.sender)):
            return None
        try:
            await bot.get_input_entity(dest)  # bot not in that chat: find out before any download
        except Exception:
            bot._tgc_bad_dests = getattr(bot, "_tgc_bad_dests", set()) | {dest}
            log.info("Bot is not in %s - premium-emoji posts there go from the account", dest)
            return None
        try:
            sent = await send(bot, dest, tms, self.cfg, reply_to, same_client=False,
                              progress=progress, topic=topic, allow_plain=False)
            log.info("Premium emoji post %s sent via the bot", tms[0].message.id)
            return sent
        except Exception as e:
            if "DOCUMENT_INVALID" in str(e).upper() or "document file was invalid" in str(e):
                bot._tgc_no_emoji = True  # Telegram doesn't let this bot use them: stop trying
                log.info("Bot can't send premium emoji (Telegram rule) - using plain emoji")
            else:
                bot._tgc_bad_dests = getattr(bot, "_tgc_bad_dests", set()) | {dest}
                log.info("Bot couldn't post in %s (%s) - sending from the account", dest, e)
            return None

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

        async def one(d) -> int:
            if skip_done and self.store.get_one(src, first.id, d) is not None:
                return 0  # already cloned (resume after crash)
            topic = None
            if src_topic:
                try:
                    topic = await self.topics.dest_topic(src, src_topic, d)
                except Exception as e:
                    log.warning("Topic for %s:%s -> %s failed: %s", src, src_topic, d, e)
            reply_to = self.store.get_one(src, replied, d) if replied else None
            sent = None
            for attempt in range(4):  # Telegram timeouts / server hiccups: wait and try again
                try:
                    t0 = time.monotonic()
                    sent = await self._send_premium_via_bot(d, tms, reply_to or topic, progress, topic)
                    if sent is None:  # a reply Telegram never sends would wait forever: time limit
                        sent = await asyncio.wait_for(send(self.sender, d, tms, self.cfg, reply_to or topic,
                                                           same_client=self.sender is self.reader,
                                                           progress=progress, topic=topic),
                                                      _post_timeout(tms))
                    took = time.monotonic() - t0
                    if took > 120:
                        log.info("Slow post %s -> %s: %.0fs (%s)", first.id, d, took,
                                 ", ".join(tm.file_type for tm in tms))
                    break
                except Exception as e:
                    if attempt < 3 and _transient(e):
                        wait = (10, 30, 60)[attempt]
                        log.warning("Post %s -> %s: %s - retrying in %ss", first.id, d, e, wait)
                        jobs_mod.stage(f"🔁 Telegram timeout on post {first.id} — retrying in {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    log.error("Send %s:%s -> %s failed: %s", src, first.id, d, e)
                    job = jobs_mod.current_job.get()
                    if job is not None and first.id not in job.failed_ids:
                        job.failed_ids.append(first.id)
                        job.note = f"Post {first.id} not sent: {str(e)[:90]}"
                    return 0
            for tm, s in zip(tms, sent):
                self.store.add(src, tm.message.id, d, s.id, media_key(tm.message))
            return 1

        try:  # every destination at the same time (a file is downloaded only once)
            done = sum(await asyncio.gather(*(one(d) for d in dests)))
        finally:
            for tm in tms:
                tm.cleanup()
        return done


def _post_timeout(tms) -> float:
    """Longest a post may take before we call it stuck: 15 min + the file at 200 KB/s."""
    size = sum(getattr(getattr(tm.message, "file", None), "size", 0) or 0 for tm in tms)
    return 900 + size / 200_000


def _transient(e: Exception) -> bool:
    """Errors worth another try later (Telegram side), not mistakes in the post."""
    from telethon.errors import RpcCallFailError, ServerError, TimedOutError
    if isinstance(e, (ServerError, TimedOutError, RpcCallFailError, ConnectionError,
                      asyncio.TimeoutError, TimeoutError)):
        return True
    s = str(e)
    return ("Request was unsuccessful" in s or "Timeout" in s or "internal issues" in s
            or "HTTP code 429" in s or "invalid buffer" in s.lower() or "Invalid response buffer" in s)


def _turbo_ok(app: App, msgs: List[Message]) -> bool:
    """Can these posts be copied server-side in one batch (no download, no re-send)?"""
    from tgclone import thumbs
    from tgclone.message import PROTECTED_CHATS, file_type
    cfg = app.cfg
    if not cfg.past.turbo or app.sender is not app.reader or app.plugins:
        return False
    if msgs and getattr(msgs[0], "chat_id", None) in PROTECTED_CHATS:
        return False
    for m in msgs:
        if m.noforwards or reply_target(m) or thumbs.needs_reupload(cfg, file_type(m)):
            return False
    return True


async def _turbo_send(app: App, src: int, dests: List[int], msgs: List[Message]) -> int:
    """Copy up to 100 posts in ONE request per destination (drop_author = no forward tag).

    Returns how many posts were copied (0 = not possible, caller sends them one by one).
    """
    import random as _rnd
    from telethon.tl import functions
    from tgclone.message import retry
    client = app.sender
    src_peer = await client.get_input_entity(src)
    src_topic = topic_of(msgs[0])
    for d in dests:
        todo = [m for m in msgs if app.store.get_one(src, m.id, d) is None]
        if not todo:
            continue
        topic = await app.topics.dest_topic(src, src_topic, d) if src_topic else None
        rids = [_rnd.randrange(-2 ** 63, 2 ** 63) for _ in todo]
        res = await retry(client, functions.messages.ForwardMessagesRequest(
            from_peer=src_peer, id=[m.id for m in todo], to_peer=await client.get_input_entity(d),
            random_id=rids, drop_author=not app.cfg.forward_mode, top_msg_id=topic))
        new_ids = {u.random_id: u.id for u in getattr(res, "updates", []) or []
                   if isinstance(u, types.UpdateMessageID)}
        if not new_ids:
            raise RuntimeError("Telegram returned no copied messages")
        for m, rid in zip(todo, rids):
            if rid in new_ids:
                app.store.add(src, m.id, d, new_ids[rid], media_key(m))
    return len(msgs)


async def run_forward(app: App, fwd: Forward, fresh: bool = False,
                      job: Optional[Job] = None) -> int:
    """Clone history oldest -> newest, resumable, as fast as Telegram allows.

    - turbo: plain posts go 100 per request (server-side copy, nothing downloaded)
    - protected chats: the next files are downloaded while the current one uploads
    - order is always kept; FloodWait is handled by waiting, so no fixed delay is needed
    """
    from collections import deque
    from tgclone.message import FILE_TYPES
    if app.reader is not app.user:
        raise RuntimeError("Copying old posts needs a login (👤 My profile → 🔐 Login). "
                           "Without login only new posts are forwarded.")
    if app.blocked(await app.resolve(fwd.source)):
        raise ValueError("This chat can't be cloned (blocked by the owner).")
    src = await app.resolve(fwd.source)
    dests = [await app.resolve(d) for d in fwd.dest]
    await app.ensure_peers(src, *dests)
    key = app.store.key(src, dests)
    if fresh:
        app.store.clear(src, dests)
        app.store.set_progress(key, 0)
    start = max(fwd.offset, app.store.get_progress(key))
    p, cfg = app.cfg.past, app.cfg
    log.info("Cloning %s -> %s from id > %s", src, dests, start)
    from tgclone.message import PROTECTED_CHATS
    try:  # protected source: download right away and prefetch from the first post
        ent = await app.reader.get_entity(src)
        if getattr(ent, "noforwards", False):
            PROTECTED_CHATS.add(src)
            log.info("Source %s is protected: download + upload with prefetch", src)
    except Exception:
        pass
    jobs_mod.stage("📁 Preparing topics…")
    try:  # forum group: create all topics first, in the source's order
        made = await app.topics.prepare(src, dests)
        if made:
            log.info("Topics ready: %s", made)
    except Exception as e:
        log.warning("Preparing topics failed (messages still go out): %s", e)
    jobs_mod.stage("🔍 Reading posts…")

    count = 0
    progress = job.file_progress if job else None
    turbo: List[Message] = []  # posts waiting for one batch request
    window: deque = deque()  # (msgs, task) prepared ahead: files downloading in background

    def account(msgs, ok: bool) -> None:
        nonlocal count
        app.store.set_progress(key, msgs[-1].id)
        count += len(msgs)
        if job:
            job.last_id = msgs[-1].id
            if ok:
                job.done += len(msgs)
            else:
                job.failed += len(msgs)

    async def pause() -> None:
        if p.delay:
            await asyncio.sleep(p.delay)
        if p.batch_size and p.batch_sleep and count % p.batch_size < 1:
            await asyncio.sleep(p.batch_sleep)

    async def prepare(msgs):
        tms = await app.process(msgs)
        if tms and (src in PROTECTED_CHATS or any(m.noforwards for m in msgs)) and cfg.protected_fallback:
            for tm in tms:  # download now (overlaps with the previous upload)
                if tm.file_type in FILE_TYPES:
                    try:
                        await asyncio.wait_for(tm.download(cfg.max_file_mb, cfg.max_disk_mb, progress),
                                               _post_timeout([tm]))
                    except Exception as e:
                        log.info("prefetch %s: %s - downloads again when sent", tm.message.id, e or type(e).__name__)
            if p.preupload and not cfg.send_via_bot:
                for tm in tms:  # ...and upload ahead too: sending is then only attaching the file
                    try:
                        await asyncio.wait_for(tm.preupload(app.sender, progress), _post_timeout([tm]))
                    except Exception as e:
                        log.debug("pre-upload %s: %s", tm.message.id, e)
        return tms

    async def send_next() -> None:
        msgs, task = window.popleft()
        tms = await task
        ok = await app.deliver(src, tms, dests, skip_done=True, progress=progress)
        failed = bool(tms) and bool(dests) and ok == 0 and any(
            app.store.get_one(src, msgs[0].id, d) is None for d in dests)
        account(msgs, not failed)
        await pause()

    async def drain() -> None:
        while window:
            await send_next()

    async def flush_turbo() -> None:
        if not turbo:
            return
        msgs = list(turbo)
        turbo.clear()
        try:
            await _turbo_send(app, src, dests, msgs)
            account(msgs, True)
            await pause()
        except Exception as e:  # e.g. forwarding restricted: send them one by one instead
            log.info("Batch copy not possible (%s) - sending one by one", e)
            for g in _groups(msgs):
                window.append((g, asyncio.ensure_future(prepare(g))))
                await send_next()

    async def push(msgs) -> None:
        if _turbo_ok(app, msgs):
            await drain()
            if turbo and (len(turbo) + len(msgs) > 100 or topic_of(turbo[0]) != topic_of(msgs[0])):
                await flush_turbo()
            turbo.extend(msgs)
            return
        await flush_turbo()
        window.append((msgs, asyncio.ensure_future(prepare(msgs))))
        if len(window) > max(0, p.prefetch):
            await send_next()

    if job:
        try:  # range for the progress bar (1 cheap request)
            last = await app.reader.get_messages(src, limit=1)
            top = last[0].id if last else 0
            job.start_id, job.end_id = start, min(fwd.end, top) if fwd.end else top
        except Exception:
            pass
    buf: List[Message] = []
    try:
        async for m in app.reader.iter_messages(src, reverse=True, min_id=start,
                                                max_id=fwd.end + 1 if fwd.end else 0):
            if job and job.cancelled:
                break
            if isinstance(m.action, types.MessageActionPinMessage) and m.reply_to_msg_id:
                if buf:
                    await push(buf)
                    buf = []
                await flush_turbo()
                await drain()
                if cfg.live.pin_sync:
                    await app.sync_pin(src, [m.reply_to_msg_id], True)  # pins copied in order
                continue
            if m.action is not None or (not m.message and not m.media):
                continue  # service / empty
            if buf and m.grouped_id != buf[0].grouped_id:
                await push(buf)
                buf = []
            buf.append(m)
            if not m.grouped_id:
                await push(buf)
                buf = []
        if buf and not (job and job.cancelled):
            await push(buf)
        if not (job and job.cancelled):
            await flush_turbo()
            await drain()
    finally:
        for _, task in window:  # stopped: don't leave downloads behind
            task.cancel()
            try:
                for tm in await task:
                    tm.cleanup()
            except BaseException:
                pass
    log.info("Done %s -> %s: %s messages", src, dests, count)
    return count


def _groups(msgs: List[Message]) -> List[List[Message]]:
    """Split posts back into albums / single posts."""
    out: List[List[Message]] = []
    for m in msgs:
        if out and m.grouped_id and out[-1][0].grouped_id == m.grouped_id:
            out[-1].append(m)
        else:
            out.append([m])
    return out
