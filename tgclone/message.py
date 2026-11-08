"""Message wrapper + the single send path used by live and past mode."""

import asyncio
import logging
import mimetypes
import os
import random
import re
import uuid
from typing import List, Optional

from telethon import TelegramClient
from telethon.errors import (ChatForwardsRestrictedError, DocumentInvalidError, FloodWaitError,
                             MediaCaptionTooLongError, ServerError)
from telethon.tl import functions
from telethon.extensions import html as tl_html

from tgclone import richtext
from telethon.tl import types
from telethon.tl.custom.message import Message

from tgclone import fast, jobs, thumbs
from tgclone.config import DOWNLOAD_DIR, Config

log = logging.getLogger("tgclone")
richtext.install()  # spoilers in Telethon's HTML (every other style is built in)

# order matters: sticker/gif/voice/video_note are documents too
_TYPES = ("sticker", "voice", "video_note", "gif", "audio", "video", "photo", "contact", "document")
FILE_TYPES = {"photo", "video", "gif", "audio", "voice", "video_note", "sticker", "document"}


def file_type(msg: Message) -> str:
    if msg.poll:
        return "poll"
    for t in _TYPES:
        if getattr(msg, t, None):
            return t
    if msg.geo or msg.dice or msg.game or msg.venue:
        return "other"
    return "text"  # includes web page previews


class FileTooBig(Exception):
    pass


def media_key(msg: Message) -> Optional[int]:
    """Stable id of the attached photo/document (detects media replacement on edit)."""
    obj = msg.photo or msg.document
    return getattr(obj, "id", None)


def _dir_size(path) -> int:
    try:
        return sum(f.stat().st_size for f in path.iterdir() if f.is_file())
    except FileNotFoundError:
        return 0


async def _wait_disk(need: int, max_disk_mb: int, timeout: int = 1800) -> None:
    """Block until temp dir has room (other jobs free space as they finish)."""
    if not max_disk_mb:
        return
    cap = max_disk_mb * 1024 * 1024
    waited = 0
    # a single file bigger than the cap is fine once nothing else is downloading
    while _dir_size(DOWNLOAD_DIR) + need > cap and not (need > cap and _dir_size(DOWNLOAD_DIR) == 0):
        if waited >= timeout:
            raise FileTooBig("download folder full (max_disk_mb), gave up waiting")
        if waited == 0:
            log.warning("Download folder full, waiting for space")
        await asyncio.sleep(5)
        waited += 5


class CloneMessage:
    """What plugins modify; the original Message is never mutated."""

    def __init__(self, message: Message) -> None:
        self.message = message
        # HTML keeps every Telegram style: quote / expandable quote, bold, italic, underline,
        # strike, spoiler, code, pre, links, mentions, premium emoji (markdown can't)
        try:
            self._orig = tl_html.unparse(message.message or "", message.entities or [])
        except Exception:
            self._orig = message.text or ""
        self.text: str = self._orig
        self.raw_text: str = message.raw_text or ""
        self.sender_id = message.sender_id
        self.file_type = file_type(message)
        self.new_file: Optional[str] = None  # replaces original media (watermark)
        self._downloaded: Optional[str] = None
        self._lock: Optional[asyncio.Lock] = None
        self._temp: List[str] = []
        self._handles: dict = {}  # client -> file uploaded ahead of time (prefetch)

    async def preupload(self, client, progress=None) -> None:
        """Upload the downloaded document now, while the previous post is still being sent.
        One pre-upload per account at a time; the send step then only attaches it."""
        doc = self.message.document
        path = self._downloaded
        if doc is None or self.new_file or not path or id(client) in self._handles \
                or not os.path.exists(path) or os.path.getsize(path) <= fast.SMALL_UPLOAD:
            return
        lock = client.__dict__.setdefault("_tgc_preupload", asyncio.Semaphore(1))
        up = (lambda c, t: progress("up", c, t)) if progress else None
        async with lock:
            handle = await fast.upload(client, path, up)
        if handle is not None:
            self._handles[id(client)] = handle

    def take_handle(self, client):
        """The pre-uploaded file, once (a second destination uploads its own copy)."""
        return self._handles.pop(id(client), None)

    @property
    def text_changed(self) -> bool:
        return self.text != self._orig

    async def download(self, max_mb: int = 0, max_disk_mb: int = 0, progress=None) -> str:
        """Download media once (cached, safe when several destinations ask at once)."""
        if self._downloaded:
            return self._downloaded
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._downloaded:
                return self._downloaded
            return await self._download(max_mb, max_disk_mb, progress)

    async def _download(self, max_mb: int, max_disk_mb: int, progress) -> str:
        if self.file_type not in FILE_TYPES:
            raise FileNotFoundError("message has no downloadable file")
        size = getattr(self.message.file, "size", 0) or 0
        if max_mb and size > max_mb * 1024 * 1024:
            raise FileTooBig(f"{size / 1048576:.0f} MB > max_file_mb={max_mb}")
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        await _wait_disk(size, max_disk_mb)
        cb = (lambda c, t: progress("down", c, t)) if progress else None
        # unique name: parallel jobs / users never overwrite each other's files
        name = uuid.uuid4().hex
        path = None
        client = getattr(self.message, "client", None)
        if client is not None and self.message.document is not None:
            ext = getattr(self.message.file, "ext", "") or ""
            path = await fast.download(client, self.message, str(DOWNLOAD_DIR / (name + ext)), cb)
        if not path:
            path = await self.message.download_media(file=str(DOWNLOAD_DIR / name),
                                                     progress_callback=cb)
        if not path:
            raise FileNotFoundError("download failed")
        self._downloaded = path
        self.track(path)
        return path

    async def download_thumb(self) -> Optional[str]:
        """Original thumbnail, so re-uploaded videos/files keep their preview."""
        doc = self.message.document
        if doc is None or not getattr(doc, "thumbs", None) or self.file_type == "sticker":
            return None
        try:
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            path = await self.message.download_media(
                file=str(DOWNLOAD_DIR / f"thumb_{uuid.uuid4().hex}.jpg"), thumb=-1)
        except Exception as e:  # thumbnail is cosmetic, never fail the send
            log.debug("Thumb download failed: %s", e)
            return None
        if path:
            self.track(path)
        return path

    def track(self, path: str) -> None:
        self._temp.append(path)

    def cleanup(self) -> None:
        for p in self._temp:
            try:
                os.remove(p)
            except OSError:
                pass
        self._temp.clear()
        self._downloaded = None


async def retry(fn, *args, tries: int = 4, **kwargs):
    """Retry on FloodWait / transient errors. Other errors are raised."""
    for attempt in range(1, tries + 1):
        try:
            return await fn(*args, **kwargs)
        except FloodWaitError as e:
            log.warning("FloodWait %ss (attempt %s)", e.seconds, attempt)
            jobs.stage(f"⏳ Telegram asked to wait {e.seconds}s — continuing after that")
            await asyncio.sleep(e.seconds + 1)
        except (ServerError, ConnectionError, asyncio.TimeoutError) as e:
            if attempt == tries:
                raise
            log.warning("Transient error %r, retrying", e)
            jobs.note(f"Telegram hiccup ({type(e).__name__}) — retried")
            await asyncio.sleep(5 * attempt)
            await _reconnect(getattr(fn, "__self__", None))
    raise RuntimeError("retries exhausted")


async def _reconnect(client) -> None:
    """The account's connection dropped ('Cannot send requests while disconnected'):
    connect again now instead of waiting for the next health check."""
    if not isinstance(client, TelegramClient):
        return
    try:
        if not client.is_connected():
            await asyncio.wait_for(client.connect(), 30)
            log.info("Reconnected to Telegram")
    except Exception as e:
        log.warning("Reconnect failed: %s", e)


def _as_list(sent) -> list:
    return sent if isinstance(sent, list) else [sent]


def _file_kwargs(tm: CloneMessage) -> dict:
    """Keep original attributes (filename, duration, voice/round flags) on re-upload."""
    doc = tm.message.document
    kw = {"force_document": tm.file_type in ("document", "sticker")}
    if doc is not None:
        kw["attributes"] = doc.attributes
    if tm.file_type == "video":
        kw["supports_streaming"] = True
    return kw


async def _uploaded_doc(client: TelegramClient, tm: CloneMessage, path: str,
                        thumb: Optional[str], up) -> types.InputMediaUploadedDocument:
    """Pre-uploaded document with our thumbnail (send_file can't set thumbs in albums)."""
    doc = tm.message.document
    handle = (tm.take_handle(client) if path == tm._downloaded else None) or \
        await fast.upload(client, path, up) or await retry(client.upload_file, path, progress_callback=up)
    thumb_h = await retry(client.upload_file, thumb) if thumb else None
    mime = getattr(doc, "mime_type", None) or mimetypes.guess_type(path)[0] \
        or "application/octet-stream"
    attrs = list(doc.attributes) if doc is not None else \
        [types.DocumentAttributeFilename(os.path.basename(path))]
    return types.InputMediaUploadedDocument(
        file=handle, mime_type=mime, attributes=attrs, thumb=thumb_h,
        force_file=True if tm.file_type == "document" else None)


_MD_EMOJI = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.S)  # premium emoji in HTML
async def _is_premium(client) -> bool:
    """Cached on the client itself (account has Telegram Premium / is a bot)."""
    cached = getattr(client, "_tgc_premium", None)
    if cached is None:
        try:
            me = await client.get_me()
            cached = bool(getattr(me, "premium", False)) or bool(getattr(me, "bot", False))
        except Exception:
            cached = True  # unknown: try as is, the retry strips if refused
        client._tgc_premium = cached
    return cached


def _no_custom_emoji(ents):
    """Non-premium accounts can't send custom (premium) emoji: keep the plain emoji."""
    if not ents:
        return ents
    return [e for e in ents if not isinstance(e, types.MessageEntityCustomEmoji)] or None


def poll_media(msg: Message) -> types.InputMediaPoll:
    """Copy of a poll / quiz (Telethon can't send MessageMediaPoll as a file)."""
    media = msg.media
    p, res = media.poll, media.results
    answers = list(p.answers)
    correct = None
    quiz = p.quiz
    if quiz:
        options = [a.option for a in answers]
        correct = [options.index(r.option) for r in (res.results or []) if r.correct and r.option in options]
        if not correct:
            quiz = None  # unanswered quiz: the right answer is hidden -> copy as a normal poll
            correct = None
    poll = types.Poll(id=random.randrange(1, 2 ** 62), question=p.question, answers=answers, hash=0,
                      public_voters=p.public_voters, multiple_choice=p.multiple_choice, quiz=quiz,
                      shuffle_answers=getattr(p, "shuffle_answers", None))
    return types.InputMediaPoll(poll=poll, correct_answers=correct,
                                solution=res.solution if quiz else None,
                                solution_entities=res.solution_entities if quiz else None)


async def _send_media_raw(client, dest: int, media, text: str, ents, reply_to: Optional[int]) -> list:
    """messages.sendMedia for media Telethon's send_file refuses (polls)."""
    res = await retry(client, functions.messages.SendMediaRequest(
        peer=await client.get_input_entity(dest), media=media, message=text or "",
        entities=ents or None, random_id=random.randrange(-2 ** 63, 2 ** 63),
        reply_to=types.InputReplyToMessage(reply_to) if reply_to else None))
    sent = [u.message for u in getattr(res, "updates", []) or []
            if isinstance(getattr(u, "message", None), types.Message)]
    return sent[:1]


async def _send_poll(client, dest: int, msg: Message, text: str, ents, reply_to) -> list:
    media = poll_media(msg)
    try:
        return await _send_media_raw(client, dest, media, text, ents, reply_to)
    except Exception as e:
        if "PUBLIC" not in str(e).upper() or not media.poll.public_voters:
            raise
        media.poll.public_voters = None  # channels don't allow public votes
        return await _send_media_raw(client, dest, media, text, ents, reply_to)


PROTECTED_CHATS: set = set()  # chats found to block forwarding: always download + upload


async def _caption_after(client, dest, sent: list, text: str, ents, reply_to=None) -> list:
    """Caption longer than the account may attach: post it as a reply right under the media."""
    if text and sent:
        await retry(client.send_message, dest, text, formatting_entities=ents,
                    parse_mode="html", reply_to=sent[0].id, link_preview=False)
        jobs.note("Caption too long for this account — posted as a reply under the media")
    return sent


async def _copy(client: TelegramClient, dest: int, tms: List[CloneMessage],
                reply_to: Optional[int], download: bool, cfg: Config, progress=None,
                plain_emoji: bool = False, split_caption: bool = False) -> list:
    up = (lambda c, t: progress("up", c, t)) if progress else None
    pid = tms[0].message.id
    if download and any(tm.file_type in FILE_TYPES for tm in tms):
        jobs.stage(f"⬇️ Downloading post {pid}")
    if len(tms) > 1:  # album
        files = []
        for tm in tms:
            if not download:
                files.append(tm.message.media)
                continue
            path = tm.new_file or await tm.download(cfg.max_file_mb, cfg.max_disk_mb, progress)
            if tm.message.document is not None and tm.file_type in thumbs.THUMB_TYPES:
                files.append(await _uploaded_doc(client, tm, path, await thumbs.pick(cfg, tm), up))
            else:
                files.append(path)
        captions = [_MD_EMOJI.sub(r"\1", tm.text) if plain_emoji else tm.text for tm in tms]
        jobs.stage(f"⬆️ Uploading album at post {pid}" if download else f"📤 Sending album at post {pid}")
        if split_caption:
            sent = _as_list(await retry(client.send_file, dest, files, reply_to=reply_to,
                                        progress_callback=up if download else None))
            return await _caption_after(client, dest, sent, "\n\n".join(c for c in captions if c), None)
        return _as_list(await retry(client.send_file, dest, files,
                                    caption=captions, reply_to=reply_to, parse_mode="html",
                                    progress_callback=up if download else None))

    tm, msg = tms[0], tms[0].message
    # exact formatting when text untouched, markdown when a plugin changed it
    if tm.text_changed:
        text, ents = tm.text, None
        if plain_emoji:
            text = _MD_EMOJI.sub(r"\1", text)
    else:
        text, ents = msg.message or "", msg.entities
        if plain_emoji:
            ents = _no_custom_emoji(ents)

    if tm.new_file or (download and tm.file_type in FILE_TYPES):
        path = tm.new_file or await tm.download(cfg.max_file_mb, cfg.max_disk_mb, progress)
        kw = _file_kwargs(tm)  # keeps duration/size/streaming, also for watermarked files
        thumb = None
        if not tm.new_file:
            thumb = await thumbs.pick(cfg, tm)
        elif thumbs.policy(cfg, tm.file_type) == "custom":
            thumb = str(thumbs.custom_path(cfg))
        if thumb:
            kw["thumb"] = thumb
        jobs.stage(f"⬆️ Uploading post {pid}")
        cap, cap_ents = (("", None) if split_caption else (text, ents))
        if msg.document is not None and os.path.getsize(path) > fast.SMALL_UPLOAD:
            # document > 1 MB: part-parallel upload (or the pre-uploaded one), attributes kept
            media = await _uploaded_doc(client, tm, path, thumb, up)
            sent = _as_list(await retry(client.send_file, dest, media, caption=cap,
                                        formatting_entities=cap_ents, reply_to=reply_to,
                                        parse_mode="html"))
        else:
            sent = _as_list(await retry(client.send_file, dest, path, caption=cap,
                                        formatting_entities=cap_ents, reply_to=reply_to,
                                        progress_callback=up, parse_mode="html", **kw))
        return await _caption_after(client, dest, sent, text, ents) if split_caption else sent

    media = None if tm.file_type == "text" else msg.media
    if media is None and not text:
        return []
    jobs.stage(f"📤 Sending post {pid}")
    if split_caption and media is not None:
        sent = _as_list(await retry(client.send_message, dest, "", file=media, reply_to=reply_to))
        return await _caption_after(client, dest, sent, text, ents)
    if tm.file_type == "poll":
        if ents is None and text:
            text, ents = tl_html.parse(text)
        return await _send_poll(client, dest, msg, text, ents, reply_to)
    return _as_list(await retry(client.send_message, dest, text, file=media,
                                formatting_entities=ents, reply_to=reply_to, parse_mode="html",
                                link_preview=bool(msg.web_preview)))


async def _forward_to_topic(client: TelegramClient, dest: int, msgs: List[Message],
                           topic: int) -> list:
    """forward_messages can't target a topic; use the raw request with top_msg_id."""
    from telethon.tl.functions.messages import ForwardMessagesRequest
    import random
    res = await retry(client, ForwardMessagesRequest(
        from_peer=await client.get_input_entity(msgs[0].chat_id), id=[m.id for m in msgs],
        to_peer=await client.get_input_entity(dest), top_msg_id=topic,
        random_id=[random.randrange(-2 ** 63, 2 ** 63) for _ in msgs]))
    sent = [u.message for u in getattr(res, "updates", [])
            if isinstance(getattr(u, "message", None), types.Message)]
    return sorted(sent, key=lambda m: m.id)


def has_custom_emoji(tms: List[CloneMessage]) -> bool:
    for tm in tms:
        if tm.text_changed:
            if _MD_EMOJI.search(tm.text or ""):
                return True
        elif any(isinstance(e, types.MessageEntityCustomEmoji) for e in tm.message.entities or []):
            return True
    return False


async def send(client: TelegramClient, dest: int, tms: List[CloneMessage], cfg: Config,
               reply_to: Optional[int] = None, same_client: bool = True,
               progress=None, topic: Optional[int] = None, allow_plain: bool = True) -> list:
    """Send one message or one album to dest. Returns sent messages (same order).

    progress(direction, current, total) is called during download ("down") / upload ("up").
    """
    chat = getattr(tms[0].message, "chat_id", None)
    protected = any(tm.message.noforwards for tm in tms) or chat in PROTECTED_CHATS
    rethumb = any(thumbs.needs_reupload(cfg, tm.file_type) for tm in tms)
    if cfg.forward_mode and same_client and not protected and not rethumb:
        try:
            if topic:
                return await _forward_to_topic(client, dest, [tm.message for tm in tms], topic)
            return _as_list(await retry(client.forward_messages, dest,
                                        [tm.message for tm in tms]))
        except ChatForwardsRestrictedError:
            protected = True

    # media references only work for the account that read them and non-protected chats
    # custom / removed thumbnail also needs a fresh upload
    must_download = protected or not same_client or rethumb
    if must_download and not cfg.protected_fallback and protected:
        raise ChatForwardsRestrictedError(None)
    plain = not await _is_premium(client)

    async def attempt(download: bool, plain_emoji: bool):
        try:
            return await _copy(client, dest, tms, reply_to, download, cfg, progress, plain_emoji)
        except MediaCaptionTooLongError:
            return await _copy(client, dest, tms, reply_to, download, cfg, progress, plain_emoji,
                               split_caption=True)
    try:
        try:
            return await attempt(must_download, plain)
        except DocumentInvalidError:
            if (plain and must_download) or not allow_plain:
                raise
            # premium emoji / a document the sender can't reuse: plain emoji + fresh upload
            log.info("Retrying %s with plain emoji / re-upload", tms[0].message.id)
            client._tgc_premium = False
            return await attempt(True, True)
    except ChatForwardsRestrictedError:
        if must_download or not cfg.protected_fallback:
            raise
        if chat is not None and chat not in PROTECTED_CHATS:
            PROTECTED_CHATS.add(chat)  # from now on: download right away, prefetch ahead
            log.info("Chat %s is protected: download + upload from now on", chat)
        return await attempt(True, plain)
