import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

import pytest
from business_powerpack.config import Config
from business_powerpack.runtime import CliRunner, RunFailed, StreamParser, safe_text


def test_real_native_emitter(capsys):
    from hermes_cli.stream_json import StreamJsonEmitter
    emitter = StreamJsonEmitter()
    emitter.on_text_delta("intermediate")
    emitter.emit_result({"final_response": "final"})
    parser = StreamParser()
    for line in capsys.readouterr().out.splitlines():
        parser.feed(line)
    assert parser.finish(0) == "final"


@pytest.mark.parametrize("events,code", [([], 0), ([{"type": "text", "text": "partial"}], 0),
    ([{"type": "result", "text": "bad", "exit_code": 1}], 0),
    ([{"type": "result", "text": "bad", "exit_code": 0}], 1),
    ([{"type": "result", "text": "bad", "exit_code": 0, "partial": True}], 0),
    ([{"type": "result", "text": "bad", "exit_code": 0}] * 2, 0),
    ([{"type": "result", "text": "bad", "exit_code": 0}, {"type": "text"}], 0),
    ([{"type": "result", "text": "bad"}], 0)])
def test_bad_streams(events, code):
    parser = StreamParser()
    with pytest.raises(RunFailed):
        for event in events:
            parser.feed(json.dumps(event))
        parser.finish(code)


def test_limits_and_malformed():
    for line in (b"not json", b"\xff", b"[]", b"x" * (8 * 1024 * 1024 + 1)):
        with pytest.raises(RunFailed):
            StreamParser().feed(line)


def test_media_paths():
    assert "/home/" not in safe_text("Создан /home/user/private/image.png")
    assert "только текст" in safe_text("MEDIA:/home/user/image.png")
    assert safe_text("https://example.com/a/b") == "https://example.com/a/b"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_child_group_cleanup(tmp_path, cancel):
    child = tmp_path / "child.py"
    child.write_text("""import os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path('pids').write_text(str(os.getpid()) + ' ' + str(p.pid))
time.sleep(60)
""")
    runner = CliRunner(tmp_path, Config(frozenset({1}), frozenset({2}), timeout=10))
    runner.argv = lambda route: [sys.executable, str(child)]
    task = asyncio.create_task(runner.run("route", "query"))
    for _ in range(1000):
        if (tmp_path / "pids").exists():
            break
        await asyncio.sleep(.01)
    assert (tmp_path / "pids").exists()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else RunFailed):
        await task
    for pid in (tmp_path / "pids").read_text().split():
        status = Path("/proc") / pid / "stat"
        # killpg delivery and the kernel's final child scheduling are asynchronous;
        # the leader can be reaped a few ticks before its child becomes a zombie.
        for _ in range(200):
            try:
                state = status.read_text().split()[2]
            except (FileNotFoundError, ProcessLookupError):
                # /proc may vanish after open but before read when PID is reaped.
                break
            if state == "Z":
                break
            await asyncio.sleep(.01)
        else:
            pytest.fail("Process-group child remained alive after SIGKILL")


def test_profile_config_no_fallback(tmp_path, monkeypatch):
    from business_powerpack.scope import profile_scope
    from agent.secret_scope import get_secret
    monkeypatch.setenv("OPENAI_API_KEY", "other-profile-secret")
    with profile_scope(tmp_path):
        cfg = Config.load(tmp_path)
        assert not cfg.owner_ids and cfg.allowed_chats is None
        assert not get_secret("OPENAI_API_KEY")
    (tmp_path / "config.yaml").write_text('plugins:\n  entries:\n    business-powerpack:\n      owner_ids: ["11"]\n')
    with profile_scope(tmp_path), pytest.raises(ValueError):
        Config.load(tmp_path)


@pytest.mark.parametrize('value,allowed', [([], False), ([22], True)])
def test_explicit_local_chat_filter_remains_fail_closed(tmp_path, value, allowed):
    from business_powerpack.scope import profile_scope
    (tmp_path / 'config.yaml').write_text('plugins:\n  entries:\n    business-powerpack:\n      owner_ids: [11]\n      allowed_chats: ' + json.dumps(value) + '\n')
    with profile_scope(tmp_path):
        cfg = Config.load(tmp_path)
    assert cfg.allows_chat(22) is allowed
    assert not cfg.allows_chat(99)


@pytest.mark.parametrize('value', ['null', '[true]', '["22"]', 'all'])
def test_invalid_local_filter_never_widens_access(tmp_path, value):
    from business_powerpack.scope import profile_scope
    (tmp_path / 'config.yaml').write_text('plugins:\n  entries:\n    business-powerpack:\n      owner_ids: [11]\n      allowed_chats: ' + value + '\n')
    with profile_scope(tmp_path), pytest.raises(ValueError):
        Config.load(tmp_path)
