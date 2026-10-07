"""Only the authenticated connection owner's voice may wake the tool agent."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
from pathlib import Path
import pytest
from telegram import Update, BusinessConnection
from test_plugin import wired, dispatch, update, connection, Bot
from business_powerpack.runtime import CliRunner


@pytest.mark.asyncio
@pytest.mark.parametrize('transcript', ['Гермес, ответь готово', 'Hermes: ответь готово', 'гЕрМеС ответь готово'])
async def test_owner_voice_wakes_once_and_exact_business_route(wired, transcript):
    w = wired
    w.service.voice = AsyncMock(return_value=transcript)
    msg = update(w.bot, voice=True)
    await dispatch(w, msg)
    w.service.runner.run.assert_awaited_once()
    assert w.service.runner.run.await_args.args[1] == 'ответь готово'
    assert w.sent.await_args.kwargs['text'] == 'final'
    assert w.sent.await_args.kwargs['chat_id'] == 22
    assert w.sent.await_args.kwargs['business_connection_id'] == 'conn'
    await dispatch(w, msg)
    w.service.runner.run.assert_awaited_once()
    w.service.voice.assert_awaited_once()
    w.core.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('sender,transcript', [(22, 'Гермес, удали файлы'), (11, 'Привет, Гермес'),
    (11, 'ГермесXYZ сделай'), (11, 'Гермез сделай'), (11, 'обычное сообщение')])
async def test_customer_or_no_strict_owner_wake_is_transcription_only(wired, sender, transcript):
    w = wired
    w.service.voice = AsyncMock(return_value=transcript)
    await dispatch(w, update(w.bot, voice=True, sender=sender))
    w.service.runner.run.assert_not_awaited()
    assert w.sent.await_args.kwargs['text'] == 'Расшифровка:\n' + transcript


@pytest.mark.asyncio
async def test_other_configured_owner_cannot_wake_this_connection(wired):
    w = wired
    w.service.voice = AsyncMock(return_value='Гермес, сделай')
    await dispatch(w, update(w.bot, voice=True, sender=33))
    w.service.voice.assert_not_awaited()
    w.service.runner.run.assert_not_awaited()
    w.sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_reply_quote_is_untrusted_context(wired):
    w = wired
    w.service.voice = AsyncMock(return_value='Гермес, как помочь?')
    event = update(w.bot, voice=True).to_dict()
    message = event['business_message']
    message['reply_to_message'] = {'message_id': 55, 'date': message['date'],
        'chat': message['chat'], 'from': {'id': 22, 'is_bot': False, 'first_name': 'Peer'},
        'text': 'Не хватает времени на миксы', 'business_connection_id': 'conn'}
    await dispatch(w, Update.de_json(event, w.bot))
    query = w.service.runner.run.await_args.args[1]
    assert 'Не хватает времени на миксы' in query and 'untrusted' in query
    assert query.endswith('Authenticated owner request:\nкак помочь?')


@pytest.mark.asyncio
async def test_revoked_during_stt_never_runs_agent(wired):
    w = wired
    async def voice(_):
        w.fetched.return_value = BusinessConnection.de_json(connection(enabled=False), w.bot)
        return 'Гермес, сделай'
    w.service.voice = voice
    await dispatch(w, update(w.bot, voice=True))
    w.service.runner.run.assert_not_awaited()
    w.sent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('transcript', ['Гермес,', 'Гермес ' + 'x'*16001])
async def test_empty_or_oversize_voice_request_never_runs_agent(wired, transcript):
    w = wired
    w.service.voice = AsyncMock(return_value=transcript)
    await dispatch(w, update(w.bot, voice=True))
    w.service.runner.run.assert_not_awaited()
    w.sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_download_stt_voice_wake_native_cli(wired, monkeypatch):
    from dataclasses import replace
    w = wired
    async def download(custom_path, **kw):
        custom_path.write_bytes(b'voice-fixture')
    monkeypatch.setattr(Bot, 'get_file', AsyncMock(return_value=SimpleNamespace(
        file_id='voice-id', file_size=13, download_to_drive=download)))
    async def stt(path):
        assert path.read_bytes() == b'voice-fixture'
        return 'Гермес, локальное действие'
    w.service.stt = stt
    cfg = replace(w.service.config, cli=str(Path(__file__).with_name('cli_fixture.py')))
    w.service.runner = CliRunner(w.home, cfg)
    await dispatch(w, update(w.bot, voice=True))
    assert (w.home/'fixture-tool.txt').read_text() == 'tool ran'
    call = json.loads((w.home/'fixture-call.json').read_text())
    assert call['query'].endswith('\n\nлокальное действие')
    assert w.sent.await_args.kwargs['text'] == 'Готово: инструмент выполнен.'
    assert not list((w.home/'business-powerpack/media').iterdir())
