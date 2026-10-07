"""Regression tests for GPT-6-Astra pre-install findings."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import BusinessConnection
from business_powerpack.config import Config
from business_powerpack.runtime import CliRunner, RunFailed, safe_text
from test_plugin import wired, dispatch, update, connection, Bot


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_cleanup(tmp_path):
    runner = CliRunner(tmp_path, Config(frozenset({1}), timeout=5))
    cleanup_started, release = asyncio.Event(), asyncio.Event()
    actual_stop = runner.stop
    async def slow_stop(proc):
        cleanup_started.set()
        await release.wait()
        await actual_stop(proc)
    runner.stop = slow_stop
    command = [sys.executable, '-c', "from pathlib import Path; import time; Path('started').touch(); time.sleep(60)"]
    task = asyncio.create_task(runner.run_command(command, '', 5))
    for _ in range(500):
        if (tmp_path / 'started').exists():
            break
        await asyncio.sleep(.01)
    assert (tmp_path / 'started').exists()
    task.cancel()
    await asyncio.wait_for(cleanup_started.wait(), 2)
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_cancellation_during_spawn_does_not_orphan_child(tmp_path, monkeypatch):
    runner = CliRunner(tmp_path, Config(frozenset({1})))
    created, release = asyncio.Event(), asyncio.Event()
    actual_spawn = asyncio.create_subprocess_exec
    processes = []
    async def delayed_spawn(*args, **kwargs):
        proc = await actual_spawn(*args, **kwargs)
        processes.append(proc)
        created.set()
        await release.wait()
        return proc
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed_spawn)
    task = asyncio.create_task(runner.run_command([sys.executable, '-c', 'import time; time.sleep(60)'], '', 5))
    await asyncio.wait_for(created.wait(), 5)
    task.cancel()
    await asyncio.sleep(.01)
    task.cancel()
    await asyncio.sleep(.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert processes[0].returncode is not None


def test_long_agent_text_explicitly_reports_truncation():
    text = safe_text('a' * 25000)
    assert len(text) <= 24000
    assert text.endswith('[Текст сокращён до лимита плагина.]')


@pytest.mark.asyncio
@pytest.mark.parametrize('via', ['authorize', 'prewritten'])
async def test_revoke_previously_stored_also_cancels_tools(wired, via):
    w = wired
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def run(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    w.service.runner.run.side_effect = run
    await w.app.process_update(update(w.bot))
    await asyncio.wait_for(started.wait(), 2)
    conn = BusinessConnection.de_json(connection(enabled=False), w.bot)
    if via == 'authorize':
        w.fetched.return_value = conn
        assert await w.service.authorize('conn', 44) is None
    else:
        w.service.state.connection(w.bot.id, conn)
    w.service.connection_update(conn)
    await asyncio.wait_for(cancelled.wait(), 2)
    await asyncio.gather(*list(w.service.tasks), return_exceptions=True)
    w.sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_whitespace_chunks_do_not_break_delivery(wired):
    wired.service.runner.run.return_value = 'a' * 1500 + ' ' * 1500 + 'b'
    await dispatch(wired, update(wired.bot))
    assert [c.kwargs['text'] for c in wired.sent.await_args_list] == ['a' * 1500, 'b']


def stt_bootstrap(home, mode='ok'):
    # Controlled provider injected only in a test child. Executes the installed
    # worker and real native profile/secret binding, not a fabricated live API.
    worker = home / 'plugins' / 'business-powerpack' / 'stt_worker.py'
    bootstrap = home / 'stt-fixture.py'
    bootstrap.write_text('''import runpy, sys, types, os, signal, time
from pathlib import Path
worker = sys.argv[1]
sys.argv = [worker, sys.argv[2]]
sys.path.insert(0, str(Path(worker).parent))
module = types.ModuleType('tools.transcription_tools')
def transcribe(path, source):
    from hermes_constants import get_hermes_home
    from agent.secret_scope import current_secret_scope_home, get_secret
    home = Path(os.environ['HERMES_HOME'])
    assert get_hermes_home() == home
    assert current_secret_scope_home() == str(home)
    assert not get_secret('OPENAI_API_KEY')
    assert source == 'gateway'
    print('DO_NOT_SEND provider log /private/path')
    assert Path(path).read_bytes() == b'fake-ogg'
    if MODE == 'hang':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        (home / 'stt-pid').write_text(str(os.getpid()))
        while True: time.sleep(1)
    if MODE == 'fail': raise RuntimeError('DO_NOT_SEND provider secret')
    return {'success': True, 'transcript': 'file://example /home/spoken/path MEDIA: слова клиента'}
module.transcribe_audio = transcribe
sys.modules['tools.transcription_tools'] = module
runpy.run_path(worker, run_name='__main__')
'''.replace('MODE', repr(mode)))
    return lambda path: [sys.executable, str(bootstrap), str(worker), str(path)]


async def prepare_voice(w, monkeypatch):
    async def download(custom_path, **kw):
        custom_path.write_bytes(b'fake-ogg')
    monkeypatch.setattr(Bot, 'get_file', AsyncMock(return_value=SimpleNamespace(
        file_id='voice-id', file_size=10, download_to_drive=download)))


@pytest.mark.asyncio
async def test_real_stt_worker_scoped_child_preserves_spoken_text(wired, monkeypatch):
    w = wired
    await prepare_voice(w, monkeypatch)
    monkeypatch.setenv('OPENAI_API_KEY', 'other-profile-secret')
    runner = CliRunner(w.home, replace(w.service.config, stt_timeout=10))
    runner.stt_argv = stt_bootstrap(w.home)
    w.service.stt = runner.transcribe
    await dispatch(w, update(w.bot, sender=22, voice=True))
    assert w.sent.call_args.kwargs['text'] == 'Расшифровка:\nfile://example /home/spoken/path MEDIA: слова клиента'
    w.service.runner.run.assert_not_awaited()
    assert not list((w.home / 'business-powerpack' / 'media').iterdir())


@pytest.mark.asyncio
async def test_hung_stt_deadline_kills_process_before_media_cleanup(wired, monkeypatch):
    w = wired
    await prepare_voice(w, monkeypatch)
    runner = CliRunner(w.home, replace(w.service.config, stt_timeout=10))
    runner.stt_argv = stt_bootstrap(w.home, 'hang')
    w.service.stt = runner.transcribe
    await asyncio.wait_for(dispatch(w, update(w.bot, sender=22, voice=True)), 20)
    pid = int((w.home / 'stt-pid').read_text())
    assert not (Path('/proc') / str(pid)).exists()
    assert not list((w.home / 'business-powerpack' / 'media').iterdir())
    assert not w.service.tasks
    w.sent.assert_not_awaited()
    w.service.runner.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_stt_revoke_retains_media_and_capacity_until_reaped(wired, monkeypatch):
    w = wired
    await prepare_voice(w, monkeypatch)
    runner = CliRunner(w.home, replace(w.service.config, stt_timeout=10))
    runner.stt_argv = stt_bootstrap(w.home, 'hang')
    started, release = asyncio.Event(), asyncio.Event()
    actual_stop = runner.stop
    async def stop(proc):
        started.set()
        await release.wait()
        await actual_stop(proc)
    runner.stop = stop
    w.service.stt = runner.transcribe
    await w.app.process_update(update(w.bot, sender=22, voice=True))
    for _ in range(500):
        if (w.home / 'stt-pid').exists(): break
        await asyncio.sleep(.01)
    assert (w.home / 'stt-pid').exists()
    revoked = BusinessConnection.de_json(connection(enabled=False), w.bot)
    w.service.connection_update(revoked)
    await asyncio.wait_for(started.wait(), 2)
    for _ in range(3):
        w.service.connection_update(revoked)
        await asyncio.sleep(.01)
    assert len(w.service.tasks) == 1
    assert list((w.home / 'business-powerpack' / 'media').iterdir())
    release.set()
    await asyncio.wait_for(asyncio.gather(*list(w.service.tasks), return_exceptions=True), 5)
    assert not list((w.home / 'business-powerpack' / 'media').iterdir())
    w.sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_stt_worker_failure_never_publishes_provider_output(wired, monkeypatch):
    w = wired
    await prepare_voice(w, monkeypatch)
    runner = CliRunner(w.home, replace(w.service.config, stt_timeout=10))
    runner.stt_argv = stt_bootstrap(w.home, 'fail')
    w.service.stt = runner.transcribe
    await dispatch(w, update(w.bot, sender=22, voice=True))
    w.sent.assert_not_awaited()
    assert not list((w.home / 'business-powerpack' / 'media').iterdir())


@pytest.mark.asyncio
async def test_noisy_stt_pipe_failure_is_bounded_and_releases_route(wired, monkeypatch):
    w = wired
    await prepare_voice(w, monkeypatch)
    runner = CliRunner(w.home, replace(w.service.config, stt_timeout=10))
    noisy = w.home / 'noisy-stt.py'
    noisy.write_text("import os, signal\nfrom pathlib import Path\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nPath('noise-pid').write_text(str(os.getpid()))\nwhile True: os.write(1, b'x' * 65536)\n")
    runner.stt_argv = lambda path: [sys.executable, str(noisy)]
    w.service.stt = runner.transcribe
    await asyncio.wait_for(dispatch(w, update(w.bot, sender=22, voice=True)), 15)
    pid = int((w.home / 'noise-pid').read_text())
    assert not (Path('/proc') / str(pid)).exists()
    assert not w.service.tasks
    assert not list((w.home / 'business-powerpack' / 'media').iterdir())
    w.sent.assert_not_awaited()
    # A fresh request on the same route must run, not remain locked/busy.
    w.service.stt = AsyncMock(return_value='после ошибки')
    await dispatch(w, update(w.bot, mid=2, sender=22, voice=True))
    assert w.sent.call_args.kwargs['text'] == 'Расшифровка:\nпосле ошибки'
