import sqlite3
import pytest
from business_powerpack.state import State, route_key
from test_plugin import wired, update, dispatch


def test_identity_survives_restart_and_is_exact(tmp_path):
    state = State(tmp_path / 'business-powerpack')
    key = route_key(tmp_path, 555, 'connection', 22)
    state.remember_route(key, 555, 'connection', 22, 11, 'Peer')
    restored = State(tmp_path / 'business-powerpack')
    assert restored.lookup_route(key, 555, 11) == {
        'route': key, 'bot': 555, 'connection': 'connection',
        'chat': 22, 'owner': 11, 'peer_label': 'Peer'}
    assert restored.lookup_route(key, 556, 11) is None
    assert restored.lookup_route(key, 555, 33) is None
    assert State(tmp_path / 'other' / 'business-powerpack').lookup_route(key, 555, 11) is None
    with pytest.raises(ValueError):
        state.remember_route(key, 555, 'connection', 23, 11, 'Peer')
    with pytest.raises(ValueError):
        state.remember_route(key, 555, 'connection', 22, 33, 'Peer')


def test_legacy_receipts_not_guessed(tmp_path):
    directory = tmp_path / 'business-powerpack'
    directory.mkdir()
    with sqlite3.connect(directory / 'receipts.sqlite3') as db:
        db.execute('CREATE TABLE receipts(route TEXT,message INTEGER,status TEXT,PRIMARY KEY(route,message))')
        db.execute("INSERT INTO receipts VALUES ('old-hash', 1, 'failed-ambiguous')")
    state = State(directory)
    assert state.lookup_route('old-hash', 555, 11) is None
    with state.db() as db:
        assert db.execute('SELECT status FROM receipts').fetchone()[0] == 'failed-ambiguous'


@pytest.mark.asyncio
async def test_native_dispatch_persists_before_agent_and_after_failure(wired):
    w = wired
    key = route_key(w.home, w.bot.id, 'conn', 22)
    async def failing_runner(route, query):
        assert route == key
        assert State(w.home / 'business-powerpack').lookup_route(key, w.bot.id, 11)['chat'] == 22
        raise RuntimeError('simulated interruption')
    w.service.runner.run.side_effect = failing_runner
    await dispatch(w, update(w.bot, mid=715))
    assert State(w.home / 'business-powerpack').lookup_route(key, w.bot.id, 11)['connection'] == 'conn'
    w.sent.assert_not_called()
    with w.service.state.db() as db:
        assert db.execute('SELECT status FROM receipts WHERE message=715').fetchone()[0] == 'failed-ambiguous'
    await dispatch(w, update(w.bot, mid=715))
    assert w.service.runner.run.call_count == 1


@pytest.mark.asyncio
async def test_denied_message_does_not_create_identity(wired):
    w = wired
    await dispatch(w, update(w.bot, mid=716, sender=22))
    key = route_key(w.home, w.bot.id, 'conn', 22)
    assert w.service.state.lookup_route(key, w.bot.id, 11) is None
