"""Conversation isolation and navigation, using temporary files and fake replies."""

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

from app.main import create_app
from app import storage


class ConversationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        storage.save_settings(self.root, "sk-synthetic", "deepseek-flash")
        self.calls = []

    async def reply(self, settings, messages):
        self.calls.append(messages)
        yield "合成回答"

    def current(self, client):
        return client.get("/api/conversation").json()["conversation"]

    def send(self, client, text):
        return client.post("/api/chat", json={"conversation_id": self.current(client)["id"], "message": text})

    def new(self, client):
        return client.post("/api/conversations", json={"conversation_id": self.current(client)["id"]})

    def select(self, client, identifier):
        return client.post(f"/api/conversations/{identifier}/select", json={"conversation_id": self.current(client)["id"]})

    def test_isolated_context_history_and_last_selection_survive_restart(self):
        with TestClient(create_app(self.root, self.reply)) as client:
            self.assertEqual(self.send(client, "指针的问题").status_code, 200)
            old = self.current(client)
            old_file = self.root / "data/conversations" / f"session-{old['id']}.json"
            old_bytes = old_file.read_bytes()
            new = self.new(client).json()["conversation"]
            self.assertNotEqual(old["id"], new["id"])
            self.assertEqual(new["messages"], [])
            self.assertEqual(old_file.read_bytes(), old_bytes)
            self.assertEqual(self.send(client, "概率的问题").status_code, 200)
            self.assertEqual(self.calls[-1][1:], [{"role": "user", "content": "概率的问题"}])
            self.assertEqual(self.calls[0][0], self.calls[1][0])
            items = client.get("/api/conversations").json()["conversations"]
            self.assertEqual({i["title"] for i in items}, {"指针的问题", "概率的问题"})
            self.assertEqual(self.select(client, old["id"]).json()["conversation"], old)
            answer = old["messages"][-1]
            self.assertEqual(client.get(f"/api/messages/{answer['id']}/context").status_code, 200)
        with TestClient(create_app(self.root, self.reply)) as restarted:
            self.assertEqual(self.current(restarted), old)
            self.send(restarted, "继续指针")
            self.assertEqual([m["content"] for m in self.calls[-1][1:]], ["指针的问题", "合成回答", "继续指针"])

    def test_empty_conversation_is_reused_and_needs_no_model_key(self):
        (self.root / ".env").unlink()
        with TestClient(create_app(self.root, self.reply)) as client:
            original = self.current(client)
            self.assertEqual(self.new(client).json()["conversation"], original)
            self.assertEqual(self.new(client).json()["conversation"], original)
            self.assertEqual(len(client.get("/api/conversations").json()["conversations"]), 1)
            self.assertEqual(self.calls, [])
        with TestClient(create_app(self.root, self.reply)) as restarted:
            self.assertEqual(self.current(restarted), original)

    def test_stale_window_cannot_send_retry_save_or_change_selection(self):
        with TestClient(create_app(self.root, self.reply)) as client:
            self.send(client, "旧窗口")
            old = self.current(client)
            new = self.new(client).json()["conversation"]
            stale = {"conversation_id": old["id"]}
            for path, body in (
                ("/api/chat", {**stale, "message": "不得串入新会话"}),
                ("/api/chat", {**stale, "retry_id": old["messages"][-1]["id"]}),
                ("/api/conversation/save", stale),
                ("/api/conversations", stale),
                (f"/api/conversations/{old['id']}/select", stale),
            ):
                with self.subTest(path=path):
                    self.assertEqual(client.post(path, json=body).status_code, 409)
            self.assertEqual(self.current(client), new)
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(client.post("/api/chat", json={"message": "旧版页面"}).status_code, 422)
            self.assertEqual(self.select(client, str(uuid4())).status_code, 404)

    def test_switching_is_blocked_during_generation_and_unsaved_answer(self):
        started = threading.Event()
        async def slow(settings, messages):
            yield "部分回答"
            started.set()
            await asyncio.Event().wait()
        with TestClient(create_app(self.root, slow)) as client:
            old = self.current(client)["id"]
            with ThreadPoolExecutor() as pool:
                future = pool.submit(self.send, client, "正在生成")
                self.assertTrue(started.wait(5))
                self.assertEqual(self.new(client).status_code, 409)
                self.assertEqual(self.select(client, old).status_code, 409)
                answer = self.current(client)["messages"][-1]
                with patch("app.storage.save_conversation", side_effect=OSError("disk full")):
                    client.post("/api/chat/stop", json={"reply_id": answer["id"]})
                    self.assertEqual(future.result(5).status_code, 200)
                self.assertEqual(self.new(client).status_code, 503)
                self.assertEqual(self.select(client, old).status_code, 503)
                self.assertEqual(client.post("/api/conversation/save", json={"conversation_id": old}).status_code, 200)
                self.assertEqual(self.new(client).status_code, 200)

    def test_failed_selection_keeps_old_conversation_in_memory_and_after_restart(self):
        with TestClient(create_app(self.root, self.reply)) as client:
            self.send(client, "原对话")
            old = self.current(client)
            other = self.new(client).json()["conversation"]
            self.select(client, old["id"])
            with patch("app.storage.select_conversation", side_effect=OSError("disk full")):
                self.assertEqual(self.select(client, other["id"]).status_code, 503)
                self.assertEqual(self.new(client).status_code, 503)
            self.assertEqual(self.current(client), old)
        with TestClient(create_app(self.root, self.reply)) as restarted:
            self.assertEqual(self.current(restarted), old)

    def test_failed_new_file_preserves_legacy_selection(self):
        with TestClient(create_app(self.root, self.reply)) as client:
            self.send(client, "旧版已有聊天")
            old = self.current(client)
            with patch("app.storage.save_conversation", side_effect=OSError("disk full")):
                self.assertEqual(self.new(client).status_code, 503)
            self.assertEqual(self.current(client), old)
        with TestClient(create_app(self.root, self.reply)) as restarted:
            self.assertEqual(self.current(restarted), old)

    def test_opening_inactive_interrupted_conversation_recovers_it(self):
        question = storage.Message(role="user", content="旧中断问题")
        answer = storage.Message(role="assistant", status="streaming", reply_to=question.id)
        interrupted = storage.Conversation(messages=[question, answer])
        storage.save_conversation(self.root, interrupted)
        other = storage.Conversation()
        storage.save_conversation(self.root, other)
        storage.select_conversation(self.root, other.id)
        with TestClient(create_app(self.root, self.reply)) as client:
            result = self.select(client, str(interrupted.id)).json()["conversation"]
            self.assertEqual(result["messages"][-1]["status"], "interrupted")
            retry = client.post("/api/chat", json={"conversation_id": result["id"], "retry_id": str(answer.id)})
            self.assertEqual(json.loads(retry.text.splitlines()[-1])["message"]["status"], "complete")

    def test_new_file_saved_but_selection_failure_does_not_replace_previous_selection(self):
        with TestClient(create_app(self.root, self.reply)) as client:
            self.send(client, "原聊天")
            old = self.current(client)
            real_select = storage.select_conversation
            def fail_new(root, identifier):
                if str(identifier) != old["id"]:
                    raise OSError("disk full")
                real_select(root, identifier)
            with patch("app.storage.select_conversation", side_effect=fail_new):
                self.assertEqual(self.new(client).status_code, 503)
            self.assertEqual(self.current(client), old)
        with TestClient(create_app(self.root, self.reply)) as restarted:
            self.assertEqual(self.current(restarted), old)
