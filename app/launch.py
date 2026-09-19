"""Start the local service in this console and open its page when ready."""

import json
import socket
import threading
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
import webbrowser

URL = "http://127.0.0.1:8000"


def is_our_app():
    try:
        # Local checks should not pass through a configured HTTP proxy.
        with build_opener(ProxyHandler({})).open(URL + "/api/health", timeout=1) as response:
            return json.load(response) == {"status": "ok", "app": "trae-learning"}
    except (OSError, URLError, ValueError):
        return False


def open_page():
    try:
        if webbrowser.open(URL):
            return
    except webbrowser.Error:
        pass
    print(f"无法自动打开浏览器，请手动打开 {URL}", flush=True)


def main():
    if is_our_app():
        print("Trae-Learning 已在运行，正在打开网页。", flush=True)
        open_page()
        return 0

    with socket.socket() as connection:
        connection.settimeout(1)
        if connection.connect_ex(("127.0.0.1", 8000)) == 0:
            print("端口 8000 已被其他程序或旧版本服务占用。请先关闭对应程序再重试。", flush=True)
            return 1

    import uvicorn
    from app.main import app

    finished = threading.Event()

    def open_when_ready():
        for _ in range(50):
            if finished.is_set():
                return
            if is_our_app():
                open_page()
                return
            if finished.wait(0.2):
                return
        print(f"网页未能及时打开，请查看此窗口的启动信息，或手动打开 {URL}", flush=True)

    print("正在启动 Trae-Learning，准备好后自动打开浏览器。", flush=True)
    print("使用时保留此窗口；退出时按 Ctrl+C 或关闭窗口。", flush=True)
    threading.Thread(target=open_when_ready, daemon=True).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=8000)
    finally:
        finished.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
