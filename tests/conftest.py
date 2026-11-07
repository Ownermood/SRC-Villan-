import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="tgclone-test-")
os.environ.update(API_ID="1", API_HASH="x", TGCLONE_DATA=os.path.join(_tmp, "data"),
                  TGCLONE_CONFIG=os.path.join(_tmp, "cfg.json"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import asyncio  # noqa: E402
import types as _t  # noqa: E402

import pytest  # noqa: E402

OWNER = 1


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def hubenv(tmp_path, monkeypatch):
    import tgclone.hub as hubmod
    from test_hub import Bot, UserClient
    monkeypatch.setenv("OWNER_ID", str(OWNER))
    monkeypatch.setenv("SESSION_STRING", "")
    monkeypatch.setattr(hubmod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(hubmod, "USERS_DIR", tmp_path / "users")
    monkeypatch.setattr(hubmod, "CONFIG_FILE", tmp_path / "legacy.json")
    made = []

    def factory(session):
        c = UserClient(session)
        c.twofa = factory.twofa
        made.append(c)
        return c
    factory.twofa = False

    def new_hub():
        h = hubmod.Hub(live_mode=True, bot=Bot(), make_client=factory)
        run(h.start())
        return h
    return _t.SimpleNamespace(new_hub=new_hub, made=made, factory=factory, tmp=tmp_path)




@pytest.fixture(autouse=True)
def _fresh_protected_chats():
    from tgclone import message
    message.PROTECTED_CHATS.clear()
    yield
    message.PROTECTED_CHATS.clear()
