"""Start a real local server and check public routes without any model calls."""

import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent


class LocalAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        cls.base_url = f"http://127.0.0.1:{port}"
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
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
            self.assertIn("尚未连接 DeepSeek", response.read().decode("utf-8"))
        for asset in ("style.css", "app.js"):
            with self.subTest(asset=asset), urlopen(self.base_url + "/static/" + asset) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.read())

    def test_health_endpoint(self):
        with urlopen(self.base_url + "/api/health") as response:
            self.assertEqual(json.load(response), {"status": "ok"})

    def test_project_files_are_not_public(self):
        for path in ("/.env", "/data/.gitkeep", "/AGENTS.md", "/static/../AGENTS.md", "/static/%2e%2e/AGENTS.md"):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(self.base_url + path)
                self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
