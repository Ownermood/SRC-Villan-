"""Admin control panel.

Bot account: inline button panel (/start or /menu) + text commands.
User account only (no BOT_TOKEN): text commands with "." prefix from your own account.
"""

import asyncio
import json
import logging
import time

import yaml
from telethon import events, functions, types, utils
from telethon.errors import MessageNotModifiedError

from tgclone import thumbs, ui
from tgclone.app import App, run_forward
from tgclone.config import Forward, norm_ref
from tgclone.jobs import Job
from tgclone.save import parse_link, save_range

log = logging.getLogger("tgclone")

BOT_COMMANDS = {
    "menu": "Open control panel", "list": "List forwards", "add": "Add forward: src dest…",
    "clone": "Clone history: #n [fresh]", "batch": "Save posts: link count", "jobs": "Running jobs",
    "stop": "Stop job(s)", "thumb": "Thumbnail settings", "setthumb": "Upload your thumbnail",
    "delthumb": "Remove your thumbnail", "status": "Bot status", "help": "How to use",
}


class Panel:
    def __init__(self, app: App, live: bool, hub=None) -> None:
        self.app = app
        self.live = live
        self.hub = hub  # multi-user bot (None = single-user mode)
        self.c = app.control
        self.is_bot = self.c is app.bot
        self.p = "/" if self.is_bot else "."
        self.started = time.time()
        self.state = {}  # admin id -> wizard state dict

    # ------------------------------------------------------------- helpers
    async def show(self, event, screen, edit=False):
        if self.hub is not None:  # premium emoji + fallbacks live in the hub
            return await self.hub._show(event, screen, edit)
        text, buttons = screen
        if not self.is_bot:
            buttons = None  # user accounts can't send inline keyboards
        try:
            if edit:
                return await event.edit(text, buttons=buttons, parse_mode="html", link_preview=False)
            return await event.respond(text, buttons=buttons, parse_mode="html", link_preview=False)
        except MessageNotModifiedError:
            return None

    def help(self) -> str:
        h = self.hub
        return ui.help_text(h.settings.trial_hours if h else 24)

    async def check_chat(self, ref, role: str) -> None:
        """Block list + without-login rules (multi-user only)."""
        if self.hub is not None:
            await self.hub.check_chat(self.app, ref, role)

    def needs_login(self, what: str) -> None:
        if self.app.botmode:
            raise ValueError(f"{what} needs a login. 👤 My profile → 🔐 Login.")

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

    async def image_from(self, msg) -> bytes:
        """Bytes of a photo / image document in msg (or the message it replies to)."""
        for m in (msg, await msg.get_reply_message() if getattr(msg, "is_reply", False) else None):
            if m is None:
                continue
            doc = m.document
            is_img = m.photo or (doc is not None and (doc.mime_type or "").startswith("image/"))
            if is_img:
                if getattr(m.file, "size", 0) and m.file.size > 10 * 1024 * 1024:
                    raise ValueError("Image too big (max 10 MB)")
                return await self.c.download_media(m, file=bytes)
        raise ValueError("Send a photo (or an image file).")

    async def set_logo(self, event, msg):
        from tgclone.plugins.mark import logo_file
        LOGO = logo_file(self.app.cfg.home)
        import io
        from PIL import Image
        try:
            img = Image.open(io.BytesIO(await self.image_from(msg)))
            img.load()
        except ValueError:
            raise
        except Exception as e:
            raise ValueError("That file is not an image") from e
        img = img.convert("RGBA")
        img.thumbnail((1024, 1024))
        LOGO.parent.mkdir(parents=True, exist_ok=True)
        img.save(LOGO, "PNG")
        mark = self.app.cfg.plugins.mark
        mark.image, mark.check = "", True
        self.save(plugins=True)
        self.state.pop(event.sender_id, None)
        loaded = any(p.id_ == "mark" for p in self.app.plugins)
        await event.respond("✅ Watermark logo saved, watermark is on." if loaded else
                            "✅ Logo saved, but the watermark plugin couldn't load "
                            "(is ffmpeg installed?). Check data/tgclone.log.")
        return await self.show(event, ui.plugins_view(self.app))

    async def set_thumb(self, event, msg):
        size = thumbs.save_custom(self.app.cfg, await self.image_from(msg))
        self.app.cfg.thumb.mode = "custom"
        self.save()
        self.state.pop(event.sender_id, None)
        await event.respond(f"✅ Thumbnail saved ({size // 1024} KB) and mode set to <b>My image</b>.",
                            file=str(thumbs.custom_path(self.app.cfg)), parse_mode="html")
        return await self.show(event, ui.thumb_view(self.app))

    # ------------------------------------------------------------- jobs
    async def run_job(self, event, job: Job, fn, resume: tuple = None):
        """Start a job and keep a live progress card updated.

        resume=(kind, data): remembered so the job continues by itself after a restart.
        """
        caps = self.app.caps or {}
        self.app.jobs.max_jobs = min(self.app.cfg.max_jobs, caps.get("max_jobs", 99))
        if self.hub:
            self.hub.check_capacity()  # all users together
        self.app.jobs.check(job)  # fail fast before sending a card
        card = await self.show(event, ui.job_card(job))
        done = asyncio.Event()

        async def finish(j):
            done.set()
            # user stop / done / failed -> forget it; a shutdown keeps it for auto-resume
            if resume and (j.cancelled or j.state in ("done", "failed")):
                self.app.store.remove_pending(job.key)
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

        if resume:
            self.app.store.add_pending(job.key, resume[0], json.dumps(resume[1]))
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
        self.needs_login("Copying old posts")
        src = await app.resolve(fwd.source)
        dests = [await app.resolve(d) for d in fwd.dest]
        title = f"{await app.title(src)} → {len(dests)} chat(s){' (fresh)' if fresh else ''}"
        job = Job("clone", ui.esc(title), f"clone:{app.store.key(src, dests)}")
        data = {"forward": {**fwd.model_dump(), "source": src, "dest": dests}}  # resume = not fresh
        await self.run_job(event, job, lambda j: run_forward(app, fwd, fresh=fresh, job=j),
                           resume=("clone", data))

    async def start_save(self, event, chat, first, last, target=None):
        app = self.app
        self.needs_login("Saving posts")
        if target is None:
            target = event.chat_id
            if app.cfg.save.target:
                target = await app.control.get_peer_id(app.cfg.save.target)
        n = last - first + 1
        job = Job("save", ui.esc(f"{chat} · {n} post(s)"), f"save:{chat}:{first}:{last}:{target}")

        async def fn(j):
            await save_range(app, chat, first, last, target, j)
        await self.run_job(event, job, fn, resume=("save", {"chat": chat, "first": first,
                                                           "last": last, "target": target}))

    # ------------------------------------------------------------- commands
    async def cmd(self, event, name: str, args: str):
        app, p = self.app, self.p
        if name in ("start", "menu"):
            self.state.pop(event.sender_id, None)
            if self.hub is not None:
                return await self.hub.send_start(event, await ui.main_menu(app))
            return await self.show(event, await ui.main_menu(app))
        if name == "help":
            return await self.show(event, (self.help(), [[ui.B("🏠 Menu", "m", "primary")]]))
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
        if name == "thumb":
            return await self.show(event, ui.thumb_view(app))
        if name == "setthumb":
            try:
                return await self.set_thumb(event, event.message)
            except ValueError:
                self.state[event.sender_id] = {"kind": "thumb"}
                return await self.show(event, ui.wizard_prompt("thumb", app))
        if name == "delthumb":
            thumbs.delete_custom(app.cfg)
            if app.cfg.thumb.mode == "custom":
                app.cfg.thumb.mode = "original"
            self.save()
            return await event.respond("🗑 Your thumbnail was removed. Mode: 📌 Original.")
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
        await self.check_chat(fwd.source, "source")
        for d in fwd.dest:
            await app.resolve(d)
            await self.check_chat(d, "dest")
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
                return await self.show(event, (self.help(), [[ui.B("« Back", "m")]]), edit=True)
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
            if k == "cl":
                self.needs_login("Copying old posts")
                self.state[uid] = {"kind": "clone_src"}
                return await self.show(event, ui.wizard_prompt("clone_src", app), edit=True)
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
            if k == "wl":
                self.state[uid] = {"kind": "logo"}
                return await self.show(event, ui.wizard_prompt("logo", app), edit=True)
            if k == "wp":
                app.cfg.plugins.mark.position = ui.cycle(ui.MARK_POSITIONS, app.cfg.plugins.mark.position)
                self.save(plugins=True)
                return await self.show(event, ui.plugins_view(app), edit=True)
            if k == "wz":
                app.cfg.plugins.mark.size = ui.cycle(ui.MARK_SIZES, app.cfg.plugins.mark.size)
                self.save(plugins=True)
                return await self.show(event, ui.plugins_view(app), edit=True)
            if k == "ol":
                app.cfg.plugins.ocr.lang = ui.cycle(ui.OCR_LANGS, app.cfg.plugins.ocr.lang)
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
                if key == "topics":
                    app.cfg.topics.enabled = not app.cfg.topics.enabled
                    self.save()
                    return await self.show(event, ui.settings_view(app), edit=True)
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
            # thumbnail
            if k == "th":
                self.state.pop(uid, None)
                return await self.show(event, ui.thumb_view(app), edit=True)
            if k == "thm":
                if parts[1] not in ui.THUMB_MODES:
                    return await event.answer("Unknown mode")
                app.cfg.thumb.mode = parts[1]
                self.save()
                if parts[1] == "custom" and not thumbs.has_custom(app.cfg):
                    self.state[uid] = {"kind": "thumb"}
                    await event.answer("Now send your image")
                    return await self.show(event, ui.wizard_prompt("thumb", app), edit=True)
                await event.answer(ui.THUMB_MODES[parts[1]])
                return await self.show(event, ui.thumb_view(app), edit=True)
            if k == "thu":
                self.state[uid] = {"kind": "thumb"}
                return await self.show(event, ui.wizard_prompt("thumb", app), edit=True)
            if k == "thp":
                if not thumbs.has_custom(app.cfg):
                    return await event.answer("No image uploaded yet", alert=True)
                await event.answer()
                return await event.respond("👁 Your current thumbnail", file=str(thumbs.custom_path(app.cfg)))
            if k == "thd":
                thumbs.delete_custom(app.cfg)
                if app.cfg.thumb.mode == "custom":
                    app.cfg.thumb.mode = "original"
                self.save()
                await event.answer("🗑 Removed")
                return await self.show(event, ui.thumb_view(app), edit=True)
            if k == "tht":
                tl = app.cfg.thumb.types
                tl.remove(parts[1]) if parts[1] in tl else tl.append(parts[1])
                self.save()
                return await self.show(event, ui.thumb_view(app), edit=True)
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
                await self.check_chat(ref, "source")
                self.state[uid] = {"kind": "new_dest", "src": ref, "src_title": await app.title(ref),
                                   "dests": [], "titles": []}
                return await self.show(event, ui.wizard_prompt("new_dest", app, self.state[uid]))
            if kind == "new_dest":
                ref = await self.chat_from_message(msg)
                await app.resolve(ref)
                if await app.resolve(ref) == await app.resolve(st["src"]):
                    raise ValueError("Destination can't be the source")
                await self.check_chat(ref, "dest")
                if ref not in st["dests"]:
                    st["dests"].append(ref)
                    st["titles"].append(await app.title(ref))
                return await self.show(event, ui.wizard_prompt("new_dest", app, st))
            if kind == "clone_src":
                ref = await self.chat_from_message(msg)
                await app.resolve(ref)
                await self.check_chat(ref, "source")
                self.state[uid] = {"kind": "clone_dest", "src": ref, "src_title": await app.title(ref)}
                return await self.show(event, ui.wizard_prompt("clone_dest", app, self.state[uid]))
            if kind == "clone_dest":
                ref = await self.chat_from_message(msg)
                if await app.resolve(ref) == await app.resolve(st["src"]):
                    raise ValueError("Destination can't be the source")
                await self.check_chat(ref, "dest")
                self.state.pop(uid, None)
                return await self.start_clone(event, Forward(source=norm_ref(st["src"]),
                                                             dest=[norm_ref(ref)]), fresh=False)
            if kind == "save":
                link = parse_link(msg.raw_text)
                if not link:
                    raise ValueError("That's not a post link. Example: https://t.me/c/123456/78")
                self.state.pop(uid, None)
                return await self.start_save(event, *link)
            if kind == "thumb":
                return await self.set_thumb(event, msg)
            if kind == "logo":
                return await self.set_logo(event, msg)
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
                 "setchat", "clone", "batch", "jobs", "stop", "cancel", "status",
                 "thumb", "setthumb", "delthumb"]
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
