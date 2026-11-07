"""Plugin system: each plugin gets its config section and may modify or drop a message.

modify() returns the CloneMessage (kept) or None (dropped). It can be sync or async.
"""

import importlib
import inspect
import logging
from typing import List, Optional

from tgclone.config import PluginConfig
from tgclone.message import CloneMessage

log = logging.getLogger("tgclone")

ORDER = ("filter", "ocr", "replace", "fmt", "caption", "mark")
STYLE_CODES = {"bold": "**", "italics": "__", "code": "`", "strike": "~~", "plain": ""}


class Plugin:
    id_ = "plugin"

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def modify(self, tm: CloneMessage) -> Optional[CloneMessage]:
        return tm


def load_plugins(cfg: PluginConfig) -> List[Plugin]:
    out: List[Plugin] = []
    for pid in ORDER:
        section = getattr(cfg, pid)
        if not section.check:
            continue
        try:
            mod = importlib.import_module(f"tgclone.plugins.{pid}")
            out.append(mod.PLUGIN(section))
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
