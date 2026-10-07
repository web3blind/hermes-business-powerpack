import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from PIL import Image
from telegram import Update, Bot
from test_plugin import wired, dispatch, Bot as FixtureBot
from test_story import FakeBot, setup
from business_powerpack.scope import profile_scope


def ordinary(bot, sender=11, chat=11, text=None, caption=None, photo=False, reply=False, mid=501):
    message = {'message_id': mid, 'date': int(datetime.now(timezone.utc).timestamp()),
        'chat': {'id': chat, 'type': 'private' if chat > 0 else 'group'},
        'from': {'id': sender, 'is_bot': False, 'first_name': 'Owner'}}
    media = {'photo': [{'file_id': 'photo-id', 'file_unique_id': 'unique',
                       'width': 80, 'height': 60, 'file_size': 100}]}
    if text is not None:
        message['text'] = text
    if caption is not None:
        message['caption'] = caption
    if photo:
        message.update(media)
    if reply:
        message['reply_to_message'] = {'message_id': 400, 'date': message['date'],
            'chat': message['chat'], 'from': message['from'], **media}
    return Update.de_json({'update_id': mid, 'message': message}, bot)


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', [False, True])
async def test_native_caption_or_reply_posts_once_and_stops_gateway(wired, monkeypatch, reply):
    w = wired
    fake = FakeBot()
    w.service.state.connection(555, fake.conn)
    handler = w.app.handlers[-101][0]
    story_module = sys.modules[handler.callback.__module__]
    monkeypatch.setattr(story_module.StoryService, '_bot', AsyncMock(return_value=fake))
    async def download(custom_path, **kwargs):
        Image.new('RGB', (80, 60), 'green').save(custom_path, format='PNG')
    telegram_file = SimpleNamespace(file_size=100, download_to_drive=AsyncMock(side_effect=download))
    monkeypatch.setattr(FixtureBot, 'get_file', AsyncMock(return_value=telegram_file))
    update = ordinary(w.bot, text='/story Подпись' if reply else None,
        caption=None if reply else '/story\nПодпись', photo=not reply, reply=reply)
    await dispatch(w, update)
    fake.post_story.assert_awaited_once()
    assert fake.post_story.await_args.kwargs['caption'] == 'Подпись'
    assert fake.post_story.await_args.args[0] == 'conn'
    assert 'ID 91' in w.sent.await_args.kwargs['text']
    await dispatch(w, update)
    fake.post_story.assert_awaited_once()
    assert 'Повторно не отправляю' in w.sent.await_args.kwargs['text']
    w.core.assert_not_awaited()
    assert not list((w.home / 'business-powerpack/story-media').glob('*.upload'))


@pytest.mark.asyncio
async def test_help_reply_failure_is_still_consumed(wired):
    w = wired
    w.sent.side_effect = RuntimeError('send failed')
    await dispatch(w, ordinary(w.bot, text='/story'))
    w.core.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('sender,chat', [(99, 99), (11, -100), (11, 99)])
async def test_native_foreign_or_group_has_no_effect(wired, sender, chat):
    w = wired
    await dispatch(w, ordinary(w.bot, sender=sender, chat=chat, caption='/story', photo=True))
    w.sent.assert_not_awaited()
    assert not (w.home / 'business-powerpack/story-media').exists()


@pytest.mark.asyncio
async def test_video_service_uses_required_ptb_duration(setup):
    import subprocess
    s = setup
    video = s.image.parent / 'clip.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=blue:s=160x90:r=30:d=1.2', '-c:v', 'libx264', '-threads', '1', '-y', str(video)],
        check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with profile_scope(s.home):
        result = await s.service.operate({'action': 'post', 'media_path': str(video)}, native_origin=(11, 502))
        assert result['story_id'] == 91
        content = s.bot.post_story.await_args.args[1]
        duration = content.duration.total_seconds() if hasattr(content.duration, 'total_seconds') else content.duration
        assert content.type == 'video' and 1 <= duration <= 1.5
        await s.service.operate({'action': 'edit', 'story_id': 91, 'caption': 'Video'}, native_origin=(11, 503))
        assert s.bot.edit_story.await_args.args[2].type == 'video'


@pytest.mark.asyncio
@pytest.mark.parametrize('story_id', [None, 1, 2])
async def test_post_cannot_use_story_id_to_bypass_receipt(setup, story_id):
    s = setup
    with profile_scope(s.home), pytest.raises(ValueError, match='does not accept'):
        await s.service.operate({'action': 'post', 'story_id': story_id, 'media_path': str(s.image)}, native_origin=(11, 504))
    s.bot.post_story.assert_not_awaited()
