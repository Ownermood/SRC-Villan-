"""Multi-user bot: every user works inside Telegram.

Owner: BOT_TOKEN / API_ID / API_HASH / OWNER_ID once (python -m tgclone setup).
Users: /start -> 🔐 Login (share phone -> type code '1 2 3 4 5' -> 2FA)  or  🤖 Use without login.
Plans: every new user gets a free trial (default 24h), then premium days from the owner.
Each user's files live in data/users/<uid>/; logins are stored encrypted (accounts.py).
"""

import asyncio
import io
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from telethon import Button, TelegramClient, events, functions, types, utils
from telethon.errors import (FloodWaitError, PasswordHashInvalidError, PhoneCodeExpiredError,
                             PhoneCodeInvalidError, PhoneNumberBannedError,
                             PhoneNumberInvalidError, SessionPasswordNeededError)
from telethon.sessions import StringSession

from tgclone import __version__, emoji, live, ui
from tgclone.accounts import Accounts, load_key
from tgclone.app import App
from tgclone.bot import Panel
from tgclone.config import CONFIG_FILE, DATA_DIR, Forward, Secrets

log = logging.getLogger("tgclone")

USERS_DIR = DATA_DIR / "users"
LOGIN_TTL = 600  # seconds a half-finished login may wait
WARN_BEFORE = 2 * 3600  # plan ending soon -> one reminder
PANEL_CMDS = ["start", "menu", "help", "list", "add", "forward", "remove", "toggle", "style",
              "setchat", "clone", "batch", "jobs", "stop", "cancel", "status", "thumb",
              "setthumb", "delthumb"]
HUB_CMDS = ["login", "logout", "account", "profile", "admin", "addpremium", "rmpremium",
            "block", "unblock"]
OWNER_CMDS = {"admin", "addpremium", "rmpremium", "block", "unblock"}
HUB_CALLBACKS = {"lg", "hw", "otp", "ac", "lo", "LO", "LD", "bm", "bx", "gp", "bp", "prd", "apP", "apI", "ast",
                 "adp3",
                 "ad", "adm", "adu", "adv", "adb", "adl", "adp", "adj", "adx", "adt", "ap", "dn",
                 "apm", "apg", "apr", "apl", "atr", "apt", "apa", "apx", "abl", "abk", "aub",
                 "aac", "alm", "asp", "aps", "apd", "aem", "aet"}
# reachable while a plan is expired (profile, logout, help, get premium)
EXPIRED_OK = {"ac", "lo", "LO", "LD", "gp", "bp", "hw", "bx", "otp", "lg", "noop"}
BOT_COMMANDS = {"start": "Open the control panel", "profile": "Plan, account, logout",
                "login": "Log in with Telegram", "batch": "Save posts: link count",
                "jobs": "Running jobs", "stop": "Stop job(s)", "thumb": "Thumbnail",
                "help": "How to use", "logout": "Log out"}
OWNER_COMMANDS = {**BOT_COMMANDS, "admin": "🛡 Admin panel", "addpremium": "user days",
                  "rmpremium": "user", "block": "chat / link / folder", "unblock": "chat"}
PHONE_RE = re.compile(r"^\+?\d[\d\s-]{6,16}\d$")
FOLDER_RE = re.compile(r"(?:t\.me|telegram\.me)/addlist/([\w-]+)")
INVITE_RE = re.compile(r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)([\w-]+)")
IMG_URL_RE = re.compile(r"^https://\S+$")


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
        emoji.set_enabled(self.settings.premium_emoji)
        self.ws: Dict[int, Tuple[App, Panel]] = {}
        self.logins: Dict[int, dict] = {}
        self.owner = self.secrets.owner_id or next(
            (int(a) for a in self.secrets.admins if a.lstrip("-").isdigit()), 0)
        self.started = time.time()
        self.admin_wait: Optional[dict] = None  # owner is typing a value for an admin tool
        self.restoring: set = set()  # users whose saved login is still being loaded at start
        self._bot_handlers: Dict[int, list] = {}  # bot-only users' live handlers on the shared bot
        self._start_media = None  # cached uploaded start photo
        self.owner_username = ""  # for the "Message the owner" button
        self.bot_name = "SRC Villan Bot"
        self._buy_asked: Dict[int, float] = {}  # user -> last premium request (anti-spam)
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
        emoji.install(self.bot)  # every bot message / edit / caption gets premium emoji
        me = await self.bot.get_me()
        log.info("Bot @%s started", getattr(me, "username", "?"))
        self.bot_name = " ".join(filter(None, [getattr(me, "first_name", ""), getattr(me, "last_name", "")])) \
            or getattr(me, "username", "") or "SRC Villan Bot"
        # answer buttons right away; users are loaded below (in parallel) while the bot already works
        self.restoring = set(self.acc.botmode_users()) | {uid for uid, _ in self.acc.sessions()}
        self.register()

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
        else:
            try:
                self.owner_username = getattr(await self.bot.get_entity(self.owner), "username", "") or ""
            except Exception:
                pass

        sem = asyncio.Semaphore(5)

        async def restore(uid, session):
            async with sem:
                try:
                    if uid not in self.ws:
                        await self._restore(uid, session)
                finally:
                    self.restoring.discard(uid)

        async def restore_bot(uid):
            try:
                if uid not in self.ws:
                    await self.open_bot_workspace(uid)
            except Exception as e:
                log.warning("Bot-only user %s not restored: %s", uid, e)
            finally:
                self.restoring.discard(uid)

        started = time.time()
        await asyncio.gather(*(restore(u, s) for u, s in self.acc.sessions()),
                             *(restore_bot(u) for u in self.acc.botmode_users()))
        self.restoring.clear()
        log.info("Users loaded in %.1fs", time.time() - started)
        await self._set_commands()
        self._tasks.append(asyncio.create_task(self._housekeeping()))
        log.info("Hub ready: %s user(s) online, mode=%s", len(self.ws), self.settings.mode)

    async def _set_commands(self) -> None:
        try:
            await self.bot(functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(), lang_code="",
                commands=[types.BotCommand(k, v) for k, v in BOT_COMMANDS.items()]))
            if self.owner:  # the owner also sees the admin commands in the menu
                peer = await self.bot.get_input_entity(self.owner)
                await self.bot(functions.bots.SetBotCommandsRequest(
                    scope=types.BotCommandScopePeer(peer), lang_code="",
                    commands=[types.BotCommand(k, v) for k, v in OWNER_COMMANDS.items()]))
        except Exception as e:
            log.info("Bot command menu: %s", e)

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
        """Limits by plan: premium runs more jobs at once than the free trial. Owner: none."""
        if uid == self.owner:
            return None
        premium = self.plan_status(uid)["kind"] in ("premium", "free")
        jobs = self.settings.premium_max_jobs if premium else self.settings.user_max_jobs
        return {"max_jobs": jobs, "max_batch": self.settings.user_max_batch, "premium": premium}

    async def start_intro(self, event) -> str:
        """The owner's /start text with the user's name filled in."""
        import html as _html
        uid = event.sender_id
        try:
            s = await event.get_sender()
            first = getattr(s, "first_name", "") or "there"
        except Exception:
            first = "there"
        tpl = self.settings.start_text or ui.DEFAULT_START
        values = {"name": f'<a href="tg://user?id={uid}">{_html.escape(first)}</a>',
                  "first": _html.escape(first), "bot": _html.escape(self.bot_name), "id": str(uid),
                  "plan": ui.plan_label(self.plan_status(uid))}
        try:
            return tpl.format(**values)
        except (KeyError, IndexError, ValueError):
            return tpl

    def _new_app(self, uid: int) -> App:
        self._migrate_legacy(uid)
        app = App(self.home(uid), uid)
        app.hub = self
        app.caps = self.caps(uid)
        app.paused = not self.plan_ok(uid)
        return app

    async def open_workspace(self, uid: int, client: TelegramClient) -> App:
        app = self._new_app(uid)
        await app.attach(client, self.bot)
        if self.live:
            live.register(app)
        self.ws[uid] = (app, Panel(app, self.live, hub=self))
        log.info("Workspace %s online (%s forwards)", uid, len(app.cfg.forwards))
        self._tasks.append(asyncio.create_task(self.resume_jobs(uid)))
        return app

    async def resume_jobs(self, uid: int, delay: float = 0.5) -> int:
        """Clones / saves that were running when the bot stopped continue by themselves."""
        await asyncio.sleep(delay)
        item = self.ws.get(uid)
        if not item or not self.plan_ok(uid):
            return 0
        app, panel = item
        started = 0
        for p in app.store.pending():
            target = _Chat(self.bot, uid)
            try:
                data = json.loads(p["data"])
                if p["kind"] == "clone":
                    await panel.start_clone(target, Forward(**data["forward"]), fresh=False)
                elif p["kind"] == "save":
                    await panel.start_save(target, data["chat"], data["first"], data["last"],
                                           data["target"])
                started += 1
            except Exception as e:
                log.warning("Resume %s for %s failed: %s", p["key"], uid, e)
                app.store.remove_pending(p["key"])
                await self._notify(uid, f"⚠️ Couldn't resume a job after restart: {ui.esc(str(e))}")
        if started:
            await self._notify(uid, f"🔄 <b>Resumed {started} job(s)</b> after a restart — "
                                    "continuing from where they stopped.")
        return started

    async def open_bot_workspace(self, uid: int) -> App:
        """Without login: the shared bot reads new posts (where it is admin) and sends."""
        app = self._new_app(uid)
        await app.attach_bot(self.bot)
        if self.live:
            self._bot_handlers[uid] = live.register(app)
        self.ws[uid] = (app, Panel(app, self.live, hub=self))
        log.info("Bot-only workspace %s online", uid)
        return app

    async def close_workspace(self, uid: int, logout: bool = False) -> None:
        item = self.ws.pop(uid, None)
        for fn in self._bot_handlers.pop(uid, []):
            self.bot.remove_event_handler(fn)
        if item:
            app = item[0]
            app.jobs.stop()
            if app.user is not None:
                try:
                    if logout:
                        await app.user.log_out()  # ends the session on Telegram too
                    else:
                        await app.user.disconnect()
                except Exception as e:
                    log.warning("Closing %s: %s", uid, e)
        if logout:
            self.acc.delete_session(uid)
            self.acc.remove_botmode(uid)

    def check_capacity(self) -> None:
        running = sum(len(a.jobs.running()) for a, _ in self.ws.values())
        if running >= self.settings.max_total_jobs:
            raise ValueError("Server is busy right now — try again in a few minutes.")

    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.tick()
            except Exception:
                log.exception("housekeeping")

    async def tick(self) -> None:
        now = time.time()
        for uid, st in list(self.logins.items()):  # abandoned logins
            if now - st["t"] > LOGIN_TTL:
                await self._drop_login(uid)
        await self.check_plans()
        for uid, (app, _) in list(self.ws.items()):  # revoked sessions
            if app.user is None:
                continue
            try:
                if not app.user.is_connected():
                    await app.user.connect()
                if not await app.user.is_user_authorized():
                    await self.close_workspace(uid)
                    await self._session_lost(uid, "was ended from Telegram")
            except Exception as e:
                log.debug("health check %s: %s", uid, e)

    # ============================================================ plans
    def plan_status(self, uid: int) -> dict:
        """kind: owner | free | trial | premium | expired | none (+ expires, left)."""
        if uid == self.owner:
            return {"kind": "owner"}
        if not self.settings.premium_enabled:
            return {"kind": "free"}
        p = self.acc.plan(uid)
        if not p:
            return {"kind": "none"}
        left = p["expires"] - time.time()
        return {"kind": p["kind"] if left > 0 else "expired", "expires": p["expires"], "left": left}

    def plan_ok(self, uid: int) -> bool:
        return self.plan_status(uid)["kind"] in ("owner", "free", "trial", "premium", "none")

    async def ensure_trial(self, uid: int) -> None:
        """First real use: start the free trial once."""
        if self.plan_status(uid)["kind"] != "none":
            return
        hours = self.settings.trial_hours
        if hours:
            self.acc.set_plan(uid, time.time() + hours * 3600, "trial")
            await self._notify(uid, f"🎁 <b>Your {hours}h free trial has started.</b> Enjoy!")
        else:
            self.acc.set_plan(uid, time.time() - 1, "trial")

    def add_days(self, uid: int, days: float) -> float:
        p = self.acc.plan(uid)
        base = max(time.time(), p["expires"]) if p else time.time()
        expires = base + days * 86400
        self.acc.set_plan(uid, expires, "premium")
        if uid in self.ws:
            self.ws[uid][0].paused = False
            self.ws[uid][0].caps = self.caps(uid)  # premium job limit right away
        return expires

    def remove_plan(self, uid: int) -> None:
        self.acc.set_plan(uid, time.time() - 1, "premium")
        if uid in self.ws:
            self.ws[uid][0].paused = True
            self.ws[uid][0].jobs.stop()

    async def check_plans(self) -> None:
        now = time.time()
        for p in self.acc.plans():
            uid, left = p["uid"], p["expires"] - now
            if not self.settings.premium_enabled or uid == self.owner:
                continue
            app = self.ws.get(uid, (None,))[0]
            if left <= 0:
                if app is not None and not app.paused:
                    app.paused = True
                    app.jobs.stop()
                if p["notified"] < 2:
                    self.acc.set_notified(uid, 2)
                    await self._notify(uid, "⌛ <b>Your plan has ended.</b> Live forwarding is paused — "
                                            "your settings are safe.\n💎 /profile → Get premium",
                                       [[ui.B("💎 Get premium", "gp", "success")]])
            else:
                if app is not None and app.paused:
                    app.paused = False
                if left < WARN_BEFORE and p["notified"] < 1:
                    self.acc.set_notified(uid, 1)
                    await self._notify(uid, f"⏰ Your plan ends in <b>{ui.left(left)}</b>. "
                                            "💎 /profile → Get premium to keep going.")

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

    # ============================================================ chats: block list & bot mode
    def resolver(self) -> TelegramClient:
        """A user account to look chats up with (owner's preferred), else the bot."""
        if self.owner in self.ws and self.ws[self.owner][0].user is not None:
            return self.ws[self.owner][0].user
        for app, _ in self.ws.values():
            if app.user is not None:
                return app.user
        return self.bot

    async def block_targets(self, msg) -> List[Tuple[int, str]]:
        """Chats to block from a folder link, invite, link, @username, id or forwarded message."""
        client = self.resolver()
        fwd = getattr(msg, "fwd_from", None)
        if fwd and isinstance(fwd.from_id, (types.PeerChannel, types.PeerChat)):
            cid = utils.get_peer_id(fwd.from_id)
            return [(cid, await self._title(client, cid))]
        text = (msg.raw_text or "").strip()
        m = FOLDER_RE.search(text)
        if m:
            res = await client(functions.chatlists.CheckChatlistInviteRequest(slug=m.group(1)))
            chats = list(getattr(res, "chats", []))
            if not chats:
                raise ValueError("That folder link has no chats I can see.")
            return [(utils.get_peer_id(c), getattr(c, "title", "") or str(c.id)) for c in chats
                    if isinstance(c, (types.Channel, types.Chat))]
        m = INVITE_RE.search(text)
        if m:
            res = await client(functions.messages.CheckChatInviteRequest(hash=m.group(1)))
            chat = getattr(res, "chat", None)
            if chat is None:
                raise ValueError("I can't see the id behind this invite. Join it with your account "
                                 "(or forward a message from it) and try again.")
            return [(utils.get_peer_id(chat), getattr(chat, "title", ""))]
        ref = ui.chat_from_text(text)
        cid = await client.get_peer_id(ref)
        return [(cid, await self._title(client, cid))]

    @staticmethod
    async def _title(client, cid: int) -> str:
        try:
            ent = await client.get_entity(cid)
            return getattr(ent, "title", None) or getattr(ent, "first_name", None) or str(cid)
        except Exception:
            return str(cid)

    async def check_chat(self, app: App, ref, role: str) -> None:
        """Called when a user adds a chat. role: 'source' | 'dest'. Raises ValueError."""
        cid = await app.resolve(ref)
        if role == "source" and app.blocked(cid):
            raise ValueError("This chat can't be cloned (blocked by the owner).")
        if not app.botmode:
            return
        what = "source" if role == "source" else "destination"
        try:
            me = await self.bot.get_permissions(cid, "me")
        except Exception:
            raise ValueError(f"I'm not in that {what}. Add me as admin there first.")
        if not (me.is_admin or me.is_creator):
            raise ValueError(f"Make me an admin in the {what} first.")
        try:
            them = await self.bot.get_permissions(cid, app.uid)
        except Exception:
            them = None
        if not them or not (them.is_admin or them.is_creator):
            raise ValueError(f"Without login you must be an admin of the {what} yourself.")
        if role == "source":
            ent = await self.bot.get_entity(cid)
            if getattr(ent, "noforwards", False):
                raise ValueError("This chat has forwarding turned off (protected). "
                                 "Log in to clone protected chats.")

    # ============================================================ sending screens
    async def _show(self, event, screen, edit: bool = False):
        text, buttons = screen  # premium emoji are added by emoji.install() on the bot
        try:
            if edit:
                return await event.edit(text, buttons=buttons, parse_mode="html", link_preview=False)
            return await event.respond(text, buttons=buttons, parse_mode="html", link_preview=False)
        except Exception as e:
            msg = str(e).lower()
            if "not modified" in msg:
                return None
            if edit and ("caption" in msg or "media" in msg):
                return await self._show(event, screen, edit=False)  # photo screen: send fresh
            raise

    async def send_start(self, event, screen) -> None:
        """/start: big photo on top (if the owner set one) with the screen as caption."""
        photo = self.settings.start_photo
        text, buttons = screen
        if photo and len(ui.strip_tags(text)) <= 1000:
            try:
                msg = await self.bot.send_file(event.chat_id, self._start_media or photo,
                                               caption=text, buttons=buttons, parse_mode="html")
                if self._start_media is None and getattr(msg, "media", None) is not None:
                    self._start_media = msg.media
                return
            except Exception as e:
                log.warning("Start photo failed (%s) - sending text only", e)
        await self._show(event, screen)

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
                await event.answer("🚫 You are banned from this bot.", alert=True)
            else:
                await event.respond("🚫 You are banned from this bot.")
            return False
        if event.sender_id in self.restoring and event.sender_id not in self.ws:
            msg = "⏳ The bot just restarted — loading your account, try again in a few seconds."
            if isinstance(event, events.CallbackQuery.Event):
                await event.answer(msg, alert=True)
            else:
                await event.respond(msg)
            return False
        return True

    async def on_command(self, event) -> None:
        if not await self._gate(event):
            raise events.StopPropagation
        uid = event.sender_id
        name = event.pattern_match.group(1)
        args = (event.pattern_match.group(2) or "").strip()
        ws = self.ws.get(uid)
        if name == "cancel" and (uid in self.logins or (uid == self.owner and self.admin_wait)):
            self.admin_wait = None if uid == self.owner else self.admin_wait
            await self._drop_login(uid)
            await event.respond("❌ Cancelled.", buttons=Button.clear())
            raise events.StopPropagation
        if name in HUB_CMDS:
            await self._run(event, self.hub_command(event, name, args))
            raise events.StopPropagation
        if ws:
            if not self.plan_ok(uid) and name not in ("help", "stop", "cancel"):
                await self._run(event, self._show(event, ui.plan_expired(self, uid)))
                raise events.StopPropagation
            panel = ws[1]
            await panel.guard(lambda e: panel.cmd(e, name, args))(event)
        if name == "help":
            await self._run(event, self._show(event, (ui.HOW, [[ui.B("🔐 Login", "lg", "success")]])))
        else:
            await self._run(event, self.show_welcome(event))
        raise events.StopPropagation

    async def on_callback(self, event) -> None:
        if not await self._gate(event):
            raise events.StopPropagation
        uid = event.sender_id
        key = event.data.decode().split(":", 1)[0]
        ws = self.ws.get(uid)
        if ws and key not in EXPIRED_OK and not self.plan_ok(uid):
            await self._run(event, self._show(event, ui.plan_expired(self, uid), edit=True))
        elif key in HUB_CALLBACKS:
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
        if uid == self.owner and self.admin_wait:
            await self._run(event, self.admin_text(event))
        elif uid in self.logins:
            await self._run(event, self.login_text(event))
        elif uid in self.ws:
            if not self.plan_ok(uid):
                await self._run(event, self._show(event, ui.plan_expired(self, uid)))
            else:
                await self.ws[uid][1].guard(self.ws[uid][1].private_text)(event)
        else:
            await self._run(event, self.show_welcome(event))
        raise events.StopPropagation

    async def show_welcome(self, event, edit: bool = False) -> None:
        acc = self.access(event.sender_id)
        screen = ui.welcome(self.settings.trial_hours if self.settings.premium_enabled else 0,
                            owner_only=acc == "private", pending=acc == "pending",
                            intro=await self.start_intro(event))
        if edit:
            await self._show(event, screen, edit=True)
        else:
            await self.send_start(event, screen)

    # ============================================================ hub commands
    async def hub_command(self, event, name: str, args: str) -> None:
        uid = event.sender_id
        if name in OWNER_CMDS and uid != self.owner:
            return await event.respond("🚫 Owner only.")
        if name == "login":
            return await self.begin_login(event)
        if name in ("logout", "account", "profile"):
            return await self._show(event, ui.profile_view(self, uid))
        if name == "admin":
            return await self._show(event, ui.admin_view(self))
        if name == "addpremium":
            target, days = await self._parse_user_days(args)
            return await self._grant(event, target, days)
        if name == "rmpremium":
            target, _ = await self._parse_user_days(args, need_days=False)
            self.remove_plan(target)
            await self._notify(target, "➖ Your premium was removed by the owner.")
            return await event.respond(f"➖ Premium removed for {target}.")
        if name == "block":
            if not args:
                self.admin_wait = {"kind": "block"}
                return await self._show(event, ui.admin_prompt("block"))
            return await self._block_from(event, _TextMsg(args))
        if name == "unblock":
            cid = await self.resolver().get_peer_id(ui.chat_from_text(args))
            self.acc.unblock(cid)
            await self._reload_all_forwards()
            return await event.respond(f"✅ Unblocked {cid}.")

    async def _parse_user_days(self, text: str, need_days: bool = True) -> Tuple[int, float]:
        parts = text.replace(",", " ").split()
        if not parts or (need_days and len(parts) < 2):
            raise ValueError("Send: user_id days   (e.g. 5021 30)  or  @username days")
        who = parts[0]
        if who.lstrip("-").isdigit():
            uid = int(who)
        else:
            try:
                ent = await self.bot.get_entity(who if who.startswith("@") else "@" + who)
            except Exception:
                raise ValueError(f"User {who} not found. Use their numeric id.")
            uid = ent.id
            if not self.acc.name_of(uid):
                self.acc.set_access(uid, self.acc.access(uid) or "seen",
                                    " ".join(filter(None, [ent.first_name, ent.last_name])))
        days = 0.0
        if need_days:
            try:
                days = float(parts[1].rstrip("dD"))
            except ValueError:
                raise ValueError("Days must be a number, e.g. 30")
            if not 0 < days <= 3650:
                raise ValueError("Days must be between 1 and 3650")
        return uid, days

    async def _grant(self, event, uid: int, days: float, edit: bool = False):
        expires = self.add_days(uid, days)
        name = self.acc.name_of(uid) or uid
        await self._notify(uid, f"🎉 <b>Premium activated!</b> +{days:g} days\n"
                                f"⏰ Valid until <b>{ui.when(expires)}</b>. Enjoy!")
        text = (f"💎 <b>{ui.esc(name)}</b> (<code>{uid}</code>) +{days:g} days\n"
                f"⏰ Until {ui.when(expires)}")
        return await self._show(event, ui.admin_screen((text, [[ui.B("👤 Open user", f"adv:{uid}"),
                                                ui.B("« Premium", "apm")]])), edit=edit)

    async def _block_from(self, event, msg) -> None:
        targets = await self.block_targets(msg)
        for cid, title in targets:
            self.acc.block(cid, title)
        await self._reload_all_forwards()
        names = "\n".join(f"• {ui.esc(t)} <code>{c}</code>" for c, t in targets[:15])
        more = f"\n…and {len(targets) - 15} more" if len(targets) > 15 else ""
        await self._show(event, ui.admin_screen((f"🚫 <b>Blocked {len(targets)} chat(s)</b>\n{names}{more}",
                                 [[ui.B("🚫 Blocked chats", "abl"), ui.B("« Admin", "ad")]])))

    async def _reload_all_forwards(self) -> None:
        for app, _ in list(self.ws.values()):
            try:
                await app.load_forwards()
            except Exception as e:
                log.warning("reload forwards %s: %s", app.uid, e)

    async def admin_text(self, event) -> None:
        kind = self.admin_wait["kind"]
        msg = event.message
        if kind == "premium":
            uid, days = await self._parse_user_days(msg.raw_text or "")
            self.admin_wait = None
            return await self._grant(event, uid, days)
        if kind == "rmpremium":
            uid, _ = await self._parse_user_days(msg.raw_text or "", need_days=False)
            self.admin_wait = None
            self.remove_plan(uid)
            await self._notify(uid, "➖ Your premium was removed by the owner.")
            return await self._show(event, ui.admin_screen((f"➖ Premium removed for <code>{uid}</code>.",
                                             [[ui.B("« Premium", "apm")]])))
        if kind == "start":
            from telethon.extensions import html as tl_html
            text = (msg.raw_text or "").strip()
            if text == "-":
                self.settings.start_text = ""
            else:
                # keep the owner's formatting (bold, quotes, premium emoji...) as HTML
                html_text = tl_html.unparse(msg.message or "", msg.entities or []) if msg.entities else text
                try:
                    html_text.format(name="", first="", bot="", id="", plan="")
                except (KeyError, IndexError, ValueError) as e:
                    raise ValueError(f"Unknown placeholder {e}. Use {{name}} {{bot}} {{plan}} {{id}}")
                self.settings.start_text = html_text[:900]
            self.acc.save_settings(self.settings)
            self.admin_wait = None
            await event.respond("✅ Start text saved. Preview:")
            return await self.send_start(event, ui.welcome(self.settings.trial_hours,
                                                           intro=await self.start_intro(event)))
        if kind in ("plans", "pay"):
            text = (msg.raw_text or "").strip()
            clear = text == "-"
            if kind == "plans":
                if not clear and not ui.parse_plans(text):
                    raise ValueError("Use days=price, e.g. 7=₹49, 30=₹149")
                self.settings.plans = "" if clear else ", ".join(f"{d}={p}" for d, p in ui.parse_plans(text))
            else:
                self.settings.pay_info = "" if clear else text[:500]
            self.acc.save_settings(self.settings)
            self.admin_wait = None
            return await self._show(event, ui.admin_premium(self))
        if kind == "block":
            await self._block_from(event, msg)
            self.admin_wait = None
            return
        if kind == "photo":
            text = (msg.raw_text or "").strip()
            if getattr(msg, "photo", None) or (getattr(msg, "document", None) and
                                               (msg.document.mime_type or "").startswith("image/")):
                data = await self.bot.download_media(msg, file=bytes)
                self.settings.start_photo = str(_save_start_photo(data))
            elif IMG_URL_RE.match(text):
                self.settings.start_photo = text
            else:
                raise ValueError("Send a photo, or a direct https:// image link.")
            self._start_media = None
            self.acc.save_settings(self.settings)
            self.admin_wait = None
            await event.respond("✅ Start photo saved. Preview:")
            return await self.send_start(event, ui.welcome(self.settings.trial_hours))

    async def hub_callback(self, event) -> None:
        parts = event.data.decode().split(":")
        k, uid = parts[0], event.sender_id
        # ---------------- users
        if k == "lg":
            return await self.begin_login(event)
        if k == "bm":
            return await self.begin_botmode(event)
        if k == "bx":
            await self.close_workspace(uid)
            self.acc.remove_botmode(uid)
            await event.answer("Stopped")
            return await self.show_welcome(event, edit=True)
        if k == "hw":
            back = [[ui.B("🔐 Login", "lg", "success"), ui.B("🤖 Without login", "bm")]] \
                if uid not in self.ws else [[ui.B("« Menu", "m")]]
            return await self._show(event, (ui.HOW, back), edit=True)
        if k == "otp":
            return await self.keypad_press(event, parts[1])
        if k == "ac":
            return await self._show(event, ui.profile_view(self, uid), edit=True)
        if k == "gp":
            return await self._show(event, ui.get_premium(self, uid), edit=True)
        if k == "bp":
            return await self.buy_request(event, int(parts[1]))
        if k == "lo":
            return await self._show(event, ui.confirm(
                "Log out? I stop forwarding for you and my session on your Telegram ends.",
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
            return await event.answer("🚫 Owner only", alert=True)
        return await self.admin_callback(event, k, parts)

    async def admin_callback(self, event, k: str, parts: list) -> None:
        st = self.settings
        self.admin_wait = None

        async def show(screen):
            return await self._show(event, screen, edit=True)

        def save():
            self.acc.save_settings(st)
            for u, (app, _) in self.ws.items():
                app.caps = self.caps(u)

        if k == "ad":
            return await show(ui.admin_view(self))
        if k == "aac":
            return await show(ui.admin_access(self))
        if k == "adm":
            st.mode = parts[1]
            save()
            return await show(ui.admin_access(self))
        if k == "alm":
            return await show(ui.admin_limits(self))
        if k in ("adj", "adx", "adt", "adp3"):
            up = parts[1] == "+"
            if k == "adp3":
                st.premium_max_jobs = max(1, min(20, st.premium_max_jobs + (1 if up else -1)))
            elif k == "adj":
                st.user_max_jobs = max(1, min(10, st.user_max_jobs + (1 if up else -1)))
            elif k == "adt":
                st.max_total_jobs = max(1, min(100, st.max_total_jobs + (1 if up else -1)))
            else:
                steps = [50, 100, 200, 500, 1000, 2000, 5000, 10000, 0]  # 0 = no limit (last)
                i = steps.index(st.user_max_batch) if st.user_max_batch in steps else len(steps) - 1
                st.user_max_batch = steps[max(0, min(len(steps) - 1, i + (1 if up else -1)))]
            save()
            return await show(ui.admin_limits(self))
        if k == "aet":
            return await show(await self.emoji_test(event.sender_id))
        if k == "ast":
            self.admin_wait = {"kind": "start"}
            return await show(ui.admin_prompt("start"))
        if k == "aem":
            st.premium_emoji = not st.premium_emoji
            emoji.set_enabled(st.premium_emoji)
            save()
            return await show(ui.admin_view(self))
        # premium
        if k == "apm":
            return await show(ui.admin_premium(self))
        if k == "apt":
            st.premium_enabled = not st.premium_enabled
            save()
            for u, (app, _) in self.ws.items():
                app.paused = not self.plan_ok(u)
            return await show(ui.admin_premium(self))
        if k == "atr":
            steps = [0, 1, 3, 6, 12, 24, 48, 72, 168]
            i = steps.index(st.trial_hours) if st.trial_hours in steps else 5
            st.trial_hours = steps[max(0, min(len(steps) - 1, i + (1 if parts[1] == "+" else -1)))]
            save()
            return await show(ui.admin_premium(self))
        if k in ("apP", "apI"):
            self.admin_wait = {"kind": "plans" if k == "apP" else "pay"}
            return await show(ui.admin_prompt(self.admin_wait["kind"]))
        if k == "prd":
            target = int(parts[1])
            await self._notify(target, "❌ Your premium request was declined. Message the owner "
                                       "if you think this is a mistake.")
            await event.answer("Declined")
            return await show(ui.admin_user(self, target))
        if k in ("apg", "apr"):
            self.admin_wait = {"kind": "premium" if k == "apg" else "rmpremium"}
            return await show(ui.admin_prompt(self.admin_wait["kind"]))
        if k == "apl":
            return await show(ui.admin_premium_list(self, int(parts[1])))
        if k == "apa":
            return await self._grant(event, int(parts[1]), float(parts[2]), edit=True)
        if k == "apx":
            target = int(parts[1])
            self.remove_plan(target)
            await self._notify(target, "➖ Your premium was removed by the owner.")
            return await show(ui.admin_user(self, target))
        # blocked chats
        if k == "abl":
            return await show(ui.admin_blocked(self))
        if k == "abk":
            self.admin_wait = {"kind": "block"}
            return await show(ui.admin_prompt("block"))
        if k == "aub":
            self.acc.unblock(int(parts[1]))
            await self._reload_all_forwards()
            await event.answer("✅ Unblocked")
            return await show(ui.admin_blocked(self))
        # start photo
        if k == "asp":
            return await show(ui.admin_photo(self))
        if k == "aps":
            self.admin_wait = {"kind": "photo"}
            return await show(ui.admin_prompt("photo"))
        if k == "apd":
            st.start_photo = ""
            self._start_media = None
            save()
            return await show(ui.admin_photo(self))
        # users & requests
        if k == "adu":
            return await show(ui.admin_users(self, int(parts[1])))
        if k == "adv":
            return await show(ui.admin_user(self, int(parts[1])))
        if k == "adb":
            target = int(parts[1])
            if target == self.owner:
                return await event.answer("You can't ban yourself", alert=True)
            if self.acc.access(target) == "banned":
                self.acc.set_access(target, None)
                await self._notify(target, "✅ You can use the bot again. /start")
            else:
                self.acc.set_access(target, "banned", self.acc.name_of(target))
                await self.close_workspace(target, logout=True)
                await self._drop_login(target)
            return await show(ui.admin_user(self, target))
        if k == "adl":
            target = int(parts[1])
            await self.close_workspace(target, logout=True)
            await self._notify(target, "🚪 You were logged out by the owner.")
            return await show(ui.admin_user(self, target))
        if k == "adp":
            return await show(ui.admin_pending(self))
        if k in ("ap", "dn"):
            target = int(parts[1])
            self.acc.set_access(target, "approved" if k == "ap" else None)
            if k == "ap":
                await self._notify(target, "✅ <b>Access approved!</b> Tap below to start.",
                                   [[ui.B("🔐 Login with Telegram", "lg", "success")],
                                    [ui.B("🤖 Use without login", "bm")]])
            else:
                await self._notify(target, "❌ Your access request was declined.")
            await event.answer("Approved" if k == "ap" else "Declined")
            return await show(ui.admin_pending(self))

    async def buy_request(self, event, days: int) -> None:
        """User picked a plan: owner gets a one-tap Grant button."""
        uid = event.sender_id
        plans = dict(ui.parse_plans(self.settings.plans))
        if days not in plans:
            return await event.answer("This plan is not available any more", alert=True)
        now = time.time()
        if now - self._buy_asked.get(uid, 0) < 600:
            await event.answer("Request already sent — the owner will reply soon", alert=True)
            return await self._show(event, ui.premium_requested(self, uid, days, plans[days]), edit=True)
        self._buy_asked[uid] = now
        sender = await event.get_sender()
        name = " ".join(filter(None, [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")])) \
            or self.acc.name_of(uid) or str(uid)
        uname = getattr(sender, "username", None)
        if not self.acc.name_of(uid):
            self.acc.set_access(uid, self.acc.access(uid) or "seen", name)
        await self._notify(self.owner, (
            f"💎 <b>Premium request</b>\n<blockquote>👤 {ui.esc(name)}"
            f"{' · @' + uname if uname else ''}\n🔑 <code>{uid}</code>\n"
            f"💰 <b>{days} days</b> · {ui.esc(plans[days])}\n"
            f"💎 now: {ui.plan_label(self.plan_status(uid))}</blockquote>"),
            ui.admin_screen(("", [[ui.B(f"✅ Grant {days} days", f"apa:{uid}:{days}", "success")],
                                  [ui.B("❌ Decline", f"prd:{uid}", "danger"),
                                   ui.B("👤 Open user", f"adv:{uid}")]]))[1])
        await self._show(event, ui.premium_requested(self, uid, days, plans[days]), edit=True)

    async def emoji_test(self, uid: int):
        """Send a raw premium-emoji message and report exactly what Telegram did."""
        ids = [emoji.PACK[e] for e in ("⚡", "💎", "✅")]
        html = "<b>Premium emoji test</b> " + " ".join(
            f'<tg-emoji emoji-id="{i}">{e}</tg-emoji>' for i, e in zip(ids, ("⚡", "💎", "✅")))
        send = getattr(self.bot, "_tgc_orig_send", self.bot.send_message)
        btn = types.KeyboardInlineButton("Icon test", types.InlineButtonTypeCallback(b"noop"),
                                         style=types.KeyboardButtonStyle(icon=ids[1]))
        try:
            msg = await send(uid, html, parse_mode="html", buttons=[[btn]])
        except Exception as e:
            verdict = f"❌ Telegram refused the message:\n<code>{ui.esc(str(e))[:300]}</code>"
        else:
            kept = sum(isinstance(x, types.MessageEntityCustomEmoji) for x in (msg.entities or []))
            rows = getattr(getattr(msg, "reply_markup", None), "rows", []) or []
            icon = any(getattr(getattr(b, "style", None), "icon", None) for r in rows for b in r.buttons)
            emoji.status = "working" if kept else "removed"
            verdict = (f"Text: <b>{kept}/3</b> premium emoji kept · Button icon: "
                       f"<b>{'kept' if icon else 'removed'}</b>\n\n"
                       + ("✅ Telegram allows this bot to use premium emoji." if kept else
                          "❌ Telegram removed them. Check in @BotFather → /mybots which account "
                          "owns this bot: <b>that</b> account needs Telegram Premium (another "
                          "premium account doesn't count)."))
        emoji.set_enabled(self.settings.premium_emoji)  # retry right away after the test
        return ui.admin_screen((f"<b>🔍 Premium emoji test</b>\n\n{verdict}",
                                [[ui.B("🔄 Refresh", "aet"), ui.B("« Admin", "ad")]]))

    # ============================================================ entry: access checks
    async def _allowed(self, event) -> bool:
        """Access mode / approval. Sends the reason and returns False when not allowed."""
        uid = event.sender_id
        acc = self.access(uid)
        if acc == "private":
            await event.respond("🔒 This bot is private.")
            return False
        if acc == "full":
            await event.respond("😕 The bot is full right now. Try again later.")
            return False
        if acc in ("request", "pending"):
            if acc == "request":
                sender = await event.get_sender()
                name = " ".join(filter(None, [getattr(sender, "first_name", ""),
                                              getattr(sender, "last_name", "")])) or str(uid)
                self.acc.set_access(uid, "pending", name)
                await self._notify(self.owner, f"📬 <b>Access request</b>\n{ui.esc(name)} · "
                                   f"<code>{uid}</code>",
                                   [[ui.B("✅ Approve", f"ap:{uid}", "success"),
                                     ui.B("❌ Deny", f"dn:{uid}", "danger")]])
            await event.respond("⏳ Request sent to the owner. You'll get a message here "
                                "when you're approved.")
            return False
        if not self.plan_ok(uid):
            await self._show(event, ui.plan_expired(self, uid))
            return False
        return True

    async def begin_botmode(self, event) -> None:
        uid = event.sender_id
        if uid in self.ws:
            return await self._show(event, await ui.main_menu(self.ws[uid][0]))
        if not await self._allowed(event):
            return
        sender = await event.get_sender()
        name = " ".join(filter(None, [getattr(sender, "first_name", ""),
                                      getattr(sender, "last_name", "")])) or str(uid)
        await self.ensure_trial(uid)
        self.acc.add_botmode(uid, name)
        app = await self.open_bot_workspace(uid)
        me = await self.bot.get_me()
        await self._show(event, (
            "🤖 <b>Without-login mode is on.</b>\n\n<blockquote>"
            f"🔑 Add @{getattr(me, 'username', 'this_bot')} as <b>admin</b> in your source and "
            "destination chats.\n📡 New forward → forward a message from each.\n"
            "✅ New posts are copied automatically.</blockquote>\n"
            "<i>Old posts, protected chats and saving links need 🔐 Login.</i>", None))
        await self._show(event, await ui.main_menu(app))

    # ============================================================ login flow
    async def begin_login(self, event) -> None:
        uid = event.sender_id
        if uid in self.ws and not self.ws[uid][0].botmode:
            return await event.respond("✅ You're already logged in.", buttons=[[ui.B("🏠 Menu", "m")]])
        if not await self._allowed(event):
            return
        await self.ensure_trial(uid)
        await self._drop_login(uid)
        self.logins[uid] = {"step": "phone", "t": time.time()}
        await event.respond(ui.login_phone(), parse_mode="html", buttons=[
            [Button.request_phone("📲 Share my number", resize=True, single_use=True)]])

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
                    raise ValueError("Please share YOUR number with the 📲 button.")
                phone = contact.phone_number
            elif PHONE_RE.match((msg.raw_text or "").strip()):
                phone = re.sub(r"[\s-]", "", msg.raw_text.strip())
            else:
                raise ValueError("Tap 📲 Share my number, or type it like +919876543210.")
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
            return await self._show(event, ui.code_prompt())
        if st["step"] == "code":
            code = re.sub(r"\D", "", msg.raw_text or "")
            try:
                await msg.delete()  # don't leave the login code in the chat
            except Exception:
                pass
            if not 5 <= len(code) <= 6:
                return await self._show(event, ui.code_prompt(
                    "⚠️ Send the 5-digit code with spaces, like <code>1 2 3 4 5</code>"))
            return await self._try_code(event, code)
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
        if key == "r":
            try:
                sent = await st["client"].send_code_request(st["phone"])
            except FloodWaitError as e:
                return await event.answer(f"Wait {e.seconds}s for a new code", alert=True)
            st.update(hash=sent.phone_code_hash)
            await event.answer("📨 New code sent")
            return await self._show(event, ui.code_prompt("📨 A new code was sent."), edit=True)
        return await event.answer()

    async def _try_code(self, event, code: str) -> None:
        uid = event.sender_id
        st = self.logins[uid]
        try:
            await st["client"].sign_in(phone=st["phone"], code=code, phone_code_hash=st["hash"])
        except PhoneCodeInvalidError:
            return await self._show(event, ui.code_prompt("❌ Wrong code, try again."))
        except PhoneCodeExpiredError:
            return await self._show(event, ui.code_prompt(
                "⌛ This code expired (or Telegram blocked it because it was sent without spaces). "
                "Tap ↩ New code and send it like <code>1 2 3 4 5</code>."))
        except SessionPasswordNeededError:
            st["step"] = "password"
            return await self._show(event, ui.login_password())
        return await self.finish_login(event)

    async def finish_login(self, event, edit: bool = False) -> None:
        uid = event.sender_id
        st = self.logins.pop(uid)
        client = st["client"]
        me = await client.get_me()
        name = " ".join(filter(None, [me.first_name, me.last_name])) or (me.username or str(me.id))
        self.acc.save_session(uid, client.session.save(), name, st["phone"])
        if uid in self.ws:  # was using without-login mode: upgrade, keep settings
            await self.close_workspace(uid)
        self.acc.remove_botmode(uid)
        app = await self.open_workspace(uid, client)
        await self._show(event, (f"✅ <b>Logged in as {ui.esc(name)}</b>\nEvery feature is unlocked.",
                                 None), edit=edit)
        await self._show(event, await ui.main_menu(app))


class _Chat:
    """Event-like target to send job cards to a user without an incoming message."""

    def __init__(self, bot, uid: int) -> None:
        self.bot, self.chat_id, self.sender_id = bot, uid, uid

    async def respond(self, text, **kw):
        return await self.bot.send_message(self.chat_id, text, **kw)


class _TextMsg:
    """Minimal message-like wrapper for text commands (/block <link>)."""

    def __init__(self, text: str) -> None:
        self.raw_text, self.fwd_from = text, None


def _save_start_photo(data: bytes) -> Path:
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise ValueError("That file is not an image") from e
    img = img.convert("RGB")
    img.thumbnail((2560, 2560))
    path = DATA_DIR / "start_photo.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=90)
    return path
