from tgclone.plugins import Plugin, as_html


class Caption(Plugin):
    id_ = "caption"

    def modify(self, tm):
        if tm.text or tm.file_type in ("text", "photo", "video", "gif", "audio", "voice", "document"):
            tm.text = f"{as_html(self.cfg.header)}{tm.text}{as_html(self.cfg.footer)}"
        return tm


PLUGIN = Caption
