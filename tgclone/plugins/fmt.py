from tgclone.plugins import STYLE_CODES, Plugin


class Fmt(Plugin):
    id_ = "fmt"

    def modify(self, tm):
        style = self.cfg.style
        if style == "preserve" or style not in STYLE_CODES or not tm.raw_text:
            return tm
        s = STYLE_CODES[style]
        tm.text = f"{s}{tm.raw_text}{s}"
        return tm


PLUGIN = Fmt
