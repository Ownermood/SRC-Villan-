"""Faster big-file transfer: 512 KB parts, several requests in flight at once.

Telethon downloads/uploads one 128 KB part at a time, so speed is limited by the round
trip to Telegram's data centre (slow from far away / WSL). Here N parts travel in
parallel over the same connection. Any problem (CDN redirect, old Telethon, odd media)
returns None and the caller falls back to Telethon's normal transfer.
"""

import asyncio
import logging
import os
import random
from typing import Callable, Optional

from telethon import utils
from telethon.tl import functions, types

log = logging.getLogger("tgclone")

PART = 512 * 1024  # Telegram maximum per request
WORKERS = 8
MIN_SIZE = 2 * 1024 * 1024  # smaller files: normal transfer is fine


async def _report(progress, done: int, total: int) -> None:
    if progress:
        try:
            r = progress(done, total)
            if asyncio.iscoroutine(r):
                await r
        except Exception:
            pass


async def download(client, message, path: str,
                   progress: Optional[Callable[[int, int], None]] = None) -> Optional[str]:
    """Parallel download of a document to `path`. None = use the normal download."""
    doc = getattr(message, "document", None)
    size = getattr(doc, "size", 0) or 0
    if doc is None or size < MIN_SIZE or not hasattr(client, "_borrow_exported_sender"):
        return None
    dc_id, location = utils.get_input_location(doc)
    exported = bool(dc_id) and dc_id != client.session.dc_id
    sender = await client._borrow_exported_sender(dc_id) if exported else client._sender
    parts = (size + PART - 1) // PART
    queue: asyncio.Queue = asyncio.Queue()
    for i in range(parts):
        queue.put_nowait(i)
    done = 0
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.ftruncate(fd, size)

        async def worker():
            nonlocal done
            while True:
                try:
                    i = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                req = functions.upload.GetFileRequest(location, offset=i * PART, limit=PART)
                for attempt in range(5):
                    try:
                        res = await client._call(sender, req)
                        break
                    except (ConnectionError, asyncio.TimeoutError):
                        if attempt == 4:
                            raise
                        await asyncio.sleep(1 + attempt)
                if not isinstance(res, types.upload.File):
                    raise RuntimeError("CDN redirect")  # caller falls back
                os.pwrite(fd, res.bytes, i * PART)
                done += len(res.bytes)
                await _report(progress, min(done, size), size)

        await asyncio.gather(*(worker() for _ in range(min(WORKERS, parts))))
        await _report(progress, size, size)
        return path
    except Exception as e:
        log.info("Fast download not possible (%s) - using normal download", e)
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    finally:
        os.close(fd)
        if exported:
            await client._return_exported_sender(sender)


async def upload(client, path: str, progress: Optional[Callable[[int, int], None]] = None):
    """Parallel upload. Returns an InputFileBig, or None to use the normal upload."""
    size = os.path.getsize(path)
    if size <= 10 * 1024 * 1024:  # Telegram wants small files uploaded with MD5 parts
        return None
    parts = (size + PART - 1) // PART
    file_id = random.getrandbits(63)
    queue: asyncio.Queue = asyncio.Queue()
    for i in range(parts):
        queue.put_nowait(i)
    done = 0
    fd = os.open(path, os.O_RDONLY)
    try:
        async def worker():
            nonlocal done
            while True:
                try:
                    i = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                data = os.pread(fd, PART, i * PART)
                req = functions.upload.SaveBigFilePartRequest(file_id, i, parts, data)
                for attempt in range(5):
                    try:
                        if not await client(req):
                            raise RuntimeError(f"part {i} refused")
                        break
                    except (ConnectionError, asyncio.TimeoutError):
                        if attempt == 4:
                            raise
                        await asyncio.sleep(1 + attempt)
                done += len(data)
                await _report(progress, min(done, size), size)

        await asyncio.gather(*(worker() for _ in range(min(WORKERS, parts))))
        return types.InputFileBig(file_id, parts, os.path.basename(path))
    except Exception as e:
        log.info("Fast upload not possible (%s) - using normal upload", e)
        return None
    finally:
        os.close(fd)
