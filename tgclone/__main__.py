"""CLI: python -m tgclone {setup|live|bot|past|login} [-v]"""

import argparse
import asyncio
import logging

from tgclone import __version__


async def _run(args) -> None:
    from tgclone import bot, live
    from tgclone.app import App, run_forward
    from tgclone.config import Forward, Secrets, norm_ref

    if args.mode in ("live", "bot") and Secrets().bot_token:
        from tgclone.hub import Hub  # multi-user bot: everyone logs in inside Telegram
        await Hub(live_mode=args.mode == "live").run()
        return

    app = App()
    await app.start(need_user=args.mode == "past")

    if args.mode == "past":
        if args.source:
            fwds = [Forward(source=norm_ref(args.source), dest=[norm_ref(d) for d in args.dest])]
        else:
            fwds = [f for f in app.cfg.forwards if f.enabled and f.source != "" and f.dest]
        if not fwds:
            raise SystemExit("No forwards. Add them in the config or use --source/--dest.")
        for f in fwds:
            try:
                await run_forward(app, f, fresh=args.fresh)
            except Exception as e:
                logging.getLogger("tgclone").error("Forward %s failed: %s", f.source, e)
        for c in app.clients:
            await c.disconnect()
        return

    if args.mode == "live":
        live.register(app)
    await bot.setup(app, live=args.mode == "live")
    await asyncio.gather(*(c.run_until_disconnected() for c in app.clients))


def _setup() -> None:
    """Owner's one-time setup: asks 4 values and writes .env (chmod 600)."""
    import os
    import re
    from pathlib import Path

    from cryptography.fernet import Fernet
    from dotenv import dotenv_values

    env_path = Path(".env")
    cur = dotenv_values(env_path) if env_path.exists() else {}

    def ask(key, prompt, check, hint):
        old = cur.get(key) or ""
        while True:
            shown = f" [{old[:6]}…]" if old and key != "API_ID" else (f" [{old}]" if old else "")
            val = input(f"{prompt}{shown}: ").strip() or old
            if re.fullmatch(check, val or ""):
                return val
            print(f"  ✗ {hint}")

    print("\n=== tgclone setup ===  (Enter keeps the value in [brackets])\n")
    print("1) https://my.telegram.org → API development tools")
    api_id = ask("API_ID", "   API_ID", r"\d{4,12}", "numbers only, e.g. 1234567")
    api_hash = ask("API_HASH", "   API_HASH", r"[0-9a-fA-F]{32}", "32 characters, e.g. 0123abcd…")
    print("2) Telegram → @BotFather → /newbot")
    token = ask("BOT_TOKEN", "   BOT_TOKEN", r"\d{5,12}:[\w-]{30,}", "looks like 123456789:AAH…")
    print("3) Telegram → @userinfobot → your Id")
    owner = ask("OWNER_ID", "   OWNER_ID", r"\d{4,15}", "numbers only")
    cur.update(API_ID=api_id, API_HASH=api_hash, BOT_TOKEN=token, OWNER_ID=owner)
    cur.setdefault("SECRET_KEY", "")
    if not cur["SECRET_KEY"] and not Path(os.getenv("TGCLONE_DATA", "data"), "secret.key").exists():
        cur["SECRET_KEY"] = Fernet.generate_key().decode()
    lines = [f"{k}={v}" for k, v in cur.items() if v is not None]
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(env_path, 0o600)
    print("\n✅ Saved .env  — keep it private (SECRET_KEY protects users' logins, back it up).")
    print("Next:  .venv/bin/python -m tgclone live   → open your bot → /start → 🔐 Login\n")


async def _login() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    from tgclone.config import Secrets

    s = Secrets()
    s.check()
    async with TelegramClient(StringSession(), s.api_id, s.api_hash) as c:
        print("\nSESSION_STRING (keep it secret, put it in .env):\n")
        print(c.session.save())


REQUIRED = {"telethon": "telethon", "pydantic": "pydantic", "dotenv": "python-dotenv",
            "yaml": "PyYAML", "PIL": "Pillow", "cryptography": "cryptography"}


def _check_deps() -> None:
    import importlib.util
    missing = [pkg for mod, pkg in REQUIRED.items() if importlib.util.find_spec(mod) is None]
    if missing:
        raise SystemExit(f"Missing Python packages: {', '.join(missing)}\n"
                         f"Run:  .venv/bin/pip install -r requirements.txt")


def main() -> None:
    _check_deps()
    ap = argparse.ArgumentParser(prog="tgclone", description=__doc__)
    ap.add_argument("mode", choices=["setup", "live", "bot", "past", "login"],
                    help="setup = write .env (owner, once) | live = multi-user bot with live sync | "
                         "bot = bot without live sync | past = clone history (CLI) | "
                         "login = create SESSION_STRING (CLI, optional)")
    ap.add_argument("--source", help="past: one-off source chat")
    ap.add_argument("--dest", nargs="+", default=[], help="past: one-off destination(s)")
    ap.add_argument("--fresh", action="store_true", help="past: ignore saved progress, start over")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()
    if args.source and not args.dest:
        ap.error("--source needs --dest")
    if args.mode == "setup":
        try:
            return _setup()
        except (KeyboardInterrupt, EOFError):
            print("\ncancelled")
            return

    from logging.handlers import RotatingFileHandler

    from tgclone.config import DATA_DIR

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    file_h = RotatingFileHandler(DATA_DIR / "tgclone.log", maxBytes=5_000_000, backupCount=3,
                                 encoding="utf8")
    file_h.setFormatter(logging.Formatter(fmt))
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format=fmt,
                        handlers=[logging.StreamHandler(), file_h])
    logging.getLogger("tgclone").setLevel(logging.INFO)
    logging.getLogger("telethon").setLevel(logging.INFO if args.verbose else logging.WARNING)
    try:
        asyncio.run(_login() if args.mode == "login" else _run(args))
    except KeyboardInterrupt:
        logging.getLogger("tgclone").info("Stopped by Ctrl+C - running jobs resume on next start")
    except SystemExit as e:
        if e.code not in (0, None):
            logging.getLogger("tgclone").error("Stopped: %s", e.code)
        raise
    except BaseException:
        # keep the reason in data/tgclone.log even when the terminal window closes
        logging.getLogger("tgclone").exception("tgclone crashed")
        raise


if __name__ == "__main__":
    main()
