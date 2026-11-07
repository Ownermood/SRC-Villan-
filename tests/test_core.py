import asyncio
import os

import pytest
from telethon.errors import ChatForwardsRestrictedError

from fakes import Client, Msg, make_app
from tgclone.config import DOWNLOAD_DIR, Config, Forward, load_config, norm_ref, save_config, CONFIG_FILE
from tgclone.jobs import Job, JobManager
from tgclone.message import CloneMessage, FileTooBig, file_type, media_key, send
from tgclone.plugins import apply_plugins, load_plugins
from tgclone.save import parse_link, save_range
from tgclone.storage import Store
from tgclone import app as appmod

run = asyncio.run


def test_config_roundtrip():
    cfg = load_config()
    assert CONFIG_FILE.exists()
    cfg.forwards.append(Forward(source="-100123", dest=["@x"]))
    save_config(cfg)
    assert load_config().forwards[0].source == "-100123"
    assert norm_ref("-100123") == -100123 and norm_ref("@x") == "@x"


@pytest.mark.parametrize("link,exp", [
    ("https://t.me/c/123456/77", (-100123456, 77, 77)),
    ("t.me/somechan/20-10", ("somechan", 10, 20)),
    ("https://t.me/c/123/9/55", (-100123, 55, 55)),
    ("hello", None),
    ("https://t.me/c/777", None),  # chat link, not a post
    ("t.me/ab/5", None),  # too short to be a username
])
def test_parse_link(link, exp):
    assert parse_link(link) == exp


def test_store_and_migration(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    db = sqlite3.connect(p)  # v1 schema without media_key
    db.execute("CREATE TABLE msg_map(src_chat INTEGER, src_id INTEGER, dest_chat INTEGER,"
               " dest_id INTEGER, PRIMARY KEY (src_chat, src_id, dest_chat))")
    db.execute("INSERT INTO msg_map VALUES (1,2,3,4)")
    db.commit(); db.close()
    st = Store(p)
    assert st.get(1, 2) == {3: 4}
    st.add(1, 2, 5, 6, media_key=77)
    assert st.get_one(1, 2, 5) == 6 and st.media_key(1, 2) in (None, 77)
    st.set_media_key(1, 2, 88)
    assert st.media_key(1, 2) == 88
    st.set_progress("k", 9)
    assert st.get_progress("k") == 9
    st.remove(1, 2)
    assert st.get(1, 2) == {}


def test_plugins():
    cfg = Config()
    f = cfg.plugins.filter
    f.check, f.text.blacklist, f.files.blacklist = True, ["SPAM"], ["sticker"]
    cfg.plugins.replace.check, cfg.plugins.replace.text = True, {"foo": "bar"}
    cfg.plugins.caption.check, cfg.plugins.caption.footer = True, "\n-end"
    pl = load_plugins(cfg.plugins)
    assert [p.id_ for p in pl] == ["filter", "replace", "caption"]
    assert run(apply_plugins(pl, CloneMessage(Msg(1, "buy spam")))) is None
    assert run(apply_plugins(pl, CloneMessage(Msg(1, kind="sticker")))) is None
    tm = run(apply_plugins(pl, CloneMessage(Msg(1, "foo x"))))
    assert tm.text == "bar x\n-end" and tm.text_changed
    f.text.blacklist, f.text.whitelist = [], ["ok"]
    assert run(apply_plugins(load_plugins(cfg.plugins), CloneMessage(Msg(1, "nope")))) is None


def test_send_paths():
    assert file_type(Msg(1, "hi")) == "text" and file_type(Msg(1, kind="photo")) == "photo"
    c = Client()
    run(send(c, 7, [CloneMessage(Msg(1, "hi"))], Config()))
    assert c.calls[0][0] == "msg"
    c = Client()
    run(send(c, 7, [CloneMessage(Msg(1, "c", media="M", kind="photo"))], Config()))
    assert c.calls[0][3] == "M"  # reference copy, no download
    c = Client(restrict=True)
    run(send(c, 7, [CloneMessage(Msg(1, "c", media="M", kind="video"))], Config()))
    assert c.calls[0][0] == "file" and c.calls[0][5] and c.calls[0][5].endswith(".jpg"), "thumb kept"
    c = Client()
    run(send(c, 7, [CloneMessage(Msg(1, "c", media="M", kind="video", noforwards=True))], Config()))
    assert c.calls[0][0] == "file"
    with pytest.raises(ChatForwardsRestrictedError):
        run(send(Client(restrict=True), 7, [CloneMessage(Msg(1, "c", media="M", kind="photo"))],
                 Config(protected_fallback=False)))
    c = Client()
    r = run(send(c, 7, [CloneMessage(Msg(i, "a", media="M", kind="photo", grouped_id=9))
                        for i in (1, 2)], Config()))
    assert len(c.calls[0][2]) == 2 and len(r) == 2
    c = Client()
    run(send(c, 7, [CloneMessage(Msg(1, "a", media="M", kind="photo"))], Config(), same_client=False))
    assert isinstance(c.calls[0][2], str)
    c = Client()
    run(send(c, 7, [CloneMessage(Msg(1, "a"))], Config(forward_mode=True)))
    assert c.calls[0][0] == "fwd"
    c = Client(restrict=True)
    run(send(c, 7, [CloneMessage(Msg(1, "a", media="M", kind="photo"))], Config(forward_mode=True)))
    assert c.calls[0][0] == "file"


def test_progress_and_limits():
    seen = []
    c = Client(restrict=True)
    run(send(c, 7, [CloneMessage(Msg(1, "c", media="M", kind="video"))], Config(),
             progress=lambda d, cur, tot: seen.append(d)))
    assert "down" in seen and "up" in seen
    with pytest.raises(FileTooBig):
        run(CloneMessage(Msg(1, kind="video", size=5 * 1048576)).download(max_mb=1))
    # one file bigger than the disk cap is allowed when nothing else is downloading
    import shutil
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    big = CloneMessage(Msg(1, kind="video", size=5 * 1048576))
    assert os.path.exists(run(big.download(max_disk_mb=1)))
    big.cleanup()
    tm = CloneMessage(Msg(3, kind="video"))
    p = run(tm.download())
    assert os.path.exists(p)
    tm.cleanup()
    assert not os.path.exists(p)


def test_disk_wait(monkeypatch):
    from tgclone import message
    sizes = iter([10 ** 9, 10 ** 9, 0])
    monkeypatch.setattr(message, "_dir_size", lambda p: next(sizes))
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(message.asyncio, "sleep", fake_sleep)
    run(message._wait_disk(10, max_disk_mb=100))
    assert slept == [5, 5]


def test_media_key():
    assert media_key(Msg(1, kind="photo", doc_id=5)) == 5 and media_key(Msg(1, "x")) is None


def _msgs():
    return [Msg(1, "one"), Msg(2, "", media="M", kind="photo", grouped_id=5),
            Msg(3, "", media="M", kind="photo", grouped_id=5), Msg(4, "reply", reply=1), Msg(5, "")]


def _wire(a, msgs):
    c = a.reader

    async def it(src, reverse, min_id, max_id):
        for m in msgs:
            if m.id > min_id and (not max_id or m.id < max_id):
                yield m

    async def gm(chat, ids=None, limit=None):
        if limit:
            return [msgs[-1]]
        return [next((m for m in msgs if m.id == i), None) for i in ids]
    c.iter_messages, c.get_messages = it, gm

    async def resolve(ref):
        return int(ref)
    a.resolve = resolve
    return c


def test_run_forward_resume_and_job():
    a = make_app()
    c = _wire(a, _msgs())
    job = Job("clone", "t", "k")
    n = run(appmod.run_forward(a, Forward(source=-1, dest=[7, 8]), job=job))
    assert n == 4 and job.done == 4 and job.end_id == 5
    kinds = [x[0] for x in c.calls]
    assert kinds.count("file") == 2 and kinds.count("msg") == 4
    replies = [x[4] for x in c.calls if x[0] == "msg" and x[2] == "reply"]
    assert len(replies) == 2 and all(replies)
    c.calls.clear()
    assert run(appmod.run_forward(a, Forward(source=-1, dest=[7, 8]))) == 0 and not c.calls
    a.store.set_progress(a.store.key(-1, [7, 8]), 0)
    run(appmod.run_forward(a, Forward(source=-1, dest=[7, 8])))
    assert not c.calls, "mapped messages are never re-sent"
    run(appmod.run_forward(a, Forward(source=-1, dest=[7, 8]), fresh=True))
    assert len(c.calls) == 6


def test_run_forward_cancel():
    a = make_app()
    c = _wire(a, _msgs())
    job = Job("clone", "t", "k2")
    job.cancelled = True
    assert run(appmod.run_forward(a, Forward(source=-3, dest=[7]), job=job)) == 0 and not c.calls


def test_save_range():
    a = make_app()
    c = _wire(a, _msgs())
    job = Job("save", "t", "s")
    assert run(save_range(a, "x", 1, 10, 99, job)) == (4, 0)
    assert all(x[1] == 99 for x in c.calls) and job.last_id == 10
    a.cfg.save.max_batch = 3
    with pytest.raises(ValueError):
        run(save_range(a, "x", 1, 10, 99))


def test_job_manager():
    async def go():
        jm = JobManager(1)
        ev = asyncio.Event()

        async def slow(j):
            await ev.wait()
        j1 = jm.start(Job("clone", "a", "k"), slow)
        with pytest.raises(ValueError):
            jm.start(Job("clone", "b", "k"), slow)  # duplicate key
        with pytest.raises(ValueError):
            jm.start(Job("clone", "c", "other"), slow)  # max jobs
        assert jm.stop(j1.id) == 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert j1.state == "stopped"
        j2 = jm.start(Job("save", "d", "k"), lambda j: asyncio.sleep(0))
        await j2.task
        assert j2.state == "done"
        j1.file_progress("down", 50, 100)
        assert "⬇️" in j1.file and "▰" in j1.file
        assert "#" in j2.render()
    run(go())


def test_live_edit_and_delete():
    from tgclone import live
    a = make_app()
    a.from_to = {-1: [7]}
    live.register(a)
    h = {type(ev).__name__: fn for fn, ev in a.reader.handlers}
    a.store.add(-1, 10, 7, 500, media_key=1000)
    a.reader.calls.clear()

    class E:
        chat_id = -1
    e = E()
    e.message = Msg(10, "new text", edit_date=None)
    run(h["MessageEdited"](e))
    assert not a.reader.calls, "reaction updates (no edit_date) are ignored"
    e.message = Msg(10, "new text", edit_date=1)
    run(h["MessageEdited"](e))
    assert a.reader.calls[-1][:4] == ("edit", 7, 500, "new text") and a.reader.calls[-1][4] is None
    e.message = Msg(10, "pic", media="NEWMEDIA", kind="photo", doc_id=2000, edit_date=2)
    run(h["MessageEdited"](e))
    assert a.reader.calls[-1][4] == "NEWMEDIA" and a.store.media_key(-1, 10) == 2000
    a.cfg.live.delete_sync = True
    d = E()
    d.deleted_ids = [10]
    run(h["MessageDeleted"](d))
    assert a.reader.calls[-1] == ("del", 7, 500) and a.store.get(-1, 10) == {}
