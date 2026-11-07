from tgclone.plugins import Plugin


class Caption(Plugin):
    id_ = "caption"

    def modify(self, tm):
        if tm.text or tm.file_type in ("text", "photo", "video", "gif", "audio", "voice", "document"):
            tm.text = f"{self.cfg.header}{tm.text}{self.cfg.footer}"
        return tm


PLUGIN = Caption
