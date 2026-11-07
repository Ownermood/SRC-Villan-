"""Plans, block list, without-login mode, pins, start photo, premium emoji, admin panel."""

import asyncio
import io
import time
import types as _t

from PIL import Image
from telethon import Button
from telethon.tl import types

from fakes import Msg, TextEvent, make_app
from test_hub import OWNER, datas, login, press, say
from tgclone import app as appmod, emoji, live, ui
from tgclone.config import Forward

run = asyncio.run


def png():
    b = io.BytesIO()
    Image.new("RGB", (800, 400), (10, 120, 200)).save(b, "PNG")
    return b.getvalue()


# ------------------------------------------------------------------ premium emoji
def test_emoji_decorate_and_fallback():
    emoji.set_enabled(True)
    text, buttons = emoji.decorate(("⚡ Hi ⚙️ <code>⚡</code>", [[Button.inline("⚙️ Settings", b"st", style="primary")]]))
    assert text.count("<tg-emoji") == 2, "code blocks untouched"
    b = buttons[0][0]
    assert b.text == "Settings" and b.style.icon == emoji.PACK["⚙"] and b.style.bg_primary
    emoji.disable_for_session()
    assert emoji.decorate(("⚡", None)) == ("⚡", None)
    emoji.set_enabled(True)
    assert emoji.is_emoji_error(Exception("DOCUMENT_INVALID")) and not emoji.is_emoji_error(Exception("FLOOD"))


def test_hub_falls_back_to_plain_emoji_when_refused(hubenv):
    hub = hubenv.new_hub()
    emoji.set_enabled(True)
    calls = []

    class Ev(TextEvent):
        async def respond(self, text, **kw):
            calls.append(text)
            if "tg-emoji" in text:
                raise Exception("DOCUMENT_INVALID (caused by SendMessageRequest)")
            return await super().respond(text, **kw)
    run(hub._show(Ev("x", 5), ("⚡ hello", None)))
    assert len(calls) == 2 and "tg-emoji" not in calls[1] and not emoji.active()
    emoji.set_enabled(True)


# ------------------------------------------------------------------ plans
def test_trial_expiry_grant_and_gating(hubenv):
    hub = hubenv.new_hub()
    login(hub, 5, "+91000000005", env=hubenv)
    st = hub.plan_status(5)
    assert st["kind"] == "trial" and 23 * 3600 < st["left"] <= 24 * 3600, "24h free trial"
    assert any(s[0] == 5 and "free trial" in s[1] for s in hub.bot.sent)
    # expire it
    hub.acc.set_plan(5, time.time() - 5, "trial")
    run(hub.check_plans())
    assert hub.ws[5][0].paused and any("plan has ended" in s[1] for s in hub.bot.sent if s[0] == 5)
    n = len(hub.bot.sent)
    run(hub.check_plans())
    assert len(hub.bot.sent) == n, "expiry message sent once"
    ev = say(hub, "/list", 5)
    assert "plan has ended" in ev.out[-1][0], "panel blocked while expired"
    ev = press(hub, "fl:0", 5)
    assert "plan has ended" in ev.edits[-1][0]
    ev = press(hub, "ac", 5)
    assert "My profile" in ev.edits[-1][0], "profile still reachable"
    # owner grants 30 days
    login(hub, OWNER, "+91000000001", env=hubenv)
    say(hub, "/addpremium 5 30", OWNER)
    st = hub.plan_status(5)
    assert st["kind"] == "premium" and 29.9 * 86400 < st["left"] <= 30 * 86400
    assert not hub.ws[5][0].paused
    assert any(s[0] == 5 and "Premium activated" in s[1] for s in hub.bot.sent)
    say(hub, "/addpremium 5 10", OWNER)
    assert hub.plan_status(5)["left"] > 39.9 * 86400, "days stack on time left"
    ev = say(hub, "/addpremium 5 30", 5)
    assert "Owner only" in ev.out[-1][0]
    say(hub, "/rmpremium 5", OWNER)
    assert hub.plan_status(5)["kind"] == "expired" and hub.ws[5][0].paused
    assert hub.plan_status(OWNER)["kind"] == "owner"


def test_reminder_before_end_and_plans_off(hubenv):
    hub = hubenv.new_hub()
    login(hub, 5, "+91000000005", env=hubenv)
    hub.acc.set_plan(5, time.time() + 3600, "premium")
    run(hub.check_plans())
    assert any(s[0] == 5 and "ends in" in s[1] for s in hub.bot.sent)
    login(hub, OWNER, "+91000000001", env=hubenv)
    hub.acc.set_plan(5, time.time() - 1, "premium")
    run(hub.check_plans())
    press(hub, "apt", OWNER)  # plans off -> everyone free
    assert hub.plan_status(5)["kind"] == "free" and not hub.ws[5][0].paused


def test_admin_premium_wizard_and_username(hubenv):
    hub = hubenv.new_hub()
    login(hub, OWNER, "+91000000001", env=hubenv)
    press(hub, "apg", OWNER)
    assert hub.admin_wait == {"kind": "premium"}
    ev = say(hub, "@rahul 7", OWNER)
    assert hub.plan_status(5021)["kind"] == "premium" and "+7 days" in ev.out[-1][0]
    assert hub.admin_wait is None
    press(hub, "apg", OWNER)
    ev = say(hub, "abc", OWNER)
    assert "user_id days" in ev.out[-1][0] and hub.admin_wait, "bad input keeps asking"
    press(hub, "apa:5021:1", OWNER)
    assert hub.plan_status(5021)["left"] > 7.9 * 86400


# ------------------------------------------------------------------ block list
def test_block_list_by_id_folder_invite_and_enforcement(hubenv):
    hub = hubenv.new_hub()
    login(hub, OWNER, "+91000000001", env=hubenv)
    login(hub, 5, "+91000000005", env=hubenv)
    owner_client = hub.ws[OWNER][0].user

    async def call(req):
        name = type(req).__name__
        if name == "CheckChatlistInviteRequest":
            return _t.SimpleNamespace(chats=[types.Channel(id=111, title="Movies", photo=types.ChatPhotoEmpty(),
                                                           date=None, access_hash=1),
                                             types.Channel(id=222, title="Songs", photo=types.ChatPhotoEmpty(),
                                                           date=None, access_hash=2)])
        if name == "CheckChatInviteRequest":
            if req.hash == "unknown":
                return _t.SimpleNamespace()
            return _t.SimpleNamespace(chat=types.Channel(id=333, title="Private", photo=types.ChatPhotoEmpty(),
                                                         date=None, access_hash=3))
    owner_client.__call__ = None
    type(owner_client).__call__ = lambda self, req: call(req)
    press(hub, "abk", OWNER)
    say(hub, "https://t.me/addlist/AbCdEf", OWNER)
    assert hub.acc.blocked_ids() == {-1000000000111, -1000000000222}
    say(hub, "/block https://t.me/+secretHash", OWNER)
    assert -1000000000333 in hub.acc.blocked_ids()
    ev = say(hub, "/block https://t.me/+unknown", OWNER)
    assert "can't see the id" in ev.out[-1][0]
    say(hub, "/block -100999", OWNER)
    ev = say(hub, "/add -100999 -100222", 5)
    assert "blocked by the owner" in ev.out[-1][0] and hub.ws[5][0].cfg.forwards == []
    say(hub, "/add -100999 -100222", OWNER)
    assert len(hub.ws[OWNER][0].cfg.forwards) == 1, "owner is never blocked"
    press(hub, "aub:-100999", OWNER)
    ev = say(hub, "/add -100999 -100222", 5)
    assert len(hub.ws[5][0].cfg.forwards) == 1
    # blocking later removes it from live forwarding
    say(hub, "/block -100999", OWNER)
    assert -100999 not in hub.ws[5][0].from_to


# ------------------------------------------------------------------ without login
def test_without_login_mode(hubenv):
    hub = hubenv.new_hub()
    ev = press(hub, "bm", 5)
    app = hub.ws[5][0]
    assert app.botmode and app.user is None and app.reader is hub.bot
    assert hub.plan_status(5)["kind"] == "trial"
    n_handlers = len(hub.bot.handlers)
    assert n_handlers >= 8, "live handlers registered on the shared bot"
    bot = hub.bot
    ev = say(hub, "/add -100111 -100222", 5)
    assert "Add me as admin" in ev.out[-1][0]
    bot.perms[(-100111, "me")] = bot.perms[(-100222, "me")] = True
    ev = say(hub, "/add -100111 -100222", 5)
    assert "must be an admin" in ev.out[-1][0]
    bot.perms[(-100111, 5)] = bot.perms[(-100222, 5)] = True
    bot.protected.add(-100111)
    ev = say(hub, "/add -100111 -100222", 5)
    assert "protected" in ev.out[-1][0]
    bot.protected.clear()
    say(hub, "/add -100111 -100222", 5)
    assert len(app.cfg.forwards) == 1
    ev = press(hub, f"fc:0:{ui.fhash(app.cfg.forwards[0])}", 5)
    assert any("needs a login" in a[0] for a in ev.answers if a[0])
    ev = say(hub, "https://t.me/c/123/5", 5)
    assert "needs a login" in ev.out[-1][0]
    # restart restores bot-only users
    hub2 = hubenv.new_hub()
    assert hub2.ws[5][0].botmode and len(hub2.ws[5][0].cfg.forwards) == 1
    # upgrade to login keeps settings and removes bot handlers
    login(hub, 5, "+91000000005", env=hubenv)
    assert not hub.ws[5][0].botmode and len(hub.ws[5][0].cfg.forwards) == 1
    assert len(hub.bot.handlers) == n_handlers - 5
    assert 5 not in hub.acc.botmode_users()


# ------------------------------------------------------------------ pins
def test_pin_sync_deletes_notice_and_history_pins():
    a = make_app()
    a.from_to = {-1000000000001: [7]}  # channel id 1 as a marked peer id
    a.store.add(-1000000000001, 10, 7, 500)
    live.register(a)
    h = {type(ev).__name__: fn for fn, ev in a.reader.handlers}
    run(h["Raw"](types.UpdatePinnedChannelMessages(channel_id=1, messages=[10], pts=1, pts_count=1, pinned=True)))
    pins = [c for c in a.reader.calls if c[0] in ("pin", "del")]
    assert pins[0] == ("pin", 7, 500) and pins[1][0] == "del", "pin notice deleted right away"
    run(h["Raw"](types.UpdatePinnedChannelMessages(channel_id=1, messages=[10], pts=1, pts_count=1, pinned=False)))
    assert ("unpin", 7, 500) in a.reader.calls
    # history clone copies pins in order
    b = make_app()
    msgs = [Msg(1, "hello"), Msg(2, "", reply=1)]
    msgs[1].action = types.MessageActionPinMessage()
    c = b.reader

    async def it(src, reverse, min_id, max_id):
        for m in msgs:
            yield m

    async def gm(chat, ids=None, limit=None):
        return [msgs[-1]]

    async def resolve(ref):
        return int(ref)
    c.iter_messages, c.get_messages, b.resolve = it, gm, resolve
    run(appmod.run_forward(b, Forward(source=-1, dest=[7])))
    assert ("pin", 7, b.store.get_one(-1, 1, 7)) in c.calls
    assert [x for x in c.calls if x[0] == "del"], "notice removed"


# ------------------------------------------------------------------ start photo & admin
def test_start_photo_set_and_shown(hubenv):
    hub = hubenv.new_hub()
    login(hub, OWNER, "+91000000001", env=hubenv)
    press(hub, "aps", OWNER)
    say(hub, "", OWNER, image=png())
    assert hub.settings.start_photo.endswith("start_photo.jpg") and hub.acc.settings().start_photo
    say(hub, "/start", 9)
    last = hub.bot.sent[-1]
    assert last[0] == 9 and len(last) == 4 and "tgclone" in last[1], "photo with the welcome as caption"
    say(hub, "/start", 9)
    assert hub.bot.sent[-1][3] == "UPLOADED", "uploaded once, then reused"
    press(hub, "aps", OWNER)
    say(hub, "https://example.com/banner.jpg", OWNER)
    assert hub.settings.start_photo == "https://example.com/banner.jpg"
    press(hub, "apd", OWNER)
    assert hub.settings.start_photo == ""


def test_admin_panel_sections_owner_only(hubenv):
    hub = hubenv.new_hub()
    login(hub, OWNER, "+91000000001", env=hubenv)
    login(hub, 5, "+91000000005", env=hubenv)
    ev = press(hub, "ad", OWNER)
    assert {"adu:0", "adp", "apm", "abl", "aac", "alm", "asp", "aem"} <= datas(ev.edits[-1][1])
    styles = {b.type.data.decode(): b.style for row in ev.edits[-1][1] for b in row}
    assert styles["apm"].bg_danger, "admin sections are red"
    for key in ("ad", "apm", "abl", "aac", "alm", "asp", "adu:0", "adp"):
        ev = press(hub, key, 5)
        assert ev.answers[-1] == ("🚫 Owner only", True), key
    press(hub, "adt:+", OWNER)
    press(hub, "atr:+", OWNER)
    assert hub.settings.max_total_jobs == 11 and hub.settings.trial_hours == 48
    press(hub, "aem", OWNER)
    assert not hub.settings.premium_emoji and not emoji.active()
    press(hub, "aem", OWNER)
    assert emoji.active()
    ev = press(hub, "adv:5", OWNER)
    assert {"apa:5:1", "apa:5:7", "apa:5:30", "adb:5", "adl:5"} <= datas(ev.edits[-1][1])


def test_all_screens_serialize():
    from telethon import TelegramClient
    from telethon.extensions import html as tl_html
    from telethon.sessions import MemorySession
    tc = TelegramClient(MemorySession(), 1, "x")
    hub = _t.SimpleNamespace(settings=__import__("tgclone.accounts", fromlist=["x"]).HubSettings(),
                             acc=None, ws={}, owner=1, started=time.time())
    screens = [ui.welcome(), ui.keypad("12"), ui.login_password(), ui.admin_prompt("photo"),
               ui.admin_prompt("block"), ui.admin_prompt("premium"), (ui.help_text(), None), (ui.HOW, None)]
    for scr in screens:
        for text, buttons in (scr, emoji.decorate(scr)):
            tl_html.parse(text)
            if buttons:
                assert bytes(tc.build_reply_markup(buttons))
    assert hub
