# Ubuntu par tgclone chalana — step by step

Ubuntu 22.04 / 24.04 (VPS ya PC) par test kiya gaya hai. Har command copy-paste karo.

---

## Step 0 — Ye 4 cheezein pehle le lo

| Cheez | Kahan se | Kaise |
|---|---|---|
| **API_ID** aur **API_HASH** | https://my.telegram.org | Login → *API development tools* → koi bhi app name bharo → dono values copy karo |
| **BOT_TOKEN** | Telegram mein **@BotFather** | `/newbot` → naam do → token copy karo (`123456:ABC...`) |
| **Aapka user ID** | Telegram mein **@userinfobot** | Bot ko `/start` bhejo → jo `Id` dikhe woh copy karo |
| **Phone number** | Woh Telegram account jo source channels ka member hai | Login ke waqt OTP aayega |

> ⚠️ API_HASH, BOT_TOKEN aur SESSION_STRING kisi ko mat dena. Inse aapka account control ho sakta hai.

---

## Step 1 — Server par login karo aur code download karo

```bash
sudo apt update && sudo apt install -y git
git clone -b ccr-1f6db1cf-flau3n https://github.com/Ownermood/SRC-Villan-.git
cd SRC-Villan-
```

> Repo private hai to GitHub username + **Personal Access Token** maangega (password nahi chalega).
> Token: GitHub → Settings → Developer settings → Personal access tokens.

---

## Step 2 — Sab install karo (ek command)

```bash
bash scripts/install_ubuntu.sh --service
```

Ye khud karta hai:
- Python, ffmpeg (watermark), tesseract + Hindi (OCR) install
- `.venv` (alag Python environment) + saari libraries
- `.env` file banata hai (sirf aap padh sakte ho)
- `--service` = systemd service, jisse bot 24×7 chalta hai aur crash/reboot ke baad khud restart hota hai

---

## Step 3 — `.env` bharo

```bash
nano .env
```

```ini
API_ID=1234567
API_HASH=0123456789abcdef0123456789abcdef
SESSION_STRING=            # Step 4 mein milega
BOT_TOKEN=123456789:AAH...your-token
ADMINS=987654321           # aapka user ID (Step 0)
```

Save: `Ctrl+O` → Enter → `Ctrl+X`

---

## Step 4 — User account login (SESSION_STRING banao)

```bash
.venv/bin/python -m tgclone login
```

1. Phone number daalo, country code ke saath (`+91...`)
2. Telegram app mein aaya **OTP** daalo
3. 2-step password ho to woh bhi daalo
4. Ek lambi string print hogi — yahi **SESSION_STRING** hai. Ise copy karo.

```bash
nano .env      # SESSION_STRING= ke aage paste karo, save karo
```

> User account isliye chahiye kyunki bots purani history aur protected channels nahi padh sakte.

---

## Step 5 — Pehli baar chala kar dekho

```bash
.venv/bin/python -m tgclone live -v
```

Logs mein `Reader: ... (user)` aur `Control panel ready (bot buttons)` dikhna chahiye.
Ab Telegram mein **apne bot ko `/start` bhejo** → control panel khulega.

Sab theek ho to `Ctrl+C` se band karo, aur Step 6 se 24×7 chalao.

---

## Step 6 — 24×7 chalao (service)

> **WSL (Windows) par ho?** WSL mein systemd default band hota hai. Ek baar on karo:
> ```bash
> printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf
> ```
> Phir **Windows PowerShell** mein `wsl --shutdown`, Ubuntu dobara kholo, aur
> `bash scripts/install_ubuntu.sh --service` dobara chalao.
> Bina systemd ke: `tmux new -s tgclone '.venv/bin/python -m tgclone live'` (detach: `Ctrl+B` phir `D`, wapas: `tmux attach -t tgclone`).
> ⚠️ WSL tabhi chalta hai jab PC on ho aur sleep mein na ho. Asli 24×7 ke liye VPS best hai.


```bash
sudo systemctl start tgclone        # start
sudo systemctl status tgclone       # chal raha hai? (q se bahar)
journalctl -u tgclone -f            # live logs (Ctrl+C se bahar)
sudo systemctl restart tgclone      # .env badalne ke baad
sudo systemctl stop tgclone         # band
```

Server reboot ke baad bhi bot khud start ho jaata hai.

---

## Step 7 — Bot use karo

1. **Live forward:** `/start` → ➕ **New forward** → source channel ka koi message bot ko **forward** karo → destination channel ka message forward karo → ✅ **Done**
2. **Purane posts copy:** 📋 Forwards → forward kholo → 🔁 **Clone history**
3. **Restricted post save:** bot ko link bhejo, jaise `https://t.me/c/1234567/45` ya range `https://t.me/channel/10-50`
4. **Thumbnail / watermark / filters:** ⚙️ Settings → 🖼 Thumbnail, aur 🧩 Plugins

> Destination channel mein **sending account admin hona chahiye** (default mein user account; "Send via bot" on ho to bot).

---

## Update karna (naya code aaye to)

```bash
cd ~/SRC-Villan-
git pull
.venv/bin/pip install -r requirements.txt -q
sudo systemctl restart tgclone
```

Aapka data (`.env`, `tgclone.config.json`, `data/`) update mein safe rehta hai.

## Backup

Ye 3 cheezein sambhal ke rakho: `.env`, `tgclone.config.json`, `data/tgclone.db`
(`data/tgclone.db` ki wajah se restart ke baad duplicate posts nahi jaate.)

---

## Problem aaye to

| Error / dikkat | Fix |
|---|---|
| `API_ID / API_HASH missing` | `.env` mein dono bharo (Step 3) |
| `Set SESSION_STRING and/or BOT_TOKEN` | Step 4 karo ya BOT_TOKEN daalo |
| Bot `/start` par **"Not authorized"** | `.env` mein `ADMINS=` aapka sahi user ID, phir restart |
| Bot jawab hi nahi deta | `journalctl -u tgclone -f` dekho. Token galat? Service chal rahi hai? |
| `Cannot find any entity` / chat resolve nahi hota | User account us channel ka member hona chahiye. Ek baar channel kholo, phir restart |
| `FloodWait` logs mein | Normal hai — Telegram limit; bot khud wait karke continue karta hai. ⚙️ Settings mein delay badhao |
| Plugins mein "⚠️ Not loaded" | `sudo apt install ffmpeg tesseract-ocr tesseract-ocr-hin`, phir restart |
| Login par `PhoneCodeInvalid` | OTP dobara, jaldi daalo (sirf app wala code, SMS nahi) |
| Session expire / logout ho gaya | Step 4 dobara karo, nayi SESSION_STRING `.env` mein |

Log file: `data/tgclone.log`
