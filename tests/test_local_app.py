"""Start a real local server and check public routes without any model calls."""

import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import httpx

ROOT = Path(__file__).resolve().parent.parent


class LocalAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        cls.base_url = f"http://127.0.0.1:{port}"
        directory = tempfile.TemporaryDirectory(prefix="trae-http-test-")
        cls.addClassCleanup(directory.cleanup)
        script = """
import asyncio
from pathlib import Path
import sys, uvicorn
from app.main import create_app
from app import storage
root = Path(sys.argv[1])
storage.save_settings(root, 'sk-test-only', 'deepseek-flash')
async def slow(settings, messages):
    yield 'partial test answer'
    await asyncio.Event().wait()
uvicorn.run(create_app(root, slow), host='127.0.0.1', port=int(sys.argv[2]))
"""
        process = subprocess.Popen(
            [sys.executable, "-c", script, directory.name, str(port)],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def stop_server():
            process.terminate()
            process.wait(timeout=5)

        cls.addClassCleanup(stop_server)
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("Test server exited before startup")
            try:
                with urlopen(cls.base_url + "/api/health", timeout=1):
                    return
            except URLError:
                time.sleep(0.1)
        raise RuntimeError("Test server did not start")

    def test_page_and_assets_are_served(self):
        with urlopen(self.base_url + "/") as response:
            self.assertIn("text/html", response.headers["Content-Type"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIn("尚未配置 DeepSeek", response.read().decode("utf-8"))
        for asset in ("style.css", "app.js"):
            with self.subTest(asset=asset), urlopen(self.base_url + "/static/" + asset) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertTrue(response.read())

    def test_health_endpoint(self):
        with urlopen(self.base_url + "/api/health") as response:
            self.assertEqual(json.load(response), {"status": "ok"})

    def test_disconnecting_browser_stops_generation_and_saves_partial_text(self):
        with httpx.Client() as client:
            with client.stream("POST", self.base_url + "/api/chat", json={"message": "disconnect test"}) as response:
                lines = response.iter_lines()
                self.assertEqual(json.loads(next(lines))["type"], "start")
                self.assertEqual(json.loads(next(lines))["type"], "delta")
            for _ in range(50):
                state = client.get(self.base_url + "/api/conversation").json()
                if not state["active"]:
                    break
                time.sleep(0.1)
        self.assertFalse(state["active"])
        self.assertEqual(state["conversation"]["messages"][-1]["status"], "stopped")
        self.assertEqual(state["conversation"]["messages"][-1]["content"], "partial test answer")

    def test_project_files_are_not_public(self):
        for path in ("/.env", "/data/.gitkeep", "/AGENTS.md", "/static/../AGENTS.md", "/static/%2e%2e/AGENTS.md"):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(self.base_url + path)
                self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
