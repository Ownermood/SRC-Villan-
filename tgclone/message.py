"""Message wrapper + the single send path used by live and past mode."""

import asyncio
import logging
import os
from typing import List, Optional

from telethon import TelegramClient
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError, ServerError
from telethon.tl.custom.message import Message

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

    async def download(self, max_mb: int = 0) -> str:
        """Download media once (cached), returns local path."""
        if self._downloaded:
            return self._downloaded
        if self.file_type not in FILE_TYPES:
            raise FileNotFoundError("message has no downloadable file")
        size = getattr(self.message.file, "size", 0) or 0
        if max_mb and size > max_mb * 1024 * 1024:
            raise FileTooBig(f"{size / 1048576:.0f} MB > max_file_mb={max_mb}")
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        path = await self.message.download_media(file=str(DOWNLOAD_DIR) + os.sep)
        if not path:
            raise FileNotFoundError("download failed")
        self._downloaded = path
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


async def _copy(client: TelegramClient, dest: int, tms: List[CloneMessage],
                reply_to: Optional[int], download: bool, max_mb: int) -> list:
    if len(tms) > 1:  # album
        files = [tm.new_file or (await tm.download(max_mb) if download else tm.message.media)
                 for tm in tms]
        return _as_list(await retry(client.send_file, dest, files,
                                    caption=[tm.text for tm in tms], reply_to=reply_to))

    tm, msg = tms[0], tms[0].message
    # exact formatting when text untouched, markdown when a plugin changed it
    if tm.text_changed:
        text, ents = tm.text, None
    else:
        text, ents = msg.message or "", msg.entities

    if tm.new_file or (download and tm.file_type in FILE_TYPES):
        path = tm.new_file or await tm.download(max_mb)
        kw = {} if tm.new_file else _file_kwargs(tm)
        return _as_list(await retry(client.send_file, dest, path, caption=text,
                                    formatting_entities=ents, reply_to=reply_to, **kw))

    media = None if tm.file_type == "text" else msg.media
    if media is None and not text:
        return []
    return _as_list(await retry(client.send_message, dest, text, file=media,
                                formatting_entities=ents, reply_to=reply_to,
                                link_preview=bool(msg.web_preview)))


async def send(client: TelegramClient, dest: int, tms: List[CloneMessage], cfg: Config,
               reply_to: Optional[int] = None, same_client: bool = True) -> list:
    """Send one message or one album to dest. Returns sent messages (same order)."""
    protected = any(tm.message.noforwards for tm in tms)
    if cfg.forward_mode and same_client and not protected:
        try:
            return _as_list(await retry(client.forward_messages, dest,
                                        [tm.message for tm in tms]))
        except ChatForwardsRestrictedError:
            protected = True

    # media references only work for the account that read them and non-protected chats
    must_download = protected or not same_client
    if must_download and not cfg.protected_fallback and protected:
        raise ChatForwardsRestrictedError(None)
    try:
        return await _copy(client, dest, tms, reply_to, must_download, cfg.max_file_mb)
    except ChatForwardsRestrictedError:
        if must_download or not cfg.protected_fallback:
            raise
        log.info("Chat is protected, falling back to download + upload")
        return await _copy(client, dest, tms, reply_to, True, cfg.max_file_mb)
