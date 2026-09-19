"""Launcher checks without opening browsers or touching personal records."""

from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import launch


class LauncherTests(unittest.TestCase):
    def test_reuses_running_app(self):
        with patch.object(launch, "is_our_app", return_value=True), patch.object(launch, "open_page") as opened, patch("uvicorn.run") as server:
            self.assertEqual(launch.main(), 0)
        opened.assert_called_once()
        server.assert_not_called()

    def test_does_not_use_another_service(self):
        with patch.object(launch, "is_our_app", return_value=False), patch.object(launch.socket, "socket") as connection, patch.object(launch, "open_page") as opened:
            connection.return_value.__enter__.return_value.connect_ex.return_value = 0
            self.assertEqual(launch.main(), 1)
        opened.assert_not_called()

    def test_opens_only_after_server_is_ready(self):
        opened = threading.Event()
        with patch.object(launch, "is_our_app", side_effect=[False, True]), patch.object(launch.socket, "socket") as connection, patch.object(launch, "open_page", side_effect=opened.set), patch("uvicorn.run", side_effect=lambda *args, **kwargs: self.assertTrue(opened.wait(3))) as server:
            connection.return_value.__enter__.return_value.connect_ex.return_value = 1
            self.assertEqual(launch.main(), 0)
        server.assert_called_once()

    def test_cmd_missing_environment_shows_error(self):
        source = Path(__file__).resolve().parent.parent / "启动 Trae-Learning.cmd"
        with tempfile.TemporaryDirectory(prefix="trae launcher ") as folder:
            target = Path(folder) / source.name
            target.write_bytes(source.read_bytes())
            result = subprocess.run(["cmd.exe", "/d", "/c", str(target)], input=b"\n", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("未找到本地 Python 环境", result.stdout.decode("utf-8", errors="replace"))
