import hashlib
import logging
import shutil
import urllib.request

from watermark import File, Position, Watermark, apply_watermark  # optional dependency (+ffmpeg)

from tgclone.config import DATA_DIR
from tgclone.plugins import Plugin

log = logging.getLogger("tgclone")


class Mark(Plugin):
    id_ = "mark"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self.overlay = self._overlay_path(cfg.image)
        self.position = Position[cfg.position]

    @staticmethod
    def _overlay_path(image: str) -> str:
        if not image.startswith("https://"):
            return image
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = DATA_DIR / f"mark_{hashlib.sha1(image.encode()).hexdigest()[:12]}.png"
        if not path.exists():
            log.info("Downloading watermark image")
            with urllib.request.urlopen(image, timeout=30) as r, open(path, "wb") as f:
                shutil.copyfileobj(r, f)
        return str(path)

    async def modify(self, tm):
        if tm.file_type not in ("photo", "video", "gif"):
            return tm
        base = await tm.download()
        out = apply_watermark(File(base), Watermark(File(self.overlay), self.position),
                              frame_rate=self.cfg.frame_rate)
        tm.new_file = out
        tm.track(out)
        return tm


PLUGIN = Mark
