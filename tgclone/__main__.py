"""CLI: python -m tgclone {live|bot|past|login} [-v]"""

import argparse
import asyncio
import logging

from tgclone import __version__


async def _run(args) -> None:
    from tgclone import bot, live
    from tgclone.app import App, run_forward
    from tgclone.config import Forward, norm_ref

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


async def _login() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    from tgclone.config import Secrets

    s = Secrets()
    s.check()
    async with TelegramClient(StringSession(), s.api_id, s.api_hash) as c:
        print("\nSESSION_STRING (keep it secret, put it in .env):\n")
        print(c.session.save())


def main() -> None:
    ap = argparse.ArgumentParser(prog="tgclone", description=__doc__)
    ap.add_argument("mode", choices=["live", "bot", "past", "login"],
                    help="live = real-time sync + control bot | bot = control/save bot only | "
                         "past = clone history | login = create SESSION_STRING")
    ap.add_argument("--source", help="past: one-off source chat")
    ap.add_argument("--dest", nargs="+", default=[], help="past: one-off destination(s)")
    ap.add_argument("--fresh", action="store_true", help="past: ignore saved progress, start over")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()
    if args.source and not args.dest:
        ap.error("--source needs --dest")

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
        pass


if __name__ == "__main__":
    main()
