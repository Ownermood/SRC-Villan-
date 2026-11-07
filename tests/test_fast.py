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
    assert srv.max_inflight >= 8 and closed == [4] and srv.borrowed == 0
    used.clear()
    f = tmp_path / "up.bin"
    f.write_bytes(data)
    srv.parts.clear()
    srv._get_dc = lambda dc: None
    handle = run(fast.upload(srv, str(f)))
    assert b"".join(srv.parts[i] for i in range(handle.parts)) == data
    assert set(used) == {0, 1, 2, 3} and closed == [4, 4]


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
        self.exports, self.imports, self.connected = 0, [], 0
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
            def __init__(self, key, loggers=None):
                self.auth_key = key

            async def connect(self, conn):
                net.connected += 1

            async def send(self, req):
                q = req.query.query  # InvokeWithLayer(InitConnection(ImportAuthorization))
                if q.bytes != net.valid:
                    raise ValueError("The provided authorization is invalid")
                net.imports.append(q.id)
                self.auth_key = f"KEY{q.id}"

            async def disconnect(self):
                pass
        return Sender


def test_pool_exports_login_once_even_with_parallel_downloads(monkeypatch):
    """Real log: 3 prefetch downloads exported at once and broke each other's import."""
    from telethon import network
    net = FakeNet()
    monkeypatch.setattr(network, "MTProtoSender", net.sender_class())
    client = net.client()

    async def go():
        return await asyncio.gather(*(fast.open_pool(client, 4, 4) for _ in range(3)))
    pools = run(go())
    assert net.exports == 1 and net.imports == [1], "one export, one import, shared"
    assert client._init_request.query is None, "Telethon's shared request is never modified"
    assert all(len(callers) == 4 for callers, _ in pools)
    run(fast.open_pool(client, 4, 4))
    assert net.exports == 1, "the authorised key is remembered"
    callers, _ = run(fast.open_pool(client, 5, 2))
    assert len(callers) == 2 and net.exports == 1, "home DC needs no export"
