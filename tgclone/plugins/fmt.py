import html

from tgclone.plugins import STYLE_CODES, Plugin


class Fmt(Plugin):
    id_ = "fmt"

    def modify(self, tm):
        style = self.cfg.style
        if style == "preserve" or style not in STYLE_CODES or not tm.raw_text:
            return tm
        open_, close = STYLE_CODES[style]
        tm.text = f"{open_}{html.escape(tm.raw_text, quote=False)}{close}"
        return tm


PLUGIN = Fmt
