"""Serve the local page. Run from the repository with python -m app.main."""

from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Trae-Learning", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
