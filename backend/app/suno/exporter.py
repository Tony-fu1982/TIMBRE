"""导出/渲染与续查（SPEC §3、§4.6；main.cjs 7784–8724 语义移植）。

状态机：submitting → pending（拿到 render_id 立即持久化）→ succeeded / failed / unknown。
- 总时限 360s（单调时钟），覆盖同步/查询/自动重试；重试绝不重置时限。
- 仅在“服务端明确终态失败”或“验证静音”时，才允许在剩余时限内至多 1 次新渲染。
- 排队/超时/断线且已知 render_id → 保持 pending + 同一 ID，可「继续查询」只读跟踪。
- 请求已发出但结果未知（NETWORK_RESULT_UNKNOWN）→ unknown，绝不自动重发。
- 续查（resume）只查询同一 render_id，绝不重新 POST 渲染。
"""
from __future__ import annotations

import asyncio
import time
from array import array
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from ..errors import AppError, SunoAPIError
from ..ffmedia import run_ff
from ..jobs import JobContext
from . import restorer
from .common import cdn_allowed, download_cdn, merge_candidates

# ---- 常量（SPEC §4.6 / main.cjs 6151–6167）----
RENDER_BUDGET_S = 360.0        # 渲染总时限（同步+查询+重试共用，绝不重置）
POLL_INTERVAL_S = 5.0          # SPEC 规定 5s 轮询
FIRST_POLL_DELAY_S = 1.0
RENDER_POST_TIMEOUT_MS = 45_000
RETRY_MIN_REMAINING_S = 45.0   # 剩余不足 45s 不再发起新渲染
SYNC_TIMEOUT_S = 40.0          # 单次工程同步上限
CLEANUP_BUDGET_S = 10.0        # 清理中间产物总预算
READY = ("complete", "completed", "streaming")
FAIL = ("error", "failed")
QUEUED = ("queued", "pending", "created", "submitted")
SONG_URL = "https://suno.com/song/"


def _timeout_code(last_status: str) -> str:
    """SPEC §6：超时仍在排队 → QUEUED_TIMEOUT，否则 RENDER_TIMEOUT（两者都可「继续查询」）。"""
    return "QUEUED_TIMEOUT" if not last_status or last_status in QUEUED else "RENDER_TIMEOUT"


async def _api(ctx: JobContext, method: str, path: str, body=None, *, timeout_ms: int = 20_000) -> dict:
    return await ctx.pf.request(method, path, body, timeout_ms=timeout_ms, cancel_event=ctx.cancel)


def _pick_clip(j: dict, rid: str) -> dict | None:
    """从 clip/feed/render-status 响应里挑出目标 clip（main.cjs pickPhaseAClip）。"""
    j = j or {}
    candidates = [j, j.get("clip"), j.get("data"), *(j.get("clips") or [])]
    for c in candidates:
        if isinstance(c, dict) and str(c.get("id") or "") == rid:
            return c
    return None


# ---- 静音校验（SPEC §4.6：仅“验证静音”可判失败）----

async def _audio_peak(src: Path, workdir: Path) -> float:
    """整段解码为 f32le 单声道 16k，取峰值。

    ponytail: 只做整段峰值判静音（detectSoundRange 的 maxPeak≤0 判据）；不做 10ms
    帧级有声区间定位——导出判定用不到。升级路径：需要裁剪静音时再移植帧级逻辑。
    """
    pcm = workdir / f"peak_{uuid4().hex[:8]}.pcm"
    try:
        await run_ff(["-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                      "-f", "f32le", str(pcm)], timeout=120)
        data = pcm.read_bytes()
        samples = array("f")
        samples.frombytes(data[: len(data) // 4 * 4])
        return max((abs(x) for x in samples), default=0.0)
    finally:
        pcm.unlink(missing_ok=True)


async def _probe_audio(ctx: JobContext, clip: dict) -> tuple[str, str]:
    """返回 (verdict, url)：sound / silent / unverifiable / error。"""
    cands = merge_candidates(clip)
    if not cands:
        return "error", ""
    plain = [c for c in cands if not c["encrypted"] and c["delivery"] != "mango"]
    if not plain:
        # ponytail: 仅有加密候选时本地无法解码（mango 解密在下载链路）；
        # “无法验证”≠静音，按 SPEC 接受。升级路径：接入 mango 解密后再验证。
        ctx.debug("成品仅有加密音频候选，无法本地校验静音，按有声接受。")
        return "unverifiable", cands[0]["url"]
    plain.sort(key=lambda c: 0 if (c["contentType"] == "audio/mpeg"
                                   or urlsplit(c["url"]).path.lower().endswith(".mp3")) else 1)
    for c in plain[:2]:
        url = c["url"]
        if not cdn_allowed(url):
            continue
        dest = ctx.workdir / f"probe_{uuid4().hex[:8]}.audio"
        try:
            await download_cdn(url, dest)
            if dest.stat().st_size < 1024:
                continue
            peak = await _audio_peak(dest, ctx.workdir)
            return ("silent" if peak <= 0 else "sound"), url
        except (AppError, OSError) as e:
            ctx.debug(f"成品音频校验暂不可用（{getattr(e, 'code', type(e).__name__)}），换候选或继续等待。")
        finally:
            dest.unlink(missing_ok=True)
    return "error", ""


# ---- 工程同步 / 渲染提交 ----

async def _sync_project(project: dict, ctx: JobContext, deadline: float) -> tuple[dict, str]:
    """导出前重读版本与片段关联；预算内至多重试一次（SPEC §4.6）。"""
    last: AppError | None = None
    for _ in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            budget = min(SYNC_TIMEOUT_S, remaining)
            state, version = await asyncio.wait_for(restorer.wait_stable(
                ctx, project["project_id"], clip_count=len(project["clip_ids"]),
                stable_reads=1, timeout_s=budget), budget)
            ids = {c.get("clipId") for c in restorer.iter_clips(state)}
            if not set(project["clip_ids"]) <= ids:
                raise AppError("STUDIO_STATE_NOT_STABLE", "工程重读缺少片段关联。")
            if not restorer.has_pitch_restore(state, set(project["clip_ids"])):
                # 确定性失败：重试无意义，直接中止导出
                raise AppError("STUDIO_PITCH_RESTORE_NOT_SAVED",
                               "导出前校验发现 +24 音高还原未保存成功，已中止导出。")
            return state, version
        except TimeoutError:
            continue
        except AppError as e:
            if e.code in ("CANCELLED", "NOT_LOGGED_IN", "STUDIO_PITCH_RESTORE_NOT_SAVED"):
                raise
            last = e
            ctx.debug(f"导出前工程同步失败（{e.code}），在剩余时限内重试。")
    raise AppError("SONG_SYNC_TIMEOUT",
                   f"导出前工程同步超时：{last.message if last else '剩余时限不足'}")


def _render_body(project: dict) -> dict:
    return {
        "title": project["title"], "lyrics": "", "state": project["state"],
        "project_id": project["project_id"],
        "from_studio_project_id": project["project_id"],
        "start_beats": project["start_beats"], "end_beats": project["end_beats"],
        "downbeats": project["downbeats"],
        "web_client_pathname": "/studio", "export_mode": "rendered_context_window",
    }


async def _submit_render(project: dict, ctx: JobContext, deadline: float) -> str:
    """POST 渲染；返回 render_id（失败返回 ""，结果未知抛 NETWORK_RESULT_UNKNOWN）。

    ponytail: SPEC 指定 POST /api/studio/project/{pid}/render-clip/{vid}，2026-11 实测
    该端点已被 Suno 移除（404）。现网 Studio 前端 bundle 实据：POST /api/studio/render-state
    （与反编译 main.cjs 一致，body 同构；render-state-multitrack 为茎下载路径，不用）。
    不做备用端点喷洒（避免双重渲染）。
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AppError("SONG_SYNC_TIMEOUT")
    ctx.set_render("", "submitting")
    remaining_ms = max(1, int(remaining * 1000))
    path = "/api/studio/render-state"
    try:
        r = await asyncio.wait_for(_api(ctx, "POST", path, _render_body(project),
                                       timeout_ms=min(RENDER_POST_TIMEOUT_MS, remaining_ms)), remaining)
    except (TimeoutError, asyncio.CancelledError) as e:
        ctx.set_render("", "unknown")
        if isinstance(e, asyncio.CancelledError):
            raise
        raise AppError("NETWORK_RESULT_UNKNOWN") from e
    except SunoAPIError as e:
        if e.code == "NETWORK_RESULT_UNKNOWN":
            ctx.set_render("", "unknown")
            raise AppError("NETWORK_RESULT_UNKNOWN",
                           "渲染请求已发出但结果未知：已停止自动重试，请先在 Suno 核对，避免重复创建渲染。") from e
        if e.code in ("CANCELLED", "NOT_LOGGED_IN"):
            raise
        if 0 < e.status < 500:
            # 服务端确定性拒绝（402/400/404…）：渲染确定未创建，落终态失败而非 unknown
            ctx.set_render("", "failed")
            ctx.log(f"渲染提交被 Suno 拒绝（HTTP {e.status}），未创建渲染。")
            if e.status == 402:
                raise AppError("INSUFFICIENT_CREDITS", "Suno 账号积分或订阅不足，无法创建渲染。") from e
            raise AppError("SUNO_HTTP_ERROR",
                           f"Suno 拒绝了渲染请求（HTTP {e.status}），未创建渲染。", e.status) from e
        ctx.debug(f"渲染提交结果未知（{e.code}）")
        return ""
    j = r.get("json") or {}
    rid = str(j.get("id") or "")
    if not rid:
        clips = j.get("clips") or []
        if clips and isinstance(clips[0], dict):
            rid = str(clips[0].get("id") or "")
    return rid


def _save_progress(ctx: JobContext, project: dict, render_ids: list) -> None:
    """尽早持久化工程/渲染信息，供超时后「继续查询」与原任务清理使用。"""
    ctx.set_result({
        "project_id": project["project_id"], "version_id": project["version_id"],
        "title": project["title"], "clip_ids": project["clip_ids"],
        "render_ids": list(render_ids),
    })


# ---- 轮询 ----

async def _poll_render(ctx: JobContext, rid: str, deadline: float) -> tuple[str, str]:
    last_status = ""

    def observe(status: str) -> None:
        nonlocal last_status
        last_status = status

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return "timeout", _timeout_code(last_status)
    try:
        return await asyncio.wait_for(ctx.interruptible(_poll_render_until(ctx, rid, observe)), remaining)
    except TimeoutError:
        return "timeout", _timeout_code(last_status)


async def _poll_render_until(ctx: JobContext, rid: str, observe) -> tuple[str, str]:
    """轮询同一 render_id 直到终态/超时。返回 (success|failed|timeout, detail)。

    ponytail: render-status 查询 shape 无实据，按 clip JSON 同构解析；首次 404/405/422
    即停用该探针，主轮询走已验证的 GET /api/clip/{id} + POST /api/feed/v3。HITL 联调确认。
    """
    probe_enabled = True
    first = True
    while True:
        ctx.check_cancel()
        delay = FIRST_POLL_DELAY_S if first else POLL_INTERVAL_S
        first = False
        await asyncio.sleep(delay)
        sources: list[dict] = []
        if probe_enabled:
            try:
                r = await _api(ctx, "GET", f"/api/studio/render-status?clip_id={rid}")
                j = r.get("json") or {}
                c = _pick_clip(j, rid)
                if c is None and j.get("status") and str(j.get("clip_id") or j.get("id") or "") == rid:
                    c = {"id": rid, "status": j["status"]}
                if c:
                    sources.append(c)
            except SunoAPIError as e:
                if e.status in (404, 405, 422):
                    probe_enabled = False
                    ctx.debug("render-status 端点不可用，本次运行改用 clip/feed 轮询。")
                elif e.code in ("CANCELLED", "NOT_LOGGED_IN"):
                    raise
            except AppError as e:
                if e.code in ("CANCELLED", "NOT_LOGGED_IN"):
                    raise
        try:
            r = await _api(ctx, "GET", f"/api/clip/{rid}")
            c = _pick_clip(r.get("json") or {}, rid)
            if c:
                sources.append(c)
        except AppError as e:
            if e.code in ("CANCELLED", "NOT_LOGGED_IN"):
                raise
            ctx.debug(f"clip 查询暂不可用（{e.code}），继续等待。")
        try:
            r = await _api(ctx, "POST", "/api/feed/v3",
                           {"filters": {"ids": {"presence": "True", "clipIds": [rid]}}, "limit": 1})
            for c in (r.get("json") or {}).get("clips") or []:
                if isinstance(c, dict) and str(c.get("id")) == rid:
                    sources.append(c)
        except AppError as e:
            if e.code in ("CANCELLED", "NOT_LOGGED_IN"):
                raise
            ctx.debug(f"feed 查询暂不可用（{e.code}），继续等待。")
        for c in sources:
            s = str(c.get("status") or "").lower()
            if s:
                observe(s)
        if any(str(c.get("status") or "").lower() in FAIL for c in sources):
            return "failed", "RENDER_ERROR"
        ready = [c for c in sources if str(c.get("status") or "").lower() in READY]
        if ready:
            clip = max(ready, key=lambda c: len(merge_candidates(c)))
            verdict, url = await _probe_audio(ctx, clip)
            if verdict == "silent":
                return "failed", "AUDIO_SILENT"
            if verdict in ("sound", "unverifiable"):
                cands = merge_candidates(clip)
                return "success", url or (cands[0]["url"] if cands else "")
            # error：CDN 暂不可用 → 不判失败，继续轮询同一 ID
        # 排队/处理中/查询暂不可用：保持 pending，绝不重新提交渲染


# ---- 清理（尽力而为，绝不影响成品判定）----

async def _cleanup(ctx: JobContext, clip_ids: list, render_ids: list, final_id: str) -> None:
    ids = [i for i in dict.fromkeys([*clip_ids, *render_ids]) if i and i != final_id]
    if not ids:
        return
    deadline = time.monotonic() + CLEANUP_BUDGET_S
    pending = ids
    for _ in range(2):  # 首次 + 至多一次补清
        if not pending or time.monotonic() >= deadline:
            break
        try:
            r = await _api(ctx, "POST", "/api/gen/trash", {"trash": True, "clip_ids": pending},
                           timeout_ms=5000)
            j = r.get("json") or {}
            if j.get("is_trashed") is True:
                confirmed = set(j.get("ids") or []) & set(pending)
                pending = [i for i in pending if i not in confirmed]
                if confirmed:
                    ctx.log(f"已清理 {len(confirmed)} 个中间片段/渲染（保留成品 {final_id}）。")
            else:
                ctx.debug("清理接口未确认 is_trashed，尝试补清。")
        except Exception as e:  # noqa: BLE001 - 清理失败（含取消）绝不失败任务
            ctx.debug(f"清理中间产物失败（{getattr(e, 'code', type(e).__name__)}），不影响成品。")
    if pending:
        ctx.debug(f"仍有 {len(pending)} 个中间产物未确认清理，可稍后在 Suno 手动处理。")


# ---- 主流程 ----

async def export(project: dict, ctx: JobContext) -> dict:
    """渲染导出。成功返回 song_id/url（由 pipeline 统一 finish）；失败抛 AppError。"""
    ctx.check_cancel()
    deadline = time.monotonic() + RENDER_BUDGET_S
    render_ids: list[str] = []
    state, version = await _sync_project(project, ctx, deadline)
    project["state"] = restorer.build_render_state(state)
    project["version_id"] = version
    _save_progress(ctx, project, render_ids)
    retry_used = False
    while True:
        ctx.check_cancel()
        rid = await _submit_render(project, ctx, deadline)
        if not rid:
            # SPEC §4.6：消耗性 POST 未拿到 render_id → 结果未知，绝不自动重发（避免重复渲染）
            ctx.set_render("", "unknown")
            raise AppError("NETWORK_RESULT_UNKNOWN",
                           "渲染提交未返回 ID，结果待确认。请先在 Suno 核对是否已产生渲染，避免重复创建。")
        render_ids.append(rid)
        ctx.set_render(rid, "pending")
        _save_progress(ctx, project, render_ids)
        ctx.log(f"已提交渲染 {rid}，开始轮询（总时限 {RENDER_BUDGET_S:.0f}s，间隔 {POLL_INTERVAL_S:.0f}s）。")
        outcome, detail = await _poll_render(ctx, rid, deadline)
        if outcome == "success":
            ctx.set_render(rid, "succeeded")
            await _cleanup(ctx, project["clip_ids"], render_ids, rid)
            ctx.log(f"导出成功：{SONG_URL}{rid}")
            return {"song_id": rid, "url": SONG_URL + rid, "audio_url": detail,
                    "project_id": project["project_id"], "version_id": project["version_id"],
                    "render_ids": render_ids}
        if outcome == "failed":
            if not retry_used and deadline - time.monotonic() > RETRY_MIN_REMAINING_S:
                retry_used = True
                ctx.set_render(rid, "failed")
                ctx.log(f"渲染 {rid} 确定性失败（{detail}），在剩余时限内重试一次（不重置总时限）。")
                state, version = await _sync_project(project, ctx, deadline)
                project["state"] = restorer.build_render_state(state)
                project["version_id"] = version
                continue
            ctx.set_render(rid, "failed")
            raise AppError(detail)
        # timeout：已知 render_id → 保持 pending + 同一 ID，交「继续查询」只读跟踪
        raise AppError(detail)  # RENDER_TIMEOUT / QUEUED_TIMEOUT（SPEC §6 均显示「继续查询」）


async def resume_query(render_id: str, ctx: JobContext) -> dict:
    """只读续查同一 render_id（SPEC §3）：绝不重新提交渲染，独立 360s 上限。"""
    if not render_id:
        raise AppError("EXPORT_RESUME_UNAVAILABLE", "没有可继续查询的渲染 ID。")
    ctx.check_cancel()
    ctx.set_render(render_id, "pending")
    ctx.log(f"继续查询渲染 {render_id}（只读，不重新提交渲染）。")
    deadline = time.monotonic() + RENDER_BUDGET_S
    outcome, detail = await _poll_render(ctx, render_id, deadline)
    if outcome == "success":
        ctx.set_render(render_id, "succeeded")
        ctx.log(f"渲染已完成：{SONG_URL}{render_id}")
        return {"song_id": render_id, "url": SONG_URL + render_id, "audio_url": detail}
    if outcome == "failed":
        ctx.set_render(render_id, "failed")
        raise AppError(detail)
    # 超时：保持 pending，可再次「继续查询」
    raise AppError(detail)


async def resume_run(job: dict, ctx: JobContext) -> None:
    """「继续查询」任务 handler：查询原任务渲染，终态回写原任务（SPEC §3）。"""
    orig_id = (job.get("options") or {}).get("resume_of")
    orig = ctx.db.get_job(orig_id) if orig_id else None
    if not orig:
        raise AppError("NOT_FOUND", "原任务不存在，无法继续查询。")
    rid = orig.get("render_id") or ""
    if orig.get("render_state") != "pending" or not rid:
        raise AppError("EXPORT_RESUME_UNAVAILABLE",
                       "该任务没有可继续查询的渲染：仅渲染仍在等待的任务可用。")
    try:
        out = await resume_query(rid, ctx)
    except AppError as e:
        if e.code in ("RENDER_ERROR", "AUDIO_SILENT"):
            ctx.db.update_job(orig_id, render_state="failed")
            ctx.bus.publish("job", ctx.db.snapshot(orig_id))
        raise  # 超时/取消/网络未知：原任务保持 pending，可再次续查
    prev = orig.get("result") or {}
    result = {**prev, **out, "resume_of": orig_id}
    ctx.db.update_job(orig_id, status="success", render_state="succeeded",
                      suno_song_id=out["song_id"], result=result,
                      error_code="", error_msg="")
    ctx.bus.publish("job", ctx.db.snapshot(orig_id))
    await _cleanup(ctx, prev.get("clip_ids") or [], prev.get("render_ids") or [rid],
                   out["song_id"])
    ctx.log(f"原任务 #{orig_id} 渲染已完成：{out['url']}")
    ctx.finish(result=result)
