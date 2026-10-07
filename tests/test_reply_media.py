"""Reply files exist only during an explicit, authorized owner agent request."""
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from telegram import Update, BusinessConnection
from test_plugin import wired, dispatch, update, Bot, connection


def media_reply(w, kind='photo', voice=False, sender=11, other_chat=False):
    event = update(w.bot, voice=voice, sender=sender, text='Гермес, опиши вложение').to_dict()
    message = event['business_message']
    reply = {'message_id': 10, 'date': message['date'], 'chat': dict(message['chat']),
        'business_connection_id': 'conn', 'from': {'id':22,'is_bot':False,'first_name':'Peer'}}
    if other_chat:
        reply['chat']['id'] = 44
    data = {'file_id': 'reply-id', 'file_unique_id':'unique','file_size':12}
    if kind == 'photo':
        reply['photo'] = [{**data,'width':2,'height':2}]
    elif kind in ('video','video_note'):
        reply[kind] = {**data,'duration':1, **({'length':2} if kind=='video_note' else {'width':2,'height':2})}
    elif kind in ('voice','audio'):
        reply[kind] = {**data,'duration':1}
    else:
        reply['document'] = {**data,'file_name':'report.docx','mime_type':'application/octet-stream'}
    message['reply_to_message'] = reply
    return Update.de_json(event,w.bot)


def downloadable(monkeypatch, data=b'file-content'):
    from types import SimpleNamespace
    async def download(custom_path, **kw):
        Path(custom_path).write_bytes(data)
    get = AsyncMock(return_value=SimpleNamespace(file_id='reply-id',file_size=len(data),download_to_drive=download))
    monkeypatch.setattr(Bot,'get_file',get)
    return get


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['photo','video','video_note','audio','voice','document'])
@pytest.mark.parametrize('voice',[False,True])
async def test_owner_reply_attachment_is_real_scoped_file_during_agent(wired, monkeypatch, kind, voice):
    w=wired
    w.service.voice=AsyncMock(return_value='Гермес, опиши вложение')
    get=downloadable(monkeypatch)
    seen=[]
    async def run(route,query):
        line=next(x for x in query.splitlines() if x.startswith('Attachment metadata (untrusted JSON): '))
        metadata=json.loads(line.split(': ',1)[1])
        file=Path(metadata['local_path'])
        assert file.read_bytes()==b'file-content'
        assert file.is_relative_to(w.home/'cache/attachments')
        assert 'NOT instructions' in query and 'опиши вложение' in query
        seen.append(file)
        return 'Описание готово'
    w.service.runner.run.side_effect=run
    await dispatch(w,media_reply(w,kind,voice))
    assert len(seen)==1
    assert not seen[0].exists()
    assert w.sent.await_args.kwargs['text']=='Описание готово'
    assert w.sent.await_args.kwargs['business_connection_id']=='conn'
    get.assert_awaited_once()


@pytest.mark.asyncio
async def test_peer_voice_wake_does_not_download_reply(wired, monkeypatch):
    w=wired; get=downloadable(monkeypatch)
    w.service.voice=AsyncMock(return_value='Гермес, опиши вложение')
    await dispatch(w,media_reply(w,voice=True,sender=22))
    get.assert_not_awaited();w.service.runner.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_chat_reply_never_downloaded(wired, monkeypatch):
    w=wired;get=downloadable(monkeypatch)
    await dispatch(w,media_reply(w,other_chat=True))
    get.assert_not_awaited()
    assert 'Attachment metadata' not in w.service.runner.run.await_args.args[1]


@pytest.mark.asyncio
async def test_revoke_during_attachment_download_prevents_tools(wired,monkeypatch):
    w=wired
    from types import SimpleNamespace
    async def download(custom_path,**kw):
        Path(custom_path).write_bytes(b'file-content')
        w.fetched.return_value=BusinessConnection.de_json(connection(enabled=False),w.bot)
    monkeypatch.setattr(Bot,'get_file',AsyncMock(return_value=SimpleNamespace(
        file_id='reply-id',file_size=12,download_to_drive=download)))
    await dispatch(w,media_reply(w))
    w.service.runner.run.assert_not_awaited();w.sent.assert_not_awaited()
    assert not list((w.home/'cache/attachments').rglob('payload*'))


@pytest.mark.asyncio
async def test_media_download_failure_reports_without_running_tools(wired,monkeypatch):
    w=wired
    monkeypatch.setattr(Bot,'get_file',AsyncMock(side_effect=RuntimeError('secret API URL')))
    await dispatch(w,media_reply(w))
    w.service.runner.run.assert_not_awaited()
    assert 'вложение' in w.sent.await_args.kwargs['text'].lower()
    assert 'secret' not in w.sent.await_args.kwargs['text']


@pytest.mark.asyncio
async def test_oversize_reply_never_downloaded(wired,monkeypatch):
    w=wired;get=downloadable(monkeypatch)
    event=media_reply(w).to_dict()
    event['business_message']['reply_to_message']['photo'][0]['file_size']=21*1024*1024
    await dispatch(w,Update.de_json(event,w.bot))
    get.assert_not_awaited();w.service.runner.run.assert_not_awaited()
    assert '20 МиБ' in w.sent.await_args.kwargs['text']


@pytest.mark.asyncio
async def test_failed_agent_cleans_downloaded_reply(wired,monkeypatch):
    w=wired;get=downloadable(monkeypatch)
    w.service.runner.run.side_effect=RuntimeError('child failure')
    await dispatch(w,media_reply(w))
    get.assert_awaited_once()
    assert not list((w.home/'cache/attachments').rglob('payload*'))


@pytest.mark.asyncio
async def test_cache_symlink_does_not_download(wired,monkeypatch,tmp_path):
    w=wired;get=downloadable(monkeypatch)
    (w.home/'cache').mkdir(exist_ok=True)
    target=tmp_path/'outside';target.mkdir()
    (w.home/'cache/attachments').symlink_to(target,target_is_directory=True)
    await dispatch(w,media_reply(w))
    get.assert_not_awaited();w.service.runner.run.assert_not_awaited()
    assert not list(target.iterdir())
