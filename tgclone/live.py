"""Live mode: new / album / edit / delete sync."""

import logging

from telethon import events

from tgclone.app import App
from tgclone.plugins import apply_plugins
from tgclone.message import CloneMessage

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
        try:
            for d, mid in mapped.items():
                await _safe(app.sender.edit_message(d, mid, tm.text))
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
