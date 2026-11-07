"""OCR: read text from photos (needs the tesseract program + pytesseract)."""

import asyncio
import shutil

import pytesseract
from PIL import Image

from tgclone.plugins import Plugin


class Ocr(Plugin):
    id_ = "ocr"

    def __init__(self, cfg, home=None) -> None:
        super().__init__(cfg, *([home] if home else []))
        if not shutil.which("tesseract"):
            raise RuntimeError("tesseract program not installed (apt install tesseract-ocr)")
        missing = set(cfg.lang.split("+")) - set(pytesseract.get_languages(config=""))
        if missing:
            raise RuntimeError(f"tesseract language(s) missing: {', '.join(sorted(missing))} "
                               f"(apt install tesseract-ocr-<lang>)")

    def _read(self, path: str) -> str:
        with Image.open(path) as img:
            return pytesseract.image_to_string(img, lang=self.cfg.lang).strip()

    async def modify(self, tm):
        if tm.file_type != "photo":
            return tm
        path = await tm.download()
        text = await asyncio.to_thread(self._read, path)  # never block the bot
        if not text:
            return tm
        import html
        text = html.escape(text, quote=False)
        if self.cfg.keep_caption and tm.text:
            tm.text = f"{tm.text}\n\n{text}"
        else:
            tm.text = text
        return tm


PLUGIN = Ocr
