"""FastAPI 入口：127.0.0.1:8000 单 worker；启动恢复队列残留；退出只断 CDP 不杀 Chrome（SPEC §1/§7）。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .chrome_session import ChromeSession
from .db import Database
from .errors import AppError
from .events import bus
from .jobs import JobQueue
from .login import LoginFlow
from .pagefetch import PageFetch
from .routers import ALL


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    db = Database()
    db.recover()  # 上次退出残留：queued→cancelled / running→failed / submitting→unknown
    chrome = ChromeSession()
    pf = PageFetch(chrome)
    login = LoginFlow(chrome, db, bus)
    queue = JobQueue(db, bus, chrome, pf, login)
    app.state.db, app.state.chrome, app.state.pf = db, chrome, pf
    app.state.login, app.state.queue, app.state.bus = login, queue, bus
    queue.start()
    try:
        yield
    finally:
        await queue.stop()
        await chrome.disconnect()  # 绝不 Browser.close / 杀 Chrome
        db.close()


app = FastAPI(title="TIMBRE", lifespan=lifespan)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    status = 404 if exc.code == "NOT_FOUND" else 400
    return JSONResponse(status_code=status, content={"detail": {"code": exc.code, "message": exc.message}})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # SPEC §3：HTTP 错误一律 {"detail":{"code","message"}}；不回显请求体（可能含 Key）
    return JSONResponse(status_code=400,
                        content={"detail": {"code": "INVALID_INPUT", "message": "请求参数无效。"}})


for r in ALL:
    app.include_router(r)

_dist = config.ROOT / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="spa")
else:
    @app.get("/")
    async def root():
        return {"app": "TIMBRE", "hint": "前端尚未构建：cd frontend && pnpm install && pnpm build"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, workers=1)