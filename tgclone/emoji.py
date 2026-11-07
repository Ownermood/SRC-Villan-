"""Premium (animated custom) emoji for every screen and button.

Screens are written with normal emoji. When premium emoji are on, `decorate()` swaps each
known emoji in the text for <tg-emoji> and moves a button's leading emoji into its premium
icon. If Telegram refuses (the bot may not be allowed to use custom emoji), callers resend
the plain screen and `disable_for_session()` stops further attempts.

Pack: UI Animation (@technoprint_official).
"""

import re
from typing import Optional, Tuple

from telethon.tl import types

VS = "️"

# emoji -> custom emoji document id (first match of the pack per emoji)
PACK = {
    "🔎": 5445023138297447592, "🔲": 5445385947069838800, "📲": 5445059250382469069,
    "🌐": 5447602197439218445, "⬅": 5447506720316225765, "➡": 5445350109862720603,
    "📂": 5447282724886839705, "🚪": 5447434637880098257, "⬇": 5444961234933806330,
    "📁": 5447389837076231920, "✔": 5447242579827523388, "✅": 5444987348334965906,
    "☑": 5445210909972655435, "➕": 5447224884562263112, "🏅": 5444931419270839381,
    "➖": 5447155993286832278, "▶": 5444883062234053429, "💬": 5445140257760639304,
    "🚫": 5447647474984449520, "⚡": 5445388803223091254, "👍": 5447153029759397673,
    "👎": 5447128406711891434, "❤": 5444887644964159628, "💔": 5445053099989303899,
    "⚠": 5447381715293074599, "🖼": 5447588260270341594, "📝": 5444889156792646660,
    "🎧": 5445276884965291212, "🔠": 5445137435967126342, "↔": 5447554042265891906,
    "↕": 5445088739627924307, "↪": 5445365692004071819, "🗑": 5445005936953424165,
    "🔽": 5445358884480916784, "🔼": 5445307250384075361, "🪙": 5447629406057034342,
    "🔛": 5445279663809130789, "📴": 5445349654596189741, "👩‍💻": 5445224894386172410,
    "🫂": 5445372439397691719, "❌": 5445092669522996408, "💰": 5444860552310457690,
    "💲": 5447579253723918909, "👛": 5444903695256941915, "✍": 5445128296276718145,
    "👨‍🎨": 5447226864542190055, "🔍": 5444989577422993015, "⭐": 5444939047132755535,
    "◀": 5447651546613449378, "↩": 5445362436418859744, "⬆": 5444988177263654261,
    "⚙": 5444869180899752137, "📹": 5445344161333015312, "🖥": 5447607759421863856,
    "📋": 5445260044398524944, "📑": 5445203741672243391, "📔": 5444854925903295394,
    "🎙": 5447321534211323909, "🔊": 5445030469806618301, "🛒": 5447319442562251569,
    "🛍": 5447387942995655277, "💳": 5447453226498552490, "⚫": 5447429226221303478,
    "⏰": 5445350406215465190, "📡": 5447448489149625830, "📟": 5447604619800770047,
    "💼": 5444924349754667822, "🔑": 5445373775132522312, "☀": 5447425115937602108,
    "🔔": 5445333299360723379, "📄": 5447421246172069841, "📊": 5445146408153806223,
    "📬": 5445163772706582819, "💦": 5444979432710242887, "✈": 5447213867971144371,
    "🔖": 5447460764166158313, "🗄": 5447257461889202335, "🗃": 5447210891558814377,
    "🗓": 5444933979071348347, "🎨": 5447642819239903347, "🔗": 5447479640547428304,
    "👨‍🦰": 5447311106030726740, "📍": 5447187153274567373, "ℹ": 5247236071795754971,
    "💎": 5260681660189408650,
}

# every other emoji the screens use -> closest emoji of the pack (so all of them animate)
ALIAS = {
    "⏱": "⏰", "⏳": "⏰", "⏸": "📴", "▫": "☑", "♻": "↩", "⚪": "⚫", "⛔": "🚫", "✖": "❌",
    "❔": "ℹ", "❗": "⚠", "⭕": "📹", "🆔": "🔑", "🎁": "🛍", "🎉": "👍", "🎞": "🖼",
    "🎬": "📹", "🎯": "📍", "🎵": "🎧", "🏠": "🖥", "👁": "🔍", "👑": "🏅", "👤": "👨‍🦰",
    "👥": "🫂", "💡": "☀", "💤": "📴", "💧": "💦", "📌": "📍", "📏": "↔", "📤": "⬆",
    "📥": "⬇", "📨": "📬", "🔁": "↪", "🔄": "↕", "🔐": "🔑", "🔒": "🔑", "🔤": "🔠",
    "🗂": "🗃", "😕": "💔", "🛡": "💼", "🟢": "🔛", "🤖": "👩‍💻", "🧩": "⚙", "🧸": "❤",
    "🧾": "📑", "👇": "⬇", "🔥": "⚡",
}
PACK.update({k.replace(VS, ""): PACK[v.replace(VS, "")] for k, v in ALIAS.items()})

_enabled = True  # owner switch (admin panel)
status = "unknown"  # unknown | working | removed  (what Telegram did with the last screen)
_broken_until = 0.0  # Telegram refused -> plain for a while, then try again (Premium may be bought)
RETRY_AFTER = 600
# longest first so multi-codepoint emoji win; optional VS16 after each
_RE = re.compile("|".join(re.escape(k) + f"{VS}?" for k in sorted(PACK, key=len, reverse=True)))
_TAG_RE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.S)


def set_enabled(on: bool) -> None:
    global _enabled, _broken_until
    _enabled, _broken_until = on, 0.0


def active() -> bool:
    import time
    return _enabled and time.time() >= _broken_until


def check_sent(sent_text: str, message) -> None:
    """Telegram may silently drop custom emoji a bot isn't allowed to use: detect it."""
    global status
    if "<tg-emoji" not in sent_text or message is None or not hasattr(message, "entities"):
        return
    kept = any(isinstance(e, types.MessageEntityCustomEmoji) for e in message.entities or [])
    new = "working" if kept else "removed"
    if new != status and not kept:
        import logging
        logging.getLogger("tgclone").warning(
            "Premium emoji: Telegram removed them from the bot's message (bot not allowed)")
    status = new


def disable_for_session() -> None:
    """Plain emoji for RETRY_AFTER seconds, then premium is tried again."""
    global _broken_until
    import time
    _broken_until = time.time() + RETRY_AFTER


def doc_id(emoji: str) -> Optional[int]:
    return PACK.get(emoji.replace(VS, ""))


def decorate_text(html: str) -> str:
    """Wrap known emoji in <tg-emoji>. Leaves <code>/<pre> content untouched."""
    parts = re.split(r"(<tg-emoji[^>]*>.*?</tg-emoji>|<code>.*?</code>|<pre>.*?</pre>|<[^>]+>)",
                     html, flags=re.S)
    for i, part in enumerate(parts):
        if part.startswith("<"):
            continue
        parts[i] = _RE.sub(lambda m: f'<tg-emoji emoji-id="{doc_id(m.group(0))}">{m.group(0)}</tg-emoji>',
                           part)
    return "".join(parts)


def strip_text(html: str) -> str:
    return _TAG_RE.sub(r"\1", html)


def _decorate_button(b):
    if not isinstance(b, types.KeyboardInlineButton):
        return b
    m = _RE.match(b.text)
    if not m:
        return b
    st = b.style
    style = types.KeyboardButtonStyle(
        bg_primary=getattr(st, "bg_primary", None), bg_danger=getattr(st, "bg_danger", None),
        bg_success=getattr(st, "bg_success", None), icon=doc_id(m.group(0)))
    text = b.text[m.end():].strip() or b.text
    return types.KeyboardInlineButton(text, b.type, style=style)


def decorate(screen: Tuple[str, Optional[list]]) -> Tuple[str, Optional[list]]:
    """Premium version of a screen (or the screen itself when premium emoji are off)."""
    text, buttons = screen
    if not active():
        return screen
    if buttons and isinstance(buttons, list):
        buttons = [[_decorate_button(b) for b in row] if isinstance(row, list) else row
                   for row in buttons]
    return decorate_text(text), buttons


def is_emoji_error(e: Exception) -> bool:
    s = str(e).upper()
    return any(k in s for k in ("EMOJI", "DOCUMENT_INVALID", "PREMIUM", "ENTITY_BOUNDS",
                                "ENTITIES_TOO_LONG", "BUTTON"))


# ---------------------------------------------------------------- every bot message
def _prepare(text: str, parse_mode) -> Optional[str]:
    """HTML ready for decoration, or None when this message must be left alone."""
    if not isinstance(text, str) or not text or not active():
        return None
    if isinstance(parse_mode, str) and parse_mode.lower() == "html":
        return text
    if parse_mode in (None, ()):  # client default (markdown): our plain texts -> escape to HTML
        import html as _html
        return _html.escape(text, quote=False)
    return None


def _is_private(entity) -> bool:
    if isinstance(entity, int):
        return entity > 0
    if isinstance(entity, (types.InputPeerUser, types.PeerUser, types.User, types.InputUser)):
        return True
    peer = getattr(entity, "peer_id", None)  # a Message being edited
    return isinstance(peer, types.PeerUser)


def install(client) -> None:
    """Make every message / edit / caption this (bot) client sends use premium emoji.

    Falls back to the original call if Telegram refuses, and records whether Telegram
    kept the custom emoji (see `status`). Callback popups can't carry formatting at all.
    """
    if getattr(client, "_tgc_emoji", False):
        return
    client._tgc_emoji = True
    orig_send, orig_edit, orig_file = client.send_message, client.edit_message, client.send_file
    client._tgc_orig_send = orig_send  # raw sender for the admin "test premium emoji" button

    async def _go(fn, args, kw, where):
        args = list(args)
        # only the bot's own screens in private chats - never cloned posts in groups/channels
        if kw.get("formatting_entities") or not _is_private(args[0] if args else kw.get("entity")):
            return await fn(*args, **kw)
        if where == "caption":
            text = kw.get("caption")
        elif where == "send":
            text = args[1] if len(args) > 1 else kw.get("message", "")
        else:  # edit: (entity, message, text) / (message, text) / text=
            if "text" in kw:
                text = kw["text"]
            elif len(args) > 2 and isinstance(args[2], str):
                text = args[2]
            elif len(args) > 1 and isinstance(args[1], str):
                text = args[1]
            else:
                text = None
        html = _prepare(text, kw.get("parse_mode", None if where != "edit" else ()))
        if html is None:
            return await fn(*args, **kw)
        new_text, buttons = decorate((html, kw.get("buttons")))
        nargs, nkw = list(args), dict(kw, parse_mode="html")
        if buttons is not None:
            nkw["buttons"] = buttons
        if where == "caption":
            nkw["caption"] = new_text
        elif where == "send":
            if len(nargs) > 1:
                nargs[1] = new_text
            else:
                nkw["message"] = new_text
        elif "text" in kw:
            nkw["text"] = new_text
        elif len(nargs) > 2 and isinstance(nargs[2], str):
            nargs[2] = new_text
        else:
            nargs[1] = new_text
        try:
            res = await fn(*nargs, **nkw)
        except Exception as e:
            if not is_emoji_error(e):
                raise
            import logging
            logging.getLogger("tgclone").warning("Premium emoji refused (%s) - normal emoji", e)
            disable_for_session()
            return await fn(*args, **kw)
        check_sent(new_text, res)
        return res

    async def send_message(*args, **kw):
        return await _go(orig_send, args, kw, "send")

    async def edit_message(*args, **kw):
        return await _go(orig_edit, args, kw, "edit")

    async def send_file(*args, **kw):
        return await _go(orig_file, args, kw, "caption")

    client.send_message, client.edit_message, client.send_file = send_message, edit_message, send_file
