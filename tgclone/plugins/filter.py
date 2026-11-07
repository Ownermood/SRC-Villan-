import re

from tgclone.plugins import Plugin


def _match(pattern: str, text: str, regex: bool, case: bool) -> bool:
    if regex:
        return re.search(pattern, text, 0 if case else re.IGNORECASE) is not None
    return (pattern if case else pattern.lower()) in (text if case else text.lower())


class Filter(Plugin):
    id_ = "filter"

    def modify(self, tm):
        if self.users_ok(tm) and self.files_ok(tm) and self.text_ok(tm):
            return tm
        return None

    def users_ok(self, tm) -> bool:
        f, s = self.cfg.users, str(tm.sender_id)
        return s not in f.blacklist and (not f.whitelist or s in f.whitelist)

    def files_ok(self, tm) -> bool:
        f, t = self.cfg.files, tm.file_type
        return t not in f.blacklist and (not f.whitelist or t in f.whitelist)

    def text_ok(self, tm) -> bool:
        f = self.cfg.text
        text = tm.raw_text or ""
        if any(_match(p, text, f.regex, f.case_sensitive) for p in f.blacklist):
            return False
        if not f.whitelist:
            return True
        return any(_match(p, text, f.regex, f.case_sensitive) for p in f.whitelist)


PLUGIN = Filter
