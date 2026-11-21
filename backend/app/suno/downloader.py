"""单曲下载（SPEC §4.7，issue 02/03）：分享页 → clip 元数据 → 候选合并排序 →
（必要时 mango 授权解密）→ ffmpeg 转 MP3/WAV → 唯一名 0600 落盘。

候选优先级/白名单/CDN 下载复用 common.py；页内请求（clip、mango rights）一律走
ctx.pf（401 换 token、429 退避由其统一处理）；参考实现先嗅探下载字节，已是明文
容器则跳过授权与解密（decryptSunoDownloadedAudio，main.cjs）。
"""
from __future__ import annotations

import httpx

from .. import config, ffmedia
from ..errors import AppError, SunoAPIError
from . import common, mango


def _unwrap_clip(payload, song_id: str) -> dict | None:
    """clip 响应解包（suno-download.cjs unwrapSunoClipPayload 移植）：优先匹配歌曲 ID。"""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    cands = [v for v in (payload.get("clip"), data.get("clip"), data, payload) if isinstance(v, dict)]
    for coll in (payload.get("clips"), data.get("clips")):
        if isinstance(coll, list):
            cands.extend(v for v in coll if isinstance(v, dict))
    if not cands:
        return None
    for c in cands:
        if str(c.get("id") or c.get("clip_id") or c.get("clipId") or "") == song_id:
            return c
    return cands[0]


async def _fetch_candidate(cand: dict, song_id: str, ctx, index: int) -> tuple[bytes, str, bool]:
    """下载一个候选并返回 (可用字节, 容器, 是否经过解密)。失败抛 AppError 供上层换下一候选。"""
    ctx.check_cancel()
    tmp = ctx.workdir / f"candidate_{index}.bin"
    # 字节进度（issue 02）：逐块回调但节流写库——增量 ≥256KB 或百分比 ≥1% 才落库，
    # 日志只在跨过 25% 整数倍时打一条，避免每 64KB 块都写库/刷屏。
    # 注意：db.append_log 会把 progress 覆盖为最新日志行（既有语义），字节进度在下载期间可见
    state = {"done": 0, "pct": 0, "logged": 0}

    def on_progress(done: int, total: int) -> None:
        pct = done * 100 // total if total else 0
        if done - state["done"] < 256 * 1024 and pct - state["pct"] < 1:
            return
        state["done"], state["pct"] = done, pct
        ctx.db.update_job(ctx.job_id, progress=f"{done}/{total}")
        if total and pct < 100 and pct // 25 > state["logged"] // 25:
            state["logged"] = pct
            ctx.log(f"候选 {index + 1} 下载中：{pct}%（{done}/{total} 字节）")

    await ctx.interruptible(common.download_cdn(cand["url"], tmp, on_progress=on_progress))
    data = tmp.read_bytes()
    tmp.unlink(missing_ok=True)
    ctx.log(f"候选 {index + 1} 下载完成：{len(data)} 字节")
    # 收尾兜底：最后一块可能不足节流阈值，补写一次确保进度到达 100%
    ctx.db.update_job(ctx.job_id, progress=f"{len(data)}/{len(data)}")
    ctx.set_result({"bytes_downloaded": len(data), "candidate": index + 1})
    container = common.sniff_container(data)
    if container != "unknown":
        return data, container, False  # 明文直接用（含标记加密但实为明文的漂移情况）
    if not cand.get("encrypted"):
        raise AppError("DOWNLOAD_FAILED", "下载内容不是可识别的音频容器。")
    # 加密候选：页内 mango 授权（authToken=Clerk token），Python 侧解密
    # ponytail: rights 在下载完成后串行请求（参考实现与 CDN 下载并行）；
    # 升级路径 = asyncio.gather 并行 rights 与下载，省一个 RTT
    token = await ctx.pf.token()
    res = await ctx.pf.request(
        "POST", "/api/mango/rights",
        {"content_params": {"content_id": song_id, "content_type": "clip"}},
        cancel_event=ctx.cancel,
    )
    key, iv = mango.unwrap_rights(res.get("json"), song_id, token)
    clear = mango.decrypt_media(data, key, iv)
    container = common.sniff_container(clear)
    if container == "unknown":
        raise AppError("MANGO_DECRYPT_FAILED", "解密结果不是可识别的音频容器。")
    ctx.log(f"Mango 解密完成：{len(clear)} 字节，容器={container}")
    return clear, container, True


async def run(job: dict, ctx) -> None:
    fmt = str(job["options"].get("format") or "mp3").lower()
    if fmt not in ("mp3", "wav"):
        raise AppError("UNSUPPORTED_FORMAT", f"输出格式仅支持 mp3/wav，收到 {fmt}。")
    url = job["input"]
    ctx.workdir.mkdir(parents=True, exist_ok=True)

    ctx.stage("parse")
    common.validate_suno_url(url)
    ctx.check_cancel()
    ctx.log("读取分享页…")
    try:
        song_id = common.extract_song_id(url)
    except AppError:
        html = await ctx.interruptible(common.fetch_share_html(url))
        song_id = common.extract_song_id(html)
    ctx.set_song(song_id)
    ctx.log(f"歌曲 ID：{song_id}")

    ctx.stage("clip")
    ctx.check_cancel()
    try:
        res = await ctx.pf.request("GET", f"/api/clip/{song_id}", cancel_event=ctx.cancel)
    except SunoAPIError as e:
        if e.status == 404:
            raise AppError("INVALID_URL", "链接指向的歌曲不存在或已被删除。")
        raise
    clip = _unwrap_clip(res.get("json"), song_id)
    if not clip:
        raise AppError("DOWNLOAD_FAILED", "歌曲信息响应缺少 clip 数据。")
    meta = clip.get("metadata") if isinstance(clip.get("metadata"), dict) else {}
    title = str(clip.get("title") or meta.get("title") or f"SUNO-{song_id}")[:120]

    candidates = common.merge_candidates(clip)
    seen = {c["url"] for c in candidates}
    attempts = candidates + [
        {"url": t.format(id=song_id), "contentType": "", "delivery": "",
         "encoding": "", "encrypted": False, "source": "fallback"}
        for t in common.CDN_FALLBACK if t.format(id=song_id) not in seen
    ]
    if not attempts:
        raise AppError("DOWNLOAD_FAILED", "该歌曲没有可用的音频候选。")

    errors: list[AppError] = []
    dst = ctx.workdir / f"out.{fmt}"
    for i, cand in enumerate(attempts):
        ctx.check_cancel()
        ctx.stage("download")
        ctx.log(f"尝试候选 {i + 1}/{len(attempts)}（{'加密' if cand.get('encrypted') else '明文'}）")
        try:
            data, container, encrypted_used = await _fetch_candidate(cand, song_id, ctx, i)
            ctx.check_cancel()
            ctx.stage("transcode")
            ctx.log(f"转码为 {fmt.upper()}…")
            src = ctx.workdir / f"src.{container}"
            src.write_bytes(data)
            dst.unlink(missing_ok=True)
            await ctx.interruptible(ffmedia.transcode(src, dst, fmt))
            break
        except AppError as e:
            if e.code in ("CANCELLED", "NOT_LOGGED_IN", "RATE_LIMITED"):
                raise  # 任务级失败：换候选也无济于事
            errors.append(e)
            ctx.log(f"候选 {i + 1} 失败：{e.message}，尝试下一个")
        except httpx.HTTPError as e:
            errors.append(AppError("DOWNLOAD_FAILED", f"网络错误：{type(e).__name__}"))
            ctx.log(f"候选 {i + 1} 网络失败，尝试下一个")
    else:
        if errors and all(e.code.startswith("MANGO_") for e in errors):
            raise errors[-1]  # 全部死于解密 → 报 MANGO_*（SPEC §6）
        raise AppError("DOWNLOAD_FAILED", "所有音频候选均下载失败，请稍后重试。")

    ctx.check_cancel()
    out_dir = ctx.settings.get("download_dir") or config.system_download_dir()
    out = ffmedia.save_output(dst.read_bytes(), out_dir, title, job["id"], fmt)
    ctx.log(f"已保存：{out.name}")
    ctx.finish(str(out), {
        "song_id": song_id, "title": title, "format": fmt, "container": container,
        "source_url": cand["url"], "encrypted": encrypted_used, "bytes": out.stat().st_size,
    })
