"""AI 配置路由（SPEC §3 + 用户指令）：provider=deepseek|qwen；Key 只进 data/.env，绝不回显。
模型默认：deepseek4.1-flash-1017 / qwen3.8-omni-flash。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import config
from ..errors import AppError

router = APIRouter(prefix="/api/ai", tags=["ai"])


class AiConfigIn(BaseModel):
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None


def _active(db) -> str:
    return db.settings().get("ai_provider", config.DEFAULT_PROVIDER)


@router.get("/config")
async def get_config(request: Request, provider: str | None = None):
    provider = provider if provider is not None else _active(request.app.state.db)
    if provider not in config.AI_DEFAULTS:
        raise AppError("INVALID_PROVIDER")
    cfg = config.ai_config(provider)
    return {"provider": provider, "model": cfg["model"], "base_url": cfg["base_url"],
            "key_set": bool(cfg["api_key"])}


@router.put("/config")
async def put_config(body: AiConfigIn, request: Request):
    if body.provider not in config.AI_DEFAULTS:
        raise AppError("INVALID_PROVIDER")
    model = (body.model or "").strip()
    if not model:
        raise AppError("INVALID_PROVIDER", "模型名不能为空。")
    db = request.app.state.db
    updates = {f"{body.provider.upper()}_MODEL": model}
    if body.base_url is not None:
        base = body.base_url.strip() or config.AI_DEFAULTS[body.provider]["base_url"]
        updates[f"{body.provider.upper()}_BASE_URL"] = base
    if body.api_key is not None and body.api_key.strip():
        updates[f"{body.provider.upper()}_API_KEY"] = body.api_key.strip()
    config.write_env(updates)
    db.set_setting("ai_provider", body.provider)
    return {"ok": True}  # SPEC §3：PUT 返回 {ok:true}；Key 绝不回显，前端保存后重读 GET
