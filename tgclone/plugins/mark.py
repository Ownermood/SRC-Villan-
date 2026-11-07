"""Watermark: put a logo on photos, videos and GIFs (needs the ffmpeg program).

Audio is kept, the logo is scaled to `size` of the media width, and ffmpeg runs
as an async subprocess so the bot stays responsive while encoding.
"""

import asyncio
import hashlib
import logging
import os
import shutil
import urllib.request
from pathlib import Path

from tgclone.config import DATA_DIR, DOWNLOAD_DIR
from tgclone.plugins import Plugin

log = logging.getLogger("tgclone")

LOGO = DATA_DIR / "watermark.png"  # uploaded from the bot
POSITIONS = {
    "top_left": "10:10",
    "top_right": "W-w-10:10",
    "centre": "(W-w)/2:(H-h)/2",
    "bottom_left": "10:H-h-10",
    "bottom_right": "W-w-10:H-h-10",
}
TIMEOUT = 1800  # seconds per file


def logo_path(image: str) -> Path:
    """Configured logo: uploaded file, local path or https URL (cached)."""
    if not image:
        return LOGO
    if not image.startswith("https://"):
        return Path(image)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"mark_{hashlib.sha1(image.encode()).hexdigest()[:12]}.png"
    if not path.exists():
        log.info("Downloading watermark image")
        tmp = path.with_suffix(".part")
        with urllib.request.urlopen(image, timeout=30) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.replace(path)
    return path


def build_cmd(src: str, logo: str, out: str, position: str, size: float,
              video: bool, frame_rate: int = 0) -> list:
    pos = POSITIONS.get(position, POSITIONS["bottom_right"])
    graph = (f"[1:v][0:v]scale2ref=w=trunc(main_w*{size}/2)*2:h=-2[wm][base];"
             f"[base][wm]overlay={pos}:format=auto")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-i", logo,
           "-filter_complex", graph]
    if video:
        cmd += ["-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart"]
        if frame_rate:
            cmd += ["-r", str(frame_rate)]
    else:
        cmd += ["-frames:v", "1", "-q:v", "2"]
    return cmd + [out]


async def run_ffmpeg(cmd: list) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), TIMEOUT)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode(errors='ignore')[-300:]}")


class Mark(Plugin):
    id_ = "mark"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg program not installed (apt install ffmpeg)")
        if not logo_path(cfg.image).exists():
            raise RuntimeError("no watermark logo (upload one from Plugins → Watermark logo)")

    async def modify(self, tm):
        if tm.file_type not in ("photo", "video", "gif"):
            return tm
        src = await tm.download()
        video = tm.file_type != "photo"
        out = str(DOWNLOAD_DIR / f"wm_{os.path.splitext(os.path.basename(src))[0]}"
                                 f"{'.mp4' if video else '.jpg'}")
        await run_ffmpeg(build_cmd(src, str(logo_path(self.cfg.image)), out,
                                   self.cfg.position, self.cfg.size, video, self.cfg.frame_rate))
        tm.new_file = out
        tm.track(out)
        return tm


PLUGIN = Mark
