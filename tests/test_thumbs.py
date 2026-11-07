import asyncio
import io

from PIL import Image
from telethon import events
from telethon.tl import types

from fakes import Client, Msg, TextEvent, make_app
from tgclone import bot, thumbs, ui
from tgclone.config import Config
from tgclone.message import CloneMessage, send

run = asyncio.run


def png(w=1200, h=800, alpha=True):
    img = Image.new("RGBA" if alpha else "RGB", (w, h), (200, 30, 30, 128) if alpha else (1, 2, 3))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def noisy(w=2000, h=2000):
    import os
    img = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_save_custom_meets_telegram_limits():
    cfg0 = Config()
    for data in (png(), noisy()):
        size = thumbs.save_custom(cfg0, data)
        img = Image.open(thumbs.custom_path(cfg0))
        assert img.format == "JPEG" and max(img.size) <= 320 and size <= 200 * 1024
    assert thumbs.delete_custom(cfg0) and not thumbs.has_custom(cfg0)
    try:
        thumbs.save_custom(cfg0, b"not an image")
        assert False
    except ValueError:
        pass


def test_policy():
    cfg = Config()
    assert thumbs.policy(cfg, "video") == "original"
    cfg.thumb.mode = "custom"
    thumbs.delete_custom(Config())
    assert thumbs.policy(cfg, "video") == "original", "custom without image falls back"
    thumbs.save_custom(Config(), png())
    assert thumbs.policy(cfg, "video") == "custom" and thumbs.policy(cfg, "document") == "custom"
    assert thumbs.policy(cfg, "photo") == "original" and thumbs.policy(cfg, "sticker") == "original"
    cfg.thumb.types = ["document"]
    assert thumbs.policy(cfg, "video") == "original"
    cfg.thumb.mode = "none"
    assert thumbs.policy(cfg, "document") == "none"


def test_send_with_custom_and_removed_thumb():
    thumbs.save_custom(Config(), png())
    cfg = Config()
    cfg.thumb.mode = "custom"
    c = Client()  # same client, not protected: would normally be a reference copy
    run(send(c, 7, [CloneMessage(Msg(1, "pdf", media="M", kind="document"))], cfg))
    assert c.calls[0][0] == "file" and c.calls[0][5] == str(thumbs.custom_path(Config()))
    c = Client()
    run(send(c, 7, [CloneMessage(Msg(1, "v", media="M", kind="video"))], cfg))
    assert c.calls[0][5] == str(thumbs.custom_path(Config()))
    cfg.thumb.mode = "none"
    c = Client()
    run(send(c, 7, [CloneMessage(Msg(1, "v", media="M", kind="video"))], cfg))
    assert c.calls[0][0] == "file" and c.calls[0][5] is None
    c = Client()
    run(send(c, 7, [CloneMessage(Msg(1, "p", media="M", kind="photo"))], cfg))
    assert c.calls[0][0] == "msg" and c.calls[0][3] == "M", "photos untouched (no re-upload)"
    cfg.thumb.mode, cfg.forward_mode = "custom", True
    c = Client()
    run(send(c, 7, [CloneMessage(Msg(1, "v", media="M", kind="video"))], cfg))
    assert c.calls[0][0] == "file", "forward mode can't change thumbnails -> copy"


def test_album_gets_custom_thumb():
    thumbs.save_custom(Config(), png())
    cfg = Config()
    cfg.thumb.mode = "custom"
    c = Client()
    tms = [CloneMessage(Msg(i, "", media="M", kind="video", grouped_id=3)) for i in (1, 2)]
    tms.append(CloneMessage(Msg(3, "", media="M", kind="photo", grouped_id=3)))
    r = run(send(c, 7, tms, cfg))
    files = c.calls[-1][2]
    assert len(r) == 3
    assert all(isinstance(f, types.InputMediaUploadedDocument) for f in files[:2])
    assert files[0].thumb == f"H:{thumbs.custom_path(Config())}"
    assert isinstance(files[2], str), "photo stays a plain upload"


async def _say(p, ev):
    m = p.c.handlers[0][1].pattern(ev.raw_text) if ev.raw_text.startswith("/") else None
    try:
        if m:
            ev.pattern_match = m
            await p.c.handlers[0][0](ev)
        else:
            await p.guard(p.private_text)(ev)
    except events.StopPropagation:
        pass
    return ev


def test_panel_thumbnail_flow():
    from test_panel import press, styles
    thumbs.delete_custom(Config())
    a = make_app()
    p = run(bot.setup(a, live=True))
    ev = run(press(p, "th"))
    text, buttons = ev.edits[-1]
    assert "Thumbnail" in text and styles(buttons)["● 📌 Original"] == "primary"
    ev = run(press(p, "thm:custom"))  # no image yet -> asks for one
    assert p.state[1]["kind"] == "thumb" and "Upload thumbnail" in ev.edits[-1][0]
    ev = run(_say(p, TextEvent("hello")))
    assert "Send a photo" in ev.out[-1][0] and p.state[1]["kind"] == "thumb"
    ev = run(_say(p, TextEvent("", image=png())))
    assert thumbs.has_custom(Config()) and a.cfg.thumb.mode == "custom" and 1 not in p.state
    assert ev.out[0][2] == str(thumbs.custom_path(Config())), "preview sent back"
    run(press(p, "tht:audio"))
    assert "audio" not in a.cfg.thumb.types
    run(press(p, "thm:none"))
    assert a.cfg.thumb.mode == "none"
    run(press(p, "thd"))
    assert not thumbs.has_custom(Config())
    # /setthumb as photo caption, /delthumb
    run(_say(p, TextEvent("/setthumb", image=png(alpha=False))))
    assert thumbs.has_custom(Config()) and a.cfg.thumb.mode == "custom"
    ev = run(_say(p, TextEvent("/delthumb")))
    assert not thumbs.has_custom(Config()) and a.cfg.thumb.mode == "original" and "removed" in ev.out[-1][0]
    ev = run(_say(p, TextEvent("/setthumb")))  # no photo -> wizard
    assert p.state[1]["kind"] == "thumb"
    from telethon import TelegramClient
    from telethon.extensions import html as tl_html
    from telethon.sessions import MemorySession
    text, buttons = ui.thumb_view(a)
    tl_html.parse(text)
    assert bytes(TelegramClient(MemorySession(), 1, "x").build_reply_markup(buttons))
