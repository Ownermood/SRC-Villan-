"""Thumbnail policy: keep original, use your own image, or remove.

Telegram allows custom thumbnails only on documents (video, file/PDF, audio cover, GIF).
Photos, stickers, voice notes and round videos can't carry one.
Changing a thumbnail needs a re-upload (a reference copy can't change it).
"""

import io
import logging
from typing import Optional

from tgclone.config import DATA_DIR, Config

log = logging.getLogger("tgclone")

CUSTOM_THUMB = DATA_DIR / "custom_thumb.jpg"
THUMB_TYPES = ("video", "document", "audio", "gif")
MAX_SIDE = 320  # Telegram limit
MAX_BYTES = 200 * 1024  # Telegram limit


def has_custom() -> bool:
    return CUSTOM_THUMB.exists() and CUSTOM_THUMB.stat().st_size > 0


def policy(cfg: Config, file_type: str) -> str:
    """'original' | 'custom' | 'none' for this file type."""
    if file_type not in THUMB_TYPES or file_type not in cfg.thumb.types:
        return "original"
    if cfg.thumb.mode == "custom" and not has_custom():
        return "original"
    return cfg.thumb.mode if cfg.thumb.mode in ("custom", "none") else "original"


def needs_reupload(cfg: Config, file_type: str) -> bool:
    return policy(cfg, file_type) != "original"


def save_custom(image: bytes) -> int:
    """Convert any image to a Telegram-valid JPEG thumbnail. Returns size in bytes."""
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover
        raise ValueError("Pillow is not installed (pip install Pillow)") from e
    try:
        img = Image.open(io.BytesIO(image))
        img.load()
    except Exception as e:
        raise ValueError("That file is not an image") from e
    if img.mode not in ("RGB", "L"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    for quality in (92, 85, 75, 65, 50, 35):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_BYTES:
            break
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CUSTOM_THUMB.with_suffix(".tmp")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(CUSTOM_THUMB)
    log.info("Custom thumbnail saved (%sx%s, %s bytes)", *img.size, buf.tell())
    return buf.tell()


def delete_custom() -> bool:
    if CUSTOM_THUMB.exists():
        CUSTOM_THUMB.unlink()
        return True
    return False


async def pick(cfg: Config, tm) -> Optional[str]:
    """Thumbnail path to upload with this message (None = no thumbnail)."""
    pol = policy(cfg, tm.file_type)
    if pol == "none":
        return None
    if pol == "custom":
        return str(CUSTOM_THUMB)
    return await tm.download_thumb()
