import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update, User
from telegram.ext import Application, ExtBot, MessageHandler, filters
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
import business_powerpack as plugin
from business_powerpack.runtime import CliRunner
from business_powerpack.state import route_key


class Bot(ExtBot):
    async def initialize(self):
        self._bot_user = User(555, "Fixture", True)
        self._initialized = True

    async def shutdown(self):
        pass


def connection(enabled=True, owner=11, cid="conn", reply=True):
    return {"id": cid, "user": {"id": owner, "is_bot": False, "first_name": "Owner"},
            "user_chat_id": owner, "date": 1700000000, "is_enabled": enabled,
            "can_reply": reply, "rights": {"can_reply": reply}}


def update(bot, mid=1, sender=11, chat=22, cid="conn", text="Hermes сделай", voice=False,
           edited=False, echo=False, age=0):
    m = {"message_id": mid, "date": int((datetime.now(timezone.utc) - timedelta(seconds=age)).timestamp()),
         "chat": {"id": chat, "type": "private"}, "from": {"id": sender, "is_bot": sender == 555,
         "first_name": "User"}, "business_connection_id": cid}
    if voice:
        m["voice"] = {"file_id": "voice-id", "file_unique_id": "unique", "duration": 4, "file_size": 10}
    else:
        m["text"] = text
    if echo:
        m["sender_business_bot"] = {"id": 555, "is_bot": True, "first_name": "Bot"}
    return Update.de_json({"update_id": mid, "edited_business_message" if edited else "business_message": m}, bot)


@pytest.fixture
async def wired(tmp_path, monkeypatch, request):
    from telegram import BusinessConnection
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    chats = '' if getattr(request, 'param', 'local') == 'telegram' else '      allowed_chats: [22, 44]\n'
    (tmp_path / "config.yaml").write_text('plugins:\n  entries:\n    business-powerpack:\n      owner_ids: [11, 33]\n' + chats)
    import shutil
    installed = tmp_path / 'plugins' / 'business-powerpack'
    installed.mkdir(parents=True)
    root = Path(__file__).resolve().parents[1]
    for name in ('__init__.py', 'config.py', 'runtime.py', 'scope.py', 'service.py', 'state.py', 'story.py', 'story_media.py', 'reply_media.py', 'stt_worker.py', 'plugin.yaml'):
        shutil.copy2(root / name, installed / name)
    manager = PluginManager()
    manifest = PluginManifest(name="business-powerpack", source="user", path=str(installed))
    loaded = manager._load_directory_module(manifest)
    ctx = PluginContext(manifest, manager)
    loaded.register(ctx)  # Native directory loader + real public registration.
    factory, name = manager._platform_handler_factories["telegram"][0]
    bot = Bot("123:fixture")
    fetched = AsyncMock(return_value=BusinessConnection.de_json(connection(), bot))
    sent = AsyncMock()
    monkeypatch.setattr(Bot, "get_business_connection", fetched)
    monkeypatch.setattr(Bot, "send_message", sent)
    app = Application.builder().bot(bot).build()
    await app.initialize()
    factory(app, SimpleNamespace())
    story_factory, _ = manager._platform_handler_factories["telegram"][1]
    story_factory(app, SimpleNamespace())
    service = app.bot_data["business-powerpack"][str(tmp_path)]
    service.runner = SimpleNamespace(run=AsyncMock(return_value="final"))
    core = AsyncMock()
    app.add_handler(MessageHandler(filters.ALL, core), group=0)
    await app.start()
    yield SimpleNamespace(app=app, bot=bot, service=service, fetched=fetched, sent=sent,
                          core=core, home=tmp_path)
    await app.stop()
    await app.shutdown()


async def dispatch(w, u):
    await w.app.process_update(u)
    if w.service.tasks:
        await asyncio.gather(*list(w.service.tasks), return_exceptions=True)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_owner_story_command_without_media_is_handled_once(wired):
    w = wired
    now = int(datetime.now(timezone.utc).timestamp())
    ordinary = Update.de_json({'update_id': 900, 'message': {
        'message_id': 900, 'date': now, 'chat': {'id': 11, 'type': 'private'},
        'from': {'id': 11, 'is_bot': False, 'first_name': 'Owner'}, 'text': '/story',
        'entities': [{'type': 'bot_command', 'offset': 0, 'length': 6}]}}, w.bot)
    await dispatch(w, ordinary)
    w.sent.assert_awaited_once()
    assert 'фото или видео' in w.sent.call_args.kwargs['text']
    w.core.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_entrypoint_cli_tool_and_exact_send(wired):
    w = wired
    from dataclasses import replace
    config = replace(w.service.config, cli=str(Path(__file__).with_name("cli_fixture.py")))
    w.service.runner = CliRunner(w.home, config)
    await dispatch(w, update(w.bot, text="Гермес: локальное действие"))
    assert (w.home / "fixture-tool.txt").read_text() == "tool ran"
    call = json.loads((w.home / "fixture-call.json").read_text())
    route = route_key(w.home, 555, "conn", 22)
    assert call["query"].endswith("\n\nлокальное действие")
    assert "final text will be visible" in call["query"]
    assert call["argv"] == ["chat", "--query-file", "-", "--oneshot", "--format", "stream-json",
                            "--source", "tool", "--continue", "business-powerpack-" + route,
                            "--create-if-missing"]
    assert not any("TOKEN" in key or "KEY" in key for key in call["env_keys"])
    w.sent.assert_awaited_once()
    assert w.sent.call_args.kwargs["business_connection_id"] == "conn"
    assert w.sent.call_args.kwargs["chat_id"] == 22
    assert w.sent.call_args.kwargs["text"] == "Готово: инструмент выполнен."
    w.core.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [dict(sender=99), dict(sender=33), dict(chat=99), dict(cid="unknown"),
    dict(sender=555), dict(echo=True), dict(edited=True), dict(text="привет"),
    dict(text="попроси Hermes сделать"), dict(text="HermesXYZ"), dict(age=86401),
    dict(voice=True, sender=99)])
async def test_denied(wired, kwargs):
    await dispatch(wired, update(wired.bot, **kwargs))
    wired.service.runner.run.assert_not_awaited()
    wired.sent.assert_not_awaited()
    wired.core.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", [connection(owner=99), connection(enabled=False), connection(reply=False)])
async def test_connection_denied(wired, conn):
    from telegram import BusinessConnection
    wired.fetched.return_value = BusinessConnection.de_json(conn, wired.bot)
    await dispatch(wired, update(wired.bot))
    wired.service.runner.run.assert_not_awaited()
    wired.sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_transcription_without_agent(wired, monkeypatch):
    w = wired
    async def download(custom_path, **kw):
        custom_path.write_bytes(b"fake-ogg")
    file = SimpleNamespace(file_id="voice-id", file_size=10, download_to_drive=download)
    get_file = AsyncMock(return_value=file)
    monkeypatch.setattr(Bot, "get_file", get_file)
    async def transcribe(path):
        assert path.read_bytes() == b"fake-ogg"
        return "слова клиента"
    w.service.stt = transcribe
    await dispatch(w, update(w.bot, sender=22, voice=True))
    w.service.runner.run.assert_not_awaited()
    get_file.assert_awaited_once_with("voice-id", read_timeout=15, connect_timeout=10)
    assert w.sent.call_args.kwargs["text"] == "Расшифровка:\nслова клиента"
    assert w.sent.call_args.kwargs["business_connection_id"] == "conn"
    assert w.sent.call_args.kwargs["chat_id"] == 22
    assert not list((w.home / "business-powerpack" / "media").iterdir())


@pytest.mark.asyncio
async def test_receipts_restart_and_ambiguous_send(wired):
    from business_powerpack.state import State
    w = wired
    w.sent.side_effect = RuntimeError("secret token /private/file")
    await dispatch(w, update(w.bot))
    w.service.state = State(w.home / "business-powerpack")
    await dispatch(w, update(w.bot))
    w.service.runner.run.assert_awaited_once()
    w.sent.assert_awaited_once()
    with w.service.state.db() as db:
        assert db.execute("SELECT status FROM receipts").fetchone()[0] == "failed-ambiguous"
    assert (w.home / "business-powerpack" / "receipts.sqlite3").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_revoke_during_agent(wired):
    from telegram import BusinessConnection
    async def run(*args):
        wired.fetched.return_value = BusinessConnection.de_json(connection(enabled=False), wired.bot)
        return "secret result"
    wired.service.runner.run.side_effect = run
    await dispatch(wired, update(wired.bot))
    wired.sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_chunk_revalidated(wired):
    from telegram import BusinessConnection
    wired.service.runner.run.return_value = "a" * 4000
    async def send(**kw):
        wired.fetched.return_value = BusinessConnection.de_json(connection(reply=False), wired.bot)
    wired.sent.side_effect = send
    await dispatch(wired, update(wired.bot))
    assert wired.sent.await_count == 1
    assert len(wired.sent.call_args.kwargs["text"]) == 1500


@pytest.mark.asyncio
async def test_lifecycle_cancels_inflight(wired):
    w = wired
    started = asyncio.Event()
    cancelled = asyncio.Event()
    async def run(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    w.service.runner.run.side_effect = run
    await w.app.process_update(update(w.bot))
    await started.wait()
    await w.app.process_update(Update.de_json({"update_id": 9, "business_connection": connection(enabled=False)}, w.bot))
    await asyncio.wait_for(cancelled.wait(), 2)
    await asyncio.gather(*list(w.service.tasks), return_exceptions=True)
    w.sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_and_capacity(wired):
    w = wired
    started, release = asyncio.Event(), asyncio.Event()
    async def run(*args):
        started.set()
        await release.wait()
        return "ok"
    w.service.runner.run.side_effect = run
    await w.app.process_update(update(w.bot))
    await started.wait()
    await w.app.process_update(update(w.bot, mid=2))  # same route refused by flock
    await asyncio.sleep(.02)
    assert w.service.runner.run.await_count == 1
    await w.app.process_update(update(w.bot, mid=3, chat=44))
    await asyncio.sleep(.02)
    await w.app.process_update(update(w.bot, mid=4, chat=44))  # capacity refusal
    assert len(w.service.tasks) <= 2
    release.set()
    await asyncio.gather(*list(w.service.tasks))
    assert w.service.runner.run.await_count == 2


def test_route_isolation(tmp_path):
    baseline = route_key(tmp_path, 1, "a", 2)
    assert len({baseline, route_key(tmp_path / "other", 1, "a", 2), route_key(tmp_path, 2, "a", 2),
                route_key(tmp_path, 1, "b", 2), route_key(tmp_path, 1, "a", 3)}) == 5


@pytest.mark.asyncio
async def test_empty_allowlists_fail_closed(wired):
    from dataclasses import replace
    for field in ("owner_ids", "allowed_chats"):
        wired.service.config = replace(wired.service.config, **{field: frozenset()})
        await dispatch(wired, update(wired.bot))
    wired.fetched.assert_not_awaited()
    wired.service.runner.run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('wired', ['telegram'], indirect=True)
async def test_telegram_selection_needs_no_local_chat_list(wired):
    assert wired.service.config.allowed_chats is None
    # These native Business updates arrive from Telegram for two distinct peers.
    for mid, chat in ((1, 777), (2, 888)):
        await dispatch(wired, update(wired.bot, mid=mid, chat=chat))
    assert wired.service.runner.run.await_count == 2
    assert [c.kwargs['chat_id'] for c in wired.sent.await_args_list] == [777, 888]
    assert all(c.kwargs['business_connection_id'] == 'conn' for c in wired.sent.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('wired', ['telegram'], indirect=True)
async def test_telegram_selection_does_not_admit_foreign_owner_or_customer(wired):
    from telegram import BusinessConnection
    await dispatch(wired, update(wired.bot, sender=777, chat=777))
    wired.fetched.return_value = BusinessConnection.de_json(connection(owner=99), wired.bot)
    await dispatch(wired, update(wired.bot, mid=2, sender=11, chat=777))
    wired.service.runner.run.assert_not_awaited()
    wired.sent.assert_not_awaited()
    wired.core.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('wired', ['telegram'], indirect=True)
async def test_ordinary_dm_remains_ordinary_even_with_business_id(wired):
    business = update(wired.bot, chat=777)
    native = Update(update_id=2, message=business.business_message)
    await dispatch(wired, native)
    wired.core.assert_awaited_once()
    wired.service.runner.run.assert_not_awaited()
    wired.sent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('wired', ['telegram'], indirect=True)
async def test_telegram_send_denial_has_no_dm_fallback_or_retry(wired):
    from telegram.error import Forbidden
    wired.sent.side_effect = Forbidden('Business chat no longer permitted')
    await dispatch(wired, update(wired.bot, chat=777))
    await dispatch(wired, update(wired.bot, chat=777))
    wired.sent.assert_awaited_once()
    assert wired.sent.call_args.kwargs['business_connection_id'] == 'conn'
    wired.service.runner.run.assert_awaited_once()
    wired.core.assert_not_awaited()
