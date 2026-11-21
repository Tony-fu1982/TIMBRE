"""会话路由（SPEC §3）：登录触发 + 惰性状态复检。"""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/session", tags=["session"])


@router.post("/login")
async def login(request: Request):
    return await request.app.state.login.start_login()


@router.get("/status")
async def status(request: Request):
    return await request.app.state.login.recheck()