"""Multi-user bot: login inside Telegram, isolation, access control, persistence."""

import asyncio
import types as _t

import pytest
from telethon import events
from telethon.errors import PasswordHashInvalidError, PhoneCodeInvalidError, SessionPasswordNeededError

from fakes import CallbackEvent, Client, TextEvent

run = asyncio.run
OWNER = 1


class Bot(Client):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.perms = {}  # (chat, user or "me") -> is admin
        self.protected = set()
        self.users = {"@rahul": 5021}

    async def send_message(self, chat, text, buttons=None, **kw):
        self.sent.append((chat, text, buttons))
        return self._m()

    async def send_file(self, chat, file, caption=None, buttons=None, **kw):
        self.sent.append((chat, caption, buttons, file))
        m = self._m()
        m.media = "UPLOADED"
        return m

    async def get_permissions(self, chat, user):
        if (chat, user) not in self.perms:
            raise ValueError("not a participant")
        adm = self.perms[(chat, user)]
        return _t.SimpleNamespace(is_admin=adm, is_creator=False)

    async def get_entity(self, ref):
        if isinstance(ref, str) and ref in self.users:
            return _t.SimpleNamespace(id=self.users[ref], first_name="Rahul", last_name=None)
        if isinstance(ref, str):
            raise ValueError("no user")
        return _t.SimpleNamespace(title=f"Chat{ref}", noforwards=ref in self.protected)

    def remove_event_handler(self, fn):
        self.handlers = [h for h in self.handlers if h[0] is not fn]

    async def get_me(self):
        return _t.SimpleNamespace(id=999, username="tgclone_bot", bot=True)


class UserClient(Client):
    """Fake Telegram account. Code 12345, optional 2FA password 'pw'."""
    registry = {}

    def __init__(self, session=""):
        super().__init__()
        self.session_str, self.authorized = session, bool(session)
        self.connected = self.logged_out = False
        self.twofa = False
        self.session = _t.SimpleNamespace(save=lambda: self.session_str)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return self.authorized and not UserClient.registry.get(self.session_str, {}).get("revoked")

    async def send_code_request(self, phone):
        self.phone = phone
        return _t.SimpleNamespace(phone_code_hash="HASH")

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        if password is not None:
            if password != "pw":
                raise PasswordHashInvalidError(None)
        else:
            if code != "12345" or phone_code_hash != "HASH":
                raise PhoneCodeInvalidError(None)
            if self.twofa:
                raise SessionPasswordNeededError(None)
        self.authorized = True
        self.session_str = f"SESSION{self.phone}"
        UserClient.registry[self.session_str] = {}

    async def get_me(self):
        return _t.SimpleNamespace(id=int(self.phone[-3:]) if hasattr(self, "phone") else 7,
                                  first_name="Acc", last_name=self.phone[-3:] if hasattr(self, "phone") else "",
                                  username=None, bot=False)

    async def log_out(self):
        self.logged_out = True
        UserClient.registry.setdefault(self.session_str, {})["revoked"] = True

    async def get_dialogs(self):
        return []


async def _dispatch(hub, ev):
    on_cmd, on_cb, on_text = hub.on_command, hub.on_callback, hub.on_text
    pattern = next(e for fn, e in hub.bot.handlers if fn == hub.on_command).pattern
    try:
        if isinstance(ev, CallbackEvent):
            await on_cb(ev)
        elif ev.raw_text.startswith("/"):
            ev.pattern_match = pattern(ev.raw_text)
            await on_cmd(ev)
        else:
            await on_text(ev)
    except events.StopPropagation:
        pass
    return ev


def say(hub, text, uid, **kw):
    return run(_dispatch(hub, TextEvent(text, uid, **kw)))


def press(hub, data, uid):
    return run(_dispatch(hub, CallbackEvent(data, uid)))


def contact(uid, phone):
    return _t.SimpleNamespace(user_id=uid, phone_number=phone.lstrip("+"))


def login(hub, uid, phone, twofa=False, env=None):
    if env:
        env.factory.twofa = twofa
    press(hub, "lg", uid)
    say(hub, "", uid, contact=contact(uid, phone))
    ev = say(hub, "1 2 3 4 5", uid)
    if twofa:
        say(hub, "pw", uid)
    return ev


def datas(buttons):
    return {b.type.data.decode() for row in buttons or [] for b in row
            if hasattr(b, "type") and hasattr(b.type, "data")}


def texts(ev):
    return [o[0] for o in ev.out] + [e[0] for e in getattr(ev, "edits", [])]


def test_welcome_and_login_required(hubenv):
    hub = hubenv.new_hub()
    ev = say(hub, "/start", 5)
    assert "Login with Telegram" in str(ev.out[0][1][0][0].text)
    ev = say(hub, "/list", 5)
    assert "tgclone" in ev.out[0][0], "not logged in -> welcome, never a panel"


def test_full_login_with_typed_code_and_2fa(hubenv):
    hub = hubenv.new_hub()
    ev = press(hub, "lg", 5)
    assert hub.logins[5]["step"] == "phone"
    ev = say(hub, "", 5, contact=contact(6, "+911111111"))
    assert "YOUR number" in ev.out[-1][0], "can't log in with someone else's shared contact"
    say(hub, "+91 98765 43005", 5)
    assert hub.logins[5]["step"] == "code"
    prompt = hub.logins[5]["msg"]  # every login step edits this one message
    ev = say(hub, "1 2 3", 5)
    assert not ev.out and "with spaces" in prompt.edits[-1][0] and ev.message.deleted, "too short"
    ev = say(hub, "1 2 3 4 9", 5)
    assert "Wrong code" in prompt.edits[-1][0] and ev.message.deleted, "code never stays in chat"
    ev = press(hub, "otp:r", 5)
    assert "new code" in ev.edits[-1][0].lower()
    prompt = hub.logins[5]["msg"]  # same message, now reached through the button press
    hubenv.made[-1].twofa = True
    ev = say(hub, "1-2-3-4-5", 5)
    assert hub.logins[5]["step"] == "password" and "two-step" in prompt.edits[-1][0]
    ev = say(hub, "nope", 5)
    assert ev.message.deleted, "password message is deleted"
    assert "Wrong password (1/3)" in prompt.edits[-1][0] and not ev.out
    ev = say(hub, "pw", 5)
    assert ev.message.deleted and 5 in hub.ws and 5 not in hub.logins
    assert "Logged in as" in prompt.edits[-1][0]
    raw = (hubenv.tmp / "accounts.db").read_bytes()
    assert b"SESSION+919876543005" not in raw, "session must be stored encrypted"
    assert hub.acc.sessions() == [(5, "SESSION+919876543005")]


def test_users_are_isolated(hubenv):
    hub = hubenv.new_hub()
    login(hub, 5, "+91000000005", env=hubenv)
    login(hub, 6, "+91000000006", env=hubenv)
    assert {5, 6} <= set(hub.ws)
    say(hub, "/add -100111 -100222", 5)
    assert len(hub.ws[5][0].cfg.forwards) == 1 and hub.ws[6][0].cfg.forwards == []
    assert hub.ws[5][0].home != hub.ws[6][0].home
    assert hub.ws[5][0].user is not hub.ws[6][0].user
    ev = say(hub, "/list", 6)
    assert "No forwards" in ev.out[-1][0]
    # main menu: account for everyone, admin only for owner
    ev = say(hub, "/start", 6)
    assert "ac" in datas(ev.out[-1][1]) and "ad" not in datas(ev.out[-1][1])


def test_owner_admin_approval_ban_and_modes(hubenv):
    hub = hubenv.new_hub()
    login(hub, OWNER, "+91000000001", env=hubenv)
    ev = say(hub, "/start", OWNER)
    assert "ad" in datas(ev.out[-1][1])
    ev = say(hub, "/admin", 7)
    assert "Owner only" in ev.out[-1][0]
    press(hub, "adm:approve", OWNER)
    assert hub.settings.mode == "approve" and hub.acc.settings().mode == "approve"
    ev = press(hub, "lg", 7)
    assert "Request sent" in ev.out[-1][0] and hub.acc.access(7) == "pending"
    assert hub.bot.sent[-1][0] == OWNER and "Access request" in hub.bot.sent[-1][1]
    press(hub, "lg", 7)
    assert sum("Access request" in s[1] for s in hub.bot.sent) == 1, "owner is pinged once"
    press(hub, "ap:7", OWNER)
    assert hub.acc.access(7) == "approved" and hub.bot.sent[-1][0] == 7
    login(hub, 7, "+91000000007", env=hubenv)
    assert 7 in hub.ws
    client7 = hub.ws[7][0].user
    press(hub, "adb:7", OWNER)
    assert 7 not in hub.ws and client7.logged_out and hub.acc.info(7) is None
    ev = say(hub, "/start", 7)
    assert "banned" in ev.out[-1][0]
    ev = press(hub, "lg", 7)
    assert "banned" in ev.answers[-1][0]
    press(hub, "adb:7", OWNER)
    assert hub.acc.access(7) is None
    press(hub, "adm:owner", OWNER)
    ev = press(hub, "lg", 8)
    assert "private" in ev.out[-1][0]
    ev = press(hub, "adm:open", 8)
    assert ev.answers[-1] == ("🚫 Owner only", True) and hub.settings.mode == "owner"


def test_limits_for_users_not_owner(hubenv):
    from tgclone.save import save_range
    hub = hubenv.new_hub()
    login(hub, OWNER, "+91000000001", env=hubenv)
    login(hub, 5, "+91000000005", env=hubenv)
    assert hub.ws[OWNER][0].caps is None and hub.ws[5][0].caps["max_batch"] == 0, "no limit by default"
    hub.settings.user_max_batch = 500
    hub.ws[5][0].caps = hub.caps(5)
    with pytest.raises(ValueError, match="500 posts"):
        run(save_range(hub.ws[5][0], "chan", 1, 600, 5))
    press(hub, "adj:+", OWNER)
    assert hub.caps(5)["max_jobs"] == 2, "trial: 1 + 1"
    hub.add_days(5, 30)
    assert hub.caps(5)["max_jobs"] == 3 and hub.ws[5][0].caps["max_jobs"] == 3, "premium: 3 at once"
    hub.settings.max_total_jobs = 1
    from tgclone.jobs import Job
    app = hub.ws[OWNER][0]
    ev_loop = asyncio.new_event_loop()

    async def busy():
        app.jobs.start(Job("clone", "x", "k"), lambda j: asyncio.sleep(1))
        with pytest.raises(ValueError, match="busy"):
            hub.check_capacity()
        app.jobs.stop()
    ev_loop.run_until_complete(busy())
    ev_loop.close()


def test_logout_and_delete_data(hubenv):
    hub = hubenv.new_hub()
    login(hub, 5, "+91000000005", env=hubenv)
    home = hub.ws[5][0].home
    say(hub, "/add -100111 -100222", 5)
    assert home.exists()
    client = hub.ws[5][0].user
    press(hub, "lo", 5)
    press(hub, "LO", 5)
    assert client.logged_out and 5 not in hub.ws and hub.acc.info(5) is None
    assert home.exists(), "settings kept after logout"
    press(hub, "LD", 5)
    assert not home.exists()


def test_restart_restores_sessions_and_drops_revoked(hubenv):
    hub = hubenv.new_hub()
    login(hub, 5, "+91000000005", env=hubenv)
    login(hub, 6, "+91000000006", env=hubenv)
    say(hub, "/add -100111 -100222", 5)
    UserClient.registry["SESSION+91000000006"]["revoked"] = True  # ended from Devices
    hub2 = hubenv.new_hub()
    assert 5 in hub2.ws and 6 not in hub2.ws
    assert len(hub2.ws[5][0].cfg.forwards) == 1, "forwards survive restart"
    assert hub2.acc.info(6) is None
    assert any(s[0] == 6 and "ended from Telegram" in s[1] for s in hub2.bot.sent)


def test_cancel_login_and_phone_validation(hubenv):
    hub = hubenv.new_hub()
    press(hub, "lg", 5)
    ev = say(hub, "hello", 5)
    assert "Share my number" in ev.out[-1][0]
    say(hub, "+91000000005", 5)
    client = hub.logins[5]["client"]
    ev = say(hub, "/cancel", 5)
    assert 5 not in hub.logins and not client.connected and "cancelled" in ev.out[-1][0].lower()


def test_hub_screens_serialize():
    from telethon import TelegramClient
    from telethon.extensions import html as tl_html
    from telethon.sessions import MemorySession
    from tgclone import ui
    tc = TelegramClient(MemorySession(), 1, "x")
    for text, buttons in [ui.welcome(), ui.code_prompt("x"), ui.login_password(),
                          ui.welcome(owner_only=True)]:
        tl_html.parse(text)
        if buttons:
            assert bytes(tc.build_reply_markup(buttons))
    assert ui.mask_phone("919876543210") == "+91••••••10"


def test_start_clears_leftover_downloads(hubenv, monkeypatch, tmp_path):
    from tgclone import config, hub as hubmod
    d = tmp_path / "downloads"
    d.mkdir()
    (d / "old.mp4").write_bytes(b"x" * 1000)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", d)
    hubmod.Hub._clear_downloads()
    assert list(d.iterdir()) == []


def test_login_with_keypad_code_never_in_chat(hubenv):
    hub = hubenv.new_hub()
    press(hub, "lg", 5)
    say(hub, "+91 98765 43005", 5)
    ev = press(hub, "otp:k", 5)
    assert "_ _ _ _ _" in ev.edits[-1][0]
    for d in "1234":
        ev = press(hub, f"otp:d{d}", 5)
    assert "1 2 3 4 _" in ev.edits[-1][0]
    ev = press(hub, "otp:b", 5)
    assert "1 2 3 _ _" in ev.edits[-1][0]
    press(hub, "otp:d4", 5)
    ev = press(hub, "otp:d5", 5)  # 5th digit -> signs in by itself
    assert 5 in hub.ws and 5 not in hub.logins
    assert "Logged in as" in ev.edits[-1][0]
