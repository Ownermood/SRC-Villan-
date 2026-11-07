"""Admin control panel.

Bot account: inline button panel (/start or /menu) + text commands.
User account only (no BOT_TOKEN): text commands with "." prefix from your own account.
"""

import asyncio
import logging
import time

import yaml
from telethon import events, functions, types, utils
from telethon.errors import MessageNotModifiedError

from tgclone import ui
from tgclone.app import App, run_forward
from tgclone.config import Forward, norm_ref
from tgclone.jobs import Job
from tgclone.save import parse_link, save_range

log = logging.getLogger("tgclone")

BOT_COMMANDS = {
    "menu": "Open control panel", "list": "List forwards", "add": "Add forward: src dest…",
    "clone": "Clone history: #n [fresh]", "batch": "Save posts: link count", "jobs": "Running jobs",
    "stop": "Stop job(s)", "status": "Bot status", "help": "How to use",
}


class Panel:
    def __init__(self, app: App, live: bool) -> None:
        self.app = app
        self.live = live
        self.c = app.control
        self.is_bot = self.c is app.bot
        self.p = "/" if self.is_bot else "."
        self.started = time.time()
        self.state = {}  # admin id -> wizard state dict

    # ------------------------------------------------------------- helpers
    async def show(self, event, screen, edit=False):
        text, buttons = screen
        if not self.is_bot:
            buttons = None  # user accounts can't send inline keyboards
        try:
            if edit:
                return await event.edit(text, buttons=buttons, parse_mode="html", link_preview=False)
            return await event.respond(text, buttons=buttons, parse_mode="html", link_preview=False)
        except MessageNotModifiedError:
            return None

    def save(self, plugins=False):
        self.app.save()
        if plugins:
            self.app.reload_plugins()

    async def reload_forwards(self):
        self.app.save()
        await self.app.load_forwards()

    def pick(self, arg: str) -> int:
        """'#2' -> index 1, or match by source."""
        fwds = self.app.cfg.forwards
        if arg.startswith("#") and arg[1:].isdigit():
            i = int(arg[1:]) - 1
            if 0 <= i < len(fwds):
                return i
            raise ValueError("No such forward number")
        ref = norm_ref(arg)
        for i, f in enumerate(fwds):
            if norm_ref(f.source) == ref:
                return i
        raise ValueError("Source not found")

    def checked(self, i: str, h: str) -> int:
        i = int(i)
        fwds = self.app.cfg.forwards
        if not (0 <= i < len(fwds)) or ui.fhash(fwds[i]) != int(h):
            raise LookupError("stale")
        return i

    async def chat_from_message(self, msg):
        """Chat ref from a forwarded message (best) or text."""
        fwd = msg.fwd_from
        if fwd and isinstance(fwd.from_id, (types.PeerChannel, types.PeerChat)):
            return utils.get_peer_id(fwd.from_id)
        if fwd and fwd.from_id is None and not msg.raw_text:
            raise ValueError("This chat hides its origin. Send its link or -100id instead.")
        return ui.chat_from_text(msg.raw_text)

    # ------------------------------------------------------------- jobs
    async def run_job(self, event, job: Job, fn):
        """Start a job and keep a live progress card updated."""
        self.app.jobs.max_jobs = self.app.cfg.max_jobs
        self.app.jobs.check(job)  # fail fast before sending a card
        card = await self.show(event, ui.job_card(job))
        done = asyncio.Event()

        async def finish(j):
            done.set()
            if card:
                try:
                    await self.show(card, ui.job_card(j), edit=True)
                except Exception:
                    pass
                if not self.is_bot:
                    return
                try:  # small ping so the admin notices completion
                    await self.c.send_message(card.chat_id, f"{'✅' if j.state == 'done' else 'ℹ️'} "
                                              f"Job #{j.id} {j.state}", reply_to=card.id)
                except Exception:
                    pass

        self.app.jobs.start(job, fn, finish)

        async def ticker():
            last = ""
            while not done.is_set():
                try:
                    await asyncio.wait_for(done.wait(), timeout=4)
                except asyncio.TimeoutError:
                    pass
                if done.is_set() or not card:
                    return
                text = job.render()
                if text != last:
                    last = text
                    try:
                        await self.show(card, ui.job_card(job), edit=True)
                    except Exception:
                        pass
        asyncio.create_task(ticker())
        return job

    async def start_clone(self, event, fwd: Forward, fresh: bool):
        app = self.app
        src = await app.resolve(fwd.source)
        dests = [await app.resolve(d) for d in fwd.dest]
        title = f"{await app.title(src)} → {len(dests)} chat(s){' (fresh)' if fresh else ''}"
        job = Job("clone", ui.esc(title), f"clone:{app.store.key(src, dests)}")
        await self.run_job(event, job, lambda j: run_forward(app, fwd, fresh=fresh, job=j))

    async def start_save(self, event, chat, first, last):
        app = self.app
        target = event.chat_id
        if app.cfg.save.target:
            target = await app.control.get_peer_id(app.cfg.save.target)
        n = last - first + 1
        job = Job("save", ui.esc(f"{chat} · {n} post(s)"), f"save:{chat}:{first}:{last}:{target}")

        async def fn(j):
            await save_range(app, chat, first, last, target, j)
        await self.run_job(event, job, fn)

    # ------------------------------------------------------------- commands
    async def cmd(self, event, name: str, args: str):
        app, p = self.app, self.p
        if name in ("start", "menu"):
            self.state.pop(event.sender_id, None)
            return await self.show(event, await ui.main_menu(app))
        if name == "help":
            return await self.show(event, (ui.HELP, [[ui.B("🏠 Menu", "m", "primary")]]))
        if name == "list":
            return await self.show(event, await ui.forwards_list(app))
        if name in ("add", "forward"):
            if not args:
                self.state[event.sender_id] = {"kind": "new_src"}
                return await self.show(event, ui.wizard_prompt("new_src", app))
            if ":" in args.split()[0] or "\n" in args:
                fwd = Forward(**yaml.safe_load(args))
            else:
                parts = args.split()
                if len(parts) < 2:
                    raise ValueError(f"Usage: {p}add <source> <dest> [dest2 …]")
                fwd = Forward(source=parts[0], dest=parts[1:])
            return await self.add_forward(event, fwd)
        if name == "remove":
            del app.cfg.forwards[self.pick(args)]
            await self.reload_forwards()
            return await event.respond("🗑 Removed")
        if name == "toggle":
            f = app.cfg.forwards[self.pick(args)]
            f.enabled = not f.enabled
            await self.reload_forwards()
            return await event.respond(f"{'🟢 Enabled' if f.enabled else '⏸ Paused'} {f.source}")
        if name == "style":
            if args not in ui.STYLES:
                raise ValueError(f"Choose: {', '.join(ui.STYLES)}")
            app.cfg.plugins.fmt.style = args
            app.cfg.plugins.fmt.check = args != "preserve"
            self.save(plugins=True)
            return await event.respond(f"🎨 Style: {args}")
        if name == "setchat":
            app.cfg.save.target = "" if args in ("", "off") else norm_ref(args)
            if app.cfg.save.target:
                await app.control.get_input_entity(app.cfg.save.target)
            self.save()
            return await event.respond(f"🎯 Save target: {app.cfg.save.target or 'this chat'}")
        if name == "clone":
            parts = args.split()
            if not parts:
                raise ValueError(f"Usage: {p}clone #1 [fresh]  or  {p}clone <source> <dest>")
            fresh = "fresh" in parts
            parts = [x for x in parts if x != "fresh"]
            if len(parts) >= 2:
                fwd = Forward(source=norm_ref(parts[0]), dest=[norm_ref(d) for d in parts[1:]])
            else:
                fwd = app.cfg.forwards[self.pick(parts[0])]
            return await self.start_clone(event, fwd, fresh)
        if name == "batch":
            parts = args.split()
            link = parse_link(parts[0]) if parts else None
            if not link:
                raise ValueError(f"Usage: {p}batch <t.me link> <count>")
            chat, first, last = link
            if len(parts) > 1:
                last = first + int(parts[1]) - 1
            return await self.start_save(event, chat, first, last)
        if name == "jobs":
            return await self.show(event, ui.jobs_view(app))
        if name in ("stop", "cancel"):
            if self.state.pop(event.sender_id, None):
                return await event.respond("✖️ Cancelled")
            n = app.jobs.stop(int(args.lstrip("#")) if args.lstrip("#").isdigit() else None)
            return await event.respond(f"⛔ Stopping {n} job(s)" if n else "Nothing running.")
        if name == "status":
            up = int(time.time() - self.started)
            return await event.respond(
                f"⏱ Up {up // 3600}h {up % 3600 // 60}m · Live {'on' if self.live else 'off'}\n"
                f"📡 {len(app.from_to)} sources · 🗂 {app.store.count()} mapped msgs\n"
                f"🧩 {', '.join(x.id_ for x in app.plugins) or 'no plugins'} · "
                f"⏳ {len(app.jobs.running())} job(s)")

    async def add_forward(self, event, fwd: Forward):
        app = self.app
        fwd.source = norm_ref(fwd.source)
        fwd.dest = [norm_ref(d) for d in fwd.dest]
        if not fwd.dest:
            raise ValueError("Add at least one destination")
        await app.resolve(fwd.source)  # validate before saving
        for d in fwd.dest:
            await app.resolve(d)
        try:
            app.cfg.forwards[self.pick(str(fwd.source))] = fwd
        except ValueError:
            app.cfg.forwards.append(fwd)
        await self.reload_forwards()
        i = self.pick(str(fwd.source))
        await event.respond("✅ Forward saved — live sync is on.")
        return await self.show(event, await ui.forward_view(app, i))

    # ------------------------------------------------------------- buttons
    async def callback(self, event):
        app = self.app
        parts = event.data.decode().split(":")
        k = parts[0]
        uid = event.sender_id
        try:
            if k == "noop":
                return await event.answer()
            if k == "m":
                self.state.pop(uid, None)
                return await self.show(event, await ui.main_menu(app), edit=True)
            if k == "x":
                self.state.pop(uid, None)
                await event.answer("Cancelled")
                return await self.show(event, await ui.main_menu(app), edit=True)
            if k == "hp":
                return await self.show(event, (ui.HELP, [[ui.B("« Back", "m")]]), edit=True)
            if k == "fl":
                return await self.show(event, await ui.forwards_list(app, int(parts[1])), edit=True)
            if k == "fv":
                return await self.show(event, await ui.forward_view(app, self.checked(*parts[1:])), edit=True)
            if k == "ft":
                i = self.checked(*parts[1:])
                f = app.cfg.forwards[i]
                f.enabled = not f.enabled
                await self.reload_forwards()
                await event.answer("🟢 Live sync on" if f.enabled else "⏸ Paused")
                return await self.show(event, await ui.forward_view(app, i), edit=True)
            if k == "fc":
                i = self.checked(*parts[1:])
                await event.answer("🔁 Clone started")
                return await self.start_clone(event, app.cfg.forwards[i], fresh=False)
            if k == "fx":
                i = self.checked(*parts[1:])
                return await self.show(event, ui.confirm(
                    "Re-clone <b>everything</b> from the first message? Already-sent posts will be sent again.",
                    f"fX:{parts[1]}:{parts[2]}", f"fv:{parts[1]}:{parts[2]}"), edit=True)
            if k == "fX":
                i = self.checked(*parts[1:])
                await event.answer("♻️ Fresh clone started")
                return await self.start_clone(event, app.cfg.forwards[i], fresh=True)
            if k == "fd":
                i = self.checked(*parts[1:])
                return await self.show(event, ui.confirm(
                    f"Delete forward <b>#{i + 1}</b>? Live sync for it stops.",
                    f"fD:{parts[1]}:{parts[2]}", f"fv:{parts[1]}:{parts[2]}"), edit=True)
            if k == "fD":
                i = self.checked(*parts[1:])
                del app.cfg.forwards[i]
                await self.reload_forwards()
                await event.answer("🗑 Deleted")
                return await self.show(event, await ui.forwards_list(app), edit=True)
            if k == "na":
                self.state[uid] = {"kind": "new_src"}
                return await self.show(event, ui.wizard_prompt("new_src", app), edit=True)
            if k == "nd":
                st = self.state.get(uid)
                if not st or st.get("kind") != "new_dest" or not st["dests"]:
                    return await event.answer("Add at least one destination first", alert=True)
                self.state.pop(uid, None)
                await event.answer("Saving…")
                return await self.add_forward(event, Forward(source=st["src"], dest=st["dests"]))
            if k == "sv":
                self.state[uid] = {"kind": "save"}
                return await self.show(event, ui.wizard_prompt("save", app), edit=True)
            if k == "jl":
                return await self.show(event, ui.jobs_view(app), edit=True)
            if k == "js":
                n = app.jobs.stop(int(parts[1]))
                await event.answer("⛔ Stopping…" if n else "Already finished")
                return await self.show(event, ui.jobs_view(app), edit=True)
            if k == "jS":
                n = app.jobs.stop()
                await event.answer(f"⛔ Stopping {n} job(s)")
                return await self.show(event, ui.jobs_view(app), edit=True)
            # plugins
            if k == "pl":
                return await self.show(event, ui.plugins_view(app), edit=True)
            if k == "pt":
                sec = getattr(app.cfg.plugins, parts[1])
                sec.check = not sec.check
                self.save(plugins=True)
                await event.answer(f"{parts[1]} {'on' if sec.check else 'off'}")
                return await self.show(event, ui.plugins_view(app), edit=True)
            if k == "ps":
                fmt = app.cfg.plugins.fmt
                fmt.style = ui.STYLES[(ui.STYLES.index(fmt.style) + 1) % len(ui.STYLES)] \
                    if fmt.style in ui.STYLES else "preserve"
                fmt.check = fmt.style != "preserve"
                self.save(plugins=True)
                return await self.show(event, ui.plugins_view(app), edit=True)
            if k == "pf":
                return await self.show(event, ui.filetypes_view(app), edit=True)
            if k == "pff":
                bl = app.cfg.plugins.filter.files.blacklist
                bl.remove(parts[1]) if parts[1] in bl else bl.append(parts[1])
                if bl:
                    app.cfg.plugins.filter.check = True
                self.save(plugins=True)
                return await self.show(event, ui.filetypes_view(app), edit=True)
            if k == "pw":
                self.state[uid] = {"kind": "pw", "field": parts[1]}
                return await self.show(event, ui.wizard_prompt(parts[1], app), edit=True)
            # settings
            if k == "st":
                return await self.show(event, ui.settings_view(app), edit=True)
            if k == "sx":
                key = parts[1]
                obj = app.cfg.live if key in ("edit_sync", "delete_sync") else app.cfg
                if key == "send_via_bot" and not (app.bot and app.user):
                    return await event.answer("Needs both SESSION_STRING and BOT_TOKEN", alert=True)
                setattr(obj, key, not getattr(obj, key))
                if key == "send_via_bot":
                    app.sender = app.bot if app.cfg.send_via_bot else app.reader
                self.save()
                return await self.show(event, ui.settings_view(app), edit=True)
            if k == "sd":
                sec = app.cfg.past if parts[1] == "past" else app.cfg.save
                sec.delay = ui.step(sec.delay, parts[2] == "+")
                self.save()
                return await self.show(event, ui.settings_view(app), edit=True)
            if k == "sg":
                self.state[uid] = {"kind": "target"}
                return await self.show(event, ui.wizard_prompt("target", app), edit=True)
            if k == "sgc":
                app.cfg.save.target = ""
                self.save()
                await event.answer("Saved posts come to this chat")
                return await self.show(event, ui.settings_view(app), edit=True)
            await event.answer("Unknown button")
        except LookupError:
            await event.answer("List changed — refreshed", alert=True)
            await self.show(event, await ui.forwards_list(app), edit=True)
        except ValueError as e:
            await event.answer(str(e)[:190], alert=True)

    # ------------------------------------------------------------- free text
    async def private_text(self, event):
        app, uid = self.app, event.sender_id
        st = self.state.get(uid)
        msg = event.message
        if st:
            kind = st["kind"]
            if kind == "new_src":
                ref = await self.chat_from_message(msg)
                await app.resolve(ref)
                self.state[uid] = {"kind": "new_dest", "src": ref, "src_title": await app.title(ref),
                                   "dests": [], "titles": []}
                return await self.show(event, ui.wizard_prompt("new_dest", app, self.state[uid]))
            if kind == "new_dest":
                ref = await self.chat_from_message(msg)
                await app.resolve(ref)
                if await app.resolve(ref) == await app.resolve(st["src"]):
                    raise ValueError("Destination can't be the source")
                if ref not in st["dests"]:
                    st["dests"].append(ref)
                    st["titles"].append(await app.title(ref))
                return await self.show(event, ui.wizard_prompt("new_dest", app, st))
            if kind == "save":
                link = parse_link(msg.raw_text)
                if not link:
                    raise ValueError("That's not a post link. Example: https://t.me/c/123456/78")
                self.state.pop(uid, None)
                return await self.start_save(event, *link)
            if kind == "target":
                ref = await self.chat_from_message(msg)
                await app.control.get_input_entity(ref)
                app.cfg.save.target = ref
                self.state.pop(uid, None)
                self.save()
                await event.respond(f"🎯 Saved posts will go to <b>{ui.esc(await app.title(ref))}</b>",
                                    parse_mode="html")
                return await self.show(event, ui.settings_view(app))
            if kind == "pw":
                note = ui.wizard_apply(st["field"], app, msg.raw_text or "")
                self.state.pop(uid, None)
                self.save(plugins=True)
                await event.respond(note)
                return await self.show(event, ui.plugins_view(app))
        link = parse_link(msg.raw_text or "")
        if link:
            return await self.start_save(event, *link)
        if self.is_bot:
            return await self.show(event, await ui.main_menu(app))

    # ------------------------------------------------------------- wiring
    def guard(self, fn):
        async def wrap(event):
            if event.sender_id not in self.app.admins:
                if isinstance(event, events.CallbackQuery.Event):
                    await event.answer("⛔ Not authorized", alert=True)
                elif event.is_private:
                    await event.respond("⛔ Not authorized.")
                raise events.StopPropagation
            try:
                await fn(event)
                if isinstance(event, events.CallbackQuery.Event) and not event._answered:
                    await event.answer()  # stop the loading spinner
            except (ValueError, IndexError) as e:
                await event.respond(f"⚠️ {e}") if not isinstance(
                    event, events.CallbackQuery.Event) else await event.answer(str(e)[:190], alert=True)
            except Exception as e:
                log.exception("Handler failed")
                try:
                    await event.respond(f"❌ Error: {e}")
                except Exception:
                    pass
            raise events.StopPropagation
        return wrap

    async def setup(self):
        prefix = "/" if self.is_bot else r"\."
        names = ["start", "menu", "help", "list", "add", "forward", "remove", "toggle", "style",
                 "setchat", "clone", "batch", "jobs", "stop", "cancel", "status"]
        pattern = rf"^{prefix}({'|'.join(names)})(?:@\w+)?(?:\s+([\s\S]*))?$"
        kw = {"pattern": pattern} if self.is_bot else {"pattern": pattern, "outgoing": True}

        async def on_cmd(event):
            await self.cmd(event, event.pattern_match.group(1),
                           (event.pattern_match.group(2) or "").strip())
        self.c.add_event_handler(self.guard(on_cmd), events.NewMessage(**kw))

        if self.is_bot:
            self.c.add_event_handler(self.guard(self.callback), events.CallbackQuery())
            text_ev = events.NewMessage(func=lambda e: e.is_private and not (e.raw_text or "").startswith("/"))
            try:
                await self.c(functions.bots.SetBotCommandsRequest(
                    scope=types.BotCommandScopeDefault(), lang_code="",
                    commands=[types.BotCommand(k, v) for k, v in BOT_COMMANDS.items()]))
            except Exception as e:
                log.warning("Could not set bot command menu: %s", e)
        else:
            text_ev = events.NewMessage(chats="me", outgoing=True,
                                        func=lambda e: not (e.raw_text or "").startswith("."))
        self.c.add_event_handler(self.guard(self.private_text), text_ev)
        log.info("Control panel ready (%s)", "bot buttons" if self.is_bot else "userbot . commands")


async def setup(app: App, live: bool) -> Panel:
    panel = Panel(app, live)
    await panel.setup()
    return panel
