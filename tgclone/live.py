"""Live mode: new / album / edit / delete sync."""

import logging

from telethon import events

from tgclone.app import App
from tgclone.plugins import apply_plugins
from tgclone.message import FILE_TYPES, CloneMessage, media_key

log = logging.getLogger("tgclone")


def register(app: App) -> None:
    c = app.reader

    @c.on(events.NewMessage())
    async def on_new(event):
        if event.chat_id not in app.from_to or event.message.grouped_id:
            return  # albums are handled by on_album
        if event.message.action is not None:
            return
        tms = await app.process([event.message])
        await app.deliver(event.chat_id, tms, app.from_to[event.chat_id])

    @c.on(events.Album())
    async def on_album(event):
        if event.chat_id not in app.from_to:
            return
        tms = await app.process(event.messages)
        await app.deliver(event.chat_id, tms, app.from_to[event.chat_id])

    @c.on(events.MessageEdited())
    async def on_edit(event):
        chat, msg = event.chat_id, event.message
        if chat not in app.from_to or not app.cfg.live.edit_sync:
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
                                                    file=file))
            if media_changed:
                app.store.set_media_key(chat, msg.id, new_key)
        except Exception as e:
            log.warning("Edit sync failed: %s", e)
        finally:
            tm.cleanup()

    @c.on(events.MessageDeleted())
    async def on_delete(event):
        chat = event.chat_id
        if chat is None or chat not in app.from_to or not app.cfg.live.delete_sync:
            return  # chat_id is only known for channels/supergroups
        for sid in event.deleted_ids:
            for d, mid in app.store.get(chat, sid).items():
                await _safe(app.sender.delete_messages(d, mid))
            app.store.remove(chat, sid)


async def _safe(coro) -> None:
    try:
        await coro
    except Exception as e:
        log.warning("Sync action failed: %s", e)
