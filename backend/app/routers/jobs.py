"""任务路由（SPEC §3）：五类提交 + 查询/日志/取消/打开文件夹/继续查询。"""
from __future__ import annotations

import asyncio
import ctypes
import os
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import config, ffmedia
from ..errors import AppError
from ..suno.common import validate_suno_url

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

MAX_UPLOAD = 50 * 1024 * 1024
MAX_CONVERT = 200 * 1024 * 1024
LABEL_MAX = 60  # 自定义显示名上限（前端 maxLength 同值）


def _clean_label(s: str) -> str:
    return s.strip()[:LABEL_MAX]


def _require_login(request: Request) -> None:
    if request.app.state.login.state != "logged_in":
        raise AppError("NOT_LOGGED_IN")


def _job_view(db, job: dict) -> dict:
    job["log_seq"] = db.log_seq(job["id"])
    return job


async def _save_upload(file: UploadFile, limit: int) -> Path:
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "audio").suffix.lower() or ".bin"
    dest = config.UPLOAD_DIR / f"{uuid.uuid4().hex[:12]}{ext}"
    total = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                f.close()
                dest.unlink(missing_ok=True)
                raise AppError("BAD_AUDIO", f"文件超过 {limit // (1024 * 1024)}MB 上限。")
            f.write(chunk)
    if total == 0:
        dest.unlink(missing_ok=True)
        raise AppError("BAD_AUDIO", "文件为空。")
    return dest


# ---- 提交 ----

@router.post("/upload")
async def submit_upload(request: Request, file: UploadFile = File(...)):
    _require_login(request)
    path = await _save_upload(file, MAX_UPLOAD)
    await ffmedia.probe(path)  # BAD_AUDIO 前置校验
    job = request.app.state.queue.submit("upload", str(path), {"source_name": file.filename or path.name})
    return JSONResponse({"job_id": job["id"]}, status_code=201)  # SPEC §3：提交一律 201 {job_id}


class DownloadIn(BaseModel):
    url: str
    format: Literal["mp3", "wav"] = "mp3"
    mode: Literal["song", "stems", "vocal"] = "song"


@router.post("/download")
async def submit_download(body: DownloadIn, request: Request):
    _require_login(request)
    url = validate_suno_url(body.url)
    job = request.app.state.queue.submit("download", url, {"format": body.format, "mode": body.mode})
    return JSONResponse({"job_id": job["id"]}, status_code=201)


@router.post("/dna")
async def submit_dna(request: Request, file: UploadFile = File(...),
                     mode: Literal["full", "lyrics", "style"] = Form("full"),
                     label: str = Form("")):
    if not config.ai_config("qwen")["api_key"]:
        raise AppError("NO_AI_KEY", "DNA 音频分析使用 Qwen，请先到「设置」填写 Qwen API Key。")
    path = await _save_upload(file, MAX_UPLOAD)
    await ffmedia.probe(path)
    opts = {"mode": mode, "source_name": file.filename or path.name}
    if label.strip():
        opts["label"] = _clean_label(label)
    job = request.app.state.queue.submit("dna", str(path), opts)
    return JSONResponse({"job_id": job["id"]}, status_code=201)


class GenerateIn(BaseModel):
    dna_result_id: int | None = None
    theme: str | None = None
    mode: Literal["full", "lyrics", "style"] = "full"


@router.post("/generate")
async def submit_generate(body: GenerateIn, request: Request):
    db = request.app.state.db
    provider = db.settings().get("ai_provider", config.DEFAULT_PROVIDER)
    if not config.ai_config(provider)["api_key"]:
        raise AppError("NO_AI_KEY")
    theme = (body.theme or "").strip()
    dna_job = None
    if body.dna_result_id is not None:
        dna_job = db.get_job(body.dna_result_id)
        if (not dna_job or dna_job["kind"] != "dna" or dna_job["status"] != "success"
                or not isinstance(dna_job["result"], dict) or not dna_job["result"].get("dnaProfile")):
            raise AppError("INVALID_GENERATION_INPUT", "所选 DNA 分析不可用：需要已完成且含 DNA 档案的分析任务。")
    if not theme and dna_job is None:
        raise AppError("INVALID_GENERATION_INPUT")
    job = request.app.state.queue.submit("generate", theme, {
        "mode": body.mode, "dna_result_id": body.dna_result_id,
        "dna": dna_job["result"] if dna_job else None,
    })
    return JSONResponse({"job_id": job["id"]}, status_code=201)


@router.post("/convert")
async def submit_convert(request: Request, file: UploadFile = File(...),
                         target: Literal["mp3", "wav"] = Form("mp3")):
    path = await _save_upload(file, MAX_CONVERT)
    job = request.app.state.queue.submit("convert", str(path), {
        "target": target, "source_name": file.filename or path.name})
    return JSONResponse({"job_id": job["id"]}, status_code=201)


# ---- 查询 / 日志 / 取消 ----

@router.get("")
async def list_jobs(request: Request, limit: int = 50):
    db = request.app.state.db
    return {"jobs": [_job_view(db, j) for j in db.list_jobs(min(max(limit, 1), 200))]}


@router.get("/{job_id}")
async def get_job(job_id: int, request: Request):
    db = request.app.state.db
    job = db.get_job(job_id)
    if not job:
        raise AppError("NOT_FOUND")
    return _job_view(db, job)


@router.get("/{job_id}/logs")
async def get_logs(job_id: int, request: Request, after_seq: int = 0, limit: int = 200):
    db = request.app.state.db
    if not db.get_job(job_id):
        raise AppError("NOT_FOUND")
    return db.logs_after(job_id, after_seq, min(max(limit, 1), 500))


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int, request: Request):
    request.app.state.queue.cancel(job_id)
    return {"ok": True}  # SPEC §3


# ---- 重试（SPEC §5 上传页失败重试；同 input/options 重新入队，遵守忙闲规则）----

@router.post("/{job_id}/retry")
async def retry_job(job_id: int, request: Request):
    db = request.app.state.db
    job = db.get_job(job_id)
    if not job:
        raise AppError("NOT_FOUND")
    if job["status"] not in ("failed", "cancelled"):
        raise AppError("BUSY", "只有失败或已取消的任务可以重试。")
    if job["render_state"] == "pending":
        raise AppError("EXPORT_RESUME_UNAVAILABLE", "请用「继续查询」查看同一渲染结果。")
    if (job["result"] or {}).get("generation_pending"):
        raise AppError("GENERATION_UNCERTAIN", "分轨生成尚未确认完成，请先到 Suno 核对已有结果。")
    if job["render_state"] in ("submitting", "unknown") or job["error_code"] in (
        "NETWORK_RESULT_UNKNOWN", "GENERATION_UNCERTAIN",
    ):
        code = "GENERATION_UNCERTAIN" if job["error_code"] == "GENERATION_UNCERTAIN" else "NETWORK_RESULT_UNKNOWN"
        raise AppError(code, "请求结果尚未确认，请先到 Suno 核对已有结果。")
    kind, options = job["kind"], dict(job["options"] or {})
    if kind in ("upload", "download"):
        _require_login(request)
    if kind in ("upload", "dna", "convert"):
        if not Path(job["input"]).exists():
            raise AppError("BAD_AUDIO", "原始文件已不存在，请重新选择文件提交。")
    if kind == "dna" and not config.ai_config("qwen")["api_key"]:
        raise AppError("NO_AI_KEY")
    if kind == "generate":
        provider = db.settings().get("ai_provider", config.DEFAULT_PROVIDER)
        if not config.ai_config(provider)["api_key"]:
            raise AppError("NO_AI_KEY")
    new_job = request.app.state.queue.submit(kind, job["input"], options)
    return _job_view(db, new_job)


# ---- 自定义显示名（备注覆盖原文件名；空串恢复默认）----

class LabelIn(BaseModel):
    label: str = ""


@router.post("/{job_id}/label")
async def set_label(job_id: int, body: LabelIn, request: Request):
    db = request.app.state.db
    job = db.get_job(job_id)
    if not job:
        raise AppError("NOT_FOUND")
    opts = dict(job["options"] or {})
    text = _clean_label(body.label)
    if text:
        opts["label"] = text
    else:
        opts.pop("label", None)
    db.update_job(job_id, options=opts)
    request.app.state.bus.publish("job", db.snapshot(job_id))
    return _job_view(db, db.get_job(job_id))


# ---- 打开所在文件夹（无 path 参数；后端解析登记产物，SPEC §3）----

def _reveal_in_explorer(p: Path) -> None:
    """资源管理器中选中文件。explorer /select, 对超长/特殊字符路径会静默失败并
    回退打开「文档」，故走 Shell API（需先初始化 COM）；失败兜底开所在文件夹。"""
    ole32 = ctypes.oledll.ole32
    com = False
    try:
        ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        com = True
    except OSError:
        pass  # 线程 COM 模式冲突等：仍试 Shell API，失败走兜底
    try:
        shell32 = ctypes.oledll.shell32
        pidl = ctypes.c_void_p()
        shell32.SHParseDisplayName(str(p), None, ctypes.byref(pidl), 0, None)
        shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
        ctypes.windll.shell32.ILFree(pidl)  # ILFree 返回 void，oledll 会把残留寄存器误判成 HRESULT
    except OSError:
        try:
            os.startfile(str(p.parent))  # noqa: S606  # ponytail: 兜底只开文件夹不选中，Shell API 修好后极少走到
        except OSError:
            raise AppError("OPEN_FOLDER_FAILED")
    finally:
        if com:
            ole32.CoUninitialize()

@router.post("/{job_id}/open-folder")
async def open_folder(job_id: int, request: Request):
    db = request.app.state.db
    job = db.get_job(job_id)
    if not job:
        raise AppError("NOT_FOUND")
    out = job.get("output_path") or ""
    registered = request.app.state.queue.output_dirs.get(job_id)
    if job["status"] != "success" or not out or registered is None:
        raise AppError("OUTPUT_UNAVAILABLE")
    p = Path(out).resolve()
    if not p.is_file() or not p.is_relative_to(registered):
        raise AppError("OUTPUT_UNAVAILABLE")
    await asyncio.to_thread(_reveal_in_explorer, p)  # COM/Shell 阻塞调用放工作线程，不占事件循环
    return {"ok": True}


# ---- 继续查询（只读同 render_id；SPEC §3/§4.6）----

@router.post("/{job_id}/resume-export")
async def resume_export(job_id: int, request: Request):
    db = request.app.state.db
    job = db.get_job(job_id)
    if not job:
        raise AppError("NOT_FOUND")
    if (job["kind"] != "upload" or job["status"] not in ("success", "failed", "cancelled")
            or job.get("render_state") != "pending" or not job.get("render_id")):
        raise AppError("EXPORT_RESUME_UNAVAILABLE", "该任务没有可继续查询的渲染：仅已结束且渲染仍在等待的任务可用。")
    _require_login(request)
    new_job = request.app.state.queue.submit("upload", job["input"], {"resume_of": job_id})
    return JSONResponse({"job_id": new_job["id"]}, status_code=201)
