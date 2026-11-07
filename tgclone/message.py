"""Message wrapper + the single send path used by live and past mode."""

import asyncio
import logging
import mimetypes
import os
from typing import List, Optional

from telethon import TelegramClient
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError, ServerError
from telethon.tl import types
from telethon.tl.custom.message import Message

from tgclone import thumbs
from tgclone.config import DOWNLOAD_DIR, Config

log = logging.getLogger("tgclone")

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
    if need > cap:
        raise FileTooBig(f"{need / 1048576:.0f} MB > max_disk_mb={max_disk_mb}")
    waited = 0
    while _dir_size(DOWNLOAD_DIR) + need > cap:
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
        self.text: str = message.text or ""
        self.raw_text: str = message.raw_text or ""
        self.sender_id = message.sender_id
        self.file_type = file_type(message)
        self.new_file: Optional[str] = None  # replaces original media (watermark)
        self._downloaded: Optional[str] = None
        self._temp: List[str] = []

    @property
    def text_changed(self) -> bool:
        return self.text != (self.message.text or "")

    async def download(self, max_mb: int = 0, max_disk_mb: int = 0, progress=None) -> str:
        """Download media once (cached), returns local path."""
        if self._downloaded:
            return self._downloaded
        if self.file_type not in FILE_TYPES:
            raise FileNotFoundError("message has no downloadable file")
        size = getattr(self.message.file, "size", 0) or 0
        if max_mb and size > max_mb * 1024 * 1024:
            raise FileTooBig(f"{size / 1048576:.0f} MB > max_file_mb={max_mb}")
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        await _wait_disk(size, max_disk_mb)
        cb = (lambda c, t: progress("down", c, t)) if progress else None
        path = await self.message.download_media(file=str(DOWNLOAD_DIR) + os.sep,
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
                file=str(DOWNLOAD_DIR / f"thumb_{self.message.id}_{doc.id}.jpg"), thumb=-1)
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
            await asyncio.sleep(e.seconds + 1)
        except (ServerError, ConnectionError, asyncio.TimeoutError) as e:
            if attempt == tries:
                raise
            log.warning("Transient error %r, retrying", e)
            await asyncio.sleep(5 * attempt)
    raise RuntimeError("retries exhausted")


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
    handle = await retry(client.upload_file, path, progress_callback=up)
    thumb_h = await retry(client.upload_file, thumb) if thumb else None
    mime = getattr(doc, "mime_type", None) or mimetypes.guess_type(path)[0] \
        or "application/octet-stream"
    attrs = list(doc.attributes) if doc is not None else \
        [types.DocumentAttributeFilename(os.path.basename(path))]
    return types.InputMediaUploadedDocument(
        file=handle, mime_type=mime, attributes=attrs, thumb=thumb_h,
        force_file=True if tm.file_type == "document" else None)


async def _copy(client: TelegramClient, dest: int, tms: List[CloneMessage],
                reply_to: Optional[int], download: bool, cfg: Config, progress=None) -> list:
    up = (lambda c, t: progress("up", c, t)) if progress else None
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
        return _as_list(await retry(client.send_file, dest, files,
                                    caption=[tm.text for tm in tms], reply_to=reply_to,
                                    progress_callback=up if download else None))

    tm, msg = tms[0], tms[0].message
    # exact formatting when text untouched, markdown when a plugin changed it
    if tm.text_changed:
        text, ents = tm.text, None
    else:
        text, ents = msg.message or "", msg.entities

    if tm.new_file or (download and tm.file_type in FILE_TYPES):
        path = tm.new_file or await tm.download(cfg.max_file_mb, cfg.max_disk_mb, progress)
        kw = {} if tm.new_file else _file_kwargs(tm)
        thumb = None
        if not tm.new_file:
            thumb = await thumbs.pick(cfg, tm)
        elif thumbs.policy(cfg, tm.file_type) == "custom":
            thumb = str(thumbs.CUSTOM_THUMB)
        if thumb:
            kw["thumb"] = thumb
        return _as_list(await retry(client.send_file, dest, path, caption=text,
                                    formatting_entities=ents, reply_to=reply_to,
                                    progress_callback=up, **kw))

    media = None if tm.file_type == "text" else msg.media
    if media is None and not text:
        return []
    return _as_list(await retry(client.send_message, dest, text, file=media,
                                formatting_entities=ents, reply_to=reply_to,
                                link_preview=bool(msg.web_preview)))


async def send(client: TelegramClient, dest: int, tms: List[CloneMessage], cfg: Config,
               reply_to: Optional[int] = None, same_client: bool = True,
               progress=None) -> list:
    """Send one message or one album to dest. Returns sent messages (same order).

    progress(direction, current, total) is called during download ("down") / upload ("up").
    """
    protected = any(tm.message.noforwards for tm in tms)
    rethumb = any(thumbs.needs_reupload(cfg, tm.file_type) for tm in tms)
    if cfg.forward_mode and same_client and not protected and not rethumb:
        try:
            return _as_list(await retry(client.forward_messages, dest,
                                        [tm.message for tm in tms]))
        except ChatForwardsRestrictedError:
            protected = True

    # media references only work for the account that read them and non-protected chats
    # custom / removed thumbnail also needs a fresh upload
    must_download = protected or not same_client or rethumb
    if must_download and not cfg.protected_fallback and protected:
        raise ChatForwardsRestrictedError(None)
    try:
        return await _copy(client, dest, tms, reply_to, must_download, cfg, progress)
    except ChatForwardsRestrictedError:
        if must_download or not cfg.protected_fallback:
            raise
        log.info("Chat is protected, falling back to download + upload")
        return await _copy(client, dest, tms, reply_to, True, cfg, progress)
