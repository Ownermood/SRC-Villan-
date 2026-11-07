"""Real-run fixes: polls, premium emoji in source messages, auto-resume after restart, no limits."""

import asyncio
import json
import types as _t

from telethon.errors import DocumentInvalidError
from telethon.tl import types

from fakes import Client, Msg, make_app
from tgclone.config import Config, load_config, save_config
from tgclone.message import CloneMessage, poll_media, send

run = asyncio.run


def poll_msg(quiz=False, answered=True, public=False):
    answers = [types.PollAnswer(text=types.TextWithEntities(t, []), option=bytes([i]))
               for i, t in enumerate(["A", "B", "C"])]
    poll = types.Poll(id=99, question=types.TextWithEntities("Q?", []), answers=answers, hash=1,
                      quiz=quiz, closed=True, public_voters=public)
    results = types.PollResults(results=[types.PollAnswerVoters(option=bytes([1]), voters=3,
                                                                correct=True)] if answered else None,
                                solution="because" if quiz else None, solution_entities=[])
    m = Msg(7, "", media=types.MessageMediaPoll(poll=poll, results=results), kind="poll")
    m.poll = poll
    return m


class RawClient(Client):
    def __init__(self, refuse_public=False):
        super().__init__()
        self.raw, self.refuse_public = [], refuse_public

    async def __call__(self, req):
        self.raw.append(req)
        if self.refuse_public and req.media.poll.public_voters:
            raise Exception("BROADCAST_PUBLIC_VOTERS_FORBIDDEN")
        return _t.SimpleNamespace(updates=[_t.SimpleNamespace(
            message=types.Message(id=555, peer_id=types.PeerChannel(7), date=None, message=""))])


def test_poll_copy_normal_quiz_and_unanswered():
    m = poll_media(poll_msg())
    assert not m.poll.closed and m.poll.id != 99 and [a.text.text for a in m.poll.answers] == ["A", "B", "C"]
    q = poll_media(poll_msg(quiz=True))
    assert q.poll.quiz and q.correct_answers == [1] and q.solution == "because", "answer index, not bytes"
    u = poll_media(poll_msg(quiz=True, answered=False))
    assert not u.poll.quiz and u.correct_answers is None, "unknown answer -> plain poll instead of failing"


def test_poll_is_sent_and_mapped():
    c = RawClient(refuse_public=True)
    sent = run(send(c, 7, [CloneMessage(poll_msg(public=True))], Config()))
    assert [s.id for s in sent] == [555]
    assert len(c.raw) == 2 and not c.raw[1].media.poll.public_voters, "retried without public votes"


def test_premium_emoji_stripped_for_normal_accounts():
    ent = [types.MessageEntityBold(0, 2), types.MessageEntityCustomEmoji(3, 2, document_id=5)]

    class Normal(Client):
        sent_ents = None

        async def get_me(self):
            return _t.SimpleNamespace(premium=False, bot=False)

        async def send_message(self, dest, text, file=None, formatting_entities=None, **kw):
            Normal.sent_ents = formatting_entities
            return await super().send_message(dest, text, file=file, **kw)
    m = Msg(1, "hi 😀")
    m.entities = ent
    run(send(Normal(), 7, [CloneMessage(m)], Config()))
    assert [type(e).__name__ for e in Normal.sent_ents] == ["MessageEntityBold"]


def test_document_invalid_retry_plain_and_reupload():
    class Picky(Client):
        tries = 0

        async def send_message(self, dest, text, file=None, formatting_entities=None, **kw):
            Picky.tries += 1
            if any(isinstance(e, types.MessageEntityCustomEmoji) for e in formatting_entities or []):
                raise DocumentInvalidError(None)
            return await super().send_message(dest, text, file=file, **kw)
    m = Msg(1, "x 😀")
    m.entities = [types.MessageEntityCustomEmoji(2, 2, document_id=5)]
    out = run(send(Picky(), 7, [CloneMessage(m)], Config()))
    assert len(out) == 1 and Picky.tries == 2


def test_old_default_limits_are_removed(tmp_path):
    p = tmp_path / "c.json"
    cfg = Config()
    cfg.max_file_mb, cfg.save.max_batch = 2000, 1000
    save_config(cfg, p)
    cfg = load_config(p)
    assert cfg.max_file_mb == 0 and cfg.save.max_batch == 0
    assert Config().max_file_mb == 0 and Config().save.max_batch == 0


def test_save_without_limit_and_resume_progress():
    from tgclone.jobs import Job
    from tgclone.save import save_range
    a = make_app()
    msgs = {i: Msg(i, f"post {i}") for i in range(1, 2501)}
    c = a.reader

    async def gm(chat, ids=None, limit=None):
        return [msgs.get(i) for i in ids]

    async def resolve(ref):
        return 1
    c.get_messages, a.resolve = gm, resolve
    job = Job("save", "t", "save:x:1:2500:9")
    a.store.set_progress(job.key, 2000)  # 2000 already sent before a restart
    sent, failed = run(save_range(a, "x", 1, 2500, 9, job))
    assert (sent, failed) == (500, 0) and c.calls[0][2] == "post 2001"


def test_jobs_survive_restart_and_resume(hubenv):
    from test_hub import OWNER, login, say
    from tgclone import ui
    hub = hubenv.new_hub()
    login(hub, OWNER, "+91000000001", env=hubenv)
    app = hub.ws[OWNER][0]
    say(hub, "/add -100111 -100222", OWNER)
    gate = asyncio.Event()

    async def slow_iter(src, reverse, min_id, max_id):
        await gate.wait()
        if False:
            yield None
    app.user.iter_messages = slow_iter

    async def gm(chat, ids=None, limit=None):
        return []
    app.user.get_messages = gm

    async def go():
        ev = await __import__("test_hub")._dispatch(hub, __import__("fakes").CallbackEvent(
            f"fc:0:{ui.fhash(app.cfg.forwards[0])}", OWNER))
        await asyncio.sleep(0.05)
        assert app.store.pending(), "running clone is remembered"
        # simulate a shutdown: the task is cancelled without the user pressing stop
        for j in app.jobs.running():
            j.task.cancel()
        await asyncio.sleep(0.05)
        return ev
    run(go())
    pend = app.store.pending()
    assert pend and json.loads(pend[0]["data"])["forward"]["source"] == -100111, "kept for resume"
    # restart: a new hub picks it up
    hub2 = hubenv.new_hub()
    app2 = hub2.ws[OWNER][0]
    app2.user.get_messages = gm

    async def resume():
        gate2 = asyncio.Event()

        async def slow_iter2(src, reverse, min_id, max_id):
            await gate2.wait()
            if False:
                yield None
        app2.user.iter_messages = slow_iter2
        n = await hub2.resume_jobs(OWNER, delay=0)
        await asyncio.sleep(0.05)
        running = app2.jobs.running()
        gate2.set()
        await asyncio.sleep(0.05)
        return n, running
    n, running = run(resume())
    assert n == 1 and running, "clone continues by itself"
    assert any("Resumed 1 job" in s[1] for s in hub2.bot.sent)
    assert not app2.store.pending(), "finished jobs are forgotten"


def test_user_stop_forgets_job(hubenv):
    from test_hub import OWNER, login, say
    from tgclone import ui
    hub = hubenv.new_hub()
    login(hub, OWNER, "+91000000001", env=hubenv)
    app = hub.ws[OWNER][0]
    say(hub, "/add -100111 -100222", OWNER)

    async def slow_iter(src, reverse, min_id, max_id):
        await asyncio.sleep(10)
        if False:
            yield None
    app.user.iter_messages = slow_iter

    async def gm(chat, ids=None, limit=None):
        return []
    app.user.get_messages = gm

    async def go():
        import test_hub
        from fakes import CallbackEvent
        await test_hub._dispatch(hub, CallbackEvent(f"fc:0:{ui.fhash(app.cfg.forwards[0])}", OWNER))
        await asyncio.sleep(0.05)
        assert app.store.pending()
        await test_hub._dispatch(hub, CallbackEvent("jS", OWNER))
        await asyncio.sleep(0.05)
    run(go())
    assert not app.store.pending()


def test_premium_emoji_posts_go_via_bot_when_account_is_not_premium():
    from fakes import Client as C

    class Account(C):
        async def get_me(self):
            return _t.SimpleNamespace(premium=False, bot=False)

    class Bot(C):
        refuse = False

        async def get_me(self):
            return _t.SimpleNamespace(premium=False, bot=True)

        async def send_message(self, dest, text, file=None, formatting_entities=None, **kw):
            if self.refuse and any(isinstance(e, types.MessageEntityCustomEmoji)
                                   for e in formatting_entities or []):
                raise DocumentInvalidError(None)
            return await super().send_message(dest, text, file=file, **kw)
    a = make_app(Account())
    a.bot = Bot()
    m = Msg(1, "hi 😀", chat_id=-1)
    m.entities = [types.MessageEntityCustomEmoji(3, 2, document_id=5)]
    run(a.deliver(-1, [CloneMessage(m)], [7]))
    assert a.bot.calls and not a.reader.calls, "bot posted it with the premium emoji"
    # Telegram refuses the bot -> account posts with plain emoji, and the bot isn't asked again
    b = make_app(Account())
    b.bot = Bot()
    b.bot.refuse = True
    m2 = Msg(2, "hi 😀", chat_id=-1)
    m2.entities = [types.MessageEntityCustomEmoji(3, 2, document_id=5)]
    run(b.deliver(-1, [CloneMessage(m2)], [7]))
    assert b.reader.calls and b.bot._tgc_no_emoji
    plain = Msg(3, "no emoji", chat_id=-1)
    run(b.deliver(-1, [CloneMessage(plain)], [7]))
    assert not b.bot.calls, "normal posts always go from the account"
