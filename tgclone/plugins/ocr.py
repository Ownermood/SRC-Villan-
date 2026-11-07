import pytesseract  # optional dependency
from PIL import Image

from tgclone.plugins import Plugin


class Ocr(Plugin):
    id_ = "ocr"

    async def modify(self, tm):
        if tm.file_type != "photo":
            return tm
        path = await tm.download()
        with Image.open(path) as img:
            tm.text = pytesseract.image_to_string(img)
        return tm


PLUGIN = Ocr
