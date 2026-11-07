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
python -m tgclone live        # real-time sync + control bot (main mode)
python -m tgclone bot         # control/save bot only, no live sync
python -m tgclone past        # clone history of all enabled forwards (resumable)
python -m tgclone past --source -100111 --dest -100222      # one-off clone
python -m tgclone past --fresh                               # start over
```

Docker: `docker build -t tgclone . && docker run --env-file .env -v $PWD/data:/app/data tgclone`

## Accounts

| `.env` | Result |
|---|---|
| `SESSION_STRING` only | the user account does everything, commands use `.` from your own account |
| `BOT_TOKEN` only | live mode only (the bot must be an admin in the source chat), no history or save |
| both (recommended) | the user account reads, the bot is the control panel. With `send_via_bot: true` the bot also sends. |

## Bot commands (admins only)

```
/add <source> <dest> [dest2..]   /list   /remove <#n|source>   /toggle <#n>
/clone <#n> [fresh]              /clone <source> <dest>
/batch <t.me link> <count>       /setchat <chat|off>   /stop   /status   /style <style>
```
Send any post link (`https://t.me/c/123/45`, `t.me/chan/10-50`) to the bot to save it.

## Config (`tgclone.config.json`)

```json
{
  "forwards": [{"name": "main", "source": -100111, "dest": [-100222], "offset": 0, "end": 0}],
  "forward_mode": false,
  "send_via_bot": false,
  "protected_fallback": true,
  "max_file_mb": 2000,
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

## Data

`data/tgclone.db` (SQLite) stores the source→dest message map (used for reply, edit and delete sync)
and past-mode progress. Restarts resume without duplicates. Temporary downloads go to `data/downloads`
and are deleted after sending.
