import asyncio
import threading
import subprocess
from types import SimpleNamespace
import pytest
from test_story import setup
from business_powerpack.scope import profile_scope
from business_powerpack.story_media import prepare, probe
from gateway.session_context import set_session_vars, clear_session_vars


@pytest.mark.asyncio
async def test_caption_clear_and_media_only_preserves_caption(setup):
    s = setup
    with profile_scope(s.home):
        await s.service.operate({'action': 'post', 'media_path': str(s.image), 'caption': 'Keep'}, native_origin=(11, 100))
        await s.service.operate({'action': 'edit', 'story_id': 91, 'media_path': str(s.image)}, native_origin=(11, 101))
        assert s.bot.edit_story.await_args.kwargs['caption'] == 'Keep'
        await s.service.operate({'action': 'edit', 'story_id': 91, 'caption': ''}, native_origin=(11, 102))
        assert s.bot.edit_story.await_args.kwargs['caption'] == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['disabled', 'foreign', 'rights', 'no_connection', 'changed'])
async def test_connection_denials_before_public_effect(setup, monkeypatch, case):
    s = setup
    if case == 'disabled':
        s.bot.conn.is_enabled = False
    elif case == 'foreign':
        s.bot.conn.user.id = 99
    elif case == 'rights':
        s.bot.conn.rights.can_manage_stories = False
    elif case == 'no_connection':
        s.bot.get_me.return_value.id = 666
    else:
        def changed(home, path):
            result = prepare(home, path)
            s.bot.conn.rights.can_manage_stories = False
            return result
        monkeypatch.setattr('business_powerpack.story.prepare', changed)
    with profile_scope(s.home), pytest.raises(ValueError):
        await s.service.operate({'action': 'post', 'media_path': str(s.image)}, native_origin=(11, 103))
    s.bot.post_story.assert_not_awaited()


@pytest.mark.parametrize('changes', [
    {'user_id': '99'}, {'chat_id': '99'}, {'chat_type': 'group'},
    {'chat_type': 'private'}, {'platform': 'vk'}, {'message_id': ''},
    {'cron_session': '1'}, {'parent_chat_id': '12'}, {'source': 'business'},
])
def test_authoritative_origin_denials(changes):
    from business_powerpack.story import origin
    args = dict(platform='telegram', chat_type='dm', user_id='11', chat_id='11', message_id='200')
    args.update(changes)
    tokens = set_session_vars(**args)
    try:
        with pytest.raises(ValueError):
            origin(frozenset({11}))
    finally:
        clear_session_vars(tokens)


@pytest.mark.asyncio
async def test_profile_mismatch_fails_before_network(setup, tmp_path):
    s = setup
    with profile_scope(tmp_path / 'other'), pytest.raises(ValueError, match='Profile'):
        await s.service.operate({'action': 'list'}, native_origin=(11, 104))
    s.service._bot.assert_not_awaited()


def test_video_real_encoder_and_playlist_rejection(setup):
    s = setup
    source = s.image.parent / 'clip.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=blue:s=160x90:r=30:d=1.2', '-c:v', 'libx264', '-threads', '1', '-y', str(source)],
                   check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    target, kind = prepare(s.home, str(source))
    stream, duration = probe(target)
    assert kind == 'video' and stream['codec_name'] == 'hevc'
    assert (stream['width'], stream['height']) == (720, 1280)
    assert 1 <= duration <= 1.5
    playlist = s.image.parent / 'evil.m3u'
    playlist.write_text('#EXTM3U\nhttps://invalid.example/secret.mp4\n')
    with pytest.raises(ValueError, match='MP4'):
        prepare(s.home, str(playlist))


@pytest.mark.asyncio
async def test_prepare_cancel_waits_for_cleanup_no_upload(setup, monkeypatch):
    s = setup
    started, release = threading.Event(), threading.Event()
    def slow(home, media):
        started.set()
        release.wait(5)
        return prepare(home, media)
    monkeypatch.setattr('business_powerpack.story.prepare', slow)
    with profile_scope(s.home):
        task = asyncio.create_task(s.service.operate({'action': 'post', 'media_path': str(s.image)}, native_origin=(11, 105)))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    s.bot.post_story.assert_not_awaited()
    assert not list((s.home / 'business-powerpack/story-media').glob('*.jpg'))


def test_canonical_cache_and_output_symlink_guard(setup, tmp_path):
    from PIL import Image
    from business_powerpack.story_media import source_path
    s = setup
    cache = s.home / 'cache/images'
    cache.mkdir(parents=True)
    image = cache / 'normal.png'
    Image.new('RGB', (80, 60), 'red').save(image)
    assert source_path(s.home, str(image)) == image
    outside = tmp_path / 'other-output'
    outside.mkdir()
    directory = s.home / 'business-powerpack/story-media'
    directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        prepare(s.home, str(image))
    assert not list(outside.iterdir())


def test_real_gateway_session_binding_authorizes_owner_dm(setup):
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    from business_powerpack.story import origin
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {}
    source = SimpleNamespace(platform=Platform.TELEGRAM, chat_id='11', chat_type='dm',
        chat_name='', thread_id=None, user_id='11', user_id_alt=None, user_name='Owner',
        message_id='300', scope_id='', parent_chat_id='', profile='default')
    context = SimpleNamespace(source=source, session_key='fixture', session_id='fixture')
    tokens = runner._set_session_env(context)
    try:
        assert origin(frozenset({11})) == (11, 300)
    finally:
        runner._clear_session_env(tokens)
