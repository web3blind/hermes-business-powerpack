import asyncio
import importlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from gateway.session_context import set_session_vars, clear_session_vars
from business_powerpack.scope import profile_scope
from business_powerpack.state import State
from business_powerpack.story import StoryService, TOOL
from business_powerpack.story_media import prepare, source_path


class FakeBot:
    def __init__(self):
        self.id = 555
        self.conn = SimpleNamespace(id='conn', user=SimpleNamespace(id=11, is_bot=False),
                                    is_enabled=True, rights=SimpleNamespace(can_reply=False,
                                                                             can_manage_stories=True))
        self.post_story = AsyncMock(return_value=SimpleNamespace(id=91, chat=SimpleNamespace(id=11)))
        self.edit_story = AsyncMock(return_value=SimpleNamespace(id=91, chat=SimpleNamespace(id=11)))
        self.delete_story = AsyncMock(return_value=True)
        self.get_business_connection = AsyncMock(side_effect=lambda cid, **kw: self.conn)
        self.get_me = AsyncMock(return_value=SimpleNamespace(id=self.id))
        self.shutdown = AsyncMock()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    (tmp_path / 'config.yaml').write_text('plugins:\n  entries:\n    business-powerpack:\n      owner_ids: [11]\n')
    state = State(tmp_path / 'business-powerpack')
    bot = FakeBot()
    state.connection(bot.id, bot.conn)
    service = StoryService(tmp_path)
    monkeypatch.setattr(service, '_bot', AsyncMock(return_value=bot))
    cache = tmp_path / 'image_cache'
    cache.mkdir()
    image = cache / 'photo.png'
    Image.new('RGB', (80, 60), 'red').save(image)
    return SimpleNamespace(home=tmp_path, state=state, bot=bot, service=service, image=image)


@pytest.mark.asyncio
async def test_post_edit_delete_durable_and_exact_connection(setup):
    s = setup
    with profile_scope(s.home):
        posted = await s.service.operate({'action': 'post', 'media_path': str(s.image), 'caption': 'Hi'}, native_origin=(11, 1))
        assert posted == {'status': 'posted', 'story_id': 91}
        assert s.bot.post_story.await_args.args[0] == 'conn'
        assert s.bot.post_story.await_args.kwargs['post_to_chat_page'] is False
        assert s.bot.post_story.await_args.kwargs['parse_mode'] is None
        assert s.state.own_story(555, 'conn', 11, 91)
        again = await s.service.operate({'action': 'post', 'media_path': str(s.image), 'caption': 'Hi'}, native_origin=(11, 1))
        assert again['status'] == 'already_claimed'
        s.bot.post_story.assert_awaited_once()
        edited = await s.service.operate({'action': 'edit', 'story_id': 91, 'caption': 'New'}, native_origin=(11, 2))
        assert edited['status'] == 'edited'
        assert s.bot.edit_story.await_args.args[0:2] == ('conn', 91)
        with pytest.raises(ValueError, match='not owned'):
            await s.service.operate({'action': 'delete', 'story_id': 92}, native_origin=(11, 3))
        deleted = await s.service.operate({'action': 'delete', 'story_id': 91}, native_origin=(11, 4))
        assert deleted['status'] == 'deleted'
        assert s.state.list_stories(555, 'conn', 11) == []


@pytest.mark.asyncio
async def test_ambiguous_no_retry_and_permission_change(setup):
    s = setup
    s.bot.post_story.side_effect = RuntimeError('secret-token-here')
    with profile_scope(s.home):
        with pytest.raises(RuntimeError):
            await s.service.operate({'action': 'post', 'media_path': str(s.image)}, native_origin=(11, 5))
        assert (await s.service.operate({'action': 'post', 'media_path': str(s.image)}, native_origin=(11, 5)))['status'] == 'already_claimed'
        s.bot.post_story.assert_awaited_once()
        s.bot.conn.rights.can_manage_stories = False
        with pytest.raises(ValueError, match='Exactly one'):
            await s.service.operate({'action': 'post', 'media_path': str(s.image)}, native_origin=(11, 6))


def test_path_and_image_guard(setup, tmp_path):
    s = setup
    photo, kind = prepare(s.home, str(s.image))
    assert kind == 'photo'
    with Image.open(photo) as image:
        assert image.size == (1080, 1920)
    outside = tmp_path.parent / 'outside.jpg'
    with pytest.raises((ValueError, FileNotFoundError)):
        source_path(s.home, str(outside))
    link = s.image.parent / 'escape'
    link.symlink_to('/etc/passwd')
    with pytest.raises(ValueError):
        source_path(s.home, str(link))
    with pytest.raises(ValueError):
        source_path(s.home, 'https://example.org/x.jpg')


def test_registry_gateway_origin_and_discovery(setup, monkeypatch):
    s = setup
    root = Path(__file__).resolve().parents[1]
    installed = s.home / 'plugins/business-powerpack'
    installed.mkdir(parents=True)
    for name in ('__init__.py', 'config.py', 'runtime.py', 'scope.py', 'service.py', 'state.py',
                 'story.py', 'story_media.py', 'reply_media.py', 'stt_worker.py', 'plugin.yaml'):
        shutil.copy2(root / name, installed / name)
    manager = PluginManager()
    manifest = PluginManifest(name='business-powerpack', source='user', path=str(installed))
    with profile_scope(s.home):
        loaded = manager._load_directory_module(manifest)
        installed_story = importlib.import_module(loaded.__name__ + '.story')
        monkeypatch.setattr(installed_story.StoryService, '_bot', AsyncMock(return_value=s.bot))
        ctx = PluginContext(manifest, manager)
        loaded.register(ctx)
        from tools.registry import registry
        entry = registry.get_entry(TOOL, scope=manager.scope_key)
        assert entry.schema['parameters']['properties']['action']['enum'] == ['post', 'edit', 'delete', 'list']
        # Use native registry dispatch, with the exact ContextVars the gateway binds.
        tokens = set_session_vars(platform='telegram', chat_type='dm', user_id='11',
                                  chat_id='11', message_id='50')
        try:
            result = json.loads(ctx.dispatch_tool(TOOL, {'action': 'list'}))
            assert result == {'story_ids': []}
        finally:
            clear_session_vars(tokens)
        monkeypatch.setenv('HERMES_SESSION_USER_ID', '11')
        result = json.loads(ctx.dispatch_tool(TOOL, {'action': 'list'}))
        assert 'error' in result
        tokens = set_session_vars(platform='telegram', chat_type='group', user_id='11',
                                  chat_id='11', message_id='51')
        try:
            result = json.loads(ctx.dispatch_tool(TOOL, {'action': 'list'}))
            assert 'error' in result
        finally:
            clear_session_vars(tokens)
