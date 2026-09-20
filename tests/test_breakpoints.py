"""Breakpoint draft/save behavior; synthetic evidence, no paid calls or personal files."""

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

from app import breakpoints, context, model, storage
from app.main import create_app

DRAFT = "\n\n".join("## " + heading + "\n合成断点内容" for heading in breakpoints.HEADINGS)


class BreakpointTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "learning"
        self.originals = {}
        for path, title in context.MATERIALS:
            text = f"# {title}\n原项目合成材料：{path}"
            storage.atomic_write(self.source / path, text)
            self.originals[path] = (self.source / path).read_bytes()
        storage.atomic_write(self.root / "data/learning-source.json", '{"root":"learning"}')
        storage.save_settings(self.root, "sk-synthetic", "deepseek-flash")
        question = storage.Message(role="user", content="我的理解是 next 保存下一个节点的地址")
        answer = storage.Message(role="assistant", content="老师讲过不等于学生掌握", reply_to=question.id, status="stopped")
        self.conversation = storage.Conversation(messages=[question, answer])
        storage.save_conversation(self.root, self.conversation)
        self.calls = []

    async def reply(self, settings, messages):
        self.calls.append(messages)
        yield DRAFT

    def generate(self, client):
        return client.post("/api/breakpoint/draft", json={"conversation_id": str(self.conversation.id)})

    def save(self, client, draft, text="用户核对并修改的断点"):
        return client.post("/api/breakpoint", json={"conversation_id": draft["conversation_id"], "draft_id": draft["id"], "content": text})

    def test_generate_edit_save_restart_and_source_evidence(self):
        original_chat = (self.root / f"data/conversations/session-{self.conversation.id}.json").read_bytes()
        with TestClient(create_app(self.root, self.reply)) as client:
            self.assertIsNone(client.get("/api/breakpoint").json()["record"])
            draft_response = self.generate(client)
            self.assertEqual(draft_response.status_code, 200)
            draft = draft_response.json()
            self.assertIsNone(client.get("/api/breakpoint").json()["record"])
            self.assertFalse((self.root / "data/computer-breakpoint.json").exists())
            payload = json.loads(self.calls[0][-1]["content"])
            self.assertIn("progress/status.md", payload["baseline"])
            self.assertEqual(payload["messages"][0]["content"], self.conversation.messages[0].content)
            self.assertEqual(payload["messages"][1]["status"], "stopped")
            self.assertIn("core/learner-model.md", self.calls[0][0]["content"])
            self.assertNotIn("sk-synthetic", json.dumps(self.calls))
            saved_response = self.save(client, draft)
            self.assertEqual(saved_response.status_code, 200)
            saved = saved_response.json()
            self.assertEqual(saved["content"], "用户核对并修改的断点")
            self.assertEqual(saved["through_message_id"], str(self.conversation.messages[-1].id))
            source = client.get("/api/breakpoint/source").json()
            self.assertEqual(source["messages"], self.conversation.model_dump(mode="json")["messages"])
            self.assertEqual(self.save(client, draft).json(), saved)
            self.assertEqual(len(self.calls), 1)  # Save and repeated save never call the model.
        with TestClient(create_app(self.root, self.reply)) as restarted:
            self.assertEqual(restarted.get("/api/breakpoint").json(), {"record": saved, "new_messages": 0})
        self.assertEqual((self.root / f"data/conversations/session-{self.conversation.id}.json").read_bytes(), original_chat)
        for path, data in self.originals.items():
            self.assertEqual((self.source / path).read_bytes(), data)

    def test_later_draft_uses_saved_breakpoint_and_new_message_count_is_visible(self):
        with TestClient(create_app(self.root, self.reply)) as client:
            self.save(client, self.generate(client).json(), "用户修正后的理解")
            response = client.post("/api/chat", json={"conversation_id": str(self.conversation.id), "message": "再问一个问题"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get("/api/breakpoint").json()["new_messages"], 2)
            self.generate(client)
            data = json.loads(self.calls[-1][-1]["content"])
            self.assertEqual(data["baseline"], "用户修正后的理解")
            self.assertEqual(len(data["messages"]), 4)
            self.assertEqual(len(client.get("/api/breakpoint/source").json()["messages"]), 2)

    def test_failed_or_malformed_generation_keeps_old_record_and_allows_retry(self):
        mode = "good"
        async def reply(settings, messages):
            if mode == "error":
                yield DRAFT[:20]
                raise model.ModelError("合成中断")
            yield DRAFT if mode == "good" else "不是完整的断点"
        with TestClient(create_app(self.root, reply)) as client:
            self.save(client, self.generate(client).json())
            path = self.root / "data/computer-breakpoint.json"
            before = path.read_bytes()
            for mode in ("error", "malformed"):
                self.assertEqual(self.generate(client).status_code, 502)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(client.get("/api/conversation").json()["active"])
            mode = "good"
            self.assertEqual(self.generate(client).status_code, 200)

    def test_atomic_save_failure_keeps_old_record_and_same_draft_can_retry(self):
        with TestClient(create_app(self.root, self.reply)) as client:
            self.save(client, self.generate(client).json(), "旧断点")
            path = self.root / "data/computer-breakpoint.json"
            before = path.read_bytes()
            draft = self.generate(client).json()
            with patch("app.storage.os.replace", side_effect=OSError("disk full")):
                self.assertEqual(self.save(client, draft, "新断点").status_code, 503)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(self.save(client, draft, "新断点").json()["content"], "新断点")

    def test_stale_draft_new_messages_and_changed_record_cannot_silently_overwrite(self):
        with TestClient(create_app(self.root, self.reply)) as client:
            first = self.generate(client).json()
            second = self.generate(client).json()
            self.assertEqual(self.save(client, first).status_code, 409)
            self.save(client, second, "已保存")
            third = self.generate(client).json()
            client.post("/api/chat", json={"conversation_id": str(self.conversation.id), "message": "新消息"})
            self.assertEqual(self.save(client, third).status_code, 409)
            fourth = self.generate(client).json()
            record = breakpoints.load(self.root)
            record.id = uuid4()
            record.content = "另外修改的记录"
            breakpoints.save(self.root, record)
            self.assertEqual(self.save(client, fourth).status_code, 409)
            self.assertEqual(breakpoints.load(self.root).content, "另外修改的记录")

    def test_other_conversation_cannot_save_draft_and_chat_does_not_auto_update(self):
        with TestClient(create_app(self.root, self.reply)) as client:
            draft = self.generate(client).json()
            self.save(client, draft, "原断点")
            path = self.root / "data/computer-breakpoint.json"
            before = path.read_bytes()
            new = client.post("/api/conversations", json={"conversation_id": str(self.conversation.id)}).json()["conversation"]
            self.assertEqual(self.save(client, draft).status_code, 409)
            client.post("/api/chat", json={"conversation_id": new["id"], "message": "闲聊其他主题"})
            self.assertEqual(path.read_bytes(), before)
            self.assertIsNone(client.get("/api/breakpoint").json()["new_messages"])

    def test_busy_generation_blocks_other_model_calls_switch_and_save(self):
        started = threading.Event()
        cancelled = threading.Event()
        async def slow(settings, messages):
            started.set()
            try:
                await asyncio.sleep(0.3)
                yield DRAFT
            finally:
                cancelled.set()
        with TestClient(create_app(self.root, slow)) as client:
            body = {"conversation_id": str(self.conversation.id)}
            with ThreadPoolExecutor() as pool:
                future = pool.submit(self.generate, client)
                self.assertTrue(started.wait(5))
                self.assertEqual(client.get("/api/conversation").json()["activity"], "breakpoint")
                for path, data in (
                    ("/api/chat", {**body, "message": "不得并发"}),
                    ("/api/breakpoint/draft", body),
                    ("/api/conversations", body),
                    ("/api/settings", {"model": "deepseek-flash"}),
                    ("/api/breakpoint", {**body, "draft_id": str(uuid4()), "content": "不得保存"}),
                ):
                    self.assertEqual(client.post(path, json=data).status_code, 409)
                self.assertEqual(future.result(5).status_code, 200)
                self.assertTrue(cancelled.is_set())
            self.assertFalse(client.get("/api/conversation").json()["active"])

    def test_missing_material_key_empty_chat_and_invalid_save_do_not_call_model(self):
        with TestClient(create_app(self.root, self.reply)) as client:
            self.assertEqual(self.save(client, {"conversation_id": str(self.conversation.id), "id": str(uuid4())}).status_code, 409)
            (self.source / "core/teaching.md").unlink()
            self.assertEqual(self.generate(client).status_code, 503)
            (self.root / ".env").unlink()
            self.assertEqual(self.generate(client).status_code, 400)
            new = client.post("/api/conversations", json={"conversation_id": str(self.conversation.id)}).json()["conversation"]
            self.assertEqual(client.post("/api/breakpoint/draft", json={"conversation_id": new["id"]}).status_code, 400)
            self.assertEqual(self.calls, [])

    def test_corrupt_saved_record_is_not_overwritten_and_blank_edit_is_rejected(self):
        with TestClient(create_app(self.root, self.reply)) as client:
            draft = self.generate(client).json()
            self.assertEqual(self.save(client, draft, " \n ").status_code, 400)
            path = self.root / "data/computer-breakpoint.json"
            path.write_text("broken", encoding="utf-8")
            self.assertEqual(client.get("/api/breakpoint").status_code, 503)
            self.assertEqual(self.save(client, draft).status_code, 503)
            self.assertEqual(self.generate(client).status_code, 503)
            self.assertEqual(path.read_text(), "broken")

    def test_restart_does_not_turn_unsaved_draft_into_saved_record(self):
        with TestClient(create_app(self.root, self.reply)) as client:
            draft = self.generate(client).json()
        with TestClient(create_app(self.root, self.reply)) as restarted:
            self.assertIsNone(restarted.get("/api/breakpoint").json()["record"])
            self.assertEqual(self.save(restarted, draft).status_code, 409)
