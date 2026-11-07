# SRC-Villan (tgclone)

One clean Telethon tool that merges **5 projects**:

| Source project | What was kept |
|---|---|
| tgcf | live sync (new/edit/delete), past mode, plugins (filter, replace, fmt, caption, ocr, watermark), admin commands |
| Telethon-Channel-Clone | full history copy + batch pause (`past.batch_size` / `past.batch_sleep`) |
| clonechat | protected-content clone (download + re-upload), resume, file-type filter, per-message delay |
| Save-Restricted-Content-Bot v2 / v3 | save posts from `t.me` links (public, private `/c/`, topic links), `/batch`, `/stop`, save target chat |

Not kept (on purpose): premium/payment/redeem codes, ad shortener, `/eval` (remote code execution),
speedtest, broadcast, yt-dlp downloader, MongoDB, multi-user session storage, Dropbox-hosted
pyrogram forks and `devgagantools` (unverifiable dependencies), obfuscated promo strings.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # fill API_ID, API_HASH, BOT_TOKEN, ADMINS
python -m tgclone login       # prints SESSION_STRING -> put it in .env
```

The first run creates `tgclone.config.json`. Secrets stay in `.env` only; the config file has no secrets.

## Run

```bash
python -m tgclone live        # real-time sync + control panel (main mode)
python -m tgclone bot         # control panel / save bot only, no live sync
python -m tgclone past        # clone history of all enabled forwards (resumable)
python -m tgclone past --source -100111 --dest -100222      # one-off clone
python -m tgclone past --fresh                               # start over
```

Docker (restarts automatically after a crash or reboot): `docker compose up -d --build`
Logs are written to `data/tgclone.log` (rotating, 3 × 5 MB).

## Accounts

| `.env` | Result |
|---|---|
| `SESSION_STRING` only | the user account does everything, commands use `.` from your own account |
| `BOT_TOKEN` only | live mode only (the bot must be an admin in the source chat), no history or save |
| both (recommended) | the user account reads, the bot is the control panel. With `send_via_bot: true` the bot also sends. |

## Control panel (bot)

Send `/start` to the bot. Everything works with buttons. The colored buttons need Telethon ≥ 1.45 (Telegram layer 229).

- **📋 Forwards**: list, pause or resume, clone history, re-clone from the start, delete (with a confirm step). The buttons carry a checksum, so an old menu never acts on the wrong forward.
- **➕ New forward**: a 2-step wizard. *Forward any message from the channel* and the id is detected for you. You can also send `@user`, `-100id` or a `t.me` link.
- **📥 Save posts**: send a post link or a range link (`t.me/c/123/10-60`). You can send a link at any time without opening the menu.
- **⏳ Jobs**: live progress card with a ▰▱ bar, done/failed counts, speed, ETA and the current file's ⬇️/⬆️ MB/s. Each job has its own ⛔ Stop button. Up to `max_jobs` run at once, and the same clone can never run twice.
- **🧩 Plugins**: on/off toggles (applied instantly, no restart), style switcher, file-type grid (tap to block), editors for blocked words, required words, replace rules, header and footer.
- **⚙️ Settings**: copy or forward mode, protected-chat fallback, send via bot, edit and delete sync, ➖/➕ delays, save target.

Text commands still work (and are the only option for a user account without a bot, using the `.` prefix):

```
/menu /list /add <src> <dest..> /remove <#n|src> /toggle <#n> /clone <#n> [fresh]
/clone <src> <dest> /batch <link> <count> /jobs /stop [id] /setchat <chat|off> /style <s> /status
```

## Config (`tgclone.config.json`)

```json
{
  "forwards": [{"name": "main", "source": -100111, "dest": [-100222], "offset": 0, "end": 0}],
  "forward_mode": false,
  "send_via_bot": false,
  "protected_fallback": true,
  "max_file_mb": 2000,
  "max_disk_mb": 6000,
  "max_jobs": 3,
  "live": {"edit_sync": true, "delete_sync": false, "delete_on_edit": ".deleteMe"},
  "past": {"delay": 1.0, "batch_size": 0, "batch_sleep": 0},
  "save": {"target": "", "use_plugins": false, "max_batch": 1000, "delay": 1.0},
  "plugins": {
    "filter":  {"check": false, "users": {"blacklist": [], "whitelist": []},
                "files": {"blacklist": ["sticker"], "whitelist": []},
                "text":  {"blacklist": [], "whitelist": [], "case_sensitive": false, "regex": false}},
    "replace": {"check": false, "text": {"old": "new"}, "regex": false},
    "fmt":     {"check": false, "style": "preserve"},
    "caption": {"check": false, "header": "", "footer": ""},
    "ocr":     {"check": false},
    "mark":    {"check": false, "image": "image.png", "position": "centre", "frame_rate": 15}
  }
}
```
File types: `text photo video gif audio voice video_note sticker document poll contact other`.

## Behaviour notes

- Re-uploaded files keep their original thumbnail, file name, duration and voice/round flags.
- `max_disk_mb` is a soft cap on temporary downloads. When it is full, new downloads wait for running jobs to free space.
- Edit sync ignores reaction and view-count updates. If the media itself was replaced, the new media is synced too.

## Tests

```bash
pip install pytest && python -m pytest -q tests
```
The tests run offline with fake clients. They cover sending (copy, forward, protected, album, thumbnail, progress), resume and dedup, jobs, live edit and delete, the control panel flows, and real Telethon serialization of every screen.

## Data

`data/tgclone.db` (SQLite) stores the source→dest message map (used for reply, edit and delete sync)
and past-mode progress. Restarts resume without duplicates. Temporary downloads go to `data/downloads`
and are deleted after sending.
