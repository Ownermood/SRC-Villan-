"""Live mode: new / album / edit / delete sync."""

import logging

from telethon import events, utils
from telethon.tl import types

from tgclone.app import App
from tgclone.plugins import apply_plugins
from tgclone.message import FILE_TYPES, CloneMessage, media_key
from tgclone.topics import topic_of

log = logging.getLogger("tgclone")


def register(app: App) -> list:
    c = app.reader

    @c.on(events.NewMessage())
    async def on_new(event):
        if app.paused or event.chat_id not in app.from_to or event.message.grouped_id:
            return  # albums are handled by on_album
        act = event.message.action
        if act is not None:
            await _topic_action(app, event.chat_id, event.message, act)
            return
        tms = await app.process([event.message])
        await app.deliver(event.chat_id, tms, app.from_to[event.chat_id])

    @c.on(events.Album())
    async def on_album(event):
        if app.paused or event.chat_id not in app.from_to:
            return
        tms = await app.process(event.messages)
        await app.deliver(event.chat_id, tms, app.from_to[event.chat_id])

    @c.on(events.MessageEdited())
    async def on_edit(event):
        chat, msg = event.chat_id, event.message
        if app.paused or chat not in app.from_to or not app.cfg.live.edit_sync:
            return
        if not msg.edit_date:
            return  # reactions / view counters also fire edit updates
        mapped = app.store.get(chat, msg.id)
        if not mapped:
            return  # never cloned; do not send duplicates
        if app.cfg.live.delete_on_edit and msg.raw_text == app.cfg.live.delete_on_edit:
            for d, mid in mapped.items():
                await _safe(app.sender.delete_messages(d, mid))
            await _safe(msg.delete())
            app.store.remove(chat, msg.id)
            return
        tm = await apply_plugins(app.plugins, CloneMessage(msg))
        if not tm:
            return
        new_key = media_key(msg)
        media_changed = (new_key is not None and tm.file_type in FILE_TYPES
                         and new_key != app.store.media_key(chat, msg.id))
        text, ents = (tm.text, None) if tm.text_changed else (msg.message or "", msg.entities)
        try:
            file = None
            if media_changed:
                if msg.noforwards or app.sender is not app.reader:
                    file = await tm.download(app.cfg.max_file_mb, app.cfg.max_disk_mb)
                else:
                    file = msg.media
            for d, mid in mapped.items():
                await _safe(app.sender.edit_message(d, mid, text, formatting_entities=ents,
                                                    file=file, parse_mode="html"))
            if media_changed:
                app.store.set_media_key(chat, msg.id, new_key)
        except Exception as e:
            log.warning("Edit sync failed: %s", e)
        finally:
            tm.cleanup()

    @c.on(events.MessageDeleted())
    async def on_delete(event):
        chat = event.chat_id
        if app.paused or chat is None or chat not in app.from_to or not app.cfg.live.delete_sync:
            return  # chat_id is only known for channels/supergroups
        for sid in event.deleted_ids:
            for d, mid in app.store.get(chat, sid).items():
                await _safe(app.sender.delete_messages(d, mid))
            app.store.remove(chat, sid)

    @c.on(events.Raw(types=[types.UpdatePinnedChannelMessages, types.UpdatePinnedMessages]))
    async def on_pin(update):
        if app.paused or not app.cfg.live.pin_sync:
            return
        peer = (types.PeerChannel(update.channel_id)
                if isinstance(update, types.UpdatePinnedChannelMessages) else update.peer)
        chat = utils.get_peer_id(peer)
        if chat in app.from_to:
            await app.sync_pin(chat, list(update.messages), bool(update.pinned))

    return [on_new, on_album, on_edit, on_delete, on_pin]  # to remove them later (shared bot)


async def _safe(coro) -> None:
    try:
        await coro
    except Exception as e:
        log.warning("Sync action failed: %s", e)


async def _topic_action(app: App, chat: int, msg, act) -> None:
    """Forum groups: mirror new topics and topic renames / close / reopen."""
    try:
        if isinstance(act, types.MessageActionTopicCreate):
            for d in app.from_to.get(chat, []):
                await app.topics.dest_topic(chat, msg.id, d)  # the service message id = topic id
        elif isinstance(act, types.MessageActionTopicEdit):
            topic = topic_of(msg) or getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)
            if topic:
                await app.topics.sync_edit(chat, topic, act)
    except Exception as e:
        log.warning("Topic sync failed: %s", e)
