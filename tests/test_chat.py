"""Behavior checks with temporary data and synthetic model responses; never paid calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

from app.main import create_app
from app import model, storage


def events(response):
    assert response.status_code == 200, response.text
    return [json.loads(line) for line in response.text.splitlines() if line]


async def success(settings, messages):
    yield "这是"
    yield "回答。"


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        storage.save_settings(self.root, "sk-test-only", "deepseek-flash")

    def client(self, stream=success):
        client = TestClient(create_app(self.root, stream))
        self.addCleanup(client.__exit__, None, None, None)
        return client.__enter__()

    def test_settings_do_not_return_key_and_blank_preserves_it(self):
        client = self.client()
        result = client.post("/api/settings", json={"api_key": "", "model": "deepseek-v4-pro"})
        self.assertTrue(result.json()["configured"])
        self.assertNotIn("sk-test-only", result.text)
        self.assertEqual(storage.read_settings(self.root)["api_key"], "sk-test-only")
        bad = client.post("/api/settings", json={"api_key": "secret\nINJECT=1", "model": "deepseek-flash"})
        self.assertEqual(bad.status_code, 422)
        self.assertNotIn("secret", bad.text)
        self.assertEqual(storage.read_settings(self.root)["model"], "deepseek-v4-pro")

    def test_missing_key_does_not_save_or_call_model(self):
        (self.root / ".env").unlink()
        client = self.client()
        response = client.post("/api/chat", json={"message": "问题"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse((self.root / "data").exists())

    def test_chat_is_saved_and_restored_by_a_new_app(self):
        with TestClient(create_app(self.root, success)) as client:
            result = events(client.post("/api/chat", json={"message": "什么是指针？"}))
            self.assertEqual(result[-1]["message"]["content"], "这是回答。")
            self.assertTrue(result[-1]["saved"])
        with TestClient(create_app(self.root, success)) as restarted:
            messages = restarted.get("/api/conversation").json()["conversation"]["messages"]
            self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
            self.assertEqual(messages[-1]["status"], "complete")
            self.assertEqual(messages[-1]["reply_to"], messages[0]["id"])
        saved = next((self.root / "data/conversations").glob("*.json")).read_text(encoding="utf-8")
        self.assertNotIn("sk-test-only", saved)

    def test_retry_reuses_question_and_omits_partial_answer(self):
        calls = []
        async def fail_once(settings, messages):
            calls.append(messages)
            if len(calls) == 1:
                yield "未完成"
                raise model.ModelError("模拟网络错误")
            yield "完整回答"
        client = self.client(fail_once)
        first = events(client.post("/api/chat", json={"message": "解释指针"}))
        failed = first[-1]["message"]
        self.assertEqual(failed["status"], "error")
        second = events(client.post("/api/chat", json={"retry_id": failed["id"]}))
        self.assertEqual(second[-1]["message"]["status"], "complete")
        self.assertEqual(calls[0], calls[1])
        messages = client.get("/api/conversation").json()["conversation"]["messages"]
        self.assertEqual(sum(m["role"] == "user" for m in messages), 1)
        self.assertEqual(len(messages), 3)
        self.assertEqual(client.post("/api/chat", json={"retry_id": failed["id"]}).status_code, 409)
        client.post("/api/chat", json={"message": "继续"})
        self.assertEqual(calls[-1], [
            {"role": "user", "content": "解释指针"},
            {"role": "assistant", "content": "完整回答"},
            {"role": "user", "content": "继续"},
        ])

    def test_save_failure_before_request_never_calls_model(self):
        called = []
        async def track(settings, messages):
            called.append(True)
            yield "不应执行"
        client = self.client(track)
        with patch("app.storage.save_conversation", side_effect=OSError("disk full")):
            response = client.post("/api/chat", json={"message": "问题"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(called, [])
        self.assertEqual(client.get("/api/conversation").json()["conversation"]["messages"], [])

    def test_final_save_failure_blocks_next_request_and_can_be_retried(self):
        client = self.client()
        real_save = storage.save_conversation
        count = 0
        def fail_second(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("disk full")
            return real_save(*args)
        with patch("app.storage.save_conversation", side_effect=fail_second):
            result = events(client.post("/api/chat", json={"message": "问题"}))
        self.assertFalse(result[-1]["saved"])
        self.assertEqual(client.post("/api/chat", json={"message": "下一个"}).status_code, 503)
        self.assertEqual(client.post("/api/conversation/save").status_code, 200)
        self.assertFalse(client.get("/api/conversation").json()["save_error"])
        self.assertEqual(storage.load_conversation(self.root).messages[-1].content, "这是回答。")

    def test_stop_and_concurrent_send(self):
        started = threading.Event()
        async def slow(settings, messages):
            yield "部分文字"
            started.set()
            await asyncio.Event().wait()
        client = self.client(slow)
        with ThreadPoolExecutor() as executor:
            future = executor.submit(client.post, "/api/chat", json={"message": "测试停止"})
            self.assertTrue(started.wait(timeout=5))
            state = client.get("/api/conversation").json()
            self.assertTrue(state["active"])
            self.assertEqual(client.post("/api/chat", json={"message": "重复"}).status_code, 409)
            self.assertEqual(client.post("/api/settings", json={"model": "deepseek-flash"}).status_code, 409)
            reply_id = state["conversation"]["messages"][-1]["id"]
            response = client.post("/api/chat/stop", json={"reply_id": reply_id})
            self.assertEqual(response.status_code, 200)
            result = events(future.result(timeout=5))
            self.assertEqual(result[-1]["message"]["status"], "stopped")
            self.assertEqual(result[-1]["message"]["content"], "部分文字")
            self.assertFalse(client.get("/api/conversation").json()["active"])

    def test_restart_marks_unfinished_record(self):
        question = storage.Message(role="user", content="问题")
        answer = storage.Message(role="assistant", status="streaming", reply_to=question.id)
        storage.save_conversation(self.root, storage.Conversation(messages=[question, answer]))
        client = self.client()
        self.assertEqual(client.get("/api/conversation").json()["conversation"]["messages"][-1]["status"], "interrupted")
        self.assertEqual(storage.load_conversation(self.root).messages[-1].status, "interrupted")

    def test_foreign_site_and_host_cannot_trigger_calls(self):
        client = self.client()
        response = client.post("/api/chat", json={"message": "问题"}, headers={"Origin": "https://other.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(client.get("/api/settings", headers={"Host": "other.example"}).status_code, 400)
        self.assertEqual(client.post("/api/settings", json={"model": "deepseek-flash"}, headers={"Origin": "http://testserver"}).status_code, 200)

    def test_atomic_replace_failure_preserves_old_file(self):
        path = self.root / "record.json"
        storage.atomic_write(path, "old")
        with patch("app.storage.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                storage.atomic_write(path, "new")
        self.assertEqual(path.read_text(), "old")


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, response):
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: response)
        with patch("app.model.httpx.AsyncClient", side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)):
            return [text async for text in model.stream_reply({"api_key": "sk-synthetic", "model": "deepseek-flash"}, [{"role": "user", "content": "问题"}])]

    async def test_stream_requires_valid_completion(self):
        chunk = 'data: {"choices":[{"delta":{"content":"你好"},"finish_reason":null}]}\n\n'
        done = 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        self.assertEqual(await self.collect(httpx.Response(200, text=chunk + done)), ["你好"])
        with self.assertRaises(model.ModelError):
            await self.collect(httpx.Response(200, text=chunk))
        with self.assertRaises(model.ModelError):
            await self.collect(httpx.Response(200, text='data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'))

    async def test_auth_errors_do_not_echo_provider_body(self):
        with self.assertRaises(model.ModelError) as caught:
            await self.collect(httpx.Response(401, text="secret provider response sk-synthetic"))
        self.assertIn("密钥", str(caught.exception))
        self.assertNotIn("sk-synthetic", str(caught.exception))

    async def test_invalid_stream_is_reported(self):
        with self.assertRaises(model.ModelError):
            await self.collect(httpx.Response(200, text="data: not-json\n\n"))


if __name__ == "__main__":
    unittest.main()
