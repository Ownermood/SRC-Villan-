import html
import re

from tgclone.plugins import STYLE_CODES, Plugin, as_html, on_text_nodes


class Replace(Plugin):
    id_ = "replace"

    def modify(self, tm):
        text = tm.text
        if not text:
            return tm
        for old, new in self.cfg.text.items():
            if not self.cfg.regex:
                o, n = html.escape(old, quote=False), as_html(new)
                text = on_text_nodes(text, lambda t, o=o, n=n: t.replace(o, n))
            elif new in STYLE_CODES:  # style the matched part, e.g. {"\\d+": "bold"}
                a, b = STYLE_CODES[new]
                text = on_text_nodes(text, lambda t, old=old: re.sub(old, lambda m: f"{a}{m.group(0)}{b}", t))
            else:
                text = on_text_nodes(text, lambda t, old=old, new=new: re.sub(old, new, t))
        tm.text = text
        return tm


PLUGIN = Replace
