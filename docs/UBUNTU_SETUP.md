# tgclone — setup (owner) aur use (sab users)

**Users ko terminal ki zarurat nahi.** Woh sirf bot kholte hain, `/start` → 🔐 Login karte hain, aur sab kuch buttons se.
Terminal sirf **owner (aap)** ko **ek baar** chahiye, bot chalane ke liye.

---

## Owner: 5 minute setup (Ubuntu 22.04 / 24.04 / WSL / VPS)

### Step 0 — 4 cheezein ready rakho

| Cheez | Kahan se |
|---|---|
| **API_ID**, **API_HASH** | https://my.telegram.org → *API development tools* → koi bhi app name bharo |
| **BOT_TOKEN** | Telegram → **@BotFather** → `/newbot` |
| **Aapka user ID** | Telegram → **@userinfobot** → `/start` → `Id` |

### Step 1 — Download + install

```bash
sudo apt update && sudo apt install -y git
git clone -b ccr-1f6db1cf-flau3n https://github.com/Ownermood/SRC-Villan-.git
cd SRC-Villan-
bash scripts/install_ubuntu.sh --service
```

> WSL par "systemd not running" aaye to koi baat nahi — Step 3 dekho.

### Step 2 — Setup (4 sawaal, `.env` khud ban jaata hai)

```bash
.venv/bin/python -m tgclone setup
```

Ye API_ID, API_HASH, BOT_TOKEN aur OWNER_ID poochta hai, aur ek **SECRET_KEY** khud banata hai.
SECRET_KEY users ke logins ko encrypt karta hai — `.env` ka **backup** rakho.

### Step 3 — Bot chalao

**VPS / systemd wala Ubuntu (24×7, auto-restart):**
```bash
sudo systemctl start tgclone
sudo systemctl status tgclone      # chal raha hai?
journalctl -u tgclone -f           # live logs
```

**WSL / bina systemd:**
```bash
sudo apt install -y tmux
tmux new -s tgclone '.venv/bin/python -m tgclone live'
```
Bahar nikalna: `Ctrl+B` phir `D`. Wapas aana: `tmux attach -t tgclone`.

> WSL par systemd on karna ho: `printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf`,
> phir Windows PowerShell mein `wsl --shutdown`, Ubuntu dobara kholo, aur `bash scripts/install_ubuntu.sh --service`.
> ⚠️ WSL tabhi chalta hai jab PC on ho. Asli 24×7 ke liye VPS lo.

### Step 4 — Done! Telegram kholo

Apne bot ko `/start` bhejo → 🔐 **Login with Telegram**. Owner hone ki wajah se aapko **🛡 Admin** button bhi dikhega.

---

## Users: kaise use karein (sab Telegram mein)

1. Bot kholo → `/start` → **🔐 Login with Telegram**
2. **📱 Share my number** button dabao
3. Telegram app mein aaya code **keypad buttons** se daalo → ✅ Done
   (Code ko message mein mat bhejna — Telegram aise code block kar deta hai)
4. 2-step password ho to bhejo — bot use turant delete kar deta hai
5. ✅ Logged in → **➕ New forward** → source channel ka koi message forward karo → destination ka message forward karo → ✅ Done

Aur features: 🔁 Clone history, 📥 post link bhejo to save, 🖼 thumbnail, 💧 watermark, 🧹 filters.
**👤 Account → 🚪 Logout** se kabhi bhi logout (Telegram → Settings → Devices mein bhi dikhta hai).

---

## Owner: 🛡 Admin panel

| Option | Kaam |
|---|---|
| 🌍 Open / ✅ Approval / 🔒 Only me | Kaun use kar sakta hai. Approval mode mein har naye user ki request aapko buttons ke saath aati hai |
| 👥 Users | Har user: online/offline, forwards, jobs · ⛔ Ban · 🚪 Force logout |
| ⏳ Requests | Pending users ko approve / deny karo |
| ➖ ➕ jobs/user, posts/save | Har user ki limit (owner par limit nahi) |

---

## Update (naya code aaye to)

```bash
cd ~/SRC-Villan-
git pull
.venv/bin/pip install -r requirements.txt -q
sudo systemctl restart tgclone        # ya tmux mein Ctrl+C karke dobara chalao
```

## Backup

`.env` (SECRET_KEY ke saath), `data/accounts.db`, `data/users/`, `data/bot.session`.
SECRET_KEY kho gaya to saved logins nahi khulenge — users ko dobara login karna padega.

---

## Problem aaye to

| Dikkat | Fix |
|---|---|
| `API_ID / API_HASH missing` / `BOT_TOKEN missing` | `.venv/bin/python -m tgclone setup` |
| Bot jawab nahi deta | `journalctl -u tgclone -f` (ya tmux window) dekho; token sahi hai? |
| User: "code expired / wrong" | Keypad par 🔄 New code. Code sirf buttons se daalo |
| User: "Wait Ns before a new code" | Telegram ki limit — utna wait karo |
| "Your login was ended from Telegram" | User ne Devices se session hataya — /login dobara |
| Chat resolve nahi hota | Us account ko source channel ka member hona chahiye |
| Destination mein post nahi jaata | Login account (ya "Send via bot" on ho to bot) destination ka **admin** ho |
| `FloodWait` logs mein | Normal — bot khud wait karta hai; ⚙️ Settings mein delay badhao |
| Plugin "⚠️ Not loaded" | `sudo apt install ffmpeg tesseract-ocr tesseract-ocr-hin` |

Logs: `data/tgclone.log`
