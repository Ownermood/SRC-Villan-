"""Multi-user bot: every user logs in inside Telegram and gets their own workspace.

Owner sets BOT_TOKEN / API_ID / API_HASH / OWNER_ID once (python -m tgclone setup).
Users: /start -> 🔐 Login -> share phone -> code on a keypad -> (2FA password) -> done.
Sessions are stored encrypted (accounts.py). Each user's files live in data/users/<uid>/.
"""

import asyncio
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from telethon import Button, TelegramClient, events, functions, types
from telethon.errors import (FloodWaitError, PasswordHashInvalidError, PhoneCodeExpiredError,
                             PhoneCodeInvalidError, PhoneNumberBannedError,
                             PhoneNumberInvalidError, SessionPasswordNeededError)
from telethon.sessions import StringSession

from tgclone import __version__, live, ui
from tgclone.accounts import Accounts, load_key
from tgclone.app import App
from tgclone.bot import Panel
from tgclone.config import CONFIG_FILE, DATA_DIR, Secrets

log = logging.getLogger("tgclone")

USERS_DIR = DATA_DIR / "users"
LOGIN_TTL = 600  # seconds a half-finished login may wait
PANEL_CMDS = ["start", "menu", "help", "list", "add", "forward", "remove", "toggle", "style",
              "setchat", "clone", "batch", "jobs", "stop", "cancel", "status", "thumb",
              "setthumb", "delthumb"]
HUB_CMDS = ["login", "logout", "account", "admin"]
HUB_CALLBACKS = ("lg", "hw", "otp", "ac", "lo", "LO", "LD", "ad", "adm", "adu", "adv", "adb",
                 "adl", "adp", "adj", "adx", "ap", "dn")
BOT_COMMANDS = {"start": "Open the panel", "login": "Log in with Telegram",
                "batch": "Save posts: link count", "jobs": "Running jobs", "stop": "Stop job(s)",
                "thumb": "Thumbnail settings", "account": "Your account", "logout": "Log out",
                "help": "How to use"}
PHONE_RE = re.compile(r"^\+?\d[\d\s-]{6,16}\d$")


class Hub:
    def __init__(self, live_mode: bool = True, bot: Optional[TelegramClient] = None,
                 make_client: Optional[Callable[[str], TelegramClient]] = None) -> None:
        self.secrets = Secrets()
        self.secrets.check()
        self.live = live_mode
        self.bot = bot
        self.make_client = make_client or self._make_client
        key = load_key(self.secrets.secret_key, DATA_DIR / "secret.key")
        self.acc = Accounts(DATA_DIR / "accounts.db", key)
        self.settings = self.acc.settings()
        self.ws: Dict[int, Tuple[App, Panel]] = {}
        self.logins: Dict[int, dict] = {}
        self.owner = self.secrets.owner_id or next(
            (int(a) for a in self.secrets.admins if a.lstrip("-").isdigit()), 0)
        self.started = time.time()
        self._tasks = []

    # ============================================================ startup
    def _make_client(self, session: str) -> TelegramClient:
        return TelegramClient(StringSession(session), self.secrets.api_id, self.secrets.api_hash,
                              device_model="tgclone bot", app_version=__version__,
                              flood_sleep_threshold=60)

    async def start(self) -> None:
        if self.bot is None:
            if not self.secrets.bot_token:
                raise SystemExit("BOT_TOKEN missing. Run: python -m tgclone setup")
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.bot = TelegramClient(str(DATA_DIR / "bot"), self.secrets.api_id,
                                      self.secrets.api_hash, flood_sleep_threshold=60)
            await self.bot.start(bot_token=self.secrets.bot_token)
        me = await self.bot.get_me()
        log.info("Bot @%s started", getattr(me, "username", "?"))

        if self.secrets.session_string:  # legacy: owner session from .env
            client = self.make_client(self.secrets.session_string)
            await client.connect()
            if await client.is_user_authorized():
                uid = (await client.get_me()).id
                self.owner = self.owner or uid
                await self.open_workspace(self.owner, client)
            else:
                log.error("SESSION_STRING in .env is not valid any more")
        if not self.owner:
            log.warning("OWNER_ID not set - nobody can open the admin panel")

        for uid, session in self.acc.sessions():
            if uid in self.ws:
                continue
            await self._restore(uid, session)

        self.register()
        try:
            await self.bot(functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(), lang_code="",
                commands=[types.BotCommand(k, v) for k, v in BOT_COMMANDS.items()]))
        except Exception as e:
            log.warning("Could not set bot commands: %s", e)
        self._tasks.append(asyncio.create_task(self._housekeeping()))
        log.info("Hub ready: %s user(s) online, mode=%s", len(self.ws), self.settings.mode)

    async def _restore(self, uid: int, session: Optional[str]) -> None:
        if session is None:
            return await self._session_lost(uid, "couldn't be decrypted")
        client = self.make_client(session)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise PermissionError("not authorized")
            await self.open_workspace(uid, client)
        except Exception as e:
            log.warning("Session of %s not restored: %s", uid, e)
            try:
                await client.disconnect()
            except Exception:
                pass
            if isinstance(e, PermissionError):
                await self._session_lost(uid, "was ended from Telegram")

    async def _session_lost(self, uid: int, why: str) -> None:
        self.acc.delete_session(uid)
        await self._notify(uid, f"⚠️ Your login {why}. Tap /login to connect again.")

    async def run(self) -> None:
        await self.start()
        await self.bot.run_until_disconnected()

    # ============================================================ workspaces
    def home(self, uid: int) -> Path:
        return USERS_DIR / str(uid)

    def _migrate_legacy(self, uid: int) -> None:
        """Owner of a single-user install keeps their old config / db / images."""
        home = self.home(uid)
        if home.exists() or uid != self.owner:
            return
        home.mkdir(parents=True, exist_ok=True)
        for src, name in ((CONFIG_FILE, "config.json"), (DATA_DIR / "tgclone.db", "tgclone.db"),
                          (DATA_DIR / "custom_thumb.jpg", "custom_thumb.jpg"),
                          (DATA_DIR / "watermark.png", "watermark.png")):
            if Path(src).exists():
                shutil.copy2(src, home / name)
                log.info("Migrated %s to %s", src, home)

    def caps(self, uid: int) -> Optional[dict]:
        if uid == self.owner:
            return None
        return {"max_jobs": self.settings.user_max_jobs, "max_batch": self.settings.user_max_batch}

    async def open_workspace(self, uid: int, client: TelegramClient) -> App:
        self._migrate_legacy(uid)
        app = App(self.home(uid), uid)
        app.hub = self
        app.caps = self.caps(uid)
        await app.attach(client, self.bot)
        if self.live:
            live.register(app)
        self.ws[uid] = (app, Panel(app, self.live, hub=self))
        log.info("Workspace %s online (%s forwards)", uid, len(app.cfg.forwards))
        return app

    async def close_workspace(self, uid: int, logout: bool = False) -> None:
        item = self.ws.pop(uid, None)
        if item:
            app = item[0]
            app.jobs.stop()
            try:
                if logout:
                    await app.user.log_out()  # ends the session on Telegram too
                else:
                    await app.user.disconnect()
            except Exception as e:
                log.warning("Closing %s: %s", uid, e)
        if logout:
            self.acc.delete_session(uid)

    def check_capacity(self) -> None:
        running = sum(len(a.jobs.running()) for a, _ in self.ws.values())
        if running >= self.settings.max_total_jobs:
            raise ValueError("Server is busy right now — try again in a few minutes.")

    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for uid, st in list(self.logins.items()):  # abandoned logins
                if now - st["t"] > LOGIN_TTL:
                    await self._drop_login(uid)
            for uid, (app, _) in list(self.ws.items()):  # revoked sessions
                try:
                    if not app.user.is_connected():
                        await app.user.connect()
                    if not await app.user.is_user_authorized():
                        await self.close_workspace(uid)
                        await self._session_lost(uid, "was ended from Telegram")
                except Exception as e:
                    log.debug("health check %s: %s", uid, e)

    # ============================================================ access
    def access(self, uid: int) -> str:
        """ok | banned | private | pending | request | full"""
        if uid == self.owner:
            return "ok"
        state = self.acc.access(uid)
        if state == "banned":
            return "banned"
        if self.settings.mode == "owner":
            return "private"
        if self.settings.mode == "approve" and state != "approved":
            return "pending" if state == "pending" else "request"
        if (self.settings.max_users and uid not in self.ws
                and self.acc.counts()["users"] >= self.settings.max_users):
            return "full"
        return "ok"

    async def _notify(self, uid: int, text: str, buttons=None) -> None:
        try:
            await self.bot.send_message(uid, text, buttons=buttons, parse_mode="html")
        except Exception as e:
            log.debug("notify %s failed: %s", uid, e)

    # ============================================================ routing
    def register(self) -> None:
        names = "|".join(PANEL_CMDS + HUB_CMDS)
        self.bot.add_event_handler(self.on_command, events.NewMessage(
            pattern=rf"^/({names})(?:@\w+)?(?:\s+([\s\S]*))?$", func=lambda e: e.is_private))
        self.bot.add_event_handler(self.on_callback, events.CallbackQuery())
        self.bot.add_event_handler(self.on_text, events.NewMessage(
            func=lambda e: e.is_private and not (e.raw_text or "").startswith("/")))

    async def _run(self, event, coro) -> None:
        try:
            await coro
        except (ValueError, IndexError) as e:
            await self._reply_error(event, str(e))
        except events.StopPropagation:
            raise
        except Exception as e:
            log.exception("Hub handler failed")
            await self._reply_error(event, f"Error: {e}")
        if isinstance(event, events.CallbackQuery.Event) and not event._answered:
            try:
                await event.answer()
            except Exception:
                pass

    async def _reply_error(self, event, msg: str) -> None:
        try:
            if isinstance(event, events.CallbackQuery.Event):
                await event.answer(msg[:190], alert=True)
            else:
                await event.respond(f"⚠️ {msg}")
        except Exception:
            pass

    async def _gate(self, event) -> bool:
        """False if this user may not use the bot (and tells them why)."""
        if self.acc.access(event.sender_id) == "banned" and event.sender_id != self.owner:
            if isinstance(event, events.CallbackQuery.Event):
                await event.answer("⛔ You are banned from this bot.", alert=True)
            else:
                await event.respond("⛔ You are banned from this bot.")
            return False
        return True

    async def on_command(self, event) -> None:
        if not await self._gate(event):
            raise events.StopPropagation
        uid = event.sender_id
        name = event.pattern_match.group(1)
        args = (event.pattern_match.group(2) or "").strip()
        ws = self.ws.get(uid)
        if name == "cancel" and uid in self.logins:
            await self._drop_login(uid)
            await event.respond("✖️ Login cancelled.", buttons=Button.clear())
            raise events.StopPropagation
        if name in HUB_CMDS:
            await self._run(event, self.hub_command(event, name))
            raise events.StopPropagation
        if ws:
            panel = ws[1]
            await panel.guard(lambda e: panel.cmd(e, name, args))(event)
        await self._run(event, self.show_welcome(event))
        raise events.StopPropagation

    async def on_callback(self, event) -> None:
        if not await self._gate(event):
            raise events.StopPropagation
        key = event.data.decode().split(":", 1)[0]
        ws = self.ws.get(event.sender_id)
        if key in HUB_CALLBACKS:
            await self._run(event, self.hub_callback(event))
        elif ws:
            await ws[1].guard(ws[1].callback)(event)
        else:
            await self._run(event, self.show_welcome(event, edit=True))
        raise events.StopPropagation

    async def on_text(self, event) -> None:
        if not await self._gate(event):
            raise events.StopPropagation
        uid = event.sender_id
        if uid in self.logins:
            await self._run(event, self.login_text(event))
        elif uid in self.ws:
            await self.ws[uid][1].guard(self.ws[uid][1].private_text)(event)
        else:
            await self._run(event, self.show_welcome(event))
        raise events.StopPropagation

    async def show_welcome(self, event, edit: bool = False) -> None:
        acc = self.access(event.sender_id)
        text, buttons = ui.welcome(owner_only=acc == "private", pending=acc == "pending")
        if edit:
            await event.edit(text, buttons=buttons, parse_mode="html", link_preview=False)
        else:
            await event.respond(text, buttons=buttons, parse_mode="html", link_preview=False)

    # ============================================================ hub commands
    async def hub_command(self, event, name: str) -> None:
        uid = event.sender_id
        if name == "login":
            return await self.begin_login(event)
        if name in ("logout", "account"):
            if uid not in self.ws:
                return await event.respond("You're not logged in. Tap /login")
            return await self._show(event, ui.account_view(self.acc.info(uid) or {}, uid))
        if name == "admin":
            if uid != self.owner:
                return await event.respond("⛔ Owner only.")
            return await self._show(event, ui.admin_view(self))

    async def _show(self, event, screen, edit: bool = False):
        text, buttons = screen
        try:
            if edit:
                return await event.edit(text, buttons=buttons, parse_mode="html", link_preview=False)
            return await event.respond(text, buttons=buttons, parse_mode="html", link_preview=False)
        except Exception as e:
            if "not modified" not in str(e).lower():
                raise

    async def hub_callback(self, event) -> None:
        parts = event.data.decode().split(":")
        k, uid = parts[0], event.sender_id
        if k == "lg":
            return await self.begin_login(event)
        if k == "hw":
            return await self._show(event, (ui.HOW, [[ui.B("🔐 Login", "lg", "success")]]
                                            if uid not in self.ws else [[ui.B("« Menu", "m")]]),
                                    edit=True)
        if k == "otp":
            return await self.keypad_press(event, parts[1])
        if k == "ac":
            return await self._show(event, ui.account_view(self.acc.info(uid) or {}, uid), edit=True)
        if k == "lo":
            return await self._show(event, ui.confirm(
                "Log out? The bot stops forwarding for you and its session on Telegram ends.",
                "LO", "ac"), edit=True)
        if k == "LO":
            await self.close_workspace(uid, logout=True)
            return await self._show(event, (
                "🚪 <b>Logged out.</b>\nYour forwards and settings are kept — /login to continue, "
                "or delete them now.",
                [[ui.B("🔐 Login again", "lg", "success")],
                 [ui.B("🗑 Delete my data", "LD", "danger")]]), edit=True)
        if k == "LD":
            if uid in self.ws:
                return await event.answer("Log out first", alert=True)
            shutil.rmtree(self.home(uid), ignore_errors=True)
            await event.answer("🗑 Deleted")
            return await self.show_welcome(event, edit=True)
        # ---------------- owner only below
        if uid != self.owner:
            return await event.answer("⛔ Owner only", alert=True)
        st = self.settings
        if k == "ad":
            return await self._show(event, ui.admin_view(self), edit=True)
        if k == "adm":
            st.mode = parts[1]
            self.acc.save_settings(st)
            return await self._show(event, ui.admin_view(self), edit=True)
        if k in ("adj", "adx"):
            if k == "adj":
                st.user_max_jobs = max(1, min(10, st.user_max_jobs + (1 if parts[1] == "+" else -1)))
            else:
                steps = [50, 100, 200, 500, 1000, 2000, 5000, 10000]
                i = steps.index(st.user_max_batch) if st.user_max_batch in steps else 3
                st.user_max_batch = steps[max(0, min(len(steps) - 1, i + (1 if parts[1] == "+" else -1)))]
            self.acc.save_settings(st)
            for u, (app, _) in self.ws.items():
                app.caps = self.caps(u)
            return await self._show(event, ui.admin_view(self), edit=True)
        if k == "adu":
            return await self._show(event, ui.admin_users(self, int(parts[1])), edit=True)
        if k == "adv":
            return await self._show(event, ui.admin_user(self, int(parts[1])), edit=True)
        if k == "adb":
            target = int(parts[1])
            if target == self.owner:
                return await event.answer("You can't ban yourself", alert=True)
            if self.acc.access(target) == "banned":
                self.acc.set_access(target, None)
                await self._notify(target, "✅ You can use the bot again. /start")
            else:
                self.acc.set_access(target, "banned")
                await self.close_workspace(target, logout=True)
                await self._drop_login(target)
            return await self._show(event, ui.admin_user(self, target), edit=True)
        if k == "adl":
            target = int(parts[1])
            await self.close_workspace(target, logout=True)
            await self._notify(target, "🚪 You were logged out by the owner.")
            return await self._show(event, ui.admin_user(self, target), edit=True)
        if k == "adp":
            return await self._show(event, ui.admin_pending(self), edit=True)
        if k in ("ap", "dn"):
            target = int(parts[1])
            self.acc.set_access(target, "approved" if k == "ap" else None)
            if k == "ap":
                await self._notify(target, "✅ <b>Access approved!</b> Tap below to log in.",
                                   [[ui.B("🔐 Login with Telegram", "lg", "success")]])
            else:
                await self._notify(target, "✖️ Your access request was declined.")
            await event.answer("Approved" if k == "ap" else "Declined")
            return await self._show(event, ui.admin_pending(self), edit=True)

    # ============================================================ login flow
    async def begin_login(self, event) -> None:
        uid = event.sender_id
        if uid in self.ws:
            return await event.respond("✅ You're already logged in.", buttons=[[ui.B("🏠 Menu", "m")]])
        acc = self.access(uid)
        if acc == "private":
            return await event.respond("🔒 This bot is private.")
        if acc == "full":
            return await event.respond("😕 The bot is full right now. Try again later.")
        if acc in ("request", "pending"):
            if acc == "request":
                sender = await event.get_sender()
                name = " ".join(filter(None, [getattr(sender, "first_name", ""),
                                              getattr(sender, "last_name", "")])) or str(uid)
                self.acc.set_access(uid, "pending", name)
                await self._notify(self.owner, f"🙋 <b>Access request</b>\n{ui.esc(name)} · "
                                   f"<code>{uid}</code>",
                                   [[ui.B("✅ Approve", f"ap:{uid}", "success"),
                                     ui.B("✖️ Deny", f"dn:{uid}", "danger")]])
            return await event.respond("⏳ Request sent to the owner. You'll get a message here "
                                       "when you're approved.")
        await self._drop_login(uid)
        self.logins[uid] = {"step": "phone", "t": time.time()}
        await event.respond(ui.login_phone(), parse_mode="html", buttons=[
            [Button.request_phone("📱 Share my number", resize=True, single_use=True)]])

    async def _drop_login(self, uid: int) -> None:
        st = self.logins.pop(uid, None)
        if st and st.get("client") is not None:
            try:
                await st["client"].disconnect()
            except Exception:
                pass

    async def login_text(self, event) -> None:
        uid = event.sender_id
        st = self.logins[uid]
        st["t"] = time.time()
        msg = event.message
        if st["step"] == "phone":
            contact = msg.contact if hasattr(msg, "contact") else None
            if contact is not None:
                if contact.user_id != uid:
                    raise ValueError("Please share YOUR number with the 📱 button.")
                phone = contact.phone_number
            elif PHONE_RE.match((msg.raw_text or "").strip()):
                phone = re.sub(r"[\s-]", "", msg.raw_text.strip())
            else:
                raise ValueError("Tap 📱 Share my number, or type it like +919876543210.")
            phone = "+" + phone.lstrip("+")
            await event.respond("📨 Sending a login code to your Telegram app…", buttons=Button.clear())
            client = self.make_client("")
            await client.connect()
            try:
                sent = await client.send_code_request(phone)
            except PhoneNumberInvalidError:
                await client.disconnect()
                raise ValueError("That phone number is not valid. Send /login to try again.")
            except PhoneNumberBannedError:
                await client.disconnect()
                await self._drop_login(uid)
                raise ValueError("This number is banned by Telegram.")
            except FloodWaitError as e:
                await client.disconnect()
                await self._drop_login(uid)
                raise ValueError(f"Telegram asks to wait {e.seconds}s before a new code.")
            st.update(step="code", client=client, phone=phone, hash=sent.phone_code_hash, digits="")
            return await self._show(event, ui.keypad(""))
        if st["step"] == "code":
            if (msg.raw_text or "").replace(" ", "").isdigit():
                return await self._show(event, ui.keypad(
                    st["digits"], "⚠️ Please use the buttons. If Telegram already blocked that "
                                  "code, tap 🔄 New code."))
            return await self._show(event, ui.keypad(st["digits"]))
        if st["step"] == "password":
            pw = msg.raw_text or ""
            try:
                await msg.delete()  # never keep the password in the chat
            except Exception:
                pass
            try:
                await st["client"].sign_in(password=pw)
            except PasswordHashInvalidError:
                st["tries"] = st.get("tries", 0) + 1
                if st["tries"] >= 3:
                    await self._drop_login(uid)
                    raise ValueError("Wrong password 3 times. Send /login to start again.")
                return await self._show(event, ui.login_password(
                    f"❌ Wrong password ({st['tries']}/3). Try again."))
            return await self.finish_login(event)

    async def keypad_press(self, event, key: str) -> None:
        uid = event.sender_id
        st = self.logins.get(uid)
        if not st or st["step"] not in ("code", "password"):
            await event.answer("Login expired — start again", alert=True)
            return await self.show_welcome(event, edit=True)
        st["t"] = time.time()
        if key == "x":
            await self._drop_login(uid)
            await event.answer("Cancelled")
            return await self.show_welcome(event, edit=True)
        if st["step"] != "code":
            return await event.answer()
        if key == "b":
            st["digits"] = st["digits"][:-1]
        elif key.isdigit():
            if len(st["digits"]) < 6:
                st["digits"] += key
        elif key == "r":
            try:
                sent = await st["client"].send_code_request(st["phone"])
            except FloodWaitError as e:
                return await event.answer(f"Wait {e.seconds}s for a new code", alert=True)
            st.update(hash=sent.phone_code_hash, digits="")
            await event.answer("📨 New code sent")
        elif key == "ok":
            if len(st["digits"]) < 5:
                return await event.answer("The code has 5 digits", alert=True)
            try:
                await st["client"].sign_in(phone=st["phone"], code=st["digits"],
                                           phone_code_hash=st["hash"])
            except PhoneCodeInvalidError:
                st["digits"] = ""
                return await self._show(event, ui.keypad("", "❌ Wrong code, try again."), edit=True)
            except PhoneCodeExpiredError:
                st["digits"] = ""
                return await self._show(event, ui.keypad(
                    "", "⌛ Code expired — tap 🔄 New code."), edit=True)
            except SessionPasswordNeededError:
                st["step"] = "password"
                return await self._show(event, ui.login_password(), edit=True)
            return await self.finish_login(event, edit=True)
        return await self._show(event, ui.keypad(st["digits"]), edit=True)

    async def finish_login(self, event, edit: bool = False) -> None:
        uid = event.sender_id
        st = self.logins.pop(uid)
        client = st["client"]
        me = await client.get_me()
        name = " ".join(filter(None, [me.first_name, me.last_name])) or (me.username or str(me.id))
        self.acc.save_session(uid, client.session.save(), name, st["phone"])
        app = await self.open_workspace(uid, client)
        await self._show(event, (f"✅ <b>Logged in as {ui.esc(name)}</b>\nWelcome! Let's set up your "
                                 "first forward.", None), edit=edit)
        await self._show(event, await ui.main_menu(app))
