"""Admin control panel. Bot uses /cmd, userbot (no BOT_TOKEN) uses .cmd from own account."""

import asyncio
import logging
import time

import yaml
from telethon import events

from tgclone.app import App, run_forward
from tgclone.config import Forward, norm_ref
from tgclone.plugins import STYLE_CODES
from tgclone.save import parse_link, save_range

log = logging.getLogger("tgclone")

HELP = """**tgclone commands**
`{p}list` - show forwards
`{p}add <source> <dest> [dest2 ...]` - add/replace forward
  (or YAML: `{p}add source: a` newline `dest: [b, c]`)
`{p}remove <source|#n>` - remove forward
`{p}toggle <#n>` - enable / disable forward
`{p}clone <#n> [fresh]` - clone full history of forward #n
`{p}clone <source> <dest>` - one-off history clone
`{p}batch <t.me link> <count>` - save posts from link onwards
`{p}setchat <chat|off>` - where saved posts go
`{p}style <preserve|plain|bold|italics|code|strike>`
`{p}stop` - cancel running clone/batch
`{p}status` - status
Send any **t.me post link** (or `link/10-50` range) to save it."""


def setup(app: App, live: bool) -> None:
    c = app.control
    is_bot = c is app.bot
    p = "/" if is_bot else "."
    prefix_re = "/" if is_bot else r"\."
    started = time.time()
    cancel = {"flag": False}

    def cmd(name):
        kw = {"pattern": rf"^{prefix_re}{name}(?:@\w+)?(?:\s|$)"}
        if not is_bot:
            kw["outgoing"] = True
        return events.NewMessage(**kw)

    def admin(fn):
        async def wrap(event):
            if event.sender_id not in app.admins:
                if event.is_private:
                    await event.respond("Not authorized.")
                raise events.StopPropagation
            try:
                await fn(event, _args(event.raw_text))
            except (ValueError, IndexError) as e:
                await event.respond(f"Error: {e}")
            except Exception as e:
                log.exception("Command failed")
                await event.respond(f"Error: {e}")
            raise events.StopPropagation
        return wrap

    async def reload_and_save():
        app.save()
        await app.load_forwards()

    def pick(arg: str):
        """'#2' -> forward index 1, else source match."""
        if arg.startswith("#") and arg[1:].isdigit():
            i = int(arg[1:]) - 1
            if 0 <= i < len(app.cfg.forwards):
                return i
            raise ValueError("No such forward number")
        ref = norm_ref(arg)
        for i, f in enumerate(app.cfg.forwards):
            if norm_ref(f.source) == ref:
                return i
        raise ValueError("Source not found")

    def start_job(event, title, coro_fn):
        if app.job and not app.job.done():
            raise ValueError(f"Busy: {app.job_info}. Use {p}stop first.")
        cancel["flag"] = False
        app.job_info = title

        async def runner():
            try:
                await coro_fn()
            except asyncio.CancelledError:
                await event.respond(f"Stopped: {title}")
            except Exception as e:
                log.exception("Job failed")
                await event.respond(f"Failed: {title}\n{e}")
        app.job = asyncio.create_task(runner())

    # -------------------------------------------------------------- commands
    async def h_help(event, args):
        await event.respond(HELP.format(p=p))

    async def h_list(event, args):
        if not app.cfg.forwards:
            return await event.respond("No forwards set.")
        lines = [f"#{i + 1} {'✅' if f.enabled else '⏸'} `{f.source}` → `{f.dest}`"
                 f"{' ' + f.name if f.name else ''}"
                 for i, f in enumerate(app.cfg.forwards)]
        await event.respond("\n".join(lines))

    async def h_add(event, args):
        if not args:
            return await event.respond(HELP.format(p=p))
        if ":" in args.split()[0] or "\n" in args:
            fwd = Forward(**yaml.safe_load(args))
        else:
            parts = args.split()
            if len(parts) < 2:
                raise ValueError("Need source and at least one dest")
            fwd = Forward(source=parts[0], dest=parts[1:])
        fwd.source = norm_ref(fwd.source)
        fwd.dest = [norm_ref(d) for d in fwd.dest]
        await app.resolve(fwd.source)  # validate before saving
        for d in fwd.dest:
            await app.resolve(d)
        try:
            app.cfg.forwards[pick(str(fwd.source))] = fwd
        except ValueError:
            app.cfg.forwards.append(fwd)
        await reload_and_save()
        await event.respond("Saved ✅")

    async def h_remove(event, args):
        del app.cfg.forwards[pick(args)]
        await reload_and_save()
        await event.respond("Removed ✅")

    async def h_toggle(event, args):
        f = app.cfg.forwards[pick(args)]
        f.enabled = not f.enabled
        await reload_and_save()
        await event.respond(f"{'Enabled' if f.enabled else 'Disabled'} `{f.source}`")

    async def h_style(event, args):
        if args not in (*STYLE_CODES, "preserve"):
            raise ValueError(f"Choose: preserve, {', '.join(STYLE_CODES)}")
        app.cfg.plugins.fmt.style = args
        app.cfg.plugins.fmt.check = args != "preserve"
        app.save()
        await event.respond("Saved ✅ (restart to apply plugin changes)")

    async def h_setchat(event, args):
        app.cfg.save.target = "" if args in ("", "off") else norm_ref(args)
        if app.cfg.save.target:
            await app.control.get_input_entity(app.cfg.save.target)
        app.save()
        await event.respond(f"Save target: `{app.cfg.save.target or 'this chat'}`")

    async def h_clone(event, args):
        parts = args.split()
        if not parts:
            raise ValueError(f"Usage: {p}clone #1 [fresh]  or  {p}clone <source> <dest>")
        fresh = "fresh" in parts
        parts = [x for x in parts if x != "fresh"]
        if len(parts) >= 2:
            fwd = Forward(source=norm_ref(parts[0]), dest=[norm_ref(d) for d in parts[1:]])
        else:
            fwd = app.cfg.forwards[pick(parts[0])]
        status = await event.respond(f"Cloning `{fwd.source}` …")

        async def prog(count, last_id):
            await _edit(status, f"Cloning `{fwd.source}`: {count} msgs (last id {last_id})")

        async def job():
            n = await run_forward(app, fwd, fresh=fresh, progress=prog)
            await _edit(status, f"Clone done ✅ `{fwd.source}`: {n} messages")
        start_job(event, f"clone {fwd.source}", job)

    async def do_save(event, chat, first, last):
        target = event.chat_id
        if app.cfg.save.target:
            target = await app.control.get_peer_id(app.cfg.save.target)
        status = await event.respond(f"Saving {last - first + 1} post(s)…")
        t0 = [0.0]

        async def prog(sent, failed, last_id):
            if time.time() - t0[0] > 5:  # avoid edit flood
                t0[0] = time.time()
                await _edit(status, f"Saving: {sent} sent, {failed} failed (id {last_id}/{last})")

        async def job():
            sent, failed = await save_range(app, chat, first, last, target, prog,
                                            lambda: cancel["flag"])
            await _edit(status, f"Done ✅ {sent} sent, {failed} failed")
        start_job(event, f"save {chat}:{first}-{last}", job)

    async def h_batch(event, args):
        parts = args.split()
        link = parse_link(parts[0]) if parts else None
        if not link:
            raise ValueError(f"Usage: {p}batch <t.me link> <count>")
        chat, first, last = link
        if len(parts) > 1:
            last = first + int(parts[1]) - 1
        await do_save(event, chat, first, last)

    async def h_stop(event, args):
        if not app.job or app.job.done():
            return await event.respond("Nothing running.")
        cancel["flag"] = True
        app.job.cancel()
        await event.respond("Stopping…")

    async def h_status(event, args):
        up = int(time.time() - started)
        job = app.job_info if app.job and not app.job.done() else "idle"
        await event.respond(
            f"Up: {up // 3600}h {up % 3600 // 60}m\nLive: {'on' if live else 'off'}\n"
            f"Active sources: {len(app.from_to)}\nMapped msgs: {app.store.count()}\n"
            f"Plugins: {', '.join(x.id_ for x in app.plugins) or 'none'}\nJob: {job}")

    handlers = {"start": h_help, "help": h_help, "list": h_list, "add": h_add,
                "forward": h_add, "remove": h_remove, "toggle": h_toggle, "style": h_style,
                "setchat": h_setchat, "clone": h_clone, "batch": h_batch, "stop": h_stop,
                "cancel": h_stop, "status": h_status}
    for name, fn in handlers.items():
        c.add_event_handler(admin(fn), cmd(name))

    # plain t.me link -> save (private chat with bot, or own Saved Messages for userbot)
    async def on_link(event):
        if event.raw_text.startswith(("/", ".")):
            return
        link = parse_link(event.raw_text)
        if link:
            await do_save(event, *link)

    link_ev = events.NewMessage(func=lambda e: e.is_private) if is_bot else \
        events.NewMessage(chats="me", outgoing=True)
    c.add_event_handler(admin(lambda e, a: on_link(e)), link_ev)
    log.info("Control commands ready (prefix %s)", p)


def _args(text: str) -> str:
    parts = (text or "").split(None, 1)
    return parts[1].strip() if len(parts) == 2 else ""


async def _edit(msg, text):
    try:
        await msg.edit(text)
    except Exception:
        pass
