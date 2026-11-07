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
async def main_menu(app: App) -> Screen:
    run = app.jobs.running()
    dests = {d for v in app.from_to.values() for d in v}
    reader = "user" if app.reader is app.user else "bot"
    sender = "bot" if app.sender is app.bot and app.bot else reader
    plugins = ", ".join(p.id_ for p in app.plugins) or "none"
    text = (
        "<b>⚡ tgclone</b> · <i>control panel</i>\n\n"
        f"<blockquote>{'🟢' if app.from_to else '⚪'} <b>Live</b> · {len(app.from_to)} source(s) → {len(dests)} chat(s)\n"
        f"{'⏳' if run else '💤'} <b>Jobs</b> · {len(run)} running\n"
        f"🧩 <b>Plugins</b> · {esc(plugins)}\n"
        f"👤 <b>Reads</b> as {reader} · 📤 <b>sends</b> as {sender}</blockquote>\n"
        "<i>Tip: send any t.me post link (or link/10-50) to save it.</i>"
    )
    buttons = [
        [B("📋 Forwards", "fl:0", "primary"), B("➕ New forward", "na", "success")],
        [B("📥 Save posts", "sv"), B(f"⏳ Jobs ({len(run)})" if run else "⏳ Jobs", "jl")],
        [B("🧩 Plugins", "pl"), B("⚙️ Settings", "st")],
        [B("🔄 Refresh", "m"), B("❔ Help", "hp")],
    ]
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
    from tgclone.plugins.mark import LOGO
    img = app.cfg.plugins.mark.image
    return bool(img) or LOGO.exists()


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
        f"<b>Clone delay</b> · {c.past.delay:g}s · <b>Save delay</b> · {c.save.delay:g}s\n"
        f"<b>Limits</b> · file {c.max_file_mb} MB · disk {c.max_disk_mb} MB · {c.max_jobs} jobs\n"
        f"<b>Saved posts go to</b> · {target}</blockquote>"
    )

    def tg(key, label, value):
        return B(f"{'✅' if value else '▫️'} {label}", f"sx:{key}", "success" if value else None)

    buttons = [
        [B("↪️ Forward mode" if c.forward_mode else "📨 Copy mode", "sx:forward_mode", "primary"),
         tg("protected_fallback", "Protected", c.protected_fallback)],
        [tg("send_via_bot", "Via bot", c.send_via_bot), tg("edit_sync", "Edit sync", c.live.edit_sync)],
        [tg("delete_sync", "Delete sync", c.live.delete_sync)],
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
    custom = thumbs.has_custom()
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


HELP = """<b>❔ How to use</b>

<b>Live forwarding</b> – ➕ New forward → forward any message from the source channel (or send @user / -100id / link) → add destinations → ✅ Done. New posts, albums, edits (and deletes if enabled) are mirrored.

<b>Copy old posts</b> – open a forward → 🔁 Clone history. It resumes after restarts and never sends duplicates.

<b>Save restricted posts</b> – send a post link: <code>t.me/c/123/45</code>, a range <code>t.me/chan/10-50</code>, or <code>/batch link 100</code>.

<blockquote expandable><b>Text commands</b>
/menu · /list · /add src dest… · /remove #n · /toggle #n
/clone #n [fresh] · /clone src dest · /batch link count
/jobs · /stop [id] · /setchat chat|off · /style name · /status
/thumb · /setthumb (send or reply to a photo) · /delthumb</blockquote>"""


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
