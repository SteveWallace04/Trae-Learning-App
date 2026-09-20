"""Verify faithful material delivery without personal data or paid requests."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

from app import context, model, storage
from app.main import create_app


class ContextTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "app"
        self.source = Path(directory.name) / "学习材料"
        self.original = {}
        for path, title in context.MATERIALS:
            # Preserve Chinese, literal Markdown, CRLF, and final newlines.
            raw = f"# {title}\r\n\r\n合成材料：{path}\r\n<script>throw new Error('test')</script>\r\n"
            file = self.source / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(raw.encode("utf-8"))
            self.original[path] = raw
        storage.atomic_write(self.root / "data/learning-source.json", json.dumps({"root": "../学习材料"}))
        storage.save_settings(self.root, "sk-test-only", "deepseek-flash")

    def test_originals_reach_provider_exactly_and_snapshot_survives_restart(self):
        requests = []
        real_client = httpx.AsyncClient
        def reply(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"合成回答"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        transport = httpx.MockTransport(reply)
        with patch("app.model.httpx.AsyncClient", side_effect=lambda **kw: real_client(transport=transport, **kw)):
            with TestClient(create_app(self.root)) as client:
                preview = client.get("/api/teaching").json()
                self.assertFalse((self.root / "data/contexts").exists())
                result = client.post("/api/chat", json={"conversation_id": client.get("/api/conversation").json()["conversation"]["id"], "message": "继续"})
                self.assertEqual(result.status_code, 200)
                answer = json.loads(result.text.splitlines()[-1])["message"]
                snapshot = client.get(f"/api/messages/{answer['id']}/context").json()
                self.assertEqual(preview, snapshot)
        sent = requests[0]["messages"]
        self.assertEqual(sent, [{"role": "system", "content": snapshot["system_prompt"]}, {"role": "user", "content": "继续"}])
        for material in snapshot["materials"]:
            self.assertEqual(material["content"], self.original[material["path"]])
            self.assertIn(material["content"], sent[0]["content"])
        self.assertNotIn("sk-test-only", json.dumps(snapshot))
        self.assertNotIn(str(self.source), sent[0]["content"])
        with TestClient(create_app(self.root)) as restarted:
            self.assertEqual(restarted.get(f"/api/messages/{answer['id']}/context").json(), snapshot)
        for path, raw in self.original.items():
            self.assertEqual((self.source / path).read_bytes(), raw.encode("utf-8"))

    def test_retry_reads_new_materials_but_preserves_old_snapshot(self):
        calls = []
        async def fail(settings, messages):
            calls.append(messages)
            raise model.ModelError("合成错误")
            yield
        with TestClient(create_app(self.root, fail)) as client:
            first = client.post("/api/chat", json={"conversation_id": client.get("/api/conversation").json()["conversation"]["id"], "message": "继续"})
            answer = json.loads(first.text.splitlines()[-1])["message"]
            old = client.get(f"/api/messages/{answer['id']}/context").json()
            (self.source / "progress/status.md").write_text("新的合成断点", encoding="utf-8")
            second = client.post("/api/chat", json={"conversation_id": client.get("/api/conversation").json()["conversation"]["id"], "retry_id": answer["id"]})
            retried = json.loads(second.text.splitlines()[-1])["message"]
            new = client.get(f"/api/messages/{retried['id']}/context").json()
            self.assertNotEqual(answer["context_id"], retried["context_id"])
            self.assertEqual(client.get(f"/api/messages/{answer['id']}/context").json(), old)
            self.assertIn("新的合成断点", new["system_prompt"])
            self.assertEqual(calls[1][0]["content"], new["system_prompt"])
            self.assertEqual(calls[1][1:], [{"role": "user", "content": "继续"}])

    def test_bad_config_missing_empty_or_invalid_material_blocks_request(self):
        calls = []
        async def track(settings, messages):
            calls.append(True)
            yield "不应调用"
        with TestClient(create_app(self.root, track)) as client:
            path = self.source / "core/teaching.md"
            for data in (None, b"  \n", b"\xff"):
                with self.subTest(data=data):
                    if data is None:
                        path.unlink()
                    else:
                        path.write_bytes(data)
                    self.assertEqual(client.get("/api/teaching").status_code, 503)
                    self.assertEqual(client.post("/api/chat", json={"conversation_id": client.get("/api/conversation").json()["conversation"]["id"], "message": "继续"}).status_code, 503)
            (self.root / "data/learning-source.json").write_text("not json")
            self.assertEqual(client.post("/api/chat", json={"conversation_id": client.get("/api/conversation").json()["conversation"]["id"], "message": "继续"}).status_code, 503)
            self.assertEqual(client.get("/api/conversation").json()["conversation"]["messages"], [])
            self.assertEqual(calls, [])

    def test_snapshot_save_failure_prevents_call_and_message_write(self):
        calls = []
        async def track(settings, messages):
            calls.append(True)
            yield "不应调用"
        with TestClient(create_app(self.root, track)) as client:
            with patch("app.context.atomic_write", side_effect=OSError("disk full")):
                self.assertEqual(client.post("/api/chat", json={"conversation_id": client.get("/api/conversation").json()["conversation"]["id"], "message": "继续"}).status_code, 503)
            self.assertEqual(calls, [])
            self.assertEqual(client.get("/api/conversation").json()["conversation"]["messages"], [])

    def test_old_messages_have_no_invented_snapshot_and_basic_mode_is_explicit(self):
        (self.root / "data/learning-source.json").unlink()
        user = storage.Message(role="user", content="旧问题")
        answer = storage.Message(role="assistant", content="旧回答", reply_to=user.id)
        storage.save_conversation(self.root, storage.Conversation(messages=[user, answer]))
        with TestClient(create_app(self.root)) as client:
            self.assertEqual(client.get("/api/teaching").json()["mode"], "basic")
            self.assertEqual(client.get(f"/api/messages/{answer.id}/context").status_code, 404)
            self.assertEqual(client.get("/api/messages/not-a-message/context").status_code, 422)
            self.assertEqual(client.get("/api/teaching", headers={"Origin": "https://other.example"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
