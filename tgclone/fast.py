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
WORKERS = 8  # requests in flight on one connection
CONNECTIONS = 4  # big files: extra connections (Telegram limits speed per connection)
PER_CONNECTION = 4  # requests in flight on each extra connection
POOL_SIZE = 10 * 1024 * 1024  # use extra connections from this size
MIN_SIZE = 2 * 1024 * 1024  # smaller files: normal transfer is fine


async def open_pool(client, dc_id: int, n: int):
    """n extra connections to a data centre (FastTelethon technique).

    Home DC: new connections with the account's own key. Other DC: the login is exported
    ONCE and imported on the first connection; the rest reuse that authorised key.
    Returns (callers, close). Raises if this Telethon can't do it -> caller falls back.
    """
    import copy
    from telethon import network
    from telethon.tl.alltlobjects import LAYER
    dc = await client._get_dc(dc_id)
    home = dc_id == client.session.dc_id
    keys = client.__dict__.setdefault("_tgc_dc_keys", {})  # dc -> authorised key (exported once)
    lock = client.__dict__.setdefault("_tgc_pool_lock", asyncio.Lock())
    senders = []

    def new_sender(key):
        return network.MTProtoSender(key, loggers=client._log)

    async def connect(snd):
        await snd.connect(client._connection(dc.ip_address, dc.port, dc.id, loggers=client._log,
                                             proxy=client._proxy, local_addr=client._local_addr))

    async def authorised_key():
        """Other DC: export the login once (several downloads may ask at the same time)."""
        async with lock:
            if dc_id in keys:
                return keys[dc_id]
            snd = new_sender(None)
            await connect(snd)
            auth = await client(functions.auth.ExportAuthorizationRequest(dc_id))
            init = copy.copy(client._init_request)  # never touch Telethon's shared request
            init.query = functions.auth.ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
            await snd.send(functions.InvokeWithLayerRequest(LAYER, init))
            keys[dc_id] = snd.auth_key
            senders.append(snd)  # already connected and authorised: use it too
            return keys[dc_id]

    key = client.session.auth_key if home else None

    async def close():
        for snd in senders:
            try:
                await snd.disconnect()
            except Exception:
                pass
    try:
        if key is None:
            key = await authorised_key()
        while len(senders) < n:
            snd = new_sender(key)
            await connect(snd)
            senders.append(snd)
    except Exception:
        await close()
        if not home:
            keys.pop(dc_id, None)  # export again next time
        raise

    def caller(snd):
        async def call(req):
            from telethon.errors import FloodWaitError
            for attempt in range(5):
                try:
                    return await snd.send(req)
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 1)
                except (ConnectionError, asyncio.TimeoutError):
                    if attempt == 4:
                        raise
                    await asyncio.sleep(1 + attempt)
        return call
    return [caller(snd) for snd in senders], close


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
    dc_id = dc_id or client.session.dc_id
    pool_close = None
    exported = False
    if size >= POOL_SIZE and CONNECTIONS > 1:
        try:
            callers, pool_close = await open_pool(client, dc_id, CONNECTIONS)
            per = PER_CONNECTION
        except Exception as e:
            log.info("Extra connections not possible (%s) - one connection", e)
    if pool_close is None:
        exported = dc_id != client.session.dc_id
        sender = await client._borrow_exported_sender(dc_id) if exported else client._sender
        callers, per = [lambda req, s=sender: client._call(s, req)], WORKERS
    parts = (size + PART - 1) // PART
    queue: asyncio.Queue = asyncio.Queue()
    for i in range(parts):
        queue.put_nowait(i)
    done = 0
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.ftruncate(fd, size)

        async def worker(call):
            nonlocal done
            while True:
                try:
                    i = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                req = functions.upload.GetFileRequest(location, offset=i * PART, limit=PART)
                for attempt in range(5):
                    try:
                        res = await call(req)
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

        slots = [c for c in callers for _ in range(per)][:parts]  # callers, not coroutines
        await asyncio.gather(*(worker(c) for c in slots))
        await _report(progress, size, size)
        return path
    except Exception as e:
        log.info("Fast download not possible (%s) - using normal download", e)
        if pool_close is not None:  # a stale export: get a fresh one next time
            client.__dict__.get("_tgc_dc_keys", {}).pop(dc_id, None)
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    finally:
        os.close(fd)
        if pool_close is not None:
            await pool_close()
        elif exported:
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
    pool_close = None
    callers, per = [client], WORKERS
    if size >= POOL_SIZE and CONNECTIONS > 1 and hasattr(client, "_get_dc"):
        try:
            callers, pool_close = await open_pool(client, client.session.dc_id, CONNECTIONS)
            per = PER_CONNECTION
        except Exception as e:
            log.info("Extra upload connections not possible (%s) - one connection", e)
    fd = os.open(path, os.O_RDONLY)
    try:
        async def worker(call):
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
                        if not await call(req):
                            raise RuntimeError(f"part {i} refused")
                        break
                    except (ConnectionError, asyncio.TimeoutError):
                        if attempt == 4:
                            raise
                        await asyncio.sleep(1 + attempt)
                done += len(data)
                await _report(progress, min(done, size), size)

        slots = [c for c in callers for _ in range(per)][:parts]
        await asyncio.gather(*(worker(c) for c in slots))
        return types.InputFileBig(file_id, parts, os.path.basename(path))
    except Exception as e:
        log.info("Fast upload not possible (%s) - using normal upload", e)
        return None
    finally:
        os.close(fd)
        if pool_close is not None:
            await pool_close()
