import asyncio

from telethon import events, types

from fakes import CallbackEvent, TextEvent, make_app
from tgclone import bot, ui
from tgclone.config import Forward

run = asyncio.run


def panel(app=None):
    a = app or make_app()
    p = run(bot.setup(a, live=True))
    return a, p


async def press(p, data, sender=1):
    ev = CallbackEvent(data, sender)
    try:
        await p.guard(p.callback)(ev)
    except events.StopPropagation:
        pass
    return ev


async def say(p, text, sender=1, fwd_from=None):
    ev = TextEvent(text, sender, fwd_from)
    m = p.c.handlers[0][1].pattern(text) if text.startswith("/") else None
    try:
        if m:
            ev.pattern_match = m
            await p.c.handlers[0][0](ev)
        else:
            await p.guard(p.private_text)(ev)
    except events.StopPropagation:
        pass
    return ev


def styles(buttons):
    return {b.text: (b.style.bg_success and "success" or b.style.bg_danger and "danger"
                     or b.style.bg_primary and "primary") if b.style else None
            for row in buttons for b in row}


def test_main_menu_colored_buttons():
    a, p = panel()
    text, buttons = run(ui.main_menu(a))
    s = styles(buttons)
    assert "tgclone" in text and "<blockquote>" in text
    assert s["📡 Forwards"] == "primary" and s["➕ New forward"] == "success"
    assert all(len(b.type.data) <= 64 for row in buttons for b in row)


def test_new_forward_wizard_by_forwarded_message():
    a, p = panel()
    run(press(p, "na"))
    assert p.state[1]["kind"] == "new_src"
    fwd = types.MessageFwdHeader(date=None, from_id=types.PeerChannel(555))
    ev = run(say(p, "", fwd_from=fwd))
    assert p.state[1]["src"] == -1000000000555 and "step 2/2" in ev.out[-1][0]
    run(say(p, "@dest_chan"))
    run(say(p, "https://t.me/c/777"))
    assert p.state[1]["dests"] == ["@dest_chan", -100777]
    ev = run(press(p, "nd"))
    assert a.cfg.forwards[0].source == -1000000000555 and a.cfg.forwards[0].dest == ["@dest_chan", -100777]
    assert 1 not in p.state and "Forward saved" in ev.out[0][0]


def test_wizard_rejects_bad_input_and_invite_links():
    a, p = panel()
    run(press(p, "na"))
    ev = run(say(p, "hello there"))
    assert "Couldn't read a chat" in ev.out[-1][0]
    ev = run(say(p, "https://t.me/+AbCdEf123"))
    assert "Invite links" in ev.out[-1][0]
    run(press(p, "x"))
    assert 1 not in p.state


def test_forward_view_toggle_delete_and_stale_guard():
    a, p = panel()
    a.cfg.forwards = [Forward(source=-1001, dest=[-1002]), Forward(source=-1003, dest=[-1004])]
    h = ui.fhash(a.cfg.forwards[0])
    ev = run(press(p, f"fv:0:{h}"))
    assert "#1" in ev.edits[-1][0]
    s = styles(ev.edits[-1][1])
    assert s["🗑 Delete"] == "danger" and s["🔁 Clone history"] == "primary"
    run(press(p, f"ft:0:{h}"))
    assert a.cfg.forwards[0].enabled is False
    ev = run(press(p, f"fd:0:{h}"))
    assert "Are you sure" in ev.edits[-1][0] and len(a.cfg.forwards) == 2
    h0 = ui.fhash(a.cfg.forwards[0])
    a.cfg.forwards.insert(0, Forward(source=-1009, dest=[-1010]))  # list changed meanwhile
    ev = run(press(p, f"fD:0:{h0}"))
    assert len(a.cfg.forwards) == 3, "stale button must not delete the wrong forward"
    assert ev.answers and ev.answers[0][1] is True
    h1 = ui.fhash(a.cfg.forwards[1])
    run(press(p, f"fD:1:{h1}"))
    assert [f.source for f in a.cfg.forwards] == [-1009, -1003]


def test_non_admin_blocked():
    a, p = panel()
    ev = run(press(p, "m", sender=999))
    assert ev.answers == [("⛔ Not authorized", True)] and not ev.edits
    ev = run(say(p, "/list", sender=999))
    assert ev.out == [("⛔ Not authorized.", None, None)]


def test_plugins_and_filetypes():
    a, p = panel()
    run(press(p, "pt:caption"))
    assert a.cfg.plugins.caption.check
    run(press(p, "ps"))
    assert a.cfg.plugins.fmt.style == "plain" and a.cfg.plugins.fmt.check
    ev = run(press(p, "pff:sticker"))
    assert "sticker" in a.cfg.plugins.filter.files.blacklist and a.cfg.plugins.filter.check
    assert any(b.text.startswith("🚫") for row in ev.edits[-1][1] for b in row)
    assert {x.id_ for x in a.plugins} >= {"filter", "fmt", "caption"}, "reloaded without restart"
    run(press(p, "pw:repl"))
    run(say(p, "foo => bar\nbad word =>"))
    assert a.cfg.plugins.replace.text == {"foo": "bar", "bad word": ""} and a.cfg.plugins.replace.check
    run(press(p, "pw:black"))
    run(say(p, "spam\nads"))
    assert a.cfg.plugins.filter.text.blacklist == ["spam", "ads"]
    run(press(p, "pw:foot"))
    run(say(p, "Join @mychan"))
    assert a.cfg.plugins.caption.footer == "\nJoin @mychan"


def test_settings():
    a, p = panel()
    run(press(p, "sx:forward_mode"))
    assert a.cfg.forward_mode
    run(press(p, "sx:delete_sync"))
    assert a.cfg.live.delete_sync
    run(press(p, "sd:past:+"))
    assert a.cfg.past.delay == 0.5
    run(press(p, "sd:past:-"))
    assert a.cfg.past.delay == 0
    run(press(p, "sg"))
    run(say(p, "-100888"))
    assert a.cfg.save.target == -100888
    run(press(p, "sgc"))
    assert a.cfg.save.target == ""


def test_link_save_starts_job_with_live_card(monkeypatch):
    a, p = panel()
    started = []

    async def fake_save(app, chat, first, last, target, job):
        started.append((chat, first, last, target))
        job.done = last - first + 1
    monkeypatch.setattr(bot, "save_range", fake_save)

    async def go():
        ev = await say(p, "https://t.me/c/555/10-12")
        await asyncio.sleep(0.05)
        return ev
    ev = run(go())
    assert started == [(-100555, 10, 12, 1)]
    assert "⏳" in ev.out[0][0] or "✅" in ev.out[0][0]


def test_text_commands_still_work():
    a, p = panel()
    run(say(p, "/add -100111 -100222 @dst"))
    assert a.cfg.forwards[0].dest == [-100222, "@dst"]
    ev = run(say(p, "/remove #9"))
    assert "No such forward" in ev.out[-1][0]
    ev = run(say(p, "/stop"))
    assert "Nothing running" in ev.out[-1][0]
    ev = run(say(p, "/style neon"))
    assert "Choose" in ev.out[-1][0]


def test_userbot_mode_has_no_buttons():
    a = make_app()
    a.bot = None
    a.control = a.user
    a, p = panel(a)
    assert not p.is_bot
    ev = TextEvent(".menu")
    ev.pattern_match = p.c.handlers[0][1].pattern(".menu")
    try:
        run(p.c.handlers[0][0](ev))
    except events.StopPropagation:
        pass
    assert ev.out and ev.out[0][1] is None


def test_screens_serialize_for_telegram():
    """Real Telethon serializer: every screen must build valid HTML + reply markup bytes."""
    from telethon import TelegramClient
    from telethon.extensions import html as tl_html
    from telethon.sessions import MemorySession
    a, p = panel()
    a.cfg.forwards = [Forward(source=-1001, dest=[-1002])]
    client = TelegramClient(MemorySession(), 1, "x")
    screens = [run(ui.main_menu(a)), run(ui.forwards_list(a)), run(ui.forward_view(a, 0)),
               ui.jobs_view(a), ui.plugins_view(a), ui.filetypes_view(a), ui.settings_view(a),
               (ui.HELP, None), ui.wizard_prompt("new_src", a), ui.wizard_prompt("repl", a)]
    for text, buttons in screens:
        plain, ents = tl_html.parse(text)
        assert plain and "<b>" not in plain
        if buttons:
            assert len(bytes(client.build_reply_markup(buttons))) > 0
    _, ents = tl_html.parse(ui.HELP)
    assert any(getattr(e, "collapsed", False) for e in ents), "expandable blockquote"
