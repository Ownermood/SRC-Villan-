import re

from tgclone.plugins import STYLE_CODES, Plugin


class Replace(Plugin):
    id_ = "replace"

    def modify(self, tm):
        text = tm.text
        if not text:
            return tm
        for old, new in self.cfg.text.items():
            if not self.cfg.regex:
                text = text.replace(old, new)
            elif new in STYLE_CODES:  # style the matched part, e.g. {"\\d+": "bold"}
                s = STYLE_CODES[new]
                text = re.sub(old, lambda m: f"{s}{m.group(0)}{s}", text)
            else:
                text = re.sub(old, new, text)
        tm.text = text
        return tm


PLUGIN = Replace
