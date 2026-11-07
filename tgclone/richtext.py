"""Complete Telegram HTML: Telethon's converter misses spoilers, this adds them.

Imported once by message.py. After that tl_html.parse / unparse handle every style:
bold, italic, underline, strike, spoiler, code, pre (+language), quote, expandable quote,
links, mentions and premium emoji.
"""

from telethon.extensions import html as tl_html
from telethon.tl.types import MessageEntitySpoiler

_Base = tl_html.HTMLToTelegramParser


class _Parser(_Base):
    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        spoiler = tag == "tg-spoiler" or (tag == "span" and ("class", "tg-spoiler") in attrs)
        if spoiler and tag not in self._building_entities:
            self._building_entities[tag] = MessageEntitySpoiler(offset=len(self.text), length=0)


def install() -> None:
    """Teach Telethon's HTML converter spoilers (idempotent)."""
    tl_html.ENTITY_TO_FORMATTER[MessageEntitySpoiler] = ("<tg-spoiler>", "</tg-spoiler>")
    if not getattr(tl_html.HTMLToTelegramParser, "_tgc_spoiler", False):
        _Parser._tgc_spoiler = True
        tl_html.HTMLToTelegramParser = _Parser


install()
