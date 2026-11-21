"""六片页内上传（issue 05，SPEC §4.4 / 文档08 §4.2 / 原作 main.cjs sunoUploadClip:5524-6033）。
每片六步：GET /api/session/ → POST /api/uploads/audio/ → S3 直传（页内 base64→Blob，≤4 次重试）→
upload-finish/ → GET …/{id}/ 轮询 ≤120 → initialize-clip/（≤5 次 + 40 次轮询兜底）→ clipId。
API 步走 PageFetch（SPEC §4.3 策略）；副作用 POST 一律不自动重放，结果不确定 → NETWORK_RESULT_UNKNOWN。"""
from __future__ import annotations

import asyncio
import base64
import random
import uuid
from pathlib import Path

from ..errors import AppError, SunoAPIError
from ..jobs import JobContext

UPLOAD_CONCURRENCY = 3  # 2026-11 优化：六片服务端处理相互独立，串行 6×~30s → 3 并发；
                        # 不拉满 6：限流概率更高，副作用 POST 遇 429 落「结果未知」会锁死重试。
POLL_MAX = 120          # 文档08 §4.2 步5 / main:5961
CLIP_POLL_MAX = 40      # main:5997 initialize-clip 后的轮询兜底
INIT_TRIES = 5          # main:5982
S3_TRIES = 4            # main:5927 s3try<4
POLL_SLEEP_S = 2.5      # main:5572 sleepPollInterval = 2500 + rand(1001)ms
POLL_JITTER_S = 1.0
INIT_RETRY_SLEEP_S = 1.5   # main:5993 1500*(try+1)ms
S3_RETRY_BASE_S = 2.0      # main:5945 2000*(s3try+1) + rand(800)ms
S3_RETRY_JITTER_S = 0.8
S3_TIMEOUT_MS = 120_000    # main:5560 FILE_UPLOAD_TIMEOUT_MS
DONE_STATES = ("complete", "completed", "error", "failed")  # main:5960

# 单次 S3 直传的页内脚本：base64→atob→Uint8Array→Blob→FormData→fetch。
# 取消：AbortController 注册进 window.__gjAborts（与 __gj.abort 同表），Python 侧 watcher 置
# window.__gjStop[reqId] 后 abort；脚本见 stop 标记即返回 cancelled，不再重试。
# ponytail: SPEC §4.4/文档08 写 "PUT"，但原作 main.cjs:5928-5934 实为 POST multipart——
# 初始化响应 {id,url,fields} 是 S3 presigned POST 凭据（PUT 无法携带 policy fields，会被 403）。
# 按原作 POST 实现；升级路径：若 Suno 改为 presigned PUT（响应无 fields），换 method 即可。
S3_FETCH_JS = r"""
async (a) => {
  const aborts = (window.__gjAborts = window.__gjAborts || {});
  const stop = (window.__gjStop = window.__gjStop || {});
  const bin = atob(a.base64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  const blob = new Blob([arr], { type: 'audio/mpeg' });
  const ctrl = new AbortController();
  aborts[a.reqId] = ctrl;
  const timer = setTimeout(() => ctrl.abort(), a.timeoutMs);
  try {
    const fd = new FormData();
    Object.keys(a.fields).forEach((k) => fd.append(k, a.fields[k]));
    fd.append('file', blob, a.fileName);
    const res = await fetch(a.url, { method: 'POST', body: fd, signal: ctrl.signal });
    return { ok: res.ok || res.status === 204, status: res.status };
  } catch (e) {
    return { ok: false, status: 0, cancelled: !!stop[a.reqId],
             text: String((e && e.message) || e) };
  } finally {
    clearTimeout(timer);
    if (aborts[a.reqId] === ctrl) delete aborts[a.reqId];
    delete stop[a.reqId];
  }
}
"""

_ABORT_JS = (
    "(id) => { const s = (window.__gjStop = window.__gjStop || {}); s[id] = 1;"
    " const c = (window.__gjAborts || {})[id];"
    " if (c) { try { c.abort(); } catch (e) {} delete window.__gjAborts[id]; } }"
)


def _json(r: dict) -> dict:
    j = r.get("json")
    return j if isinstance(j, dict) else {}


def _clip_id(j: dict) -> str:
    """main:5734-5737 extractClipId 的字段顺序。"""
    clip = j.get("clip")
    return (j.get("clip_id") or j.get("clipId")
            or (clip.get("id") if isinstance(clip, dict) else "")
            or j.get("suno_clip_id") or "")


async def _api(ctx: JobContext, method: str, path: str, body=None) -> dict:
    return await ctx.pf.request(method, path, body, cancel_event=ctx.cancel)


async def _poll_sleep() -> None:
    await asyncio.sleep(POLL_SLEEP_S + random.random() * POLL_JITTER_S)


async def _s3_upload(ctx: JobContext, path: Path, url: str, fields: dict, file_name: str) -> None:
    """页内 S3 直传；Python 侧重试 ≤4（每次 evaluate 一个 attempt，便于取消与计数）。"""
    page = await ctx.chrome.ensure()
    await ctx.chrome.check_page(page)  # 每次页内请求前白名单校验
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    last = "无响应"
    for attempt in range(S3_TRIES):
        ctx.check_cancel()
        req_id = uuid.uuid4().hex[:12]

        async def _watch():
            await ctx.cancel.wait()
            try:
                await page.evaluate(_ABORT_JS, req_id)
            except Exception:
                pass

        watcher = asyncio.create_task(_watch())
        try:
            res = await page.evaluate(S3_FETCH_JS, {
                "url": url, "fields": fields, "base64": b64,
                "fileName": file_name, "reqId": req_id, "timeoutMs": S3_TIMEOUT_MS,
            })
        finally:
            watcher.cancel()
        ctx.check_cancel()
        if res and res.get("ok"):
            return
        if res and res.get("cancelled"):
            raise AppError("CANCELLED")
        last = f"HTTP {res['status']}" if res and res.get("status") else (res or {}).get("text") or last
        if attempt < S3_TRIES - 1:
            await asyncio.sleep(S3_RETRY_BASE_S * (attempt + 1) + random.random() * S3_RETRY_JITTER_S)
    raise AppError("NETWORK_ERROR", f"S3 直传失败（已重试 {S3_TRIES} 次）：{last}")


async def _upload_one(sl: dict, ctx: JobContext) -> str:
    path = Path(sl["path"])
    file_name = path.name
    ctx.check_cancel()
    # 1) 登录探活（401 → NOT_LOGGED_IN，由 PageFetch 策略处理）
    # 2026-11 漂移：/api/session/ 变匿名公开探不出退登，改用户类轻量端点。
    await _api(ctx, "GET", "/api/user/user_config/")
    ctx.check_cancel()
    # 2) 创建上传记录（副作用 POST：传输失败/5xx → 结果未知，绝不重放；main:5905-5916）
    try:
        r = await _api(ctx, "POST", "/api/uploads/audio/", {"extension": "mp3"})
    except SunoAPIError as e:
        if e.status == 0 or e.status >= 500:
            raise AppError("NETWORK_RESULT_UNKNOWN", "创建上传记录结果不确定，请到 Suno 核对后再试。") from e
        raise
    j = _json(r)
    up_id, url, fields = j.get("id"), j.get("url"), j.get("fields")
    if not (up_id and url and isinstance(fields, dict) and fields):
        raise AppError("NETWORK_RESULT_UNKNOWN", "创建上传记录响应缺 id/url/fields，结果不确定，不自动重放。")
    ctx.check_cancel()
    # 3) S3 直传
    await _s3_upload(ctx, path, url, fields, file_name)
    ctx.check_cancel()
    # 4) upload-finish（5xx/传输失败 → 标记未知，先轮询尝试恢复；main:5950-5957）
    finish_unknown = False
    try:
        await _api(ctx, "POST", f"/api/uploads/audio/{up_id}/upload-finish/",
                   {"upload_type": "file_upload", "upload_filename": file_name})
    except SunoAPIError as e:
        if e.status == 0 or e.status >= 500:
            finish_unknown = True
        else:
            raise
    # 5) 轮询处理状态 ≤120 次（GET 失败容忍继续；main:5959-5975）
    status, clip = "", ""
    for _ in range(POLL_MAX):
        ctx.check_cancel()
        try:
            rp = await _api(ctx, "GET", f"/api/uploads/audio/{up_id}/")
        except SunoAPIError:
            await _poll_sleep()
            continue
        jp = _json(rp)
        status = jp.get("status") or ""
        clip = _clip_id(jp) or clip
        if status in DONE_STATES or clip:
            break
        await _poll_sleep()
    if status in ("error", "failed"):
        raise AppError("SUNO_HTTP_ERROR", f"Suno 处理上传音频失败（{status}）。")
    if finish_unknown and status not in ("complete", "completed") and not clip:
        raise AppError("NETWORK_RESULT_UNKNOWN", "upload-finish 结果不确定且未确认处理完成，请到 Suno 核对。")
    # 6) initialize-clip（≤5 次；ok 无 clipId/5xx/传输失败 → 停止 POST 转轮询兜底；main:5980-6010）
    init_unknown = False
    tries = 0
    while not clip and tries < INIT_TRIES:
        ctx.check_cancel()
        try:
            rc = await _api(ctx, "POST", f"/api/uploads/audio/{up_id}/initialize-clip/", {})
        except SunoAPIError as e:
            if e.status == 0 or e.status >= 500:
                init_unknown = True
                break
            if e.code in ("NOT_LOGGED_IN", "RATE_LIMITED"):
                raise
            await asyncio.sleep(INIT_RETRY_SLEEP_S * (tries + 1))
            tries += 1
            continue
        clip = _clip_id(_json(rc))
        if clip:
            break
        init_unknown = True  # 200 但无 clipId：已发出，不再重放
        break
    if not clip:
        for _ in range(CLIP_POLL_MAX):
            ctx.check_cancel()
            try:
                rp = await _api(ctx, "GET", f"/api/uploads/audio/{up_id}/")
            except SunoAPIError:
                await _poll_sleep()
                continue
            clip = _clip_id(_json(rp))
            if clip:
                break
            await _poll_sleep()
    if not clip:
        if init_unknown or finish_unknown:
            raise AppError("NETWORK_RESULT_UNKNOWN", "initialize-clip 已发出但未取得 clipId，请到 Suno 核对，不自动重放。")
        raise AppError("SUNO_HTTP_ERROR", "上传处理完成但未取得 clipId，请稍后重试。")
    return clip


async def upload_slices(slices: list[dict], ctx: JobContext) -> list[str]:
    """slices=[{path,duration}×6]（transformer 产物）→ 6 个 clipId，全部有效才返回。"""
    if len(slices) != 6:
        raise AppError("INTERNAL_ERROR", f"切片数量应为 6，实际 {len(slices)}。")
    async def one(i: int, sl: dict) -> str:
        ctx.check_cancel()
        ctx.log(f"上传 {i}/{len(slices)}")
        clip = await _upload_one(sl, ctx)
        ctx.log(f"片段 {i} 上传完成：clipId={clip}")
        return clip

    # 波次并发（6 片/3 并发=两波，性能等价于信号量）：不用 Semaphore——
    # 失败释放许可会先唤醒下一等待者、cancel 来不及，零延迟下失败多米诺全片起跑。
    # 波内任一失败/取消 → 取消本波其余任务，且不开下一波，不留孤儿上传。
    clip_ids: list[str] = []
    for start in range(0, len(slices), UPLOAD_CONCURRENCY):
        wave = slices[start:start + UPLOAD_CONCURRENCY]
        tasks = [asyncio.create_task(one(start + j + 1, sl))
                 for j, sl in enumerate(wave)]
        try:
            clip_ids += await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    return clip_ids
