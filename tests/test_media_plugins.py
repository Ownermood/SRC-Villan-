"""Real OCR / watermark runs (skipped when tesseract / ffmpeg are not installed)."""

import asyncio
import json
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw, ImageFont

from fakes import Msg
from tgclone.config import DATA_DIR, Config
from tgclone.message import CloneMessage

run = asyncio.run
need_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
need_tess = pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")


class FileMsg(Msg):
    """Msg whose download_media copies a real local file."""

    def __init__(self, id, src, kind, text=""):
        super().__init__(id, text, media="M", kind=kind)
        self.src = src

    async def download_media(self, file, thumb=None, progress_callback=None):
        dest = file + f"src{self.id}{self.src[self.src.rfind('.'):]}"
        shutil.copy(self.src, dest)
        return dest


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    return {s["codec_type"]: s for s in json.loads(out)["streams"]}


@need_tess
def test_ocr_reads_text(tmp_path):
    from tgclone.plugins.ocr import Ocr
    img = Image.new("RGB", (900, 200), "white")
    ImageDraw.Draw(img).text((30, 60), "HELLO TGCLONE 2026", fill="black",
                             font=ImageFont.load_default(size=60))
    src = tmp_path / "t.png"
    img.save(src)
    cfg = Config().plugins.ocr
    tm = run(Ocr(cfg).modify(CloneMessage(FileMsg(1, str(src), "photo", text="caption"))))
    assert "HELLO TGCLONE 2026" in tm.text and tm.text.startswith("caption\n\n")
    cfg.keep_caption = False
    tm = run(Ocr(cfg).modify(CloneMessage(FileMsg(2, str(src), "photo", text="caption"))))
    assert tm.text.startswith("HELLO")
    cfg.lang = "xxx"
    with pytest.raises(RuntimeError, match="language"):
        Ocr(cfg)


@need_ffmpeg
def test_watermark_video_keeps_audio_and_photo(tmp_path):
    from tgclone.plugins import mark
    logo = Image.new("RGBA", (300, 120), (255, 0, 0, 160))
    logo.save(mark.LOGO)
    video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=640x360:rate=25:duration=2", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-c:v", "libx264", "-c:a", "aac",
                    "-shortest", str(video)], check=True)
    cfg = Config().plugins.mark
    plugin = mark.Mark(cfg)
    tm = run(plugin.modify(CloneMessage(FileMsg(1, str(video), "video"))))
    streams = probe(tm.new_file)
    assert "audio" in streams, "audio must be kept"
    assert streams["video"]["width"] == 640 and streams["video"]["height"] == 360
    # logo pixel present in bottom-right corner (red-ish)
    frame = tmp_path / "f.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tm.new_file, "-frames:v", "1",
                    str(frame)], check=True)
    r, g, b = Image.open(frame).convert("RGB").getpixel((640 - 40, 360 - 20))[:3]
    assert r > 150 and g < 120, (r, g, b)
    out = tm.new_file
    tm.cleanup()
    assert not shutil.os.path.exists(out)

    photo = tmp_path / "p.jpg"
    Image.new("RGB", (800, 600), "white").save(photo)
    tm = run(plugin.modify(CloneMessage(FileMsg(2, str(photo), "photo"))))
    im = Image.open(tm.new_file)
    assert im.size == (800, 600) and im.convert("RGB").getpixel((800 - 30, 600 - 15))[1] < 150
    tm.cleanup()

    # sticker / document untouched
    tm = run(plugin.modify(CloneMessage(Msg(3, "x", media="M", kind="document"))))
    assert tm.new_file is None


@need_ffmpeg
def test_watermark_needs_logo():
    from tgclone.plugins import mark
    if mark.LOGO.exists():
        mark.LOGO.unlink()
    with pytest.raises(RuntimeError, match="logo"):
        mark.Mark(Config().plugins.mark)


def test_plugin_load_failure_is_reported_not_fatal(monkeypatch):
    from tgclone.plugins import load_plugins
    cfg = Config().plugins
    cfg.mark.check = True
    cfg.mark.image = str(DATA_DIR / "does-not-exist.png")
    assert [p.id_ for p in load_plugins(cfg)] == []


@need_ffmpeg
def test_panel_logo_upload_and_cycles():
    import io
    from test_panel import press
    from test_thumbs import _say
    from fakes import TextEvent, make_app
    from tgclone import bot
    from tgclone.plugins import mark
    if mark.LOGO.exists():
        mark.LOGO.unlink()
    a = make_app()
    p = run(bot.setup(a, live=True))
    ev = run(press(p, "wl"))
    assert p.state[1]["kind"] == "logo" and "Watermark logo" in ev.edits[-1][0]
    buf = io.BytesIO()
    Image.new("RGBA", (2000, 500), (0, 0, 255, 128)).save(buf, "PNG")
    ev = run(_say(p, TextEvent("", image=buf.getvalue())))
    assert mark.LOGO.exists() and max(Image.open(mark.LOGO).size) <= 1024
    assert Image.open(mark.LOGO).mode == "RGBA", "transparency kept"
    assert a.cfg.plugins.mark.check and any(x.id_ == "mark" for x in a.plugins)
    assert "watermark is on" in ev.out[0][0]
    run(press(p, "wp"))
    assert a.cfg.plugins.mark.position == "bottom_left"
    run(press(p, "wz"))
    assert a.cfg.plugins.mark.size == 0.3
    run(press(p, "ol"))
    assert a.cfg.plugins.ocr.lang == "eng+hin"


@need_tess
def test_ocr_hindi_pack_detected():
    from tgclone.plugins.ocr import Ocr
    import pytesseract
    if "hin" not in pytesseract.get_languages(config=""):
        pytest.skip("Hindi pack not installed")
    cfg = Config().plugins.ocr
    cfg.lang = "eng+hin"
    Ocr(cfg)  # loads without error
