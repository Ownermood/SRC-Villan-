import json
"""Parallel big-file transfer against a fake Telegram file server."""

import asyncio
import os
import types as _t

from telethon.tl import types

from tgclone import fast

run = asyncio.run
SIZE = 5 * 1024 * 1024 + 12345


class FileServer:
    def __init__(self, data, cdn=False, dc=4):
        self.data, self.cdn, self.dc = data, cdn, dc
        self.session = _t.SimpleNamespace(dc_id=2)
        self._sender = "main"
        self.inflight = self.max_inflight = 0
        self.borrowed = self.returned = 0
        self.parts = {}

    async def _borrow_exported_sender(self, dc):
        self.borrowed += 1
        return f"dc{dc}"

    async def _return_exported_sender(self, sender):
        self.returned += 1

    async def _call(self, sender, req):
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        await asyncio.sleep(0.01)
        self.inflight -= 1
        if self.cdn:
            return types.upload.FileCdnRedirect(dc_id=1, file_token=b"", encryption_key=b"",
                                                encryption_iv=b"", file_hashes=[])
        return types.upload.File(type=types.storage.FilePartial(), mtime=0,
                                 bytes=self.data[req.offset:req.offset + req.limit])

    async def __call__(self, req):  # upload parts
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        await asyncio.sleep(0.01)
        self.inflight -= 1
        self.parts[req.file_part] = req.bytes
        return True


def doc_msg(size):
    doc = types.Document(id=1, access_hash=2, file_reference=b"r", date=None, mime_type="video/mp4",
                         size=size, dc_id=4, attributes=[])
    return _t.SimpleNamespace(document=doc)


def test_parallel_download_is_exact(tmp_path):
    data = os.urandom(SIZE)
    srv = FileServer(data)
    seen = []
    out = run(fast.download(srv, doc_msg(SIZE), str(tmp_path / "f.mp4"), lambda c, t: seen.append(c)))
    assert open(out, "rb").read() == data, "file rebuilt byte for byte"
    assert srv.max_inflight > 1, "several parts in flight"
    assert srv.borrowed == srv.returned == 1, "other-DC connection returned"
    assert seen[-1] == SIZE


def test_cdn_redirect_falls_back(tmp_path):
    srv = FileServer(os.urandom(SIZE), cdn=True)
    p = tmp_path / "f.mp4"
    assert run(fast.download(srv, doc_msg(SIZE), str(p))) is None and not p.exists()
    assert srv.returned == 1


def test_small_files_use_normal_path(tmp_path):
    assert run(fast.download(FileServer(b"x"), doc_msg(1000), str(tmp_path / "s"))) is None
    small = tmp_path / "small.bin"
    small.write_bytes(os.urandom(1024))
    assert run(fast.upload(FileServer(b""), str(small))) is None


def test_parallel_upload_parts(tmp_path):
    data = os.urandom(12 * 1024 * 1024 + 7)
    f = tmp_path / "big.bin"
    f.write_bytes(data)
    srv = FileServer(b"")
    handle = run(fast.upload(srv, str(f)))
    assert isinstance(handle, types.InputFileBig) and handle.parts == len(srv.parts)
    assert b"".join(srv.parts[i] for i in range(handle.parts)) == data
    assert srv.max_inflight > 1


def test_big_files_use_several_connections(tmp_path, monkeypatch):
    big = 12 * 1024 * 1024 + 99
    data = os.urandom(big)
    srv = FileServer(data)
    used, closed = [], []

    async def fake_pool(client, dc_id, n):
        def caller(i):
            async def call(req):
                used.append(i)
                if type(req).__name__ == "GetFileRequest":
                    return await srv._call("pool", req)
                return await srv(req)
            return call
        async def close():
            closed.append(n)
        return [caller(i) for i in range(n)], close
    monkeypatch.setattr(fast, "open_pool", fake_pool)
    out = run(fast.download(srv, doc_msg(big), str(tmp_path / "big.mp4")))
    assert open(out, "rb").read() == data
    assert set(used) == {0, 1, 2, 3}, "parts spread over 4 connections"
    assert srv.max_inflight >= 8 and srv.borrowed == 0
    assert srv._tgc_pools[4].refs == 0, "pool released (closed after IDLE_CLOSE)"
    used.clear()
    f = tmp_path / "up.bin"
    f.write_bytes(data)
    srv.parts.clear()
    srv._get_dc = lambda dc: None
    handle = run(fast.upload(srv, str(f)))
    assert b"".join(srv.parts[i] for i in range(handle.parts)) == data
    assert set(used) == {0, 1, 2, 3} and srv._tgc_pools[2].refs == 0


def test_pool_failure_falls_back_to_one_connection(tmp_path, monkeypatch):
    big = 11 * 1024 * 1024
    data = os.urandom(big)
    srv = FileServer(data)

    async def broken(client, dc_id, n):
        raise RuntimeError("no extra connections here")
    monkeypatch.setattr(fast, "open_pool", broken)
    out = run(fast.download(srv, doc_msg(big), str(tmp_path / "b.mp4")))
    assert open(out, "rb").read() == data and srv.borrowed == srv.returned == 1


class FakeNet:
    """Fake Telethon internals for open_pool: one Telegram DC that checks login imports."""

    def __init__(self):
        self.exports, self.imports, self.connected, self.home_inits = 0, [], 0, 0
        self.valid = None  # the auth bytes Telegram expects right now

    def client(self):
        net = self

        class Init:
            query = None

        class Client:
            session = _t.SimpleNamespace(dc_id=5, auth_key="HOMEKEY")
            _log, _proxy, _local_addr = None, None, None
            _init_request = Init()

            async def _get_dc(self, dc):
                return _t.SimpleNamespace(ip_address="1.2.3.4", port=443, id=dc)

            def _connection(self, *a, **k):
                return "conn"

            async def __call__(self, req):  # ExportAuthorizationRequest
                await asyncio.sleep(0.01)
                net.exports += 1
                net.valid = f"bytes{net.exports}".encode()
                return _t.SimpleNamespace(id=net.exports, bytes=net.valid)
        return Client()

    def sender_class(self):
        net = self

        class Sender:
            def __init__(self, key, loggers=None, **kw):
                self.auth_key = key

            async def connect(self, conn):
                net.connected += 1

            async def send(self, req):
                q = req.query.query  # InvokeWithLayer(InitConnection(...))
                if type(q).__name__ != "ImportAuthorizationRequest":
                    net.home_inits += 1  # home DC: initConnection with a cheap call
                    return None
                if q.bytes != net.valid:
                    raise ValueError("The provided authorization is invalid")
                net.imports.append(q.id)
                self.auth_key = f"KEY{q.id}"

            async def disconnect(self):
                pass
        return Sender


def test_pool_each_connection_has_its_own_login(monkeypatch):
    """Real logs: a shared imported key made Telegram drop the extra connections, and parallel
    exports mixed each other up. Now: one shared pool, each connection imports its own login."""
    from telethon import network
    net = FakeNet()
    monkeypatch.setattr(network, "MTProtoSender", net.sender_class())
    client = net.client()

    async def go():
        got = await asyncio.gather(*(fast.acquire(client, 4) for _ in range(3)))
        for _, release in got:
            await release()
        return got
    pools = run(go())
    assert net.exports == 4 and net.imports == [1, 2, 3, 4], "4 connections, 4 valid imports"
    assert all(callers is pools[0][0] for callers, _ in pools), "3 downloads share one pool"
    assert client._init_request.query is None, "Telethon's shared request is never modified"
    callers, _ = run(fast.open_pool(client, 5, 2))
    assert len(callers) == 2 and net.exports == 4 and net.home_inits == 2, "home DC: init, no export"


def test_parallel_downloads_share_one_pool_and_resets_pause_it(tmp_path, monkeypatch):
    """Real log: every file opened 4 connections (3 prefetched x 2 clones) -> Telegram reset them."""
    big = 11 * 1024 * 1024
    data = os.urandom(big)
    srv = FileServer(data)
    opened = []

    async def pool(client, dc_id, n):
        opened.append(dc_id)

        async def call(req):
            return await srv._call("pool", req)
        return [call] * n, (lambda: asyncio.sleep(0))
    monkeypatch.setattr(fast, "open_pool", pool)

    async def three():
        return await asyncio.gather(*(fast.download(srv, doc_msg(big), str(tmp_path / f"{i}.mp4"))
                                      for i in range(3)))
    outs = run(three())
    assert all(open(o, "rb").read() == data for o in outs)
    assert opened == [4], "3 parallel downloads, ONE set of connections"

    # Telegram cuts the extra connections -> this file falls back, and pools pause for 10 min
    srv2 = FileServer(data)

    async def dead_pool(client, dc_id, n):
        opened.append("dead")

        async def call(req):
            raise ConnectionError("Connection reset by peer")
        return [call] * n, (lambda: asyncio.sleep(0))
    monkeypatch.setattr(fast, "open_pool", dead_pool)
    out = run(fast.download(srv2, doc_msg(big), str(tmp_path / "x.mp4")))
    assert open(out, "rb").read() == data, "same file finished on one connection, not the slow path"
    assert srv2._tgc_pool_off > fast.time.time() + 500
    out = run(fast.download(srv2, doc_msg(big), str(tmp_path / "y.mp4")))
    assert open(out, "rb").read() == data and opened.count("dead") == 1, "paused: one connection"


def test_pause_grows_and_no_unretrieved_errors(tmp_path, monkeypatch):
    big = 11 * 1024 * 1024
    data = os.urandom(big)
    srv = FileServer(data)
    errors = []

    async def half_dead(client, dc_id, n):
        async def bad(req):
            await asyncio.sleep(0.005)
            raise ConnectionError("Connection reset by peer")

        async def slow(req):
            await asyncio.sleep(0.05)
            return await srv._call("pool", req)
        return [bad, slow, slow, slow], (lambda: asyncio.sleep(0))
    monkeypatch.setattr(fast, "open_pool", half_dead)

    async def go():
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: errors.append(ctx))
        r = await fast.download(srv, doc_msg(big), str(tmp_path / "a.mp4"))
        await asyncio.sleep(0.2)  # let leftover workers finish
        import gc
        gc.collect()  # asyncio reports unretrieved task errors when tasks are collected
        await asyncio.sleep(0)
        return r
    r = run(go())
    assert open(r, "rb").read() == data, "finished on one connection"
    assert not errors, "no asyncio errors logged (Telethon's own futures can't be faked here)"
    first = srv._tgc_pool_off - fast.time.time()
    assert 500 < first <= 600
    srv._tgc_pool_off = 0  # pause over, Telegram refuses again -> longer pause
    run(go())
    assert srv._tgc_pool_off - fast.time.time() > 3000, "10 min, then 1 h, then 6 h ..."


def test_dropped_extra_connection_error_is_retrieved():
    """Telethon sets the disconnect error on sender._disconnected; nobody awaits it, so asyncio
    printed 'Future exception was never retrieved'. _watch consumes it."""
    import gc
    from telethon import network

    async def go(watch):
        errors = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda lp, ctx: errors.append(ctx))
        snd = network.MTProtoSender(None, loggers=fast._QuietLoggers(), auto_reconnect=False)
        fut = loop.create_future()
        snd._MTProtoSender__disconnected = fut  # what connect() does
        if watch:
            fast._watch(snd)
        fut.set_exception(asyncio.IncompleteReadError(b"", 8))  # what _disconnect(error) does
        await asyncio.sleep(0)
        del snd, fut
        gc.collect()
        await asyncio.sleep(0)
        return errors
    assert run(go(False)), "without the fix asyncio reports the error (proves the test is real)"
    assert not run(go(True))


def test_pause_is_remembered_across_restarts(tmp_path, monkeypatch):
    state = tmp_path / "fast_pool.json"
    monkeypatch.setattr(fast, "_state", {"fails": 0, "off": 0.0})
    fast.load_state(state)
    try:
        c1 = FileServer(b"")
        fast._broken(c1)
        fast._broken(c1)  # second transfer on the same broken pool: counted once
        assert fast._state["fails"] == 1 and json.loads(state.read_text())["fails"] == 1
        monkeypatch.setattr(fast, "_state", {"fails": 0, "off": 0.0})
        fast.load_state(state)  # restart: new client, pause still known
        assert fast._paused(FileServer(b""))
        fast._state["off"] = 0
        c2 = FileServer(b"")
        fast._broken(c2)
        assert fast._state["off"] - fast.time.time() > 3000, "second reset: longer pause"
    finally:
        fast.STATE_FILE = None


def test_shutdown_cancel_skips_finish_but_user_stop_runs_it():
    from tgclone.jobs import Job, JobManager

    async def go():
        jm = JobManager()
        finished = []

        async def work(job):
            await asyncio.sleep(10)

        async def on_finish(job):
            finished.append(job.id)
        a = jm.start(Job("clone", "A", "a"), work, on_finish)
        b = jm.start(Job("clone", "B", "b"), work, on_finish)
        await asyncio.sleep(0)
        jm.stop(a.id)  # user pressed Stop -> card / DM / forget
        b.task.cancel()  # Ctrl+C -> nothing sent, kept for resume
        await asyncio.gather(a.task, b.task, return_exceptions=True)
        await asyncio.sleep(0.01)
        return finished, a, b
    finished, a, b = run(go())
    assert finished == [a.id] and b.state == "stopped" and not b.cancelled


def test_extra_connections_switch_and_quiet_wrong_session_log(tmp_path, monkeypatch, caplog):
    import logging
    big = 11 * 1024 * 1024
    data = os.urandom(big)
    srv = FileServer(data)
    opened = []

    async def pool(client, dc_id, n):
        opened.append(1)
        raise RuntimeError("must not open")
    monkeypatch.setattr(fast, "open_pool", pool)
    monkeypatch.setattr(fast, "ENABLED", False)
    out = run(fast.download(srv, doc_msg(big), str(tmp_path / "a.mp4")))
    assert open(out, "rb").read() == data and not opened, "switched off: one connection only"
    # Telethon's 'wrong session ID' warnings: one line per minute instead of a flood
    lg = logging.getLogger("telethon.network.mtprotosender")
    with caplog.at_level(logging.WARNING, logger="telethon.network.mtprotosender"):
        for _ in range(20):
            lg.warning("Security error while unpacking a received message: %s",
                       "Server replied with a wrong session ID (see FAQ for details)")
        lg.warning("something else")
    msgs = [r.getMessage() for r in caplog.records if r.name == "telethon.network.mtprotosender"]
    assert len(msgs) == 2 and "harmless" in msgs[0] and msgs[1] == "something else"


def test_mid_size_upload_is_parallel_with_md5(tmp_path):
    import hashlib
    data = os.urandom(3 * 1024 * 1024 + 5)  # 1-10 MB: saveFilePart, parallel, InputFile + MD5
    f = tmp_path / "mid.bin"
    f.write_bytes(data)
    srv = FileServer(b"")
    handle = run(fast.upload(srv, str(f)))
    assert isinstance(handle, types.InputFile) and handle.md5_checksum == hashlib.md5(data).hexdigest()
    assert b"".join(srv.parts[i] for i in range(handle.parts)) == data and srv.max_inflight > 1


def test_preupload_is_used_once_by_send(tmp_path):
    from tgclone.message import CloneMessage, _uploaded_doc
    data = os.urandom(2 * 1024 * 1024)
    f = tmp_path / "v.mp4"
    f.write_bytes(data)
    srv = FileServer(b"")
    msg = _t.SimpleNamespace(message="", entities=None, raw_text="", text="", sender_id=1, id=1,
                             media=None, photo=None, poll=None, document=types.Document(
                                 id=1, access_hash=1, file_reference=b"", date=None, mime_type="video/mp4",
                                 size=len(data), dc_id=2, attributes=[], thumbs=None, video_thumbs=None))
    calls = []
    real = FileServer.__call__

    async def counting(self, req):
        calls.append(req)
        return await real(self, req)
    FileServer.__call__ = counting
    try:
        tm = CloneMessage(msg)
        tm._downloaded = str(f)
        run(tm.preupload(srv))
        assert len(calls) == 4 and id(srv) in tm._handles
        media = run(_uploaded_doc(srv, tm, str(f), None, None))
        assert len(calls) == 4 and isinstance(media.file, types.InputFile), "send reused the pre-upload"
        run(_uploaded_doc(srv, tm, str(f), None, None))
        assert len(calls) == 8, "a second destination uploads its own copy"
    finally:
        FileServer.__call__ = real


def test_lost_reply_part_is_asked_again(tmp_path, monkeypatch):
    big = 3 * 1024 * 1024
    data = os.urandom(big)
    srv = FileServer(data)
    monkeypatch.setattr(fast, "PART_TIMEOUT", 0.05)
    lost = {"n": 0}
    real = srv._call

    async def flaky(sender, req):
        if req.offset == fast.PART and lost["n"] == 0:  # first reply for part 1 never comes
            lost["n"] += 1
            await asyncio.sleep(3600)
        return await real(sender, req)
    srv._call = flaky
    out = run(fast.download(srv, doc_msg(big), str(tmp_path / "a.mp4")))
    assert open(out, "rb").read() == data and lost["n"] == 1, "stuck part asked again, file complete"


def test_retry_reconnects_a_dropped_client(monkeypatch):
    from telethon import TelegramClient
    from tgclone import message as m
    real_sleep = asyncio.sleep
    monkeypatch.setattr(m.asyncio, "sleep", lambda s: real_sleep(0))

    class C(TelegramClient):
        def __init__(self):
            self.up, self.connects = False, 0

        def is_connected(self):
            return self.up

        async def connect(self):
            self.connects += 1
            self.up = True

        async def send_message(self, *a, **k):
            if not self.up:
                raise ConnectionError("Cannot send requests while disconnected")
            return "sent"
    c = C()
    assert run(m.retry(c.send_message, 1, "x")) == "sent" and c.connects == 1
