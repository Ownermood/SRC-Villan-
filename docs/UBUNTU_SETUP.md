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
tmux new -s tgclone 'bash scripts/run.sh'
```
`scripts/run.sh` crash hone par 10 second mein khud restart karta hai aur band hone ki wajah dikhata hai.
Restart ke baad adhoore clone / save **khud wahin se aage chalte hain**.
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
3. Telegram app mein aaya code **spaces ke saath** bhejo, jaise `1 2 3 4 5`
   (bina space ke Telegram code block kar sakta hai; bot aapka message turant delete kar deta hai)
4. 2-step password ho to bhejo — bot use turant delete kar deta hai
5. ✅ Logged in → **➕ New forward** → source channel ka koi message forward karo → destination ka message forward karo → ✅ Done

**Topics wala group clone:** source aur destination dono groups daalo. Bot destination mein saare topics
(same naam, icon) khud banata hai aur har post apne topic mein bhejta hai. Destination mein login account
**admin + "Manage topics"** permission ke saath hona chahiye.

Aur features: 🔁 Clone history, 📥 post link bhejo to save, 🖼 thumbnail, 💧 watermark, 🧹 filters.
**👤 Account → 🚪 Logout** se kabhi bhi logout (Telegram → Settings → Devices mein bhi dikhta hai).

---

## Plan (users)

- Naye user ko **24 ghante free** (pehli baar login ya "without login" shuru karne par)
- Uske baad owner premium days deta hai → **👤 My profile** mein plan, kitna time bacha, kab tak — sab dikhta hai
- Plan khatam → live forwarding ruk jaata hai (settings safe), 2 ghante pehle reminder aata hai

## Without login (users)

`/start` → **🤖 Use without login** → bot ko apne source aur destination mein **admin** banao (aap bhi dono ke admin ho) → ➕ New forward.
Sirf naye posts, aur sirf woh chats jahan forwarding allowed hai (protected nahi). Purane posts / save ke liye login.

## Owner: 🛡 Admin panel

| Option | Kaam |
|---|---|
| 🌍 Open / ✅ Approval / 🔒 Only me | Kaun use kar sakta hai. Approval mode mein har naye user ki request aapko buttons ke saath aati hai |
| 👥 Users | Har user: online/offline, forwards, jobs · ⛔ Ban · 🚪 Force logout |
| ⏳ Requests | Pending users ko approve / deny karo |
| ⚙️ Limits | Har user ke jobs, posts/save, sab users ke total jobs (owner par limit nahi) |
| 💎 Premium | ➕ Give premium: `user_id days` ya `@username days` · ➖ Remove · trial hours · plans on/off |
| 🚫 Blocked chats | Folder link `t.me/addlist/…`, `@username`, `t.me/c/…`, invite `t.me/+…`, `-100…` id ya forwarded message — ye chats koi clone nahi kar sakta |
| 🖼 Start photo | Photo bhejo ya `https://….jpg` link — /start par bada dikhega |
| ⭐ Premium emoji | Animated emoji on/off. **Telegram ka niyam:** bot ke owner (jis account se @BotFather mein bot banaya) ke paas **Telegram Premium** ho tabhi dikhte hain, warna normal emoji |

Commands (sirf owner): `/admin` · `/addpremium 5021 30` · `/rmpremium 5021` · `/block t.me/addlist/xyz` · `/unblock -100123`

---

## Update (naya code aaye to)

```bash
cd ~/SRC-Villan-
git pull
.venv/bin/pip install -r requirements.txt -q
sudo systemctl restart tgclone        # ya: tmux kill-session -t tgclone; tmux new -s tgclone 'bash scripts/run.sh'
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
