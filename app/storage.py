"""Local settings and conversation files. No model or web calls here."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Literal
from uuid import UUID, uuid4

from dotenv import dotenv_values
from pydantic import BaseModel, Field

MODELS = ("deepseek-flash", "deepseek-v4-pro")


class Message(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    role: Literal["user", "assistant"]
    content: str = ""
    status: Literal["complete", "streaming", "stopped", "interrupted", "error"] = "complete"
    reply_to: UUID | None = None
    error: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Conversation(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    messages: list[Message] = Field(default_factory=list)


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
            name = file.name
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def read_settings(root: Path):
    path = root / ".env"
    values = dotenv_values(path, interpolate=False) if path.exists() else {}
    return {"api_key": values.get("DEEPSEEK_API_KEY") or "", "model": values.get("DEEPSEEK_MODEL") or MODELS[0]}


def save_settings(root: Path, api_key: str, model: str):
    # API validation restricts these values to single-line identifiers.
    atomic_write(root / ".env", f"DEEPSEEK_API_KEY={api_key}\nDEEPSEEK_MODEL={model}\n")


def load_conversation(root: Path):
    files = list((root / "data" / "conversations").glob("session-*.json"))
    if not files:
        return Conversation()
    latest = max(files, key=lambda path: path.stat().st_mtime_ns)
    return Conversation.model_validate_json(latest.read_text(encoding="utf-8"))


def save_conversation(root: Path, conversation: Conversation):
    path = root / "data" / "conversations" / f"session-{conversation.id}.json"
    atomic_write(path, json.dumps(conversation.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
