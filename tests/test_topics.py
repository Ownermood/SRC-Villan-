"""Forum groups: topic-by-topic cloning."""

import asyncio
import types as _t

from telethon.errors import ChatAdminRequiredError, PremiumAccountRequiredError
from telethon.tl import types

from fakes import Client, Msg, make_app
from tgclone import app as appmod, live
from tgclone.config import Forward
from tgclone.message import CloneMessage, send
from tgclone.topics import reply_target, topic_of

run = asyncio.run
SRC, DEST = -1, 7


def topic(id, title, closed=False, emoji=None, color=0x6FB9F0):
    return types.ForumTopic(id=id, date=None, peer=types.PeerChannel(1), title=title, icon_color=color,
                            top_message=id, read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0,
                            unread_mentions_count=0, unread_reactions_count=0, unread_poll_votes_count=0,
                            from_id=types.PeerUser(1), notify_settings=types.PeerNotifySettings(),
                            closed=closed, icon_emoji_id=emoji)


class ForumClient(Client):
    def __init__(self, topics, dest_forum=True, refuse_emoji=False, toggle_ok=True):
        super().__init__()
        self.topics, self.refuse_emoji, self.toggle_ok = topics, refuse_emoji, toggle_ok
        self.forums = {SRC: True, DEST: dest_forum, 8: dest_forum}
        self.requests, self.next_topic = [], 500

    async def get_entity(self, cid):
        return _t.SimpleNamespace(title=f"Chat{cid}", forum=self.forums.get(cid, False))

    def created(self):
        return [r for r in self.requests if type(r).__name__ == "CreateForumTopicRequest"]

    async def __call__(self, req):
        name = type(req).__name__
        self.requests.append(req)
        if name == "GetForumTopicsRequest":
            return _t.SimpleNamespace(topics=list(self.topics), count=len(self.topics))
        if name == "GetForumTopicsByIDRequest":
            return _t.SimpleNamespace(topics=[t for t in self.topics if t.id in req.topics])
        if name == "CreateForumTopicRequest":
            await asyncio.sleep(0)
            if req.icon_emoji_id and self.refuse_emoji:
                raise PremiumAccountRequiredError(None)
            self.next_topic += 1
            svc = _t.SimpleNamespace(id=self.next_topic, action=types.MessageActionTopicCreate(
                title=req.title, icon_color=req.icon_color or 0))
            return _t.SimpleNamespace(updates=[_t.SimpleNamespace(message=svc)])
        if name == "ToggleForumRequest":
            if not self.toggle_ok:
                raise ChatAdminRequiredError(None)
            self.forums[DEST] = True
            return None
        if name == "ForwardMessagesRequest":
            self.n += len(req.id)
            return _t.SimpleNamespace(updates=[_t.SimpleNamespace(message=types.Message(
                id=self.n - i, peer_id=types.PeerChannel(7), date=None, message="")) for i in range(len(req.id))])
        return None


TOPICS = [topic(1, "General"), topic(5, "🎬 Movies", emoji=111), topic(9, "Songs", closed=True)]


def forum_msgs():
    return [Msg(10, "general post", chat_id=SRC),
            Msg(11, "movie 1", topic=5, chat_id=SRC),
            Msg(12, "reply to movie 1", topic=5, reply=11, chat_id=SRC),
            Msg(13, "song", topic=9, chat_id=SRC),
            Msg(14, "", media="M", kind="photo", grouped_id=77, topic=9, chat_id=SRC),
            Msg(15, "", media="M", kind="photo", grouped_id=77, topic=9, chat_id=SRC)]


def wire(a, msgs):
    c = a.reader

    async def it(src, reverse, min_id, max_id):
        for m in msgs:
            if m.id > min_id and (not max_id or m.id < max_id):
                yield m

    async def gm(chat, ids=None, limit=None):
        return [msgs[-1]]

    async def resolve(ref):
        return int(ref)
    c.iter_messages, c.get_messages, a.resolve = it, gm, resolve
    return c


def test_helpers():
    assert topic_of(Msg(1, "x", topic=5)) == 5 and reply_target(Msg(1, "x", topic=5)) is None
    assert topic_of(Msg(1, "x", topic=5, reply=3)) == 5 and reply_target(Msg(1, "x", topic=5, reply=3)) == 3
    assert topic_of(Msg(1, "x", reply=3)) is None and reply_target(Msg(1, "x", reply=3)) == 3
    assert topic_of(Msg(1, "x")) is None and reply_target(Msg(1, "x")) is None


def test_clone_forum_topic_by_topic():
    a = make_app(ForumClient(TOPICS))
    c = wire(a, forum_msgs())
    run(appmod.run_forward(a, Forward(source=SRC, dest=[DEST])))
    created = c.created()
    assert [r.title for r in created] == ["🎬 Movies", "Songs"], "made in source order, General skipped"
    assert created[0].icon_emoji_id == 111 and created[0].icon_color == TOPICS[1].icon_color
    edits = [r for r in c.requests if type(r).__name__ == "EditForumTopicRequest"]
    assert len(edits) == 1 and edits[0].closed is True, "closed topic stays closed"
    movies, songs = a.store.get_topic(SRC, 5, DEST), a.store.get_topic(SRC, 9, DEST)
    sends = {x[2] if x[0] == "msg" else "album": x for x in c.calls}
    assert sends["general post"][4] is None, "General -> no topic"
    assert sends["movie 1"][4] == movies
    reply_id = a.store.get_one(SRC, 11, DEST)
    assert sends["reply to movie 1"][4] == reply_id, "real reply stays a reply (inside the topic)"
    assert sends["song"][4] == songs and sends["album"][4] == songs
    # resume / fresh: topics are never created twice
    run(appmod.run_forward(a, Forward(source=SRC, dest=[DEST])))
    run(appmod.run_forward(a, Forward(source=SRC, dest=[DEST]), fresh=True))
    assert len(c.created()) == 2


def test_premium_emoji_refused_retries_without():
    a = make_app(ForumClient(TOPICS, refuse_emoji=True))
    run(a.topics.dest_topic(SRC, 5, DEST))
    created = a.reader.created()
    assert [r.icon_emoji_id for r in created] == [111, None] and a.store.get_topic(SRC, 5, DEST)


def test_destination_without_topics():
    a = make_app(ForumClient(TOPICS, dest_forum=False))
    assert run(a.topics.dest_topic(SRC, 5, DEST)) and a.reader.forums[DEST], "topics turned on"
    a = make_app(ForumClient(TOPICS, dest_forum=False, toggle_ok=False))
    c = wire(a, forum_msgs())
    run(appmod.run_forward(a, Forward(source=SRC, dest=[DEST])))
    assert not c.created() and all(x[4] in (None, a.store.get_one(SRC, 11, DEST)) for x in c.calls)
    assert len(c.calls) == 5, "still delivered, just without topics"


def test_topics_disabled():
    a = make_app(ForumClient(TOPICS))
    a.cfg.topics.enabled = False
    c = wire(a, forum_msgs())
    run(appmod.run_forward(a, Forward(source=SRC, dest=[DEST])))
    assert not c.created()


def test_concurrent_requests_create_one_topic():
    a = make_app(ForumClient(TOPICS))

    async def both():
        return await asyncio.gather(*(a.topics.dest_topic(SRC, 5, DEST) for _ in range(5)))
    ids = run(both())
    assert len(set(ids)) == 1 and len(a.reader.created()) == 1


def test_live_new_topic_rename_and_post():
    a = make_app(ForumClient(TOPICS + [topic(20, "New topic")]))
    a.from_to = {SRC: [DEST, 8]}
    live.register(a)
    h = {type(ev).__name__: fn for fn, ev in a.reader.handlers}

    class E:
        chat_id = SRC
    e = E()
    e.message = Msg(20, "", chat_id=SRC)
    e.message.action = types.MessageActionTopicCreate(title="New topic", icon_color=0)
    run(h["NewMessage"](e))
    assert len(a.reader.created()) == 2 and a.store.get_topic(SRC, 20, 8)
    e.message = Msg(21, "", topic=20, chat_id=SRC)
    e.message.action = types.MessageActionTopicEdit(title="Renamed", closed=None)
    run(h["NewMessage"](e))
    edits = [r for r in a.reader.requests if type(r).__name__ == "EditForumTopicRequest"]
    assert {r.title for r in edits} == {"Renamed"} and len(edits) == 2
    e.message = Msg(22, "hello", topic=20, chat_id=SRC)
    run(h["NewMessage"](e))
    assert {x[4] for x in a.reader.calls if x[2] == "hello"} == {a.store.get_topic(SRC, 20, DEST),
                                                                 a.store.get_topic(SRC, 20, 8)}


def test_forward_mode_into_topic():
    c = ForumClient(TOPICS)
    from tgclone.config import Config
    sent = run(send(c, DEST, [CloneMessage(Msg(11, "x", topic=5, chat_id=SRC))],
                    Config(forward_mode=True), topic=501))
    req = [r for r in c.requests if type(r).__name__ == "ForwardMessagesRequest"][0]
    assert req.top_msg_id == 501 and req.id == [11] and len(sent) == 1
