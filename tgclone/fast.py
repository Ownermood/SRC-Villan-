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
import time
from typing import Callable, Optional

from telethon import utils
from telethon.tl import functions, types

log = logging.getLogger("tgclone")

PART = 512 * 1024  # Telegram maximum per request
WORKERS = 8  # requests in flight on one connection
CONNECTIONS = 4  # big files: extra connections (Telegram limits speed per connection)
PER_CONNECTION = 4  # requests in flight on each extra connection
POOL_SIZE = 10 * 1024 * 1024  # use extra connections from this size
IDLE_CLOSE = 60  # seconds an unused shared pool stays open
PAUSE_AFTER_RESET = 600  # Telegram cut our extra connections: one connection for 10 min
MIN_SIZE = 2 * 1024 * 1024  # smaller files: normal transfer is fine


class _QuietLoggers(dict):
    """Telethon's sender logs every dropped extra connection as ERROR; keep them silent."""

    def __missing__(self, key):
        lg = logging.getLogger("tgclone.pool." + key)
        lg.setLevel(logging.CRITICAL + 1)
        self[key] = lg
        return lg


async def open_pool(client, dc_id: int, n: int):
    """n extra connections to a data centre (FastTelethon technique, made stricter).

    Every connection starts with its own initConnection. Other DC: every connection imports
    its OWN exported login (sharing one imported key made Telegram drop the connections).
    Returns (callers, close). Raises if not possible -> the caller uses one connection.
    """
    import copy
    from telethon import network
    from telethon.tl.alltlobjects import LAYER
    dc = await client._get_dc(dc_id)
    home = dc_id == client.session.dc_id
    lock = client.__dict__.setdefault("_tgc_pool_lock", asyncio.Lock())
    quiet = _QuietLoggers()
    senders = []

    async def close():
        for snd in senders:
            try:
                await snd.disconnect()
            except Exception:
                pass

    async def new_connection():
        # no reconnect loop: if Telegram cuts an extra connection, the transfer falls back instead
        snd = network.MTProtoSender(client.session.auth_key if home else None, loggers=quiet,
                                    auto_reconnect=False, retries=2)
        await snd.connect(client._connection(dc.ip_address, dc.port, dc.id, loggers=quiet,
                                             proxy=client._proxy, local_addr=client._local_addr))
        senders.append(snd)
        init = copy.copy(client._init_request)  # never touch Telethon's shared request
        if home:
            init.query = functions.help.GetNearestDcRequest()
        else:
            async with lock:  # one export at a time (parallel downloads used to mix them up)
                auth = await client(functions.auth.ExportAuthorizationRequest(dc_id))
            init.query = functions.auth.ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
        await snd.send(functions.InvokeWithLayerRequest(LAYER, init))

    try:
        for _ in range(n):
            await new_connection()
    except Exception:
        await close()
        raise

    def caller(snd):
        async def call(req):
            from telethon.errors import FloodWaitError
            for attempt in range(5):
                try:
                    return await snd.send(req)
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 1)
            raise RuntimeError("Telegram kept asking to wait")
        return call
    return [caller(snd) for snd in senders], close


class _Shared:
    """One set of extra connections per account and data centre, shared by all transfers."""

    def __init__(self) -> None:
        self.callers = None
        self.close = None
        self.refs = 0
        self.idle: Optional[asyncio.Future] = None
        self.lock = asyncio.Lock()


def _paused(client) -> bool:
    return time.time() < client.__dict__.get("_tgc_pool_off", 0)


async def acquire(client, dc_id: int):
    """(callers, release). release(broken=True) after a failure pauses extra connections."""
    if _paused(client):
        raise RuntimeError("extra connections paused after Telegram reset them")
    pools = client.__dict__.setdefault("_tgc_pools", {})
    p = pools.setdefault(dc_id, _Shared())
    async with p.lock:
        if p.idle is not None:
            p.idle.cancel()
            p.idle = None
        if p.callers is None:
            p.callers, p.close = await open_pool(client, dc_id, CONNECTIONS)
        p.refs += 1
        callers = p.callers

    async def close_later():
        await asyncio.sleep(IDLE_CLOSE)
        async with p.lock:
            if p.refs == 0 and p.close is not None:
                await p.close()
                p.callers = p.close = None

    async def release(broken: bool = False) -> None:
        p.refs -= 1
        if broken:
            fails = client.__dict__.get("_tgc_pool_fails", 0) + 1
            client._tgc_pool_fails = fails
            pause = min(PAUSE_AFTER_RESET * 6 ** (fails - 1), 24 * 3600)  # 10 min, 1 h, 6 h, 24 h
            client._tgc_pool_off = time.time() + pause
            log.warning("Extra connections refused by Telegram (network / IP limit) - "
                        "one connection for the next %s", f"{pause // 3600} h" if pause >= 3600
                        else f"{pause // 60} min")
            async with p.lock:
                if p.close is not None:
                    await p.close()
                p.callers = p.close = None
        elif p.refs == 0:
            p.idle = asyncio.ensure_future(close_later())
    return callers, release


async def _report(progress, done: int, total: int) -> None:
    if progress:
        try:
            r = progress(done, total)
            if asyncio.iscoroutine(r):
                await r
        except Exception:
            pass


async def _run_all(worker, slots) -> None:
    """Run the workers; if one fails, stop the others cleanly before re-raising."""
    tasks = [asyncio.ensure_future(worker(c)) for c in slots]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def download(client, message, path: str,
                   progress: Optional[Callable[[int, int], None]] = None) -> Optional[str]:
    """Parallel download of a document to `path`. None = use the normal download."""
    doc = getattr(message, "document", None)
    size = getattr(doc, "size", 0) or 0
    if doc is None or size < MIN_SIZE or not hasattr(client, "_borrow_exported_sender"):
        return None
    dc_id, location = utils.get_input_location(doc)
    dc_id = dc_id or client.session.dc_id
    release = None
    exported = False
    if size >= POOL_SIZE and CONNECTIONS > 1:
        try:
            callers, release = await acquire(client, dc_id)
            per = PER_CONNECTION
        except Exception as e:
            log.info("Extra connections not used (%s) - one connection", e)
    if release is None:
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
                        if attempt == 4 or release is not None:  # pooled socket gone: fall back now
                            raise
                        await asyncio.sleep(1 + attempt)
                if not isinstance(res, types.upload.File):
                    raise RuntimeError("CDN redirect")  # caller falls back
                os.pwrite(fd, res.bytes, i * PART)
                done += len(res.bytes)
                await _report(progress, min(done, size), size)

        slots = [c for c in callers for _ in range(per)][:parts]  # callers, not coroutines
        await _run_all(worker, slots)
        await _report(progress, size, size)
        return path
    except Exception as e:
        log.info("Fast download not possible (%s) - using normal download", e)
        if release is not None:
            await release(broken=True)
            release = None
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    finally:
        os.close(fd)
        if release is not None:
            await release()
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
    release = None
    callers, per = [client], WORKERS
    if size >= POOL_SIZE and CONNECTIONS > 1 and hasattr(client, "_get_dc"):
        try:
            callers, release = await acquire(client, client.session.dc_id)
            per = PER_CONNECTION
        except Exception as e:
            log.info("Extra upload connections not used (%s) - one connection", e)
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
                        if attempt == 4 or release is not None:  # pooled socket gone: fall back now
                            raise
                        await asyncio.sleep(1 + attempt)
                done += len(data)
                await _report(progress, min(done, size), size)

        slots = [c for c in callers for _ in range(per)][:parts]
        await _run_all(worker, slots)
        return types.InputFileBig(file_id, parts, os.path.basename(path))
    except Exception as e:
        log.info("Fast upload not possible (%s) - using normal upload", e)
        if release is not None:
            await release(broken=True)
            release = None
        return None
    finally:
        os.close(fd)
        if release is not None:
            await release()
