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
