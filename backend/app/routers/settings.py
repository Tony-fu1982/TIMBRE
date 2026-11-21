"""设置路由（SPEC §3）：下载目录（可写校验，失败保留上次有效值）+ 调试日志开关。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..errors import AppError

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsIn(BaseModel):
    download_dir: str | None = None
    debug_logs: str | None = None


def _check_writable(dir_: str) -> Path:
    if not (dir_ or "").strip():
        raise AppError("DIR_INVALID")
    p = Path(dir_).expanduser().resolve()  # 相对路径落库前转绝对，避免根随 cwd 漂移
    try:
        p.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=p, prefix=".gm_write_test_", delete=True):
            pass
    except OSError:
        raise AppError("DIR_INVALID")
    return p


@router.get("")
async def get_settings(request: Request):
    s = request.app.state.db.settings()
    return {"download_dir": s.get("download_dir", ""), "debug_logs": s.get("debug_logs", "0")}


@router.put("")
async def put_settings(body: SettingsIn, request: Request):
    db = request.app.state.db
    if body.download_dir is not None:
        p = _check_writable(body.download_dir)
        db.set_setting("download_dir", str(p))
    if body.debug_logs is not None:
        if body.debug_logs not in ("0", "1"):
            raise AppError("DIR_INVALID", "debug_logs 只能是 0 或 1。")
        db.set_setting("debug_logs", body.debug_logs)
    return {"ok": True}  # SPEC §3：PUT 返回 {ok:true}；前端保存后重读 GET