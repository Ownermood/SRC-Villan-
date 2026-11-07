"""Real-run fixes: polls, premium emoji in source messages, auto-resume after restart, no limits."""

import asyncio
import json
import types as _t

from telethon.errors import DocumentInvalidError
from telethon.tl import types

from fakes import Client, Msg, make_app
from tgclone.config import Config, Forward, load_config, save_config
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


def test_every_screen_emoji_is_premium():
    import re
    from pathlib import Path
    from tgclone import emoji
    root = Path(__file__).resolve().parents[1] / "tgclone"
    src = "".join((root / f).read_text() for f in ("ui.py", "hub.py", "bot.py", "jobs.py"))
    found = set(re.findall("[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿⏩-⏺"
                           "ℹ▪-◾](?:‍[\U0001F000-\U0001FAFF☀-➿])*", src))
    text_glyphs = {"→", "▰", "▱", "●"}  # progress bars / arrows, not emoji
    plain = {e for e in found if emoji.doc_id(e) is None} - text_glyphs
    assert not plain, f"not premium: {plain}"


def test_detects_when_telegram_removes_custom_emoji():
    from tgclone import emoji
    sent = '<tg-emoji emoji-id="1">⚡</tg-emoji> hi'
    emoji.check_sent(sent, _t.SimpleNamespace(entities=[types.MessageEntityBold(0, 1)]))
    assert emoji.status == "removed"
    emoji.check_sent(sent, _t.SimpleNamespace(entities=[types.MessageEntityCustomEmoji(0, 1, document_id=1)]))
    assert emoji.status == "working"
    emoji.check_sent("plain", _t.SimpleNamespace(entities=[]))
    assert emoji.status == "working", "plain screens don't change the verdict"


def test_premium_emoji_retried_after_a_refusal(monkeypatch):
    from tgclone import emoji
    emoji.set_enabled(True)
    emoji.disable_for_session()
    assert not emoji.active()
    import time
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + emoji.RETRY_AFTER + 1)
    assert emoji.active(), "tried again later (e.g. after the owner bought Premium)"
    monkeypatch.setattr(time, "time", real)
    emoji.set_enabled(True)


def test_clone_wizard_from_main_menu(hubenv, monkeypatch):
    from test_hub import OWNER, login, press, say
    from tgclone import bot as botmod
    hub = hubenv.new_hub()
    login(hub, OWNER, "+91000000001", env=hubenv)
    started = []

    async def fake_clone(self, event, fwd, fresh):
        started.append((fwd.source, fwd.dest, fresh))
    monkeypatch.setattr(botmod.Panel, "start_clone", fake_clone)
    ev = press(hub, "cl", OWNER)
    assert "step 1/2" in ev.edits[-1][0]
    say(hub, "-100111", OWNER)
    ev = say(hub, "-100111", OWNER)
    assert "can't be the source" in ev.out[-1][0]
    say(hub, "-100222", OWNER)
    assert started == [(-100111, [-100222], False)]
    assert hub.ws[OWNER][0].cfg.forwards == [], "a one-off clone doesn't add a live forward"


def test_admin_emoji_test_reports_truth(hubenv):
    from test_hub import OWNER, login, press
    hub = hubenv.new_hub()
    login(hub, OWNER, "+91000000001", env=hubenv)
    bot = hub.bot

    async def kept(uid, text, **kw):
        return _t.SimpleNamespace(entities=[types.MessageEntityCustomEmoji(0, 1, document_id=1)] * 3,
                                  reply_markup=types.ReplyInlineMarkup(rows=[types.KeyboardButtonRow(
                                      kw["buttons"][0])]))
    bot._tgc_orig_send = kept
    ev = press(hub, "aet", OWNER)
    assert ev.edits, ev.answers
    assert "3/3" in ev.edits[-1][0] and "kept" in ev.edits[-1][0] and "allows" in ev.edits[-1][0]

    async def removed(uid, text, **kw):
        return _t.SimpleNamespace(entities=[], reply_markup=None)
    bot._tgc_orig_send = removed
    ev = press(hub, "aet", OWNER)
    assert "0/3" in ev.edits[-1][0] and "BotFather" in ev.edits[-1][0]

    async def refused(uid, text, **kw):
        raise Exception("ENTITY_TEXT_INVALID")
    bot._tgc_orig_send = refused
    ev = press(hub, "aet", OWNER)
    assert "ENTITY_TEXT_INVALID" in ev.edits[-1][0]
    assert press(hub, "aet", 5).answers[-1] == ("🚫 Owner only", True)


class TurboClient:
    """Fake account that understands batch forwarding (ForwardMessagesRequest)."""

    def __init__(self, restrict=False):
        from fakes import Client as C
        self.base = C()
        self.restrict, self.batches = restrict, []

    def __getattr__(self, name):
        return getattr(self.base, name)

    async def __call__(self, req):
        from telethon.errors import ChatForwardsRestrictedError
        if type(req).__name__ != "ForwardMessagesRequest":
            return None
        if self.restrict:
            raise ChatForwardsRestrictedError(None)
        self.batches.append(req)
        ups = []
        for rid in req.random_id:
            self.base.n += 1
            ups.append(types.UpdateMessageID(id=self.base.n, random_id=rid))
        return _t.SimpleNamespace(updates=ups)


def _wire_turbo(a, msgs):
    c = a.reader

    async def it(src, reverse, min_id, max_id):
        for m in msgs:
            if m.id > min_id and (not max_id or m.id < max_id):
                yield m

    async def gm(chat, ids=None, limit=None):
        return [msgs[-1]]

    async def resolve(ref):
        return int(ref)
    c.base.iter_messages, c.base.get_messages, a.resolve = it, gm, resolve


def test_turbo_copies_100_posts_per_request():
    from tgclone import app as appmod
    from tgclone.jobs import Job
    c = TurboClient()
    a = make_app(c)
    a.cfg.past.turbo = True
    msgs = [Msg(i, f"post {i}", chat_id=-1) for i in range(1, 251)]
    _wire_turbo(a, msgs)
    job = Job("clone", "t", "k")
    n = run(appmod.run_forward(a, Forward(source=-1, dest=[7]), job=job))
    assert n == 250 and job.done == 250
    assert [len(b.id) for b in c.batches] == [100, 100, 50], "3 requests instead of 250"
    assert all(b.drop_author for b in c.batches), "copy without 'Forwarded from'"
    assert a.store.get_one(-1, 250, 7) is not None, "mapped for edit/delete sync"
    c.batches.clear()
    run(appmod.run_forward(a, Forward(source=-1, dest=[7])))
    assert not c.batches, "resume: nothing sent twice"


def test_turbo_keeps_order_and_falls_back():
    from tgclone import app as appmod
    c = TurboClient()
    a = make_app(c)
    a.cfg.past.turbo = True
    msgs = [Msg(1, "a", chat_id=-1), Msg(2, "b", chat_id=-1), Msg(3, "reply", reply=1, chat_id=-1),
            Msg(4, "c", chat_id=-1)]
    _wire_turbo(a, msgs)
    run(appmod.run_forward(a, Forward(source=-1, dest=[7])))
    assert [b.id for b in c.batches] == [[1, 2], [4]], "the reply goes alone, order kept"
    assert [x[2] for x in c.base.calls] == ["reply"]
    assert c.base.calls[0][4] == a.store.get_one(-1, 1, 7), "reply linked to the copied post"
    # forwarding restricted -> one by one
    r = TurboClient(restrict=True)
    b = make_app(r)
    b.cfg.past.turbo = True
    _wire_turbo(b, [Msg(1, "x", chat_id=-1), Msg(2, "y", chat_id=-1)])
    run(appmod.run_forward(b, Forward(source=-1, dest=[7])))
    assert [x[2] for x in r.base.calls] == ["x", "y"]


def test_protected_chat_prefetches_next_files():
    from tgclone import app as appmod
    order = []

    class Slow(Msg):
        async def download_media(self, file, thumb=None, progress_callback=None):
            order.append(("down", self.id))
            await asyncio.sleep(0.01)
            return await super().download_media(file, thumb, progress_callback)
    from fakes import Client as C

    class Up(C):
        async def send_file(self, dest, f, caption=None, **kw):
            order.append(("up", caption))
            return await super().send_file(dest, f, caption=caption, **kw)
    a = make_app(Up())
    msgs = [Slow(i, f"v{i}", media="M", kind="video", noforwards=True, chat_id=-1) for i in range(1, 6)]
    c = a.reader

    async def it(src, reverse, min_id, max_id):
        for m in msgs:
            yield m

    async def gm(chat, ids=None, limit=None):
        return [msgs[-1]]

    async def resolve(ref):
        return int(ref)
    c.iter_messages, c.get_messages, a.resolve = it, gm, resolve
    run(appmod.run_forward(a, Forward(source=-1, dest=[7])))
    ups = [x for x in order if x[0] == "up"]
    assert [u[1] for u in ups] == ["v1", "v2", "v3", "v4", "v5"], "order kept"
    first_up = order.index(("up", "v1"))
    assert sum(1 for x in order[:first_up] if x[0] == "down") >= 3, "next files downloaded ahead"


def test_buy_premium_flow(hubenv):
    from test_hub import OWNER, datas, login, press, say
    from tgclone import ui
    hub = hubenv.new_hub()
    login(hub, OWNER, "+91000000001", env=hubenv)
    login(hub, 5, "+91000000005", env=hubenv)
    hub.owner_username = "the_owner"
    ev = press(hub, "gp", 5)
    text, rows = ev.edits[-1]
    assert "Go Premium" in text and "Trial" in text
    assert {"bp:7", "bp:30", "bp:90"} <= datas(rows)
    urls = [getattr(b.type, "url", None) for row in rows for b in row]
    assert "https://t.me/the_owner" in urls
    ev = press(hub, "bp:30", 5)
    assert "Request sent" in ev.edits[-1][0] and "₹149" in ev.edits[-1][0]
    req = [s for s in hub.bot.sent if s[0] == OWNER and "Premium request" in s[1]]
    assert len(req) == 1 and "apa:5:30" in datas(req[0][2])
    press(hub, "bp:30", 5)
    assert len([s for s in hub.bot.sent if s[0] == OWNER and "Premium request" in s[1]]) == 1, "no spam"
    ev = press(hub, "bp:999", 5)
    assert ev.answers[-1][1] is True, "unknown plan refused"
    press(hub, "apa:5:30", OWNER)  # owner taps Grant
    assert hub.plan_status(5)["kind"] == "premium"
    # owner edits plans and payment info
    press(hub, "apP", OWNER)
    ev = say(hub, "nonsense", OWNER)
    assert "days=price" in ev.out[-1][0]
    say(hub, "1=₹10, 365d=₹999", OWNER)
    assert hub.settings.plans == "1=₹10, 365=₹999"
    press(hub, "apI", OWNER)
    say(hub, "UPI: owner@upi", OWNER)
    hub._buy_asked.clear()
    ev = press(hub, "bp:365", 5)
    assert "owner@upi" in ev.edits[-1][0]
    assert ui.parse_plans("7=₹49, bad, 0=x, 30 = $5") == [(7, "₹49"), (30, "$5")]


def test_every_telegram_style_survives_plugins():
    """Quote (incl. expandable), spoiler, underline, bold, link, premium emoji: kept after plugins."""
    from telethon.extensions import html as tl_html
    from fakes import Client as C
    from tgclone.plugins import apply_plugins, load_plugins
    text = "Quote line\nhidden secret under bold link 😀"
    ents = [types.MessageEntityBlockquote(0, 10, collapsed=True),
            types.MessageEntitySpoiler(18, 6), types.MessageEntityUnderline(25, 5),
            types.MessageEntityBold(31, 4), types.MessageEntityTextUrl(36, 4, url="https://x.y"),
            types.MessageEntityCustomEmoji(41, 2, document_id=5)]
    m = Msg(1, text, chat_id=-1)
    m.message, m.entities = text, ents
    cfg = Config()
    cfg.plugins.caption.check, cfg.plugins.caption.footer = True, "\n— via <b>MyChan</b> & co"
    cfg.plugins.replace.check, cfg.plugins.replace.text = True, {"secret": "SECRET", "a": "@"}
    tm = run(apply_plugins(load_plugins(cfg.plugins), CloneMessage(m)))
    sent = {}

    class Premium(C):
        async def get_me(self):
            return _t.SimpleNamespace(premium=True, bot=False)

        async def send_message(self, dest, text, file=None, formatting_entities=None, parse_mode=None, **kw):
            sent.update(text=text, ents=formatting_entities, pm=parse_mode)
            return await super().send_message(dest, text, file=file, **kw)
    run(send(Premium(), 7, [tm], cfg))
    assert sent["pm"] == "html" and sent["ents"] is None
    plain, out = tl_html.parse(sent["text"])
    kinds = {type(e).__name__ for e in out}
    assert {"MessageEntityBlockquote", "MessageEntitySpoiler", "MessageEntityUnderline",
            "MessageEntityBold", "MessageEntityTextUrl", "MessageEntityCustomEmoji"} <= kinds
    assert any(getattr(e, "collapsed", False) for e in out), "expandable quote stays expandable"
    assert "SECRET" in plain and "— via MyChan & co" in plain
    assert 'href="https://x.y"' in sent["text"], "replace never touches tags / links"
    # untouched posts: Telegram's own entities are sent as they are
    m2 = Msg(2, text, chat_id=-1)
    m2.message, m2.entities = text, ents
    run(send(Premium(), 7, [CloneMessage(m2)], Config()))
    assert sent["ents"] == ents and sent["text"] == text


def test_buttons_one_icon_and_colours():
    from telethon import Button
    from tgclone import emoji
    emoji.set_enabled(True)
    rows = [[Button.inline("✅ 🗂 Topics", b"a", style="success"), Button.inline("▫️ Via bot", b"b")],
            [Button.inline("➖", b"c"), Button.inline("« Back", b"m"), Button.inline("🔄 Refresh", b"r")],
            [Button.inline("❌ Cancel", b"x", style="danger")]]
    out = emoji.decorate(("x", emoji.paint(rows)))[1]
    topics, via = out[0]
    assert topics.text == "Topics" and topics.style.bg_success, "one icon, no second emoji, ON = green"
    assert via.style.bg_primary and via.style.icon == emoji.PACK["⚫"], "OFF = blue with a neutral icon"
    minus, back, refresh = out[1]
    assert minus.text == emoji.BLANK and minus.style.icon == emoji.PACK["➖"], "emoji-only label not doubled"
    assert back.style.bg_danger and refresh.style.bg_success
    assert out[2][0].style.bg_danger


def test_start_text_and_premium_jobs(hubenv):
    from test_hub import OWNER, login, press, say
    hub = hubenv.new_hub()
    login(hub, OWNER, "+91000000001", env=hubenv)
    ev = say(hub, "/start", 9)
    assert "Welcome" in ev.out[-1][0] and '<a href="tg://user?id=9">' in ev.out[-1][0]
    press(hub, "ast", OWNER)
    ev = say(hub, "🤝 Hello {name}, plan: {plan} {oops}", OWNER)
    assert "Unknown placeholder" in ev.out[-1][0]
    say(hub, "🤝 Hello {name} on {bot} ({plan})", OWNER)
    assert "{name}" in hub.settings.start_text
    ev = say(hub, "/start", 9)
    text = ev.out[-1][0]
    assert 'Hello <a href="tg://user?id=9">User9</a>' in text and hub.bot_name in text
    assert hub.settings.user_max_jobs == 1 and hub.settings.premium_max_jobs == 3
    press(hub, "adp3:+", OWNER)
    assert hub.settings.premium_max_jobs == 4


def test_restart_reaches_chats_without_reading_all_dialogs():
    from fakes import Client as C

    class Acc(C):
        def __init__(self):
            super().__init__()
            self.session = _t.SimpleNamespace(_entities=set())

        async def get_input_entity(self, pid):
            for row in self.session._entities:
                if row[0] == pid:
                    return types.InputPeerChannel(channel_id=abs(pid) % 10 ** 10, access_hash=row[1])
            if pid == -1001:
                return types.InputPeerChannel(channel_id=1, access_hash=777)
            raise ValueError("Could not find the input entity")
    a = make_app(Acc())
    run(a.remember(-1001, -1009))
    assert a.store.peers() == [(-1001, 777)], "only reachable chats are saved"
    b = make_app(Acc())
    b.store = a.store
    b.preload_peers()
    assert (-1001, 777, None, None, None) in b.reader.session._entities, "known right after restart"


def test_restoring_users_get_a_wait_message(hubenv):
    from test_hub import press, say
    hub = hubenv.new_hub()
    hub.restoring = {5}
    ev = say(hub, "/start", 5)
    assert "just restarted" in ev.out[-1][0]
    ev = press(hub, "fl:0", 5)
    assert "just restarted" in ev.answers[-1][0]
    hub.restoring.clear()
    assert "just restarted" not in say(hub, "/start", 5).out[-1][0]


def test_destinations_are_sent_at_the_same_time():
    from fakes import Client as C
    active, peak = [0], [0]

    class Slow(C):
        async def send_file(self, dest, f, caption=None, **kw):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            await asyncio.sleep(0.05)
            active[0] -= 1
            return await super().send_file(dest, f, caption=caption, **kw)
    downloads = []

    class M(Msg):
        async def download_media(self, file, thumb=None, progress_callback=None):
            if thumb is None:
                downloads.append(1)
            await asyncio.sleep(0.01)
            return await super().download_media(file, thumb, progress_callback)
    a = make_app(Slow())
    m = M(1, "v", media="M", kind="video", noforwards=True, chat_id=-1)
    n = run(a.deliver(-1, [CloneMessage(m)], [7, 8, 9]))
    assert n == 3 and peak[0] == 3, "3 destinations in parallel"
    assert len(downloads) == 1, "the file is downloaded once for all destinations"


def _wire_plain(a, msgs, protected=False):
    c = a.reader

    async def it(src, reverse, min_id, max_id):
        for m in msgs:
            if m.id > min_id:
                yield m

    async def gm(chat, ids=None, limit=None):
        return [msgs[-1]]

    async def ent(cid):
        return _t.SimpleNamespace(title="Src", noforwards=protected)

    async def resolve(ref):
        return int(ref)
    c.iter_messages, c.get_messages, c.get_entity, a.resolve = it, gm, ent, resolve


def test_protected_chat_detected_once_and_prefetched():
    """Real log: posts without the noforwards flag in a protected group -> a doomed try per post."""
    from fakes import Client as C
    from tgclone import app as appmod
    tries, order = [], []

    class Restricted(C):
        async def send_message(self, dest, text, file=None, **kw):
            from telethon.errors import ChatForwardsRestrictedError
            if file is not None:
                tries.append(1)
                raise ChatForwardsRestrictedError(None)
            return await super().send_message(dest, text, file=file, **kw)

        async def send_file(self, dest, f, caption=None, **kw):
            order.append(("up", caption))
            return await super().send_file(dest, f, caption=caption, **kw)

    class M(Msg):
        async def download_media(self, file, thumb=None, progress_callback=None):
            if thumb is None:
                order.append(("down", self.id))
            await asyncio.sleep(0.01)
            return await super().download_media(file, thumb, progress_callback)
    a = make_app(Restricted())
    msgs = [M(i, f"v{i}", media="M", kind="video", chat_id=-1) for i in range(1, 6)]
    _wire_plain(a, msgs, protected=True)
    run(appmod.run_forward(a, Forward(source=-1, dest=[7])))
    assert not tries, "protected source detected up front: no doomed reference tries"
    first_up = order.index(("up", "v1"))
    assert sum(1 for x in order[:first_up] if x[0] == "down") >= 3, "prefetch works for chat-level protection"


def test_long_caption_goes_under_the_media():
    from fakes import Client as C
    from telethon.errors import MediaCaptionTooLongError
    long_text = "x" * 2000

    class Normal(C):
        async def get_me(self):
            return _t.SimpleNamespace(premium=False, bot=False)

        async def send_file(self, dest, f, caption=None, **kw):
            if caption and len(caption) > 1024:
                raise MediaCaptionTooLongError(None)
            return await super().send_file(dest, f, caption=caption, **kw)

        async def send_message(self, dest, text="", file=None, **kw):
            if file is not None and len(text) > 1024:
                raise MediaCaptionTooLongError(None)
            return await super().send_message(dest, text, file=file, **kw)
    c = Normal()
    out = run(send(c, 7, [CloneMessage(Msg(1, long_text, media="M", kind="video", noforwards=True))], Config()))
    media_call, caption_call = c.calls[0], c.calls[1]
    assert media_call[0] == "file" and not media_call[3], "media without caption"
    assert caption_call[0] == "msg" and caption_call[2] == long_text and caption_call[4] == out[0].id
    c2 = Normal()
    run(send(c2, 7, [CloneMessage(Msg(2, long_text, media="M", kind="photo"))], Config()))
    assert c2.calls[0][3] == "M" and c2.calls[1][2] == long_text, "reference copy too"


def test_telegram_timeouts_are_retried_not_dropped(monkeypatch):
    from fakes import Client as C
    from tgclone import app as appmod
    from tgclone.jobs import Job, current_job
    real_sleep = asyncio.sleep
    waits = []

    async def no_wait(sec):
        waits.append(sec)
        await real_sleep(0)
    monkeypatch.setattr(appmod.asyncio, "sleep", no_wait)
    fails = {"n": 2}

    class Flaky(C):
        async def send_message(self, dest, text, file=None, **kw):
            if fails["n"]:
                fails["n"] -= 1
                raise ValueError("Request was unsuccessful 6 time(s)")
            return await super().send_message(dest, text, file=file, **kw)
    a = make_app(Flaky())
    assert run(a.deliver(-1, [CloneMessage(Msg(1, "hi", chat_id=-1))], [7])) == 1
    assert waits[:2] == [10, 30], "waits 10 s, then 30 s, then succeeds"

    class Broken(C):
        async def send_message(self, dest, text, file=None, **kw):
            raise ValueError("MESSAGE_EMPTY")
    b = make_app(Broken())
    job = Job("clone", "t", "k")

    async def go():
        current_job.set(job)
        return await b.deliver(-1, [CloneMessage(Msg(9, "x", chat_id=-1))], [7])
    assert run(go()) == 0 and job.failed_ids == [9] and "Post 9 not sent" in job.note
    job.state = "done"
    assert "Not sent: 9" in job.render()


def test_bot_not_in_destination_is_tried_once():
    from fakes import Client as C

    class Acc(C):
        async def get_me(self):
            return _t.SimpleNamespace(premium=False, bot=False)

    class Bot(C):
        looked = 0

        async def get_input_entity(self, ref):
            Bot.looked += 1
            raise ValueError("Could not find the input entity")
    a = make_app(Acc())
    a.bot = Bot()
    for i in (1, 2):
        m = Msg(i, "hi 😀", chat_id=-1)
        m.entities = [types.MessageEntityCustomEmoji(3, 2, document_id=5)]
        run(a.deliver(-1, [CloneMessage(m)], [7]))
    assert Bot.looked == 1 and len(a.reader.calls) == 2 and not a.bot.calls


def test_job_card_shows_what_is_happening():
    from tgclone import jobs
    j = jobs.Job("clone", "t", "k")
    token = jobs.current_job.set(j)
    jobs.stage("⬇️ Downloading post 2477")
    jobs.note("Caption too long for this account — posted as a reply under the media")
    jobs.current_job.reset(token)
    text = j.render()
    assert "Downloading post 2477" in text and "Caption too long" in text
    jobs.stage("outside a job does nothing")
    assert "outside" not in j.render()
