"""Control panel screens: every function returns (html_text, buttons).

Buttons use Telegram's colored inline buttons (Telethon >= 1.45, layer 229):
style = "primary" (blue) | "success" (green) | "danger" (red).
"""

import html
import re
import zlib
from typing import Optional, Tuple

from telethon import Button

from tgclone.app import App
from tgclone.config import Forward
from tgclone.jobs import Job
from tgclone.save import parse_link
from tgclone import thumbs

Screen = Tuple[str, Optional[list]]
PAGE = 8
FILE_TYPES_UI = [("text", "💬 Text"), ("photo", "🖼 Photo"), ("video", "🎬 Video"),
                 ("gif", "🎞 GIF"), ("audio", "🎵 Audio"), ("voice", "🎙 Voice"),
                 ("video_note", "⭕ Round"), ("sticker", "🧸 Sticker"), ("document", "📄 File"),
                 ("poll", "📊 Poll"), ("contact", "👤 Contact"), ("other", "📍 Other")]
STYLES = ["preserve", "plain", "bold", "italics", "code", "strike"]
DELAYS = [0, 0.5, 1, 2, 3, 5, 10, 20, 30, 60]
CHAT_LINK = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.(?:me|dog))/(?:c/(\d+)|(\+[\w-]+|joinchat/[\w-]+)|([A-Za-z]\w{3,}))/?$")


def B(text: str, data: str, style: Optional[str] = None):
    return Button.inline(text, data.encode(), style=style)


def esc(x) -> str:
    return html.escape(str(x))


def fhash(f: Forward) -> int:
    """Short checksum so a stale button never acts on the wrong forward."""
    return zlib.crc32(f"{f.source}|{f.dest}".encode()) % 100000


def on_off(v: bool) -> str:
    return "✅" if v else "❌"


def step(value: float, up: bool) -> float:
    if up:
        return next((d for d in DELAYS if d > value), DELAYS[-1])
    return next((d for d in reversed(DELAYS) if d < value), 0)


def chat_from_text(text: str):
    """Chat ref from @user / id / t.me link / post link. Raises ValueError with a hint."""
    t = (text or "").strip()
    if not t:
        raise ValueError("Send a chat.")
    link = parse_link(t)
    if link:
        return link[0]
    m = CHAT_LINK.match(t)
    if m:
        if m.group(2):
            raise ValueError("Invite links can't be resolved. Join the chat with the user account, "
                             "then forward a message from it or send its -100 id.")
        return int(f"-100{m.group(1)}") if m.group(1) else m.group(3)
    if t.startswith("@") and len(t) > 4:
        return t
    if t.lstrip("-").isdigit():
        return int(t)
    raise ValueError("Couldn't read a chat. Forward a message from it, or send @username / -100id / link.")


# ------------------------------------------------------------------ screens
DEFAULT_START = ("<blockquote>🤝 Welcome: <b>{name}</b> to <b>{bot}</b> — clone channels & groups "
                 "on Telegram</blockquote>\n<blockquote>💡 Tap ❔ Help to learn every feature.</blockquote>")


_QUOTE_TAG = re.compile(r"(<blockquote[^>]*>|</blockquote>)")


def bold_blocks(text: str) -> str:
    """Bold every text run, opening <b> again inside each quote (never one <b> around a quote)."""
    out = []
    for part in _QUOTE_TAG.split(text):
        if not part or _QUOTE_TAG.fullmatch(part) or not part.strip():
            out.append(part)
            continue
        lead = part[:len(part) - len(part.lstrip("\n"))]
        trail = part[len(part.rstrip("\n")):]
        out.append(f"{lead}<b>{part.strip(chr(10))}</b>{trail}")
    return "".join(out)


async def main_menu(app: App, intro: str = "") -> Screen:
    run = app.jobs.running()
    dests = {d for v in app.from_to.values() for d in v}
    plugins = ", ".join(p.id_ for p in app.plugins) or "none"
    hub = getattr(app, "hub", None)
    status = [
        f"{'🟢' if app.from_to and not app.paused else '⚪'} Live · {len(app.from_to)} source(s) → {len(dests)} chat(s)"
        + (" · ⏸ paused" if app.paused else ""),
        f"{'⏳' if run else '💤'} Jobs · {len(run)} running",
        f"🧩 Plugins · {esc(plugins)}",
    ]
    name = esc(getattr(hub, "bot_name", "") or "SRC Villan Bot")
    # bold inside each block (Telegram drops a bold that wraps quotes); t.me in code, not a link
    text = f"⚡ {name} • TG Clone"
    if intro:
        text += "\n" + re.sub(r"</?b>", "", intro).strip()
    text += "\n\n📊 Status\n<blockquote>" + "\n".join(status) + "</blockquote>"
    if hub is not None:
        mode = ("🤖 Mode · without login (new posts only)" if app.botmode
                else "🔐 Mode · Logged in (all features unlocked)")
        text += (f"\n\n👤 Account\n<blockquote>💎 Plan · {plan_label(hub.plan_status(app.uid))}\n"
                 f"{mode}</blockquote>")
    text += "\n" + ("<i>💡 Add me as admin in your chats, then ➕ New forward.</i>" if app.botmode
                    else "<i>💡 Send any post link (<code>t.me/…</code>) to save it.</i>")
    text = bold_blocks(text)
    buttons = [
        [B("🔁 Clone Channel/Group", "cl", "success")],
        [B("📡 Forwards", "fl:0", "primary"), B("➕ New forward", "na", "primary")],
        [B("📥 Save posts", "sv"), B(f"⏳ Jobs ({len(run)})" if run else "⏳ Jobs", "jl")],
        [B("🧩 Plugins", "pl"), B("⚙️ Settings", "st")],
        [B("👤 My profile", "ac") if hub is not None else B("📊 Status", "noop"), B("❔ Help", "hp")],
    ]
    if hub is not None and getattr(hub, "is_admin", lambda u: u == hub.owner)(app.uid):
        buttons.append([B("🛡 Admin panel", "ad", "danger")])
    buttons.append([B("🔄 Refresh", "m")])
    return text, buttons


async def forwards_list(app: App, page: int = 0) -> Screen:
    fwds = app.cfg.forwards
    if not fwds:
        return ("<b>📋 Forwards</b>\n\n<blockquote>No forwards yet.</blockquote>\n"
                "Tap <b>New forward</b> to connect a source to destinations.",
                [[B("➕ New forward", "na", "success")], [B("« Back", "m")]])
    pages = max(1, (len(fwds) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    rows = []
    for i in range(page * PAGE, min(len(fwds), (page + 1) * PAGE)):
        f = fwds[i]
        name = f.name or await app.title(f.source)
        rows.append([B(f"{'🟢' if f.enabled else '⏸'} #{i + 1} · {name[:28]} → {len(f.dest)}",
                       f"fv:{i}:{fhash(f)}")])
    nav = []
    if page > 0:
        nav.append(B("‹ Prev", f"fl:{page - 1}"))
    if pages > 1:
        nav.append(B(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(B("Next ›", f"fl:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([B("➕ New forward", "na", "success"), B("« Back", "m")])
    return f"<b>📋 Forwards</b> · {len(fwds)} total\n<i>Tap one to manage it.</i>", rows


async def forward_view(app: App, i: int) -> Screen:
    f = app.cfg.forwards[i]
    h = fhash(f)
    src_t = await app.title(f.source)
    dest_lines = []
    for d in f.dest:
        dest_lines.append(f"  • {esc(await app.title(d))} <code>{esc(d)}</code>")
    progress = ""
    try:
        src = await app.resolve(f.source)
        dests = [await app.resolve(d) for d in f.dest]
        last = app.store.get_progress(app.store.key(src, dests))
        if last:
            progress = f"\n📍 History cloned up to id <b>{last}</b>"
    except Exception:
        progress = "\n⚠️ Can't resolve this chat with the reading account"
    text = (
        f"<b>📡 #{i + 1}{' · ' + esc(f.name) if f.name else ''}</b>\n\n"
        f"<blockquote><b>From</b>  {esc(src_t)} <code>{esc(f.source)}</code>\n"
        f"<b>To</b>\n" + "\n".join(dest_lines) + "</blockquote>\n"
        f"State: {'🟢 live sync on' if f.enabled else '⏸ paused'}{progress}"
    )
    buttons = [
        [B("⏸ Pause" if f.enabled else "▶️ Resume", f"ft:{i}:{h}", None if f.enabled else "success"),
         B("🔁 Clone history", f"fc:{i}:{h}", "primary")],
        [B("♻️ Re-clone from start", f"fx:{i}:{h}"), B("🗑 Delete", f"fd:{i}:{h}", "danger")],
        [B("« Back", "fl:0")],
    ]
    return text, buttons


def confirm(question: str, yes_data: str, back: str, danger: bool = True) -> Screen:
    return (f"<b>⚠️ Are you sure?</b>\n\n{question}",
            [[B("✔️ Yes", yes_data, "danger" if danger else "success"), B("✖️ Cancel", back)]])


def jobs_view(app: App) -> Screen:
    jobs = list(app.jobs.jobs.values())[-8:]
    if not jobs:
        return ("<b>⏳ Jobs</b>\n\n<blockquote>Nothing running.</blockquote>\n"
                "Start one from a forward (🔁 Clone history) or send a post link.",
                [[B("🔄 Refresh", "jl"), B("« Back", "m")]])
    text = "<b>⏳ Jobs</b>\n\n" + "\n\n".join(f"<blockquote>{j.render()}</blockquote>" for j in jobs)
    rows = [[B(f"⛔ Stop #{j.id}", f"js:{j.id}", "danger")] for j in jobs if j.state == "running"]
    if len(app.jobs.running()) > 1:
        rows.append([B("⛔ Stop all", "jS", "danger")])
    rows.append([B("🔄 Refresh", "jl"), B("« Back", "m")])
    return text, rows


def job_card(job: Job) -> Screen:
    if job.state == "running":
        return job.render(), [[B("⛔ Stop", f"js:{job.id}", "danger"), B("⏳ All jobs", "jl")]]
    return job.render(), [[B("⏳ Jobs", "jl"), B("🏠 Menu", "m", "primary")]]


def plugins_view(app: App) -> Screen:
    p = app.cfg.plugins
    loaded = {x.id_ for x in app.plugins}
    warn = [n for n in ("ocr", "mark") if getattr(p, n).check and n not in loaded]
    blocked = len(p.filter.files.blacklist)
    text = (
        "<b>🧩 Plugins</b> · <i>applied in this order</i>\n\n<blockquote>"
        f"{on_off(p.filter.check)} <b>Filter</b> · {blocked} type(s) blocked, "
        f"{len(p.filter.text.blacklist)} blocked / {len(p.filter.text.whitelist)} required word(s)\n"
        f"{on_off(p.ocr.check)} <b>OCR</b> · photo → text ({esc(p.ocr.lang)})\n"
        f"{on_off(p.replace.check)} <b>Replace</b> · {len(p.replace.text)} rule(s)\n"
        f"{on_off(p.fmt.check)} <b>Format</b> · {esc(p.fmt.style)}\n"
        f"{on_off(p.caption.check)} <b>Caption</b> · header {len(p.caption.header)} / footer {len(p.caption.footer)} chars\n"
        f"{on_off(p.mark.check)} <b>Watermark</b> · {esc(p.mark.position)}, {int(p.mark.size * 100)}% wide, "
        f"logo {'✅' if mark_logo_ok(app) else '— not set'}</blockquote>"
    )
    if warn:
        text += f"\n⚠️ Not loaded (missing dependency): {', '.join(warn)}"

    def t(pid, label):
        on = getattr(p, pid).check
        return B(f"{'✅' if on else '▫️'} {label}", f"pt:{pid}", "success" if on else None)

    buttons = [
        [t("filter", "Filter"), t("replace", "Replace")],
        [t("fmt", "Format"), t("caption", "Caption")],
        [t("ocr", "OCR"), t("mark", "Watermark")],
        [B(f"🎨 Style: {p.fmt.style}", "ps"), B("🗂 File types", "pf")],
        [B("🚫 Block words", "pw:black"), B("✔️ Required words", "pw:white")],
        [B("🔁 Replace rules", "pw:repl"), B("🧾 Header", "pw:head"), B("🧾 Footer", "pw:foot")],
        [B("💧 Watermark logo", "wl"), B(f"📍 {p.mark.position}", "wp"),
         B(f"📏 {int(p.mark.size * 100)}%", "wz")],
        [B(f"🔤 OCR language: {p.ocr.lang}", "ol")],
        [B("« Back", "m")],
    ]
    return text, buttons


MARK_POSITIONS = ["bottom_right", "bottom_left", "top_right", "top_left", "centre"]
MARK_SIZES = [0.1, 0.15, 0.2, 0.3, 0.4, 0.5]
OCR_LANGS = ["eng", "eng+hin", "hin"]


def mark_logo_ok(app: App) -> bool:
    from tgclone.plugins.mark import logo_file
    img = app.cfg.plugins.mark.image
    return bool(img) or logo_file(app.cfg.home).exists()


def cycle(options: list, current):
    return options[(options.index(current) + 1) % len(options)] if current in options else options[0]


def filetypes_view(app: App) -> Screen:
    bl = set(app.cfg.plugins.filter.files.blacklist)
    rows, row = [], []
    for key, label in FILE_TYPES_UI:
        row.append(B(f"{'🚫' if key in bl else '✅'} {label}", f"pff:{key}",
                     "danger" if key in bl else None))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("« Back", "pl")])
    note = "" if app.cfg.plugins.filter.check else "\n⚠️ Filter plugin is off — turn it on to apply."
    return ("<b>🗂 File types</b>\nTap to block / allow a type.\n"
            f"<blockquote>Blocked: {esc(', '.join(sorted(bl)) or 'nothing')}</blockquote>{note}", rows)


def settings_view(app: App) -> Screen:
    c = app.cfg
    target = esc(c.save.target) if c.save.target else "the chat that asks"
    text = (
        "<b>⚙️ Settings</b>\n\n<blockquote>"
        f"<b>Send mode</b> · {'↪️ forward (shows “Forwarded from”)' if c.forward_mode else '📨 copy (clean, no tag)'}\n"
        f"<b>Protected chats</b> · {'download + re-upload' if c.protected_fallback else 'skip'}\n"
        f"<b>Send via bot</b> · {on_off(c.send_via_bot)}{'' if app.bot and app.user else ' (needs user + bot)'}\n"
        f"<b>Live</b> · edits {on_off(c.live.edit_sync)} · deletes {on_off(c.live.delete_sync)}\n"
        f"<b>Topics</b> · {'auto-create in groups with topics' if c.topics.enabled else 'off (all in General)'}\n"
        f"<b>Clone delay</b> · {c.past.delay:g}s · <b>Save delay</b> · {c.save.delay:g}s\n"
        f"<b>Limits</b> · file {f'{c.max_file_mb} MB' if c.max_file_mb else 'no limit'} · "
        f"posts/save {c.save.max_batch or 'no limit'} · {c.max_jobs} jobs\n"
        f"<b>Saved posts go to</b> · {target}</blockquote>"
    )

    def tg(key, label, value):
        return B(f"{'✅' if value else '▫️'} {label}", f"sx:{key}", "success" if value else None)

    buttons = [
        [B("↪️ Forward mode" if c.forward_mode else "📨 Copy mode", "sx:forward_mode", "primary"),
         tg("protected_fallback", "Protected", c.protected_fallback)],
        [tg("send_via_bot", "Via bot", c.send_via_bot), tg("edit_sync", "Edit sync", c.live.edit_sync)],
        [tg("delete_sync", "Delete sync", c.live.delete_sync), tg("topics", "🗂 Topics", c.topics.enabled)],
        [B("➖", "sd:past:-"), B(f"⏱ Clone {c.past.delay:g}s", "noop"), B("➕", "sd:past:+")],
        [B("➖", "sd:save:-"), B(f"⏱ Save {c.save.delay:g}s", "noop"), B("➕", "sd:save:+")],
        [B("🎯 Save target", "sg"), B("↩️ Reset target", "sgc")],
        [B("🖼 Thumbnail", "th", "primary")],
        [B("« Back", "m")],
    ]
    return text, buttons


THUMB_MODES = {"original": "📌 Original", "custom": "🖼 My image", "none": "🚫 No thumbnail"}
THUMB_TYPES_UI = [("video", "🎬 Video"), ("document", "📄 PDF / File"),
                  ("audio", "🎵 Audio"), ("gif", "🎞 GIF")]


def thumb_view(app: App) -> Screen:
    t = app.cfg.thumb
    custom = thumbs.has_custom(app.cfg)
    warn = ""
    if t.mode == "custom" and not custom:
        warn = "\n⚠️ No image uploaded yet — originals are used until you upload one."
    applies = ", ".join(label for key, label in THUMB_TYPES_UI if key in t.types) or "nothing"
    text = (
        "<b>🖼 Thumbnail</b>\n\n<blockquote>"
        f"<b>Mode</b> · {THUMB_MODES.get(t.mode, t.mode)}\n"
        f"<b>My image</b> · {'✅ uploaded' if custom else '— not set'}\n"
        f"<b>Applies to</b> · {esc(applies)}</blockquote>\n"
        "<i>📌 keeps the source thumbnail · 🖼 puts your image on every video/file · "
        "🚫 sends without one.\nPhotos, stickers, voice and round videos can't have thumbnails. "
        "🖼/🚫 re-upload files, so they are slower.</i>" + warn
    )

    def mode_btn(key):
        on = t.mode == key
        return B(("● " if on else "") + THUMB_MODES[key], f"thm:{key}", "primary" if on else None)

    type_row = [B(f"{'✅' if key in t.types else '▫️'} {label}", f"tht:{key}",
                  "success" if key in t.types else None) for key, label in THUMB_TYPES_UI]
    buttons = [
        [mode_btn("original")],
        [mode_btn("custom"), mode_btn("none")],
        [B("📤 Upload image", "thu", "success"), B("👁 Preview", "thp")],
        type_row[:2], type_row[2:],
        [B("🗑 Delete my image", "thd", "danger")] if custom else [],
        [B("« Back", "st")],
    ]
    return text, [r for r in buttons if r]


def help_text(trial_hours: int = 24) -> str:
    return f"""<b>❔ Help</b>

<b>📡 Live forwarding</b>
➕ New forward → forward a message from the source → then from your channel → ✅ Done.
New posts, albums, edits, deletes and pins are copied automatically.

<b>🔁 Copy old posts</b> <i>(login)</i>
📡 Forwards → open one → 🔁 Clone history. Resumes after restarts, never duplicates.

<b>🗂 Groups with topics</b>
Topics are created in the destination with the same name and icon, every post lands in its topic.

<b>📥 Save restricted posts</b> <i>(login)</i>
Send a post link <code>t.me/c/123/45</code> or a range <code>t.me/chan/10-50</code>.

<b>🤖 Without login</b>
Add me as <b>admin</b> in the source and destination (you must be their admin too).
Only new posts, and only from chats where forwarding is allowed.

<b>💎 Plan</b>
New users get {trial_hours}h free. More time: 👤 My profile → 💎 Get premium.

<blockquote expandable><b>⌨️ All commands</b>
/start · /menu — control panel
/profile — plan, account, logout
/login · /logout — Telegram login
/list · /add src dest… · /remove #n · /toggle #n
/clone #n [fresh] · /clone src dest
/batch link count · /jobs · /stop [id]
/thumb · /setthumb · /delthumb · /style name
/setchat chat|off · /status · /help</blockquote>"""



def wizard_prompt(kind: str, app: App, data: dict = None) -> Screen:
    cancel = [[B("✖️ Cancel", "x")]]
    p = app.cfg.plugins
    if kind == "new_src":
        return ("<b>➕ New forward · step 1/2</b>\n\n"
                "Send the <b>source</b> chat:\n<blockquote>• forward any message from it\n"
                "• or @username / -100id / t.me link</blockquote>", cancel)
    if kind == "new_dest":
        got = "\n".join(f"  • {esc(t)}" for t in data.get("titles", [])) or "  —"
        return (f"<b>➕ New forward · step 2/2</b>\n\nFrom: <b>{esc(data['src_title'])}</b>\n\n"
                f"Send <b>destination</b> chat(s), one by one. Added:\n{got}",
                [[B("✅ Done", "nd", "success"), B("✖️ Cancel", "x")]])
    if kind == "clone_src":
        return ("<b>🔁 Clone Channel/Group · step 1/2</b>\n\nSend the chat to <b>copy from</b>:\n<blockquote>"
                "• forward any message from it\n• or @username / -100id / t.me link</blockquote>\n"
                "<i>Every old post is copied (topics too), in order. It continues by itself after a "
                "restart.</i>", cancel)
    if kind == "clone_dest":
        return (f"<b>🔁 Clone Channel/Group · step 2/2</b>\n\nFrom: <b>{esc(data['src_title'])}</b>\n\n"
                "Now send the chat to <b>copy into</b> (forward a message from it, or its "
                "@username / -100id). Your account must be an admin there.", cancel)
    if kind == "save":
        return ("<b>📥 Save posts</b>\n\nSend a post link:\n<blockquote>"
                "<code>https://t.me/channel/123</code>\n<code>https://t.me/c/1234567/123</code>\n"
                "Range: <code>https://t.me/c/1234567/10-60</code></blockquote>", cancel)
    if kind == "thumb":
        return ("<b>📤 Upload thumbnail</b>\n\nSend a <b>photo</b> (or an image file). "
                "It's resized to 320px JPEG automatically and used for videos, PDFs/files, "
                "audio covers and GIFs.", [[B("✖️ Cancel", "th")]])
    if kind == "logo":
        return ("<b>💧 Watermark logo</b>\n\nSend your logo as a <b>file</b> (PNG with a transparent "
                "background looks best — sending it as a photo removes transparency).\n"
                "It's placed on photos, videos and GIFs.", [[B("✖️ Cancel", "pl")]])
    if kind == "target":
        return ("<b>🎯 Save target</b>\n\nForward a message from the chat where saved posts "
                "should go, or send @username / -100id.", cancel)
    texts = {
        "black": ("🚫 Block words", "One word/phrase per line. Messages containing any are skipped.",
                  "\n".join(p.filter.text.blacklist)),
        "white": ("✔️ Required words", "One per line. Only messages containing one are sent.",
                  "\n".join(p.filter.text.whitelist)),
        "repl": ("🔁 Replace rules", "One rule per line: <code>old => new</code> "
                 "(empty new deletes the word).",
                 "\n".join(f"{k} => {v}" for k, v in p.replace.text.items())),
        "head": ("🧾 Header", "Text added before every message.", p.caption.header),
        "foot": ("🧾 Footer", "Text added after every message.", p.caption.footer),
    }
    title, hint, cur = texts[kind]
    return (f"<b>{title}</b>\n\n{hint}\nSend <code>-</code> to clear.\n\n"
            f"<b>Current:</b>\n<blockquote expandable>{esc(cur) or '—'}</blockquote>",
            [[B("✖️ Cancel", "x")]])


def wizard_apply(kind: str, app: App, text: str) -> str:
    """Apply a plugin text setting. Returns a confirmation line."""
    p = app.cfg.plugins
    clear = text.strip() == "-"
    lines = [] if clear else [x.strip() for x in text.splitlines() if x.strip()]
    if kind == "black":
        p.filter.text.blacklist = lines
        if lines:
            p.filter.check = True
        return f"🚫 {len(lines)} blocked word(s) saved"
    if kind == "white":
        p.filter.text.whitelist = lines
        if lines:
            p.filter.check = True
        return f"✔️ {len(lines)} required word(s) saved"
    if kind == "repl":
        rules = {}
        for ln in lines:
            if "=>" not in ln:
                raise ValueError(f"Bad rule (use old => new): {ln}")
            old, new = ln.split("=>", 1)
            if old.strip():
                rules[old.strip()] = new.strip()
        p.replace.text = rules
        p.replace.check = bool(rules)
        return f"🔁 {len(rules)} rule(s) saved"
    value = "" if clear else text
    if kind == "head":
        p.caption.header = value + ("\n" if value and not value.endswith("\n") else "")
    else:
        p.caption.footer = ("\n" if value and not value.startswith("\n") else "") + value
    p.caption.check = bool(p.caption.header or p.caption.footer)
    return "🧾 Caption saved"


# ================================================================== multi-user
import datetime as _dt
import time as _time


def mask_phone(phone: str) -> str:
    p = (phone or "").lstrip("+")
    return f"+{p[:2]}••••••{p[-2:]}" if len(p) > 4 else "—"


def left(seconds: float) -> str:
    seconds = max(0, int(seconds))
    d, h, m = seconds // 86400, seconds % 86400 // 3600, seconds % 3600 // 60
    return f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")


def when(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).strftime("%d %b %Y, %H:%M")


def plan_label(st: dict) -> str:
    k = st["kind"]
    if k == "owner":
        return "👑 Owner · unlimited"
    if k == "free":
        return "✅ Free · unlimited"
    if k == "expired":
        return "⌛ Expired"
    if k == "none":
        return "🎁 Free trial starts when you begin"
    icon = "⭐ Premium" if k == "premium" else "🎁 Trial"
    return f"{icon} · {left(st['left'])} left"


# ------------------------------------------------------------------ user side
def welcome(trial_hours: int = 24, owner_only: bool = False, pending: bool = False,
            intro: str = "", custom: bool = False, bot_name: str = "") -> Screen:
    """/start for new users. custom=True: the owner's own start text is the whole message."""
    if custom and intro:
        text = intro
    else:
        text = _default_welcome(trial_hours, intro, bot_name)
    if owner_only:
        return text + "\n\n🔒 This bot is private.", None
    if pending:
        return text + "\n\n⏳ Your access request is waiting for the owner.", None
    return text, [[B("🔐 Login with Telegram", "lg", "success")],
                  [B("🤖 Use without login", "bm"), B("❔ How it works", "hw")]]


def _default_welcome(trial_hours: int, intro: str, bot_name: str) -> str:
    return (
        f"<b>⚡ {esc(bot_name or 'SRC Villan Bot')}</b> · <i>TG Clone</i>\n\n"
        + (f"{intro}\n\n" if intro else "") + "<blockquote>"
        "📡 Auto-forward channels & groups in real time\n"
        "🔁 Copy a full history — even protected chats\n"
        "🗂 Groups with topics cloned topic by topic\n"
        "📥 Save restricted posts from any link\n"
        "🖼 Thumbnail · 💧 watermark · 🔎 filters</blockquote>\n"
        + (f"🎁 New here? You get <b>{trial_hours}h free</b>.\n" if trial_hours else "")
        + "🔐 <b>Login</b> — every feature.\n"
        "🤖 <b>Without login</b> — new posts only; I must be admin in your chats.\n"
        "<i>Logins are stored encrypted. Log out anytime (Telegram → Settings → Devices too).</i>"
    )


HOW = """<b>❔ How it works</b>

🔑 <b>Login</b> — 🔐 Login → 📲 share your number → tap the code on the keypad.
📡 <b>Forward</b> — ➕ New forward → forward a message from the source → then from your channel → ✅ Done.
✅ <b>Relax</b> — new posts are copied automatically. 🔁 Clone history copies old posts too.

<blockquote><b>🤖 Without login?</b> Add me as admin in both chats (you must be admin too).
Only new posts, and only chats where forwarding is allowed.
<b>Why login?</b> Bots can't read old posts or protected chats — your account can.</blockquote>"""


def login_phone() -> str:
    return ("<b>🔐 Login · step 1/3</b>\n\nTap <b>📲 Share my number</b> below "
            "(or type it with country code, e.g. <code>+919876543210</code>).\n\n"
            "<i>Send /cancel to stop.</i>")


def code_keypad(digits: str, length: int = 5, note: str = "") -> Screen:
    """Login step 2 with buttons: the code is never sent as a message."""
    shown = " ".join(list(digits) + ["_"] * max(0, length - len(digits)))
    text = ("<b>🔐 Login · step 2/3</b>\n\nTelegram sent a code to your <b>Telegram app</b>.\n"
            "Tap it on the buttons below:\n"
            f"<blockquote><code>{shown}</code></blockquote>\n"
            "<i>⚠️ Never type or forward the code — Telegram blocks a code that was sent in a chat.</i>")
    if note:
        text += f"\n{note}"
    d = [B(str(n), f"otp:d{n}") for n in range(10)]
    rows = [d[1:4], d[4:7], d[7:10], [B("⌫", "otp:b"), d[0], B("↩ New", "otp:r")],
            [B("❌ Cancel", "otp:x", "danger")]]
    return text, rows


def login_password(note: str = "") -> Screen:
    return ("<b>🔐 Login · step 3/3</b>\n\nYour account has a <b>two-step password</b>. "
            "Send it as a message — I delete it immediately." + (f"\n\n{note}" if note else ""),
            [[B("❌ Cancel", "otp:x", "danger")]])


def profile_view(hub, uid: int) -> Screen:
    st = hub.plan_status(uid)
    ws = hub.ws.get(uid)
    app = ws[0] if ws else None
    info = hub.acc.info(uid) or {}
    name = info.get("name") or hub.acc.name_of(uid) or str(uid)
    if app is not None and not app.botmode:
        account = f"🔐 {esc(info.get('name') or '—')} · {mask_phone(info.get('phone'))} · 1/1 account"
    elif app is not None:
        account = "🤖 Without login (new posts only)"
    else:
        account = "— not connected"
    lines = [f"👤 <b>Name</b> · {esc(name)}", f"🔑 <b>Id</b> · <code>{uid}</code>",
             f"💎 <b>Plan</b> · {plan_label(st)}"]
    if st.get("expires"):
        lines.append(f"⏰ <b>{'Ended' if st['kind'] == 'expired' else 'Until'}</b> · {when(st['expires'])}")
    lines.append(f"📲 <b>Account</b> · {account}")
    if app is not None:
        active = len(app.from_to)
        lines.append(f"📡 <b>Forwards</b> · {len(app.cfg.forwards)} ({active} live) · "
                     f"⏳ <b>Jobs</b> · {len(app.jobs.running())}")
        lines.append(f"📋 <b>Copied messages</b> · {app.store.count():,}")
    rows = []
    if st["kind"] not in ("owner", "free"):
        rows.append([B("💎 Get premium", "gp", "success")])
    if app is None:
        rows.append([B("🔐 Login", "lg", "success"), B("🤖 Without login", "bm")])
    elif app.botmode:
        rows.append([B("🔐 Login for all features", "lg", "success")])
        rows.append([B("🚫 Stop bot-only mode", "bx", "danger")])
    else:
        rows.append([B("🚪 Logout", "lo", "danger")])
    rows.append([B("« Back", "m")])
    return "<b>👤 My profile</b>\n\n<blockquote>" + "\n".join(lines) + "</blockquote>", rows


def parse_plans(text: str) -> list:
    """'7=₹49, 30=₹149' -> [(7, '₹49'), (30, '₹149')] (bad parts are skipped)."""
    out = []
    for part in (text or "").replace("\n", ",").split(","):
        if "=" not in part:
            continue
        days, price = part.split("=", 1)
        days = days.strip().lower().rstrip("d").strip()
        if days.isdigit() and 0 < int(days) <= 3650 and price.strip():
            out.append((int(days), price.strip()[:20]))
    return out


PLAN_ICONS = ["⭐", "💎", "🏅", "👑"]


def get_premium(hub, uid: int) -> Screen:
    st = hub.plan_status(uid)
    plans = parse_plans(hub.settings.plans)
    current = plan_label(st)
    text = ("<b>💎 Go Premium</b> · <i>unlock everything</i>\n\n<blockquote>"
            "✅ Clone any channel or group — full history\n"
            "✅ Groups with topics, topic by topic\n"
            "✅ Protected chats (download + re-upload)\n"
            "✅ Live auto-forward, edits, pins, polls\n"
            "✅ Save restricted posts from links\n"
            "✅ Thumbnail · watermark · filters · OCR\n"
            "⚡ Fast parallel downloads & turbo copy</blockquote>\n"
            f"💎 <b>Your plan:</b> {current}\n\n")
    rows = []
    if plans:
        text += "👇 <b>Choose a plan</b> — the owner gets your request instantly:"
        best = max(range(len(plans)), key=lambda i: plans[i][0])
        for i, (days, price) in enumerate(plans):
            tag = " · 🔥 popular" if i == min(1, len(plans) - 1) and len(plans) > 1 else ""
            tag = " · 🏅 best value" if i == best and len(plans) > 2 else tag
            rows.append([B(f"{PLAN_ICONS[min(i, 3)]} {days} days · {price}{tag}", f"bp:{days}",
                           "success" if tag else "primary")])
    else:
        text += "Message the owner with your id below to get premium."
    text += f"\n\n🔑 Your id: <code>{uid}</code>"
    contact = getattr(hub, "owner_username", "")
    if contact:
        rows.append([Button.url("💬 Message the owner", f"https://t.me/{contact}")])
    rows.append([B("👤 My profile", "ac"), B("« Back", "m")])
    return text, rows


def premium_requested(hub, uid: int, days: int, price: str) -> Screen:
    pay = hub.settings.pay_info.strip()
    text = (f"<b>📬 Request sent!</b>\n\n<blockquote>💎 <b>{days} days</b> · {esc(price)}\n"
            f"🔑 Your id: <code>{uid}</code></blockquote>\n")
    text += (f"💳 <b>How to pay</b>\n<blockquote>{esc(pay)}</blockquote>\n"
             "After paying, send the screenshot to the owner. " if pay else
             "The owner will contact you about payment. ")
    text += "You'll get a message here the moment premium is active. 🎉"
    rows = []
    contact = getattr(hub, "owner_username", "")
    if contact:
        rows.append([Button.url("💬 Message the owner", f"https://t.me/{contact}")])
    rows.append([B("« Back", "gp")])
    return text, rows


def plan_expired(hub, uid: int) -> Screen:
    st = hub.plan_status(uid)
    ended = f" on {when(st['expires'])}" if st.get("expires") else ""
    return (f"<b>⌛ Your plan has ended{ended}</b>\n\n<blockquote>Live forwarding is paused.\n"
            "Your forwards and settings are safe.</blockquote>\n💎 Get premium to continue.",
            [[B("💎 Get premium", "gp", "success")], [B("👤 My profile", "ac"), B("❔ Help", "hw")]])


# ------------------------------------------------------------------ admin (owner only)
def _a(text, data):  # admin section button: blue
    return B(text, data, "primary")


def _paint(rows):
    """Admin colours: « Back = red, Refresh = green, everything else blue
    (buttons that already have a colour - Ban / Remove red, Approve / Give green - keep it)."""
    from telethon.tl import types as _types
    out = []
    for row in rows or []:
        new = []
        for b in row:
            if isinstance(b, _types.KeyboardInlineButton):
                st = b.style
                has = st is not None and (st.bg_primary or st.bg_danger or st.bg_success)
                if b.text.startswith("«") or "Cancel" in b.text:
                    color = "danger"
                elif "Refresh" in b.text:
                    color = "success"
                elif has:
                    new.append(b)
                    continue
                else:
                    color = "primary"
                b = _types.KeyboardInlineButton(b.text, b.type, style=_types.KeyboardButtonStyle(
                    **{f"bg_{color}": True}))
            new.append(b)
        out.append(new)
    return out


def admin_screen(fn):
    """Decorator / wrapper: applies the admin colours to a screen."""
    import functools
    if callable(fn):
        @functools.wraps(fn)
        def wrap(*a, **k):
            text, rows = fn(*a, **k)
            return text, _paint(rows)
        return wrap
    text, rows = fn
    return text, _paint(rows)


@admin_screen
def admin_view(hub, viewer: int = 0) -> Screen:
    if viewer and viewer != hub.owner:
        return _subadmin_view(hub)
    st, c = hub.settings, hub.acc.counts()
    running = sum(len(app.jobs.running()) for app, _ in hub.ws.values())
    up = int(_time.time() - hub.started)
    trial = sum(1 for p in hub.acc.plans() if p["kind"] == "trial" and p["expires"] > _time.time())
    mode = dict(open="🌐 anyone", approve="✅ approved users", owner="🔒 only you")[st.mode]
    text = ("<b>🛡 Admin panel</b> · <i>only you can see this</i>\n\n<blockquote>"
            f"👥 <b>Users</b> · {c['users']} logged in · {c['botmode']} without login · {len(hub.ws)} online\n"
            f"💎 <b>Premium</b> · {'on' if st.premium_enabled else 'off'} · {c['premium']} premium · "
            f"{trial} on trial · trial {st.trial_hours}h\n"
            f"⏳ <b>Jobs</b> · {running} running / {st.max_total_jobs}\n"
            f"🔐 <b>Access</b> · {mode} · {c['pending']} waiting · {c['banned']} banned\n"
            f"🚫 <b>Blocked chats</b> · {c['blocked']}\n"
            f"⏰ <b>Uptime</b> · {left(up)}</blockquote>\n" + _emoji_note(st))
    rows = [
        [_a("👥 Users", "adu:0"), _a(f"📬 Requests ({c['pending']})", "adp")],
        [B("📊 Bot stats · live", "abs", "success")],
        [_a("💎 Premium", "apm"), _a("🚫 Blocked chats", "abl")],
        [_a("🔐 Access", "aac"), _a("⚙️ Limits", "alm")],
        [_a("🖼 Start photo", "asp"), _a(f"⭐ Premium emoji: {'on' if st.premium_emoji else 'off'}", "aem")],
        [_a("✏️ Start text", "ast"), _a("🔍 Test premium emoji", "aet")],
        [_a(f"👮 Admins ({len(hub.acc.admins())})", "adA")],
        [B("🔄 Refresh", "ad"), B("« Back", "m")],
    ]
    return text, rows


def _subadmin_view(hub) -> Screen:
    c = hub.acc.counts()
    running = sum(len(app.jobs.running()) for app, _ in hub.ws.values())
    text = ("<b>🛡 Admin panel</b> · <i>👮 admin</i>\n\n<blockquote>"
            f"👥 <b>Users</b> · {c['users']} logged in · {c['botmode']} without login · {len(hub.ws)} online\n"
            f"💎 <b>Premium</b> · {c['premium']} premium\n"
            f"⏳ <b>Jobs</b> · {running} running\n"
            f"📬 <b>Requests</b> · {c['pending']} waiting · {c['banned']} banned\n"
            f"🚫 <b>Blocked chats</b> · {c['blocked']}</blockquote>\n"
            "<i>You can manage users, premium and blocked chats. Settings stay with the owner.</i>")
    rows = [
        [_a("👥 Users", "adu:0"), _a(f"📬 Requests ({c['pending']})", "adp")],
        [B("📊 Bot stats · live", "abs", "success")],
        [_a("💎 Premium", "apm"), _a("🚫 Blocked chats", "abl")],
        [B("🔄 Refresh", "ad"), B("« Back", "m")],
    ]
    return text, rows


@admin_screen
def admin_admins(hub, note: str = "") -> Screen:
    items = hub.acc.admins()
    rows = [[B(f"❌ {(a['name'] or str(a['uid']))[:22]} · {a['uid']}", f"adR:{a['uid']}")] for a in items]
    rows.append([B("➕ Add admin", "adA:add", "success")])
    rows.append([_a("📜 Activity", "adG")])
    rows.append([B("« Admin", "ad")])
    text = (f"<b>👮 Admins</b> · {len(items)}\n\n<blockquote>"
            "✅ Can: approve / ban / log out users, give / remove premium, block / unblock chats, "
            "see 📊 Bot stats\n"
            "🔒 Only you: admins, access mode, limits, plans & payment, start photo / text, "
            "premium emoji, extra connections</blockquote>\n"
            "<i>An admin can't ban or change you or another admin. Tap ❌ to remove one.</i>")
    if note:
        text += f"\n\n{note}"
    return text, rows


@admin_screen
def admin_activity(hub) -> Screen:
    items = hub.acc.recent_actions(15)
    if not items:
        body = "No admin actions yet."
    else:
        body = "\n".join(f"• <code>{_time.strftime('%d %b %H:%M', _time.localtime(a['t']))}</code> "
                         f"<b>{esc(hub.acc.name_of(a['uid']) or a['uid'])}</b> {esc(a['text'])}"
                         for a in items)
    return (f"<b>📜 Activity</b> · last {len(items)}\n\n<blockquote expandable>{body}</blockquote>",
            [[B("🔄 Refresh", "adG")], [B("« Admins", "adA")]])


def _server() -> dict:
    """CPU load, RAM, bot RAM, free disk (Linux /proc; missing values stay 0)."""
    import os
    import shutil
    from tgclone.config import DATA_DIR
    s = {"cores": os.cpu_count() or 1, "load": 0.0, "mem_total": 0, "mem_free": 0, "rss": 0,
         "disk_free": 0, "disk_total": 0}
    try:
        s["load"] = os.getloadavg()[0]
    except (OSError, AttributeError):
        pass
    try:
        with open("/proc/meminfo") as f:
            info = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in f}
        s["mem_total"], s["mem_free"] = info.get("MemTotal", 0), info.get("MemAvailable", 0)
        with open("/proc/self/status") as f:
            s["rss"] = next((int(line.split()[1]) * 1024 for line in f if line.startswith("VmRSS")), 0)
    except OSError:
        pass
    try:
        du = shutil.disk_usage(DATA_DIR if DATA_DIR.exists() else ".")
        s["disk_free"], s["disk_total"] = du.free, du.total
    except OSError:
        pass
    return s


@admin_screen
def admin_stats(hub, live: bool = True) -> Screen:
    """📊 Bot stats: users, plans, work right now, totals since start, server. Refreshed live."""
    from tgclone import fast
    from tgclone.config import DOWNLOAD_DIR
    from tgclone.jobs import STATS, human_size
    now = _time.time()
    c = hub.acc.counts()
    plans = hub.acc.plans()
    prem = sum(1 for p in plans if p["kind"] == "premium" and p["expires"] > now)
    trial = sum(1 for p in plans if p["kind"] == "trial" and p["expires"] > now)
    expired = sum(1 for p in plans if p["expires"] <= now)
    apps = [app for app, _ in hub.ws.values()]
    jobs = [j for app in apps for j in app.jobs.running()]
    clones = sum(1 for j in jobs if j.kind == "clone")
    busy = sum(1 for app in apps if app.jobs.running())
    sources = sum(len(app.from_to) for app in apps if not app.paused)
    live_users = sum(1 for app in apps if app.from_to and not app.paused)
    down = sum(j.speed for j in jobs if j.direction == "down")
    up = sum(j.speed for j in jobs if j.direction == "up")
    try:
        files = [f for f in DOWNLOAD_DIR.iterdir() if f.is_file()]
    except FileNotFoundError:
        files = []
    tmp = sum(f.stat().st_size for f in files if f.exists())
    sv = _server()
    cpu = min(100, int(sv["load"] / sv["cores"] * 100))
    ram_used = sv["mem_total"] - sv["mem_free"]
    pool = ("❌ off (Admin → Limits)" if not fast.ENABLED else
            "⏸ paused (Telegram cut them)" if fast._paused(_PoolProbe()) else "✅ allowed")
    total = c["users"] + c["botmode"]

    def pct(a, b):
        return f"{int(a / b * 100)}%" if b else "—"
    text = (
        "<b>📊 Bot stats</b>" + (" · <i>🟢 live</i>" if live else "") + "\n\n"
        "<b>👥 Users</b>\n<blockquote>"
        f"👥 Total · <b>{total}</b> · 🆕 {hub.acc.new_since(now - 86400)} in 24h\n"
        f"🔐 Logged in · <b>{c['users']}</b> · 🤖 Without login · <b>{c['botmode']}</b>\n"
        f"🟢 Online now · <b>{len(hub.ws)}</b> · 📬 Waiting · {c['pending']} · ⛔ Banned · {c['banned']}"
        "</blockquote>\n"
        "<b>💎 Plans</b>\n<blockquote>"
        f"⭐ Premium · <b>{prem}</b> · 🎁 Trial · <b>{trial}</b> · ⌛ Expired · {expired}"
        "</blockquote>\n"
        "<b>⚡ Working now</b>\n<blockquote>"
        f"⏳ Jobs · <b>{len(jobs)}</b> / {hub.settings.max_total_jobs} · 🔁 {clones} clone · "
        f"📥 {len(jobs) - clones} save · 👥 {busy} user(s)\n"
        f"📡 Live forwards · <b>{sources}</b> source(s) · {live_users} user(s)\n"
        f"⬇️ {human_size(down)}/s · ⬆️ {human_size(up)}/s\n"
        f"📂 Temp files · {len(files)} · {human_size(tmp)}"
        "</blockquote>\n"
        "<b>📈 Since start</b>\n<blockquote>"
        f"⏰ Uptime · <b>{left(now - hub.started)}</b>\n"
        f"✅ {STATS['done']} done · ❌ {STATS['failed']} failed · ⏹ {STATS['stopped']} stopped\n"
        f"📨 Posts sent · <b>{STATS['posts']:,}</b>\n"
        f"⬇️ {human_size(STATS['down'])} downloaded · ⬆️ {human_size(STATS['up'])} uploaded"
        "</blockquote>\n"
        "<b>🖥 Server</b>\n<blockquote>"
        f"⚙️ CPU · <b>{cpu}%</b> · {sv['cores']} cores\n"
        f"🧠 RAM · {human_size(ram_used)} / {human_size(sv['mem_total'])} ({pct(ram_used, sv['mem_total'])}) · "
        f"bot {human_size(sv['rss'])}\n"
        f"💾 Disk free · {human_size(sv['disk_free'])} / {human_size(sv['disk_total'])}\n"
        f"🔌 Extra connections · {pool}"
        "</blockquote>\n"
        f"<i>🔄 Updated {_time.strftime('%H:%M:%S')}"
        + (" · refreshes every 5 s for 2 min</i>" if live else " · live view paused, tap 🔄 Refresh</i>"))
    return text, [[B("🔄 Refresh", "abs")], [B("« Admin", "ad")]]


class _PoolProbe:
    """fast._paused() wants a client; the remembered pause is global."""


def _emoji_note(st) -> str:
    from tgclone import emoji
    if not st.premium_emoji:
        return "⭐ <b>Premium emoji</b> · off"
    return {
        "working": "⭐ <b>Premium emoji</b> · ✅ Telegram shows them",
        "removed": "⭐ <b>Premium emoji</b> · ❌ Telegram removes them. Since Bot API 9.4 (Feb 2026) a bot "
                   "may use premium emoji only if <b>the account that owns the bot in @BotFather has "
                   "Telegram Premium</b>. Buy Premium on that account — they appear by themselves, "
                   "no restart needed. Everything else works normally.",
    }.get(emoji.status, "⭐ <b>Premium emoji</b> · checking on the next screen…")


@admin_screen
def admin_access(hub) -> Screen:
    st = hub.settings

    def mode(key, label):
        on = st.mode == key
        return B(("● " if on else "") + label, f"adm:{key}", "success" if on else None)
    return ("<b>🔐 Access</b>\n\n<blockquote>🌐 <b>Open</b> — anyone can use the bot\n"
            "✅ <b>Approval</b> — new users need your OK (you get a request with buttons)\n"
            "🔒 <b>Only me</b> — private bot</blockquote>",
            [[mode("open", "🌐 Open")], [mode("approve", "✅ Approval")], [mode("owner", "🔒 Only me")],
             [B("« Admin", "ad")]])


@admin_screen
def admin_limits(hub) -> Screen:
    st = hub.settings
    return ("<b>⚙️ Limits</b> · <i>for users, not for you</i>\n\n<blockquote>"
            f"🎁 Jobs at once · free trial · <b>{st.user_max_jobs}</b>\n"
            f"💎 Jobs at once · premium · <b>{st.premium_max_jobs}</b> (e.g. channel + group + topics)\n"
            f"📥 Posts per save · <b>{st.user_max_batch or '∞ no limit'}</b>\n"
            f"📊 Jobs at once, all users · <b>{st.max_total_jobs}</b></blockquote>\n"
            f"🔌 <b>Extra connections</b> · {'on' if st.extra_connections else 'off'}\n<i>Big files use 4 extra "
            f"connections. If your network keeps cutting them (warnings in the log), turn this off: "
            f"one connection, 8 parts at once.</i>",
            [[B("➖", "adj:-"), B(f"🎁 Trial {st.user_max_jobs}", "noop"), B("➕", "adj:+")],
             [B("➖", "adp3:-"), B(f"💎 Premium {st.premium_max_jobs}", "noop"), B("➕", "adp3:+")],
             [B("➖", "adx:-"), B(f"📥 {st.user_max_batch or '∞'}/save", "noop"), B("➕", "adx:+")],
             [B("➖", "adt:-"), B(f"📊 {st.max_total_jobs} total", "noop"), B("➕", "adt:+")],
             [B(("✅" if st.extra_connections else "❌") + " Extra connections " +
                ("on" if st.extra_connections else "off"), "axc", "success" if st.extra_connections else None)],
             [B("« Admin", "ad")]])


@admin_screen
def admin_premium(hub) -> Screen:
    st, now = hub.settings, _time.time()
    plans = hub.acc.plans()
    prem = sum(1 for p in plans if p["kind"] == "premium" and p["expires"] > now)
    trial = sum(1 for p in plans if p["kind"] == "trial" and p["expires"] > now)
    expired = sum(1 for p in plans if p["expires"] <= now)
    return ("<b>💎 Premium</b>\n\n<blockquote>"
            f"⚙️ <b>Plans</b> · {'✅ on — users need trial or premium' if st.premium_enabled else '❌ off — everyone free'}\n"
            f"🎁 <b>Free trial</b> · {st.trial_hours}h for every new user\n"
            f"⭐ {prem} premium · 🎁 {trial} on trial · ⌛ {expired} expired</blockquote>\n"
            f"💰 <b>Plans</b> · {esc(st.plans) or '— none'}\n"
            f"💳 <b>Payment info</b> · {esc(st.pay_info) or '— not set'}\n\n"
            "<i>➕ Give premium: send <code>user_id days</code> or <code>@username days</code>.</i>",
            [[B("➕ Give premium", "apg", "success"), B("➖ Remove premium", "apr", "danger")],
             [B("📋 Premium users", "apl:0")],
             [B("💰 Plans & prices", "apP"), B("💳 Payment info", "apI")],
             [B("➖", "atr:-"), B(f"🎁 Trial {st.trial_hours}h", "noop"), B("➕", "atr:+")],
             [B(("✅" if st.premium_enabled else "❌") + " Plans " + ("on" if st.premium_enabled else "off"),
                "apt", "success" if st.premium_enabled else None)],
             [B("« Admin", "ad")]])


@admin_screen
def admin_premium_list(hub, page: int = 0) -> Screen:
    now = _time.time()
    plans = [p for p in hub.acc.plans() if p["expires"] > now and p["kind"] == "premium"]
    pages = max(1, (len(plans) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    rows = []
    for p in plans[page * PAGE:(page + 1) * PAGE]:
        name = hub.acc.name_of(p["uid"]) or p["uid"]
        rows.append([B(f"⭐ {str(name)[:20]} · {left(p['expires'] - now)}", f"adv:{p['uid']}")])
    nav = ([B("‹ Prev", f"apl:{page - 1}")] if page else []) + \
          ([B("Next ›", f"apl:{page + 1}")] if page < pages - 1 else [])
    if nav:
        rows.append(nav)
    rows.append([B("« Premium", "apm")])
    return (f"<b>⭐ Premium users</b> · {len(plans)}\n<i>Only paid premium (free trials are not listed). "
            f"Tap one to manage.</i>" if plans else "<b>⭐ Premium users</b> · 0\n<i>Nobody has premium yet.</i>"), rows


@admin_screen
def admin_prompt(kind: str) -> Screen:
    back = {"premium": "apm", "rmpremium": "apm", "block": "abl", "photo": "asp", "start": "ad", "addadmin": "adA",
            "plans": "apm", "pay": "apm"}[kind]
    texts = {
        "premium": "<b>➕ Give premium</b>\n\nSend <code>user_id days</code> or <code>@username days</code>.\n"
                   "Example: <code>5021 30</code>\n<i>Days are added on top of time left.</i>",
        "start": "<b>✏️ Start text</b>\n\nSend the message users see on /start (under the start photo). "
                 "Bold, quotes, premium emoji — your formatting is kept. It becomes the <b>whole</b> "
                 "welcome message for new users (max 3500 characters).\n<blockquote>"
                 "<code>{name}</code> user's name · <code>{bot}</code> bot name · "
                 "<code>{plan}</code> their plan · <code>{id}</code> their id</blockquote>\n"
                 "Example: <code>🤝 Welcome {name} to {bot}!</code>\nSend <code>-</code> for the default.",
        "plans": "<b>💰 Plans & prices</b>\n\nSend <code>days=price</code> separated by commas.\n"
                 "Example: <code>7=₹49, 30=₹149, 90=₹349</code>\nSend <code>-</code> to hide the plans.",
        "pay": "<b>💳 Payment info</b>\n\nWhat users see after choosing a plan — UPI id, payment "
               "link, bank details…\nExample: <code>UPI: yourname@upi</code>\nSend <code>-</code> to clear.",
        "rmpremium": "<b>➖ Remove premium</b>\n\nSend the <code>user_id</code> or <code>@username</code>.",
        "addadmin": "<b>👮 Add admin</b>\n\nSend their <code>user_id</code> or <code>@username</code>.\n"
                    "<i>They should /start the bot first, so I can message them.</i>",
        "block": "<b>🚫 Block a chat</b>\n\nSend any of these — users won't be able to clone it:\n<blockquote>"
                 "📁 folder link <code>t.me/addlist/…</code> (blocks every chat in it)\n"
                 "🔗 <code>@username</code> or <code>t.me/username</code>\n"
                 "🔑 private link <code>t.me/c/123…</code> or invite <code>t.me/+…</code>\n"
                 "🆔 <code>-100…</code> id, or forward a message from the chat</blockquote>",
        "photo": "<b>🖼 SET START PHOTO</b>\n━━━━━━━━━━━━━━━━━━━━\n🖼 Send a photo directly (best)\n"
                 "🔗 or a direct image link: <code>https://….jpg</code>\n"
                 "It will show big at the top of the /start screen.",
    }
    return texts[kind], [[B("❌ Cancel", back)]]


@admin_screen
def admin_blocked(hub) -> Screen:
    items = hub.acc.blocked()
    folders: dict = {}
    for b in items:
        if b["folder"]:
            folders.setdefault(b["folder"], [b["fname"] or "Folder", 0])[1] += 1
    single = [b for b in items if not b["folder"]]
    rows = [[B(f"📁 {name[:24]} · {n} chats", f"abf:{slug}")] for slug, (name, n) in folders.items()]
    rows += [[B(f"❌ {str(b['title'] or b['chat_id'])[:26]}", f"aub:{b['chat_id']}")] for b in single[:20]]
    rows.append([B("➕ Block chat", "abk", "success")])
    rows.append([B("« Admin", "ad")])
    more = f"\n<i>…and {len(single) - 20} more</i>" if len(single) > 20 else ""
    return (f"<b>🚫 Blocked chats</b> · {len(items)}\n\n<blockquote>Users can't clone, forward or save "
            f"from these.\n👑 You (owner) are never blocked, so test with another account.</blockquote>\n"
            f"<i>📁 Open a folder to see its chats · tap ❌ to unblock.</i>{more}", rows)


FOLDER_PAGE = 20


@admin_screen
def admin_folder(hub, slug: str, page: int = 0) -> Screen:
    chats = [b for b in hub.acc.blocked() if b["folder"] == slug]
    if not chats:
        return admin_blocked.__wrapped__(hub)
    name = chats[0]["fname"] or "Folder"
    pages = (len(chats) - 1) // FOLDER_PAGE + 1
    page = max(0, min(page, pages - 1))
    part = chats[page * FOLDER_PAGE:(page + 1) * FOLDER_PAGE]
    rows = [[B(f"❌ {str(b['title'] or b['chat_id'])[:26]}", f"aub:{b['chat_id']}:{slug}")] for b in part]
    if pages > 1:
        nav = []
        if page:
            nav.append(B("◀️ Prev", f"abf:{slug}:{page - 1}"))
        nav.append(B(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(B("Next ▶️", f"abf:{slug}:{page + 1}"))
        rows.append(nav)
    rows.append([B(f"🗑 Remove whole folder ({len(chats)})", f"abF:{slug}", "danger")])
    rows.append([B("« Blocked chats", "abl")])
    return (f"<b>📁 {esc(name)}</b> · {len(chats)} blocked chat(s)\n\n<blockquote>Every chat from this "
            f"folder link is blocked.</blockquote>\n<i>Tap ❌ to unblock one chat, or remove the whole "
            f"folder at once.</i>", rows)


@admin_screen
def admin_photo(hub) -> Screen:
    cur = hub.settings.start_photo
    return ("<b>🖼 Start photo</b>\n\n<blockquote>"
            f"{'✅ set · ' + esc(cur if cur.startswith('http') else 'uploaded photo') if cur else '— not set'}</blockquote>",
            [[_a("🖼 Set start photo", "aps")] + ([B("🗑 Remove", "apd")] if cur else []),
             [B("« Admin", "ad")]])


@admin_screen
def admin_users(hub, page: int = 0) -> Screen:
    users = hub.acc.users()
    known = {u["uid"] for u in users}
    for uid in hub.acc.botmode_users():
        if uid not in known:
            users.append({"uid": uid, "name": hub.acc.name_of(uid), "bot": True})
            known.add(uid)
    users += [{"uid": b["uid"], "name": b["name"]} for b in hub.acc.by_state("banned")
              if b["uid"] not in known]
    pages = max(1, (len(users) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    rows = []
    for u in users[page * PAGE:(page + 1) * PAGE]:
        if hub.acc.access(u["uid"]) == "banned":
            icon = "🚫"
        else:
            icon = ("🟢" if u["uid"] in hub.ws else "⚫") + ("🤖" if u.get("bot") else "")
        rows.append([B(f"{icon} {(u.get('name') or '?')[:22]} · {u['uid']}", f"adv:{u['uid']}")])
    nav = ([B("‹ Prev", f"adu:{page - 1}")] if page else []) + \
          ([B("Next ›", f"adu:{page + 1}")] if page < pages - 1 else [])
    if nav:
        rows.append(nav)
    rows.append([B("« Admin", "ad")])
    return f"<b>👥 Users</b> · {len(users)}\n<i>🟢 online · ⚫ offline · 🤖 without login · 🚫 banned</i>", rows


@admin_screen
def admin_user(hub, uid: int) -> Screen:
    info = hub.acc.info(uid) or {}
    banned = hub.acc.access(uid) == "banned"
    ws = hub.ws.get(uid)
    st = hub.plan_status(uid)
    name = info.get("name") or hub.acc.name_of(uid) or uid
    mode = "🔐 login" if info else ("🤖 without login" if uid in hub.acc.botmode_users() else "—")
    text = (f"<b>👤 {esc(name)}</b>\n\n<blockquote>"
            f"🔑 <b>Id</b> · <code>{uid}</code>\n📲 <b>Phone</b> · {mask_phone(info.get('phone'))}\n"
            f"🔐 <b>Mode</b> · {mode}\n"
            f"⚫ <b>Status</b> · {'🚫 banned' if banned else ('🟢 online' if ws else 'offline')}\n"
            f"💎 <b>Plan</b> · {plan_label(st)}\n"
            f"📡 <b>Forwards</b> · {len(ws[0].cfg.forwards) if ws else 0} · "
            f"⏳ <b>Jobs</b> · {len(ws[0].jobs.running()) if ws else 0}</blockquote>")
    rows = [[B("➕ 1 day", f"apa:{uid}:1"), B("➕ 7 days", f"apa:{uid}:7"), B("➕ 30 days", f"apa:{uid}:30")]]
    if st["kind"] in ("premium", "trial"):
        rows.append([B("➖ Remove premium", f"apx:{uid}", "danger")])
    rows.append([B("✅ Unban", f"adb:{uid}", "success") if banned else B("🚫 Ban", f"adb:{uid}", "danger")])
    if ws or info:
        rows.append([B("🚪 Force logout", f"adl:{uid}", "danger")])
    rows.append([B("« Users", "adu:0")])
    return text, rows


@admin_screen
def admin_pending(hub) -> Screen:
    pend = hub.acc.by_state("pending")
    if not pend:
        return "<b>📬 Requests</b>\n\nNo pending requests.", [[B("« Admin", "ad")]]
    rows = [[B(f"✅ {(p['name'] or '?')[:18]} · {p['uid']}", f"ap:{p['uid']}", "success"),
             B("❌", f"dn:{p['uid']}", "danger")] for p in pend[:PAGE]]
    rows.append([B("« Admin", "ad")])
    return f"<b>📬 Requests</b> · {len(pend)}\nApprove or deny:", rows


def strip_tags(html: str) -> str:
    """Visible text length check (captions are limited to 1024 chars)."""
    import re as _re
    return _re.sub(r"<[^>]+>", "", html)
