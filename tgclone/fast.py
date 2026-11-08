"""Faster big-file transfer: 512 KB parts, several requests in flight at once.

Telethon downloads/uploads one 128 KB part at a time, so speed is limited by the round
trip to Telegram's data centre (slow from far away / WSL). Here N parts travel in
parallel over the same connection. Any problem (CDN redirect, old Telethon, odd media)
returns None and the caller falls back to Telethon's normal transfer.
"""

import asyncio
import hashlib
import json
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
MAX_INFLIGHT = 12  # file-part requests at once per account on its main connection (all transfers)
FLOOD_PAUSE = 600  # after Telegram's transport flood (-429): fewer parts at once for 10 min
PART_TIMEOUT = 60  # one 512 KB part; no answer by then = lost reply, ask again
MAX_PAUSE = 7 * 24 * 3600  # after repeated resets: extra connections stay off for a week
MIN_SIZE = 1024 * 1024  # smaller files: one part, nothing to run in parallel
SMALL_UPLOAD = 1024 * 1024  # documents above this are uploaded part-parallel (incl. 1-10 MB)
BIG_FILE = 10 * 1024 * 1024  # Telegram: above this saveBigFilePart, below saveFilePart + MD5
ENABLED = True  # owner switch (Admin -> Limits): False = never open extra connections
STATE_FILE = None  # set by the hub: remembers the pause across restarts (same network/IP)
_state = {"fails": 0, "off": 0.0}


def load_state(path) -> None:
    """Read the remembered pause (the hub calls this at start)."""
    global STATE_FILE
    STATE_FILE = path
    try:
        with open(path) as f:
            d = json.load(f)
        _state.update(fails=int(d.get("fails", 0)), off=float(d.get("off", 0)))
    except (OSError, ValueError, TypeError):
        pass


def _save_state() -> None:
    if STATE_FILE is None:
        return
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(_state, f)
    except OSError:
        pass


def _watch(snd) -> None:
    fut = getattr(snd, "_disconnected", None)
    if fut is not None:
        fut.add_done_callback(_consume)


class _WrongSessionFilter(logging.Filter):
    """After extra connections close, Telegram may still deliver their late replies on the main
    connection; Telethon drops each one with a 'wrong session ID' warning. One line per minute."""

    def __init__(self) -> None:
        super().__init__()
        self.last, self.hidden = 0.0, 0

    def filter(self, record) -> bool:
        if "wrong session ID" not in record.getMessage():
            return True
        now = time.time()
        if now - self.last < 60:
            self.hidden += 1
            return False
        record.msg = ("Telegram sent late replies of closed extra connections (ignored, harmless)"
                      + (f" - {self.hidden} more hidden" if self.hidden else ""))
        record.args = ()
        self.last, self.hidden = now, 0
        return True


logging.getLogger("telethon.network.mtprotosender").addFilter(_WrongSessionFilter())


class _Limiter:
    """Caps file-part requests in flight per account, over ALL its downloads and uploads.
    Telegram answers too many at once with a transport flood (HTTP code 429) and drops the
    connection; then the cap halves for 10 min and grows back by itself."""

    def __init__(self) -> None:
        self.inflight = 0
        self.low_until = 0.0
        self.cond = asyncio.Condition()

    @property
    def limit(self) -> int:
        return max(2, MAX_INFLIGHT // 2) if time.time() < self.low_until else MAX_INFLIGHT

    async def __aenter__(self):
        async with self.cond:
            await self.cond.wait_for(lambda: self.inflight < self.limit)
            self.inflight += 1

    async def __aexit__(self, *exc):
        async with self.cond:
            self.inflight -= 1
            self.cond.notify_all()


def _limiter(client) -> _Limiter:
    lim = client.__dict__.get("_tgc_limiter")
    if lim is None:
        lim = client.__dict__["_tgc_limiter"] = _Limiter()
    return lim


def is_flood(e: BaseException) -> bool:
    """Telegram's transport-level flood: too many connections / requests at once."""
    return getattr(e, "code", None) == 429 or "HTTP code 429" in str(e)


def flooded(client) -> None:
    """Back off after a transport flood: fewer parts at once, no extra connections for now."""
    lim = _limiter(client)
    if time.time() >= lim.low_until:
        log.warning("Telegram: too many requests at once (429) - slowing down for 10 min")
    lim.low_until = time.time() + FLOOD_PAUSE
    if not _paused(client):
        _broken(client)


def _consume(fut) -> None:
    """Telethon puts the disconnect error on a future nobody awaits ('never retrieved')."""
    if not fut.cancelled():
        fut.exception()


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
        if home:  # tell Telegram these sessions are gone, so it stops sending their replies
            for snd in senders:
                try:
                    await asyncio.wait_for(client(functions.DestroySessionRequest(snd._state.id)), 5)
                except Exception:
                    pass
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
        _watch(snd)
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
    now = time.time()
    return now < client.__dict__.get("_tgc_pool_off", 0) or (STATE_FILE is not None and now < _state["off"])


def _broken(client) -> None:
    """Telegram cut / refused the extra connections: pause them (longer each time, remembered)."""
    if not _paused(client):  # parallel transfers on the same broken pool count once
        fails = max(client.__dict__.get("_tgc_pool_fails", 0),
                    _state["fails"] if STATE_FILE is not None else 0) + 1
        pause = min(PAUSE_AFTER_RESET * 6 ** (fails - 1), MAX_PAUSE)  # 10m, 1h, 6h, 36h, 7d
        client._tgc_pool_fails = _state["fails"] = fails
        client._tgc_pool_off = _state["off"] = time.time() + pause
        _save_state()
        log.warning("Telegram closed the extra connections on this network - single "
                    "connection (8 parts in flight) for the next %s",
                    f"{pause // 86400} days" if pause >= 86400 else
                    f"{pause // 3600} h" if pause >= 3600 else f"{pause // 60} min")


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
            _broken(client)
            async with p.lock:
                if p.close is not None:
                    await p.close()
                p.callers = p.close = None
        else:
            if client.__dict__.get("_tgc_pool_fails") or _state["fails"]:
                client._tgc_pool_fails = _state["fails"] = 0
                _save_state()
            if p.refs == 0:
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
                   progress: Optional[Callable[[int, int], None]] = None,
                   pool: bool = True) -> Optional[str]:
    """Parallel download of a document to `path`. None = use the normal download."""
    doc = getattr(message, "document", None)
    size = getattr(doc, "size", 0) or 0
    if doc is None or size < MIN_SIZE or not hasattr(client, "_borrow_exported_sender"):
        return None
    dc_id, location = utils.get_input_location(doc)
    dc_id = dc_id or client.session.dc_id
    release = None
    exported = False
    if pool and ENABLED and size >= POOL_SIZE and CONNECTIONS > 1 and not _paused(client):
        try:
            callers, release = await acquire(client, dc_id)
            per = PER_CONNECTION
        except Exception as e:
            log.info("Extra connections could not open (%s)", e)
            _broken(client)
    if release is None:
        exported = dc_id != client.session.dc_id
        sender = await client._borrow_exported_sender(dc_id) if exported else client._sender
        callers, per = [lambda req, s=sender: client._call(s, req)], WORKERS
    parts = (size + PART - 1) // PART
    lim = _limiter(client)
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
                        if release is None:  # main connection: shared cap for all transfers
                            async with lim:
                                res = await asyncio.wait_for(call(req), PART_TIMEOUT)
                        else:
                            res = await asyncio.wait_for(call(req), PART_TIMEOUT)
                        break
                    except Exception as e:
                        flood = is_flood(e)
                        if flood:
                            flooded(client)
                        if not (flood or isinstance(e, (ConnectionError, asyncio.TimeoutError))) \
                                or attempt == 4 or (release is not None and not flood):
                            raise  # pooled socket gone: fall back now
                        await asyncio.sleep((10 if flood else 1) * (1 + attempt))
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
        retry = release is not None
        if retry:
            await release(broken=True)
            release = None
        else:
            log.info("Fast download not possible (%s) - using normal download", e)
        try:
            os.remove(path)
        except OSError:
            pass
        if not retry:
            return None
    finally:
        os.close(fd)
        if release is not None:
            await release()
        elif exported:
            await client._return_exported_sender(sender)
    # extra connections were cut: same file again on one connection (still 8 parts in flight)
    return await download(client, message, path, progress, pool=False)


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def upload(client, path: str, progress: Optional[Callable[[int, int], None]] = None,
                 pool: bool = True):
    """Parallel upload. Returns InputFileBig (> 10 MB) / InputFile (1-10 MB), or None to use the
    normal upload."""
    size = os.path.getsize(path)
    if size <= SMALL_UPLOAD:
        return None
    big = size > BIG_FILE
    parts = (size + PART - 1) // PART
    file_id = random.getrandbits(63)
    lim = _limiter(client)
    queue: asyncio.Queue = asyncio.Queue()
    for i in range(parts):
        queue.put_nowait(i)
    done = 0
    release = None
    callers, per = [client], WORKERS
    if pool and ENABLED and size >= POOL_SIZE and CONNECTIONS > 1 and hasattr(client, "_get_dc") \
            and not _paused(client):
        try:
            callers, release = await acquire(client, client.session.dc_id)
            per = PER_CONNECTION
        except Exception as e:
            log.info("Extra upload connections could not open (%s)", e)
            _broken(client)
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
                req = (functions.upload.SaveBigFilePartRequest(file_id, i, parts, data) if big
                       else functions.upload.SaveFilePartRequest(file_id, i, data))
                for attempt in range(5):
                    try:
                        if release is None:  # main connection: shared cap for all transfers
                            async with lim:
                                ok = await asyncio.wait_for(call(req), PART_TIMEOUT)
                        else:
                            ok = await asyncio.wait_for(call(req), PART_TIMEOUT)
                        if not ok:
                            raise RuntimeError(f"part {i} refused")
                        break
                    except Exception as e:
                        flood = is_flood(e)
                        if flood:
                            flooded(client)
                        if not (flood or isinstance(e, (ConnectionError, asyncio.TimeoutError))) \
                                or attempt == 4 or (release is not None and not flood):
                            raise  # pooled socket gone: fall back now
                        await asyncio.sleep((10 if flood else 1) * (1 + attempt))
                done += len(data)
                await _report(progress, min(done, size), size)

        slots = [c for c in callers for _ in range(per)][:parts]
        await _run_all(worker, slots)
        if big:
            return types.InputFileBig(file_id, parts, os.path.basename(path))
        return types.InputFile(file_id, parts, os.path.basename(path), _md5(path))
    except Exception as e:
        retry = release is not None
        if retry:
            await release(broken=True)
            release = None
        else:
            log.info("Fast upload not possible (%s) - using normal upload", e)
            return None
    finally:
        os.close(fd)
        if release is not None:
            await release()
    # extra connections were cut: upload again on one connection (new file id, 8 parts in flight)
    return await upload(client, path, progress, pool=False)
