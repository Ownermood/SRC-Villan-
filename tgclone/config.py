"""Settings live in a JSON file, secrets live in environment / .env only."""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Union

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()
log = logging.getLogger("tgclone")

CONFIG_FILE = Path(os.getenv("TGCLONE_CONFIG", "tgclone.config.json"))
DATA_DIR = Path(os.getenv("TGCLONE_DATA", "data"))
DOWNLOAD_DIR = DATA_DIR / "downloads"

ChatRef = Union[int, str]


# ---------------------------------------------------------------- plugins
class FilterList(BaseModel):
    blacklist: List[str] = []
    whitelist: List[str] = []


class TextFilter(FilterList):
    case_sensitive: bool = False
    regex: bool = False


class FilterCfg(BaseModel):
    check: bool = False
    users: FilterList = FilterList()
    # file types: text photo video gif audio voice video_note sticker document poll contact other
    files: FilterList = FilterList()
    text: TextFilter = TextFilter()


class ReplaceCfg(BaseModel):
    check: bool = False
    text: Dict[str, str] = {}
    regex: bool = False


class FmtCfg(BaseModel):
    check: bool = False
    style: str = "preserve"  # preserve | plain | bold | italics | code | strike


class CaptionCfg(BaseModel):
    check: bool = False
    header: str = ""
    footer: str = ""


class MarkCfg(BaseModel):
    check: bool = False
    image: str = "image.png"  # local path or https:// url
    position: str = "centre"
    frame_rate: int = 15


class OcrCfg(BaseModel):
    check: bool = False


class PluginConfig(BaseModel):
    filter: FilterCfg = FilterCfg()
    ocr: OcrCfg = OcrCfg()
    replace: ReplaceCfg = ReplaceCfg()
    fmt: FmtCfg = FmtCfg()
    caption: CaptionCfg = CaptionCfg()
    mark: MarkCfg = MarkCfg()


# ---------------------------------------------------------------- core
class Forward(BaseModel):
    name: str = ""
    enabled: bool = True
    source: ChatRef = ""
    dest: List[ChatRef] = []
    offset: int = 0  # past: start after this message id
    end: int = 0  # past: stop at this message id (0 = till latest)


class LiveSettings(BaseModel):
    sequential_updates: bool = False
    edit_sync: bool = True
    delete_sync: bool = False
    delete_on_edit: str = ".deleteMe"


class PastSettings(BaseModel):
    delay: float = Field(1.0, ge=0, le=300)  # seconds after every message/album
    batch_size: int = Field(0, ge=0)  # extra pause after N messages (0 = off)
    batch_sleep: float = Field(0, ge=0)


class SaveSettings(BaseModel):
    """Save-restricted-content: send a t.me link to the bot, get the post back."""
    target: ChatRef = ""  # where saved posts go ("" = the chat that asked)
    use_plugins: bool = False  # apply filter/replace/caption... to saved posts
    max_batch: int = Field(1000, ge=1, le=10000)
    delay: float = Field(1.0, ge=0, le=60)


class Config(BaseModel):
    admins: List[ChatRef] = []
    forwards: List[Forward] = []
    forward_mode: bool = False  # True = real forward ("Forwarded from" tag)
    send_via_bot: bool = False  # user account reads, BOT_TOKEN account sends
    protected_fallback: bool = True  # download + re-upload when copy is blocked
    max_file_mb: int = 2000  # skip bigger files on download (0 = no limit)
    live: LiveSettings = LiveSettings()
    past: PastSettings = PastSettings()
    save: SaveSettings = SaveSettings()
    plugins: PluginConfig = PluginConfig()


def load_config() -> Config:
    if not CONFIG_FILE.exists():
        cfg = Config()
        save_config(cfg)
        log.warning("Created default %s - add your forwards there.", CONFIG_FILE)
        return cfg
    return Config.model_validate_json(CONFIG_FILE.read_text(encoding="utf8"))


def save_config(cfg: Config) -> None:
    """Atomic write: never leaves a half-written config on crash."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CONFIG_FILE.parent or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf8") as f:
            json.dump(cfg.model_dump(), f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


class Secrets:
    """Read once from env. Never logged."""

    def __init__(self) -> None:
        self.api_id = int(os.getenv("API_ID", "0") or 0)
        self.api_hash = os.getenv("API_HASH", "")
        self.session_string = os.getenv("SESSION_STRING", "")
        self.bot_token = os.getenv("BOT_TOKEN", "")
        self.admins = [a.strip() for a in os.getenv("ADMINS", "").split(",") if a.strip()]

    def check(self) -> None:
        if not self.api_id or not self.api_hash:
            raise SystemExit("API_ID / API_HASH missing (get them from my.telegram.org).")


def norm_ref(ref: ChatRef) -> ChatRef:
    """'-100123' -> -100123, 't.me/x' / '@x' stay strings."""
    if isinstance(ref, str):
        s = ref.strip()
        if s.lstrip("-").isdigit():
            return int(s)
        return s
    return ref
