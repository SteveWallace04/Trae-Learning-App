"""Single-file local C/Java practice. No model calls or learning judgments."""
import asyncio
from datetime import datetime, timezone
import json
import locale
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app import storage

OUTPUT_LIMIT = 16_384  # Combined bytes per compile/run stage.
TEMPLATES = {
    "c": '#include <stdio.h>\n\nint main(void) {\n    printf("Hello!\\n");\n    return 0;\n}\n',
    "java": 'public class Main {\n    public static void main(String[] args) {\n        System.out.println("Hello!");\n    }\n}\n',
}


class CodeDraft(BaseModel):
    code: str = Field(default="", max_length=20_000)
    stdin: str = Field(default="", max_length=8_000)


class Drafts(BaseModel):
    revision: int = Field(default=0, ge=0)
    language: Literal["c", "java"] = "c"
    c: CodeDraft = Field(default_factory=lambda: CodeDraft(code=TEMPLATES["c"]))
    java: CodeDraft = Field(default_factory=lambda: CodeDraft(code=TEMPLATES["java"]))


class SaveDrafts(Drafts):
    conversation_id: UUID


class RunInput(CodeDraft):
    conversation_id: UUID
    language: Literal["c", "java"]


class StopInput(BaseModel):
    conversation_id: UUID
    run_id: UUID


def draft_path(root, conversation_id):
    return root / "data/practice" / f"{conversation_id}.json"


def load_drafts(root, conversation_id):
    path = draft_path(root, conversation_id)
    if not path.exists():
        return Drafts()
    try:
        return Drafts.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise HTTPException(503, "练习草稿文件格式异常，未覆盖文件。请保留页面内容并检查本地记录。") from exc


def tools_available():
    # Resolve trusted tool names, never arbitrary commands supplied by the page.
    gcc, javac = shutil.which("gcc"), shutil.which("javac")
    java = str(Path(javac).with_name("java.exe")) if javac else None
    if java and not Path(java).is_file():
        java = shutil.which("java")
    return {"c": [gcc] if gcc else None, "java": [javac, java] if javac and java else None}


def decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getencoding(), errors="replace")


def execute_stage(command, directory, stdin, timeout, stopped):
    from app.process_job import ProcessJob
    job = ProcessJob()
    process = None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limit = threading.Event()
    lock = threading.Lock()
    readers = []
    status = "complete"
    started = time.monotonic()

    def read_output(pipe, name):
        try:
            while chunk := pipe.read1(4096):
                with lock:
                    remaining = OUTPUT_LIMIT - sum(len(part) for part in buffers.values())
                    buffers[name].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        limit.set()
        finally:
            pipe.close()

    def feed():
        try:
            payload = json.dumps({"command": command, "stdin": stdin}, ensure_ascii=True) + "\n"
            process.stdin.write(payload.encode("utf-8"))
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    writer = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__).with_name("practice_worker.py"))],
            cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        # The helper cannot start learner code until the job owns its process tree.
        job.assign(process.pid)
        for name in buffers:
            thread = threading.Thread(target=read_output, args=(getattr(process, name), name), daemon=True)
            thread.start()
            readers.append(thread)
        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        while process.poll() is None:
            if stopped.is_set():
                status = "stopped"
                break
            if limit.is_set():
                status = "output_limit"
                break
            if time.monotonic() - started >= timeout:
                status = "timeout"
                break
            stopped.wait(0.03)
    finally:
        job.close()  # Also ends descendants after their parent has already exited.
        if process:
            if process.poll() is None:
                process.kill()
            process.wait()
            if writer:
                writer.join()
            else:
                process.stdin.close()
            for thread in readers:
                thread.join()
            if not readers:
                process.stdout.close()
                process.stderr.close()
    if status == "complete" and limit.is_set():
        status = "output_limit"
    return {"command": command, "status": status, "exit_code": process.returncode,
            "stdout": decode(buffers["stdout"]), "stderr": decode(buffers["stderr"]),
            "elapsed_ms": round((time.monotonic() - started) * 1000)}


def execute(body, tools, stopped):
    result = {"id": str(uuid4()), "conversation_id": str(body.conversation_id),
              "language": body.language, "code": body.code, "stdin": body.stdin,
              "started_at": datetime.now(timezone.utc).isoformat(), "stages": [], "status": "stopped"}
    try:
        with tempfile.TemporaryDirectory(prefix="trae-practice-") as name:
            directory = Path(name)
            filename = "main.c" if body.language == "c" else "Main.java"
            (directory / filename).write_text(body.code, encoding="utf-8")
            if body.language == "c":
                compile_command = [tools[0], "-std=c17", "-Wall", "-Wextra", "-finput-charset=UTF-8", "-fexec-charset=UTF-8", "main.c", "-o", "main.exe"]
                run_command = [str(directory / "main.exe")]
            else:
                compile_command = [tools[0], "-encoding", "UTF-8", "-proc:none", "Main.java"]
                run_command = [tools[1], "-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8", "-cp", str(directory), "Main"]
            for phase, command, timeout, stdin in (
                ("compile", compile_command, 30, ""), ("run", run_command, 10, body.stdin)
            ):
                if stopped.is_set():
                    result["status"] = "stopped"
                    break
                stage = execute_stage(command, directory, stdin, timeout, stopped)
                stage["phase"] = phase
                result["stages"].append(stage)
                result["status"] = stage["status"]
                if stage["status"] != "complete":
                    break
                if stage["exit_code"] != 0:
                    result["status"] = "compile_error" if phase == "compile" else "runtime_error"
                    break
    except Exception as exc:
        result["status"] = "error"
        result["error"] = f"本地运行失败：{exc}"
    return result


class Practice:
    def __init__(self, app, root, require_current):
        self.task = None
        self.active = None
        self.results = {}
        self.stopped = threading.Event()

        @app.get("/api/practice/{conversation_id}")
        async def get_practice(conversation_id: UUID):
            require_current(conversation_id)
            return {"drafts": load_drafts(root, conversation_id).model_dump(),
                    "tools": {key: bool(value) for key, value in tools_available().items()},
                    "supported": os.name == "nt", "active": self.active,
                    "result": self.results.get(str(conversation_id))}

        @app.post("/api/practice/save")
        async def save_practice(body: SaveDrafts):
            require_current(body.conversation_id)
            current = load_drafts(root, body.conversation_id)
            incoming = Drafts(**body.model_dump())
            if incoming.revision != current.revision:
                # A response may have been lost after a successful write.
                if incoming.model_dump(exclude={"revision"}) == current.model_dump(exclude={"revision"}):
                    return current.model_dump()
                raise HTTPException(409, "练习已在其他窗口保存。请复制保留当前代码，再重新读取；未覆盖新记录。")
            incoming.revision += 1
            # Persist an otherwise empty chat so its practice remains discoverable after restart.
            conversation_path = root / "data/conversations" / f"session-{body.conversation_id}.json"
            if not conversation_path.exists():
                storage.save_conversation(root, app.state.conversation)
            storage.atomic_write(draft_path(root, body.conversation_id), incoming.model_dump_json(indent=2) + "\n")
            return incoming.model_dump()

        @app.post("/api/practice/run")
        async def run_practice(body: RunInput):
            require_current(body.conversation_id)
            if os.name != "nt":
                raise HTTPException(400, "本轮运行区只支持 Windows。")
            if self.task:
                raise HTTPException(409, "已有程序正在运行，请等待或停止。")
            if not body.code.strip():
                raise HTTPException(400, "请先写入代码。")
            tools = tools_available()[body.language]
            if not tools:
                missing = "GCC（gcc）" if body.language == "c" else "完整 JDK（javac 和 java）"
                raise HTTPException(400, f"未找到 {missing}，请安装并加入 PATH 后重启应用。")
            self.stopped = threading.Event()
            run_id = str(uuid4())
            self.active = {"id": run_id, "conversation_id": str(body.conversation_id)}
            self.results.pop(str(body.conversation_id), None)

            async def finish():
                try:
                    result = await asyncio.to_thread(execute, body, tools, self.stopped)
                    result["id"] = run_id
                    self.results[str(body.conversation_id)] = result
                finally:
                    self.active = None
                    self.task = None

            self.task = asyncio.create_task(finish())
            return self.active

        @app.post("/api/practice/stop")
        async def stop_practice(body: StopInput):
            if self.active and (self.active["id"] != str(body.run_id) or self.active["conversation_id"] != str(body.conversation_id)):
                raise HTTPException(409, "运行编号已变化，请重新读取。")
            self.stopped.set()
            return {"stopping": bool(self.task)}

    async def close(self):
        self.stopped.set()
        if self.task:
            await self.task
