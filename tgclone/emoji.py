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

_enabled = True  # owner switch (admin panel)
_broken = False  # Telegram refused once this run -> plain from now on
# longest first so multi-codepoint emoji win; optional VS16 after each
_RE = re.compile("|".join(re.escape(k) + f"{VS}?" for k in sorted(PACK, key=len, reverse=True)))
_TAG_RE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.S)


def set_enabled(on: bool) -> None:
    global _enabled, _broken
    _enabled, _broken = on, False


def active() -> bool:
    return _enabled and not _broken


def disable_for_session() -> None:
    global _broken
    _broken = True


def doc_id(emoji: str) -> Optional[int]:
    return PACK.get(emoji.replace(VS, ""))


def decorate_text(html: str) -> str:
    """Wrap known emoji in <tg-emoji>. Leaves <code>/<pre> content untouched."""
    parts = re.split(r"(<code>.*?</code>|<pre>.*?</pre>|<[^>]+>)", html, flags=re.S)
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
