"""Exercise the real PTB serializer without any live Telegram writes."""
import json
import subprocess
import pytest
from telegram import Bot
from telegram.request import BaseRequest
from test_story import setup
from business_powerpack.scope import profile_scope


class UploadRequest(BaseRequest):
    def __init__(self):
        self.effects = []

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit('/', 1)[-1]
        if endpoint == 'getMe':
            result = {'id': 555, 'is_bot': True, 'first_name': 'Test', 'username': 'test_bot'}
        else:
            assert endpoint in {'postStory', 'editStory'}
            params = request_data.json_parameters
            content = json.loads(params['content'])
            field = 'photo' if content['type'] == 'photo' else 'video'
            assert content[field].startswith('attach://'), 'Story media must be uploaded, not a server-local file URL'
            part = content[field].removeprefix('attach://')
            assert part in request_data.multipart_data
            filename, body, mimetype = request_data.multipart_data[part]
            assert len(body) > 100 and filename.endswith('.jpg' if field == 'photo' else '.mp4')
            assert mimetype.startswith('image/' if field == 'photo' else 'video/')
            self.effects.append(endpoint)
            result = {'id': 91, 'chat': {'id': 11, 'type': 'private', 'first_name': 'Owner'}}
        return 200, json.dumps({'ok': True, 'result': result}).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize('video', [False, True])
async def test_real_ptb_post_and_caption_edit_upload_media(setup, video):
    s = setup
    request = UploadRequest()
    bot = Bot('123:TEST_TOKEN', request=request)
    await bot.initialize()
    s.bot.post_story = bot.post_story
    s.bot.edit_story = bot.edit_story
    media = s.image
    if video:
        media = s.image.parent / 'clip.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
            'color=c=blue:s=160x90:r=30:d=1.2', '-c:v', 'libx264', '-threads', '1', '-y', str(media)],
            check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with profile_scope(s.home):
            assert (await s.service.operate({'action': 'post', 'media_path': str(media)}, native_origin=(11, 700)))['status'] == 'posted'
            assert (await s.service.operate({'action': 'edit', 'story_id': 91, 'caption': 'New'}, native_origin=(11, 701)))['status'] == 'edited'
        assert request.effects == ['postStory', 'editStory']
    finally:
        await bot.shutdown()
