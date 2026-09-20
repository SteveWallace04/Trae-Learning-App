"""One local service for the page, settings, and a single active conversation."""

import asyncio
from contextlib import asynccontextmanager, suppress
import json
import logging
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import breakpoints, context as teaching, model, storage

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
logger = logging.getLogger(__name__)


class SettingsInput(BaseModel):
    api_key: SecretStr = SecretStr("")
    model: Literal["deepseek-flash", "deepseek-v4-pro"]

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value):
        key = value.get_secret_value().strip()
        if len(key) > 512 or any(not (c.isascii() and (c.isalnum() or c in "-_.")) for c in key):
            raise ValueError("Invalid key format")
        return SecretStr(key)


class ChatInput(BaseModel):
    conversation_id: UUID
    message: str = Field(default="", max_length=100_000)
    retry_id: UUID | None = None


class ConversationInput(BaseModel):
    conversation_id: UUID


def create_app(root: Path = ROOT, stream_reply=model.stream_reply):
    def recover(conversation):
        changed = False
        for message in conversation.messages:
            if message.status == "streaming":
                message.status = "interrupted"
                message.error = "程序上次意外中断，回答未完成。"
                changed = True
        if changed:
            storage.save_conversation(root, conversation)
        return conversation

    # Injectable root and model call keep tests away from personal files and paid APIs.
    @asynccontextmanager
    async def lifespan(app):
        app.state.conversation = recover(storage.load_conversation(root))
        yield
        task = app.state.active_task
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if app.state.breakpoint_task:
            app.state.breakpoint_task.cancel()
            with suppress(asyncio.CancelledError):
                await app.state.breakpoint_task

    app = FastAPI(title="Trae-Learning", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.active_task = None
    app.state.active_id = None
    app.state.save_error = False
    app.state.breakpoint_task = None
    app.state.breakpoint_draft = None
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        # A different website must not change local keys or trigger paid requests.
        origin = request.headers.get("origin")
        if request.url.path.startswith("/api/") and origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "只接受本页发起的请求。"}, status_code=403)
        response = await call_next(request)
        # Local updates must not mix a new page with cached old scripts/styles.
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # Default validation errors include input values, possibly credentials.
        return JSONResponse({"detail": "输入格式不正确，请检查内容和模型设置。"}, status_code=422)

    @app.exception_handler(OSError)
    async def file_error(request, exc):
        return JSONResponse({"detail": "本地文件读写失败，请检查磁盘空间和文件权限。"}, status_code=503)

    def settings_view():
        settings = storage.read_settings(root)
        return {"configured": bool(settings["api_key"]), "model": settings["model"], "models": list(storage.MODELS)}

    def persist():
        try:
            storage.save_conversation(root, app.state.conversation)
            app.state.save_error = False
            return True
        except OSError:
            app.state.save_error = True
            return False

    def require_current(conversation_id):
        if conversation_id != app.state.conversation.id:
            raise HTTPException(409, "其他窗口已切换对话，请重新读取聊天后再操作。")

    def require_no_breakpoint_task():
        if app.state.breakpoint_task:
            raise HTTPException(409, "正在整理学习断点，请等待完成或取消整理。")

    def read_breakpoint():
        try:
            return breakpoints.load(root)
        except ValueError as exc:
            raise HTTPException(503, "已保存的断点文件格式异常，未覆盖原文件，请先检查本地记录。") from exc

    def require_switchable(conversation_id):
        require_current(conversation_id)
        require_no_breakpoint_task()
        if app.state.active_task:
            raise HTTPException(409, "请先停止当前回答，再新建或切换对话。")
        if app.state.save_error:
            raise HTTPException(503, "当前记录尚未保存，请先重试保存，再切换对话。")

    def conversation_view():
        return {"conversation": app.state.conversation.model_dump(mode="json"), "active": bool(app.state.active_task or app.state.breakpoint_task), "activity": "breakpoint" if app.state.breakpoint_task else "chat" if app.state.active_task else None, "save_error": app.state.save_error}

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "app": "trae-learning"}

    @app.get("/api/settings")
    async def get_settings():
        return settings_view()

    @app.post("/api/settings")
    async def update_settings(body: SettingsInput):
        require_no_breakpoint_task()
        if app.state.active_task:
            raise HTTPException(409, "请等待当前回答结束后再修改设置。")
        key = body.api_key.get_secret_value() or storage.read_settings(root)["api_key"]
        if not key:
            raise HTTPException(400, "请填写 DeepSeek API Key。")
        storage.save_settings(root, key, body.model)
        return settings_view()

    @app.get("/api/conversation")
    async def get_conversation():
        return conversation_view()

    @app.get("/api/conversations")
    async def conversations():
        return {"conversations": storage.list_conversations(root, app.state.conversation)}

    @app.post("/api/conversations")
    async def new_conversation(body: ConversationInput):
        require_switchable(body.conversation_id)
        if not app.state.conversation.messages:
            storage.save_conversation(root, app.state.conversation)
            storage.select_conversation(root, app.state.conversation.id)
            return conversation_view()
        # Preserve the previous selection even if creating/selecting the new file fails.
        storage.select_conversation(root, app.state.conversation.id)
        conversation = storage.Conversation()
        storage.save_conversation(root, conversation)
        storage.select_conversation(root, conversation.id)
        app.state.conversation = conversation
        return conversation_view()

    @app.post("/api/conversations/{conversation_id}/select")
    async def select_conversation(conversation_id: UUID, body: ConversationInput):
        require_switchable(body.conversation_id)
        if conversation_id == app.state.conversation.id:
            return conversation_view()
        try:
            conversation = storage.load_conversation(root, conversation_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, "找不到这段对话，请刷新列表。") from exc
        conversation = recover(conversation)
        storage.select_conversation(root, conversation.id)
        app.state.conversation = conversation
        return conversation_view()

    @app.get("/api/teaching")
    async def preview_teaching():
        try:
            return teaching.prepare(root)
        except teaching.ContextError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/api/messages/{message_id}/context")
    async def message_context(message_id: UUID):
        message = next((item for item in app.state.conversation.messages if item.id == message_id), None)
        if message is None or not message.context_id:
            raise HTTPException(404, "这条消息没有教学材料快照；旧回答不会补造记录。")
        path = root / "data" / "contexts" / f"{message.context_id}.json"
        if not path.exists():
            raise HTTPException(404, "这条回答的教学材料快照已不存在。")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.post("/api/conversation/save")
    async def retry_save(body: ConversationInput):
        require_current(body.conversation_id)
        require_no_breakpoint_task()
        if app.state.active_task:
            raise HTTPException(409, "请先停止生成，再重试保存。")
        if not persist():
            raise HTTPException(503, "保存仍然失败，请保留此页面并检查磁盘空间和权限。")
        return {"saved": True}

    @app.post("/api/chat/stop")
    async def stop_chat(body: dict):
        if str(app.state.active_id) != body.get("reply_id"):
            raise HTTPException(409, "这条回答已结束，请刷新聊天记录。")
        task = app.state.active_task
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        return {"stopped": True, "saved": not app.state.save_error}

    @app.post("/api/chat")
    async def chat(body: ChatInput, request: Request):
        require_current(body.conversation_id)
        require_no_breakpoint_task()
        if app.state.active_task:
            raise HTTPException(409, "已有回答正在生成，请勿在多个窗口同时发送。")
        if app.state.save_error:
            raise HTTPException(503, "当前记录尚未保存，请先重试保存。")
        settings = storage.read_settings(root)
        if not settings["api_key"]:
            raise HTTPException(400, "请先在设置中填写 DeepSeek API Key。")
        if settings["model"] not in storage.MODELS:
            raise HTTPException(400, "请在设置中选择支持的模型。")
        conversation = app.state.conversation
        if body.retry_id:
            if body.message.strip():
                raise HTTPException(400, "重试时不能同时提交新问题。")
            if not conversation.messages:
                raise HTTPException(409, "没有可以重试的问题。")
            last = conversation.messages[-1]
            if last.id != body.retry_id or last.role != "assistant" or last.status not in ("error", "stopped", "interrupted"):
                raise HTTPException(409, "只支持重试最后一条未完成的回答。")
            user = next(message for message in reversed(conversation.messages) if message.id == last.reply_to)
        else:
            text = body.message.strip()
            if not text:
                raise HTTPException(400, "请先输入问题。")
            user = storage.Message(role="user", content=text)
        # Only completed turns enter context; incomplete attempts stay visible in history.
        users = {message.id: message for message in conversation.messages if message.role == "user"}
        try:
            prepared = teaching.prepare(root)
        except teaching.ContextError as exc:
            raise HTTPException(503, str(exc)) from exc
        context = [{"role": "system", "content": prepared["system_prompt"]}]
        for message in conversation.messages:
            if message.role == "assistant" and message.status == "complete":
                context.extend([
                    {"role": "user", "content": users[message.reply_to].content},
                    {"role": "assistant", "content": message.content},
                ])
        context.append({"role": "user", "content": user.content})
        # Record exactly what this attempt will send, before any paid request.
        context_id = teaching.save_snapshot(root, prepared)
        previous_count = len(conversation.messages)
        if not body.retry_id:
            conversation.messages.append(user)
        answer = storage.Message(role="assistant", reply_to=user.id, status="streaming", context_id=context_id)
        conversation.messages.append(answer)
        try:
            storage.save_conversation(root, conversation)
        except OSError:
            del conversation.messages[previous_count:]
            raise HTTPException(503, "问题未能保存，尚未调用模型。请检查磁盘空间和权限后重试。")

        events = asyncio.Queue()

        async def produce():
            try:
                async for text in stream_reply(settings, context):
                    answer.content += text
                    events.put_nowait({"type": "delta", "text": text})
                if not answer.content:
                    raise model.ModelError("模型没有返回文字，请重试。")
                answer.status = "complete"
            except asyncio.CancelledError:
                answer.status = "stopped"
            except model.ModelError as exc:
                answer.status, answer.error = "error", str(exc)
            except Exception:
                logger.error("Generation failed unexpectedly; provider payload omitted")
                answer.status, answer.error = "error", "生成遇到异常，已保留问题和收到的文字。"
            finally:
                saved = persist()
                app.state.active_task = None
                app.state.active_id = None
                events.put_nowait({"type": "done", "message": answer.model_dump(mode="json"), "saved": saved})

        task = asyncio.create_task(produce())
        app.state.active_task = task
        app.state.active_id = answer.id

        async def event_stream():
            try:
                initial = {"type": "start", "user": user.model_dump(mode="json"), "message": {**answer.model_dump(mode="json"), "content": "", "status": "streaming"}}
                yield json.dumps(initial, ensure_ascii=False) + "\n"
                while True:
                    try:
                        event = await asyncio.wait_for(events.get(), timeout=1)
                    except TimeoutError:
                        if await request.is_disconnected():
                            break
                        continue
                    yield json.dumps(event, ensure_ascii=False) + "\n"
                    if event["type"] == "done":
                        break
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(event_stream(), media_type="application/x-ndjson", headers={"X-Content-Type-Options": "nosniff"})

    @app.get("/api/breakpoint")
    async def get_breakpoint():
        record = read_breakpoint()
        current = app.state.conversation
        new_messages = None
        if record and record.conversation_id == current.id:
            new_messages = max(0, len(current.messages) - record.message_count)
        return {"record": record.model_dump(mode="json") if record else None, "new_messages": new_messages}

    @app.get("/api/breakpoint/source")
    async def breakpoint_source():
        record = read_breakpoint()
        if record is None:
            raise HTTPException(404, "还没有已保存的学习断点。")
        try:
            source = storage.load_conversation(root, record.conversation_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, "来源对话文件已不存在，已保存断点仍保留。") from exc
        messages = source.messages[:record.message_count]
        if not messages or messages[-1].id != record.through_message_id:
            raise HTTPException(409, "来源对话与保存位置不一致，请检查本地文件。")
        return {"record_id": str(record.id), "messages": [m.model_dump(mode="json") for m in messages]}

    @app.post("/api/breakpoint/draft")
    async def draft_breakpoint(body: ConversationInput, request: Request):
        require_switchable(body.conversation_id)
        conversation = app.state.conversation
        if not conversation.messages:
            raise HTTPException(400, "当前对话还没有消息，先学一段再整理。")
        settings = storage.read_settings(root)
        if not settings["api_key"] or settings["model"] not in storage.MODELS:
            raise HTTPException(400, "请先配置有效的模型设置，再整理断点。")
        previous = read_breakpoint()
        try:
            prepared = teaching.prepare_materials(root)
        except teaching.ContextError as exc:
            raise HTTPException(503, str(exc)) from exc
        context = breakpoints.prompt(prepared, previous, conversation)

        async def collect():
            text = ""
            async with asyncio.timeout(120):
                async for chunk in stream_reply(settings, context):
                    text += chunk
                    if len(text) > breakpoints.MAX_CONTENT:
                        raise ValueError("Draft too long")
            return breakpoints.validate_generated(text)

        task = asyncio.create_task(collect())
        app.state.breakpoint_task = task
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.5)
                if await request.is_disconnected():
                    raise HTTPException(499, "整理已取消，旧断点未改变。")
            text = task.result()
            draft = breakpoints.Draft(
                base_revision=previous.id if previous else None,
                conversation_id=conversation.id,
                conversation_title=storage.conversation_title(conversation),
                through_message_id=conversation.messages[-1].id,
                message_count=len(conversation.messages), content=text,
            )
            app.state.breakpoint_draft = draft
            return draft.model_dump(mode="json")
        except model.ModelError as exc:
            raise HTTPException(502, f"整理未完成：{exc} 旧断点未改变。") from exc
        except asyncio.CancelledError as exc:
            raise HTTPException(409, "整理已取消，旧断点未改变。") from exc
        except (ValueError, TimeoutError) as exc:
            raise HTTPException(502, "未获得完整的断点草稿，旧断点未改变，可以手动重试。") from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Breakpoint generation failed; provider payload omitted")
            raise HTTPException(502, "整理遇到异常，旧断点未改变，可以手动重试。") from exc
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if not task.cancelled():
                task.exception()  # Retrieve failures even if the browser disconnected at completion.
            if app.state.breakpoint_task is task:
                app.state.breakpoint_task = None

    @app.post("/api/breakpoint/cancel")
    async def cancel_breakpoint(body: ConversationInput):
        require_current(body.conversation_id)
        task = app.state.breakpoint_task
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if app.state.breakpoint_task is task:
                app.state.breakpoint_task = None
        return {"cancelled": True}

    @app.post("/api/breakpoint")
    async def save_breakpoint(body: breakpoints.SaveInput):
        require_switchable(body.conversation_id)
        draft = app.state.breakpoint_draft
        if draft is None or draft.id != body.draft_id or draft.conversation_id != body.conversation_id:
            raise HTTPException(409, "草稿已失效或属于另一段对话，请保留修改内容并重新整理。")
        text = body.content.strip()
        if not text:
            raise HTTPException(400, "断点内容不能为空。")
        previous = read_breakpoint()
        # Repeating a save after a lost HTTP response must not create a second revision.
        if previous and previous.id == draft.id and previous.content == text:
            return previous.model_dump(mode="json")
        if (previous.id if previous else None) != draft.base_revision:
            raise HTTPException(409, "已保存断点发生变化，请保留修改内容并重新整理，避免覆盖新记录。")
        conversation = app.state.conversation
        if len(conversation.messages) != draft.message_count or conversation.messages[-1].id != draft.through_message_id:
            raise HTTPException(409, "整理后已有新消息，请保留修改内容并重新整理。旧断点未改变。")
        record = breakpoints.Breakpoint(**{**draft.model_dump(), "content": text})
        breakpoints.save(root, record)
        return record.model_dump(mode="json")

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
