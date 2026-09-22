"""Guidance and material transactions: synthetic users, no real credentials."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app import context, guidance, learning_materials as materials, model, storage
from app.main import create_app


class GuidanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        storage.save_settings(self.root, 'sk-synthetic', storage.MODELS[0])
        self.calls = []
        self.invalid = False

    async def reply(self, settings, messages):
        self.calls.append(messages)
        if '现在整理修改提案' in json.dumps(messages, ensure_ascii=False):
            if self.invalid:
                yield '{bad'
                return
            discussion = json.loads(messages[-1]['content'].split('\n', 1)[1])
            evidence = next(m['id'] for m in discussion if m['role'] == 'user')
            originals = json.loads(messages[1]['content'].split('\n', 1)[1])
            yield json.dumps({'summary': '调整讲解与目标', 'changes': [
                {'material': key, 'content': originals[key] + '\n合成修改', 'reason': '用户明确希望调整', 'evidence': [evidence]}
                for key in ('teaching', 'goals')]}, ensure_ascii=False)
        else:
            yield '我们可以把你的长期偏好整理成建议。'

    def client(self, reply=None):
        client = TestClient(create_app(self.root, reply or self.reply))
        self.addCleanup(client.__exit__, None, None, None)
        return client.__enter__()

    def talk(self, client, text='以后先解释再提问', **extra):
        revision = client.get('/api/guidance').json()['revision']
        result = client.post('/api/guidance/chat', json={'revision': revision, 'message': text, **extra})
        self.assertEqual(result.status_code, 200, result.text)
        return client.get('/api/guidance').json()

    def proposal(self, client):
        view = self.talk(client)
        result = client.post('/api/guidance/propose', json={'revision': view['revision']})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_initialize_fresh_and_never_overwrite_existing_templates(self):
        client = self.client()
        preview = client.get('/api/teaching').json()
        self.assertEqual(len(preview['materials']), 3)
        original = materials.read(self.root, 'profile')
        materials.save(self.root, 'profile', materials.MaterialInput(revision=original['revision'], content='我的实际背景'))
        materials.initialize(self.root)
        self.assertEqual(materials.read(self.root, 'profile')['content'], '我的实际背景')
        (self.root / 'data/learning-source.json').unlink()
        with self.assertRaises(ValueError):
            materials.initialize(self.root)
        self.assertEqual((self.root / 'data/learning-materials/core/profile.md').read_text(encoding='utf-8'), '我的实际背景')

    def test_existing_ten_materials_remain_byte_identical(self):
        for path, _ in context.MATERIALS:
            storage.atomic_write(self.root / 'data/learning-materials' / path, '# 原文\r\n保留\r\n')
        storage.atomic_write(self.root / 'data/learning-source.json', json.dumps({'root':'data/learning-materials', 'managed':True}))
        before = {p:p.read_bytes() for p in (self.root/'data/learning-materials').rglob('*.md')}
        client = self.client()
        self.assertEqual(len(client.get('/api/teaching').json()['materials']),10)
        self.talk(client)
        self.assertEqual(before, {p:p.read_bytes() for p in before})

    def test_discussion_proposal_apply_restart_undo_and_classroom_isolation(self):
        client = self.client()
        old = context.prepare(self.root)
        proposal = self.proposal(client)
        self.assertEqual(old, context.prepare(self.root))
        self.assertEqual(client.get('/api/conversation').json()['conversation']['messages'], [])
        self.assertIsNone(client.get('/api/breakpoint').json()['record'])
        self.assertEqual(len(client.get('/api/conversations').json()['conversations']), 1)
        response = client.post('/api/guidance/apply', json={'id':proposal['id']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('合成修改',context.prepare(self.root)['system_prompt'])
        self.assertEqual(client.post('/api/guidance/apply',json={'id':proposal['id']}).status_code,200)
        with TestClient(create_app(self.root,self.reply)) as restarted:
            self.assertEqual(restarted.get('/api/guidance').json()['revision'],2)
            self.assertEqual(restarted.post('/api/guidance/undo',json={'id':proposal['id']}).status_code,200)
            self.assertEqual(context.prepare(self.root),old)
            self.assertEqual(restarted.post('/api/guidance/undo',json={'id':proposal['id']}).status_code,200)
            self.assertEqual(restarted.post('/api/guidance/apply',json={'id':proposal['id']}).status_code,409)

    def test_stale_discussion_material_and_undo_conflicts(self):
        client=self.client()
        proposal=self.proposal(client)
        self.assertEqual(client.post('/api/guidance/chat',json={'revision':0,'message':'旧窗口'}).status_code,409)
        current=materials.read(self.root,'profile')
        materials.save(self.root,'profile',materials.MaterialInput(revision=current['revision'],content='新背景'))
        self.assertEqual(client.post('/api/guidance/apply',json={'id':proposal['id']}).status_code,409)
        proposal=self.proposal(client)
        self.talk(client,'补充新的要求')
        self.assertEqual(client.post('/api/guidance/apply',json={'id':proposal['id']}).status_code,409)
        proposal=self.proposal(client)
        self.assertEqual(client.post('/api/guidance/apply',json={'id':proposal['id']}).status_code,200)
        current=materials.read(self.root,'goals')
        materials.save(self.root,'goals',materials.MaterialInput(revision=current['revision'],content='后来编辑'))
        self.assertEqual(client.post('/api/guidance/undo',json={'id':proposal['id']}).status_code,409)
        self.assertEqual(materials.read(self.root,'goals')['content'],'后来编辑')

    def test_partial_commit_recovers_before_read_or_manual_edit(self):
        client=self.client()
        proposal=self.proposal(client)
        real=storage.atomic_write
        def fail(path,text):
            if path.name=='goals.md': raise OSError('disk')
            return real(path,text)
        with patch('app.storage.atomic_write',side_effect=fail):
            result=client.post('/api/guidance/apply',json={'id':proposal['id']})
            self.assertEqual(result.status_code,503)
            self.assertTrue(materials.transaction_path(self.root).exists())
            self.assertEqual(client.get('/api/teaching').status_code,503)
        with TestClient(create_app(self.root,self.reply)) as restarted:
            self.assertIn('合成修改',restarted.get('/api/materials/goals').json()['content'])
            self.assertIn('合成修改',restarted.get('/api/materials/teaching').json()['content'])
            self.assertFalse(materials.transaction_path(self.root).exists())
            self.assertEqual(restarted.post('/api/guidance/apply',json={'id':proposal['id']}).status_code,200)

    def test_feedback_is_explicit_and_does_not_read_other_chats(self):
        client=self.client()
        cid=client.get('/api/conversation').json()['conversation']['id']
        response=client.post('/api/chat',json={'conversation_id':cid,'message':'解释指针'})
        answer=json.loads(response.text.splitlines()[-1])['message']
        self.talk(client,'讲解太跳跃',conversation_id=cid,answer_id=answer['id'])
        self.assertIn('解释指针',self.calls[-1][-1]['content'])
        self.assertIn('AI 发言不是用户要求',self.calls[-1][-1]['content'])
        self.assertEqual(client.get(f'/api/guidance/feedback/{cid}/{uuid4()}').status_code,409)
        before=len(client.get('/api/conversation').json()['conversation']['messages'])
        self.assertEqual(before,2)

    def test_invalid_proposal_does_not_replace_valid_or_change_materials(self):
        client=self.client()
        proposal=self.proposal(client)
        original=context.prepare(self.root)
        self.invalid=True
        result=client.post('/api/guidance/propose',json={'revision':2})
        self.assertEqual(result.status_code,502)
        self.assertEqual(client.get('/api/guidance').json()['proposal']['id'],proposal['id'])
        self.assertEqual(context.prepare(self.root),original)

    def test_window_is_bounded_and_discards_partial_answers(self):
        conversation=storage.Conversation()
        for i in range(20):
            user=storage.Message(role='user',content=str(i))
            conversation.messages.extend([user,storage.Message(role='assistant',content='答',reply_to=user.id)])
        conversation.messages.append(storage.Message(role='assistant',content='残片',status='error',reply_to=user.id))
        selected=guidance.recent(conversation)
        self.assertEqual(len(selected),24)
        self.assertEqual(selected[0].content,'8')
        self.assertNotIn('残片',[m.content for m in selected])

    def test_initial_save_failure_prevents_call_and_final_failure_can_retry(self):
        client=self.client()
        with patch('app.storage.atomic_write',side_effect=OSError('disk')):
            self.assertEqual(client.post('/api/guidance/chat',json={'revision':0,'message':'问题'}).status_code,503)
        self.assertEqual(self.calls,[])
        real=storage.atomic_write
        count=0
        def fail_second(path,text):
            nonlocal count
            if path.name=='conversation.json':
                count+=1
                if count==2: raise OSError('disk')
            return real(path,text)
        with patch('app.storage.atomic_write',side_effect=fail_second):
            result=client.post('/api/guidance/chat',json={'revision':0,'message':'问题'})
            self.assertFalse(json.loads(result.text.splitlines()[-1])['saved'])
            self.assertEqual(client.post('/api/guidance/chat',json={'revision':2,'message':'继续'}).status_code,503)
        self.assertEqual(client.post('/api/guidance/save',json={}).status_code,200)

    def test_stop_blocks_concurrent_paid_calls_and_retry_retains_question(self):
        started=threading.Event()
        calls=0
        async def slow(settings,messages):
            nonlocal calls
            calls+=1
            yield '片段'
            if calls==1:
                started.set()
                await asyncio.Event().wait()
        client=self.client(slow)
        with ThreadPoolExecutor() as pool:
            future=pool.submit(client.post,'/api/guidance/chat',json={'revision':0,'message':'要求'})
            self.assertTrue(started.wait(3))
            cid=client.get('/api/conversation').json()['conversation']['id']
            self.assertEqual(client.post('/api/chat',json={'conversation_id':cid,'message':'冲突'}).status_code,409)
            self.assertEqual(client.post('/api/guidance/propose',json={'revision':2}).status_code,409)
            self.assertEqual(client.post('/api/guidance/stop',json={}).status_code,200)
            self.assertEqual(future.result(timeout=3).status_code,200)
        last=client.get('/api/guidance').json()['conversation']['messages'][-1]
        self.assertEqual(last['status'],'stopped')
        result=client.post('/api/guidance/chat',json={'revision':2,'retry_id':last['id']})
        self.assertEqual(result.status_code,200)
        self.assertEqual(len(client.get('/api/guidance').json()['conversation']['messages']),3)


if __name__=='__main__':
    unittest.main()
