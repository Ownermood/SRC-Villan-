"""Minimal Telethon stand-ins (no network)."""

import types as _t

from telethon import events
from telethon.errors import ChatForwardsRestrictedError

KINDS = ("poll", "sticker", "voice", "video_note", "gif", "audio", "video", "photo",
         "contact", "document", "geo", "dice", "game", "venue")


class Msg:
    def __init__(self, id, text="", media=None, kind=None, grouped_id=None, noforwards=False,
                 reply=None, sender=5, size=10, doc_id=None, edit_date=None, fwd_from=None,
                 topic=None, chat_id=-1):
        self.id, self.text, self.raw_text, self.message = id, text, text, text
        self.entities = None
        self.media, self.grouped_id, self.noforwards = media, grouped_id, noforwards
        self.reply_to_msg_id, self.sender_id, self.action = reply or topic, sender, None
        self.chat_id = chat_id
        from telethon.tl.types import MessageReplyHeader
        if topic:  # forum: a post in a topic "replies" to the topic root; real replies set top_id
            self.reply_to = MessageReplyHeader(forum_topic=True, reply_to_msg_id=reply or topic,
                                               reply_to_top_id=topic if reply else None)
        else:
            self.reply_to = MessageReplyHeader(reply_to_msg_id=reply) if reply else None
        self.web_preview, self.edit_date, self.fwd_from = None, edit_date, fwd_from
        for a in KINDS:
            setattr(self, a, None)
        if kind:
            setattr(self, kind, object())
        if kind in ("video", "document", "audio"):
            self.document = _t.SimpleNamespace(id=doc_id or id * 1000, attributes=[], thumbs=[1])
            if kind != "document":
                setattr(self, kind, self.document)
        elif kind == "photo":
            self.photo = _t.SimpleNamespace(id=doc_id or id * 1000)
        self.file = _t.SimpleNamespace(size=size)
        self.deleted = False
        self.is_reply = False

    async def get_reply_message(self):
        return None

    async def download_media(self, file, thumb=None, progress_callback=None):
        name = f"t{self.id}.jpg" if thumb is not None else f"m{self.id}.bin"
        path = file if file.endswith(".jpg") else file + name
        with open(path, "w") as f:
            f.write("x")
        if progress_callback:
            progress_callback(5, 10)
            progress_callback(10, 10)
        return path

    async def delete(self):
        self.deleted = True


class Client:
    def __init__(self, restrict=False):
        self.calls, self.restrict, self.n = [], restrict, 100
        self.handlers = []

    def _m(self):
        self.n += 1
        return _t.SimpleNamespace(id=self.n)

    async def send_message(self, dest, text, file=None, **kw):
        if self.restrict and file is not None:
            raise ChatForwardsRestrictedError(None)
        self.calls.append(("msg", dest, text, file, kw.get("reply_to")))
        return self._m()

    async def send_file(self, dest, f, caption=None, **kw):
        self.calls.append(("file", dest, f, caption, kw.get("reply_to"), kw.get("thumb")))
        if kw.get("progress_callback"):
            kw["progress_callback"](10, 10)
        return [self._m() for _ in f] if isinstance(f, list) else self._m()

    async def forward_messages(self, dest, msgs):
        if self.restrict:
            raise ChatForwardsRestrictedError(None)
        self.calls.append(("fwd", dest, len(msgs)))
        return [self._m() for _ in msgs]

    async def edit_message(self, dest, mid, text, **kw):
        self.calls.append(("edit", dest, mid, text, kw.get("file")))

    async def delete_messages(self, dest, mid):
        self.calls.append(("del", dest, mid))

    async def pin_message(self, dest, mid, notify=False):
        self.calls.append(("pin", dest, mid))
        self.n += 1
        return _t.SimpleNamespace(id=self.n)  # the "pinned a message" service message

    async def unpin_message(self, dest, mid):
        self.calls.append(("unpin", dest, mid))

    async def upload_file(self, path, progress_callback=None):
        self.calls.append(("upload", path))
        return f"H:{path}"

    async def download_media(self, msg, file=None):
        return msg.image_bytes

    async def get_peer_id(self, ref):
        return int(ref) if str(ref).lstrip("-").isdigit() else 42

    async def get_input_entity(self, ref):
        return ref

    async def get_entity(self, cid):
        return _t.SimpleNamespace(title=f"Chat{cid}")

    def add_event_handler(self, fn, ev=None):
        self.handlers.append((fn, ev))

    def on(self, ev):
        def deco(fn):
            self.handlers.append((fn, ev))
            return fn
        return deco

    async def __call__(self, req):
        return None


class Sent:
    def __init__(self, chat_id=1):
        self.chat_id, self.id, self.edits = chat_id, 999, []

    async def edit(self, text, **kw):
        self.edits.append((text, kw.get("buttons")))


class TextEvent:
    def __init__(self, text, sender=1, fwd_from=None, image=None, contact=None):
        self.raw_text, self.sender_id, self.is_private, self.chat_id = text, sender, True, sender
        self.message = Msg(1, text, fwd_from=fwd_from, kind="photo" if image else None)
        self.message.image_bytes = image
        self.message.contact = contact
        self.out = []

    async def get_sender(self):
        return _t.SimpleNamespace(id=self.sender_id, first_name=f"User{self.sender_id}", last_name=None)

    async def respond(self, text, **kw):
        self.out.append((text, kw.get("buttons"), kw.get("file")))
        return Sent()


class CallbackEvent(events.CallbackQuery.Event):
    def __init__(self, data, sender=1):  # no super(): offline fake
        self._data, self._sender_id, self._answered = data.encode(), sender, False
        self.out, self.edits, self.answers = [], [], []

    @property
    def data(self):
        return self._data

    @property
    def sender_id(self):
        return self._sender_id

    @property
    def chat_id(self):
        return self._sender_id

    async def get_sender(self):
        return _t.SimpleNamespace(id=self._sender_id, first_name=f"User{self._sender_id}", last_name=None)

    async def edit(self, text, **kw):
        self.edits.append((text, kw.get("buttons")))

    async def respond(self, text, **kw):
        self.out.append((text, kw.get("buttons")))
        return Sent()

    async def get_message(self):
        ev = self

        class _M:
            edits = ev.edits  # the callback's message: edits land on the event

            async def edit(self, text, **kw):
                ev.edits.append((text, kw.get("buttons")))
        return _M()

    async def answer(self, message=None, alert=False, **kw):
        self._answered = True
        self.answers.append((message, alert))


def make_app(client=None):
    from pathlib import Path
    from tgclone import app as appmod
    from tgclone.config import DATA_DIR, Config
    from tgclone.jobs import JobManager
    from tgclone.storage import Store

    a = appmod.App.__new__(appmod.App)
    a.cfg = Config()
    a.cfg.past.delay = a.cfg.save.delay = 0
    a.store = Store(Path(DATA_DIR) / f"t{next(_ids)}.db")
    a.plugins, a.admins, a.from_to, a.titles = [], {1}, {}, {}
    a.caps, a.hub, a.uid, a.home = None, None, 1, Path(DATA_DIR)
    a.paused = a.botmode = False
    a.cfg.past.turbo = False  # most tests check the post-by-post path; turbo has its own tests
    from tgclone.topics import Topics
    a.topics = Topics(a)
    c = client or Client()
    a.reader = a.sender = a.user = a.control = a.bot = c
    a.jobs = JobManager(3)
    a.save = lambda: None

    async def load_forwards():
        a.from_to = {}
    a.load_forwards = load_forwards
    return a


import itertools  # noqa: E402
_ids = itertools.count()
