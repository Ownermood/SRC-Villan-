"""Plugin system: each plugin gets its config section and may modify or drop a message.

modify() returns the CloneMessage (kept) or None (dropped). It can be sync or async.
"""

import importlib
import inspect
import logging
import re
from pathlib import Path
from typing import List, Optional

from tgclone.config import DATA_DIR, PluginConfig
from tgclone.message import CloneMessage

log = logging.getLogger("tgclone")

ORDER = ("filter", "ocr", "replace", "fmt", "caption", "mark")
# message text inside the bot is HTML (keeps quotes, spoilers, underline... intact)
STYLE_CODES = {"bold": ("<b>", "</b>"), "italics": ("<i>", "</i>"), "code": ("<code>", "</code>"),
               "strike": ("<s>", "</s>"), "plain": ("", "")}
_TAG = re.compile(r"(<[^>]+>)")
_HTML_HINT = re.compile(r"</?(b|i|u|s|a|code|pre|blockquote|tg-spoiler|tg-emoji|strong|em|del)\b", re.I)


def as_html(text: str) -> str:
    """User-written text (header, footer, replacement): HTML if it has tags, else escaped."""
    import html
    return text if _HTML_HINT.search(text or "") else html.escape(text or "", quote=False)


def on_text_nodes(html_text: str, fn) -> str:
    """Apply fn to the visible text only, never inside tags."""
    return "".join(p if p.startswith("<") else fn(p) for p in _TAG.split(html_text))


class Plugin:
    id_ = "plugin"

    def __init__(self, cfg, home: Path = DATA_DIR) -> None:
        self.cfg = cfg
        self.home = Path(home)  # the user's folder (logo etc.)

    def modify(self, tm: CloneMessage) -> Optional[CloneMessage]:
        return tm


def load_plugins(cfg: PluginConfig, home: Path = DATA_DIR) -> List[Plugin]:
    out: List[Plugin] = []
    for pid in ORDER:
        section = getattr(cfg, pid)
        if not section.check:
            continue
        try:
            mod = importlib.import_module(f"tgclone.plugins.{pid}")
            out.append(mod.PLUGIN(section, home))
            log.info("Plugin loaded: %s", pid)
        except Exception as e:  # missing optional dependency etc.
            log.error("Plugin %s not loaded: %s", pid, e)
    return out


async def apply_plugins(plugins: List[Plugin], tm: CloneMessage) -> Optional[CloneMessage]:
    for p in plugins:
        try:
            res = p.modify(tm)
            if inspect.isawaitable(res):
                res = await res
        except Exception as e:
            if p.id_ == "filter":  # fail closed: never leak unfiltered content
                log.error("Filter failed, message dropped: %s", e)
                tm.cleanup()
                return None
            log.error("Plugin %s failed, skipped: %s", p.id_, e)
            continue
        if res is None:
            tm.cleanup()
            return None
        tm = res
    return tm
