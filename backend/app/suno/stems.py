"""分轨/人声下载（SPEC §4.7，issue 10/11）。施工：stems 代理。

状态机移植 suno-stems.cjs：stems/pages 历史复用判定 → c/check 预检（fail-closed）→
generate/v2-web 单次提交（消耗性请求永不重试）→ feed/v3 轮询（3s/600s）→
全分轨按类型分组选用户侧 / 人声按 complement_of_id 互补配对 →
逐轨 CDN 下载 + mango 解密（匿名优先、登录态兜底，suno-stem-network.cjs）→
ffmpeg 转码 → stdlib zipfile 打包（命名规则 suno-stem-archive.cjs）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import unicodedata
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from .. import config, ffmedia
from ..errors import AppError, SunoAPIError
from .common import (cdn_allowed, download_cdn, extract_song_id, fetch_share_html,
                     merge_candidates, sniff_container, validate_suno_url)
from .mango import decrypt_media, unwrap_rights

POLL_INTERVAL = 3.0
POLL_TIMEOUT = 600.0
MAX_STEMS = 64
MAX_TOTAL_BYTES = 2 * 1024 ** 3
FAILED = {"error", "failed", "cancelled", "canceled"}
ID_RE = re.compile(r"[a-z0-9_-]{6,128}", re.I)
_MAXINT = 2 ** 63 - 1
_EXT = {"mp4": "m4a", "webm": "webm", "ogg": "ogg", "mp3": "mp3"}
_NAME_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = ({"con", "prn", "aux", "nul"}
             | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)})


# ---------- 纯函数：选择/分类/命名（移植 suno-stems.cjs 63-182） ----------

def _batch_index(clip: dict) -> int:
    v = clip.get("batch_index")
    return v if isinstance(v, int) and not isinstance(v, bool) else _MAXINT


def _batch_or_neg(clip: dict) -> int:
    v = clip.get("batch_index")
    return v if isinstance(v, int) and not isinstance(v, bool) else -1


def _clip_time(clip: dict) -> float:
    raw = str(clip.get("created_at") or clip.get("updated_at") or "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _matches(meta: dict, mode: str) -> bool:
    if mode == "vocal":
        try:
            type_ok = float(meta.get("stem_type_id")) == 91
        except (TypeError, ValueError):
            type_ok = False
        return (type_ok and meta.get("stem_type_group_name") == "Lead Vocal"
                and meta.get("stem_task") in ("extract", "remove"))
    return meta.get("stem_task") == "twelve"


def _classify(clip, song_id: str) -> str:
    if not isinstance(clip, dict):
        return "unknown"
    cid = str(clip.get("id") or "")
    meta = clip.get("metadata") or {}
    if not ID_RE.fullmatch(cid) or cid == song_id or meta.get("stem_from_id") != song_id:
        return "unknown"
    if _matches(meta, "stems"):
        return "full"
    if _matches(meta, "vocal"):
        return "vocal"
    return "unknown"


def _is_ready(clip: dict) -> bool:
    return clip.get("status") == "complete" and cdn_allowed(str(clip.get("audio_url") or ""))


def select_full(clips: list) -> list:
    """按 stem_type_group_name 分组，整组静音可省略，取 batch_index 最大的用户侧。"""
    groups: dict[str, list] = {}
    for clip in clips:
        name = str((clip.get("metadata") or {}).get("stem_type_group_name") or "")
        if name:
            groups.setdefault(name, []).append(clip)
    picked = []
    for members in groups.values():
        if all((c.get("metadata") or {}).get("is_loudness_under_threshold") is True for c in members):
            continue
        members.sort(key=_batch_index)  # 稳定排序，取最后 = batch_index 最大
        picked.append(members[-1])
    picked.sort(key=_batch_index)
    return picked


def select_vocal(clips: list) -> list:
    """complement_of_id 双向互配 extract+remove；缺对返回空，绝不视为成功。"""
    by_id = {str(c.get("id") or ""): c for c in clips}
    pairs, seen = [], set()
    for cid, clip in by_id.items():
        if cid in seen:
            continue
        meta = clip.get("metadata") or {}
        other = by_id.get(str(meta.get("complement_of_id") or ""))
        if other is None:
            continue
        ometa = other.get("metadata") or {}
        if str(ometa.get("complement_of_id") or "") != cid:
            continue
        tasks = {str(meta.get("stem_task") or ""), str(ometa.get("stem_task") or "")}
        if not {"extract", "remove"} <= tasks:
            continue
        seen.add(cid)
        seen.add(str(other.get("id") or ""))
        pairs.append((clip, other))
    if not pairs:
        return []
    pairs.sort(key=lambda p: (max(_clip_time(p[0]), _clip_time(p[1])),
                              max(_batch_or_neg(p[0]), _batch_or_neg(p[1]))))
    pair = pairs[-1]
    order = {"extract": 0, "remove": 1}
    return sorted(pair, key=lambda c: order.get(str((c.get("metadata") or {}).get("stem_task") or ""), 2))


def _select(clips: list, mode: str) -> list:
    return select_vocal(clips) if mode == "vocal" else select_full(clips)


def _public_stem(clip: dict, mode: str) -> dict:
    meta = clip.get("metadata") or {}
    task = str(meta.get("stem_task") or "")
    type_ = ("人声" if task == "extract" else "伴奏") if mode == "vocal" \
        else str(meta.get("stem_type_group_name") or "")
    return {"id": str(clip.get("id") or ""), "title": str(clip.get("title") or ""), "type": type_}


def generation_body(song_id: str, title: str, mode: str) -> dict:
    """04 §1 逐字段复刻；唯一消耗性请求的 body。"""
    vocal = mode == "vocal"
    body = {
        "token": None, "task": "gen_stem", "generation_type": "TEXT", "title": title,
        "tags": "", "negative_tags": "", "mv": "chirp-v3-5-b", "prompt": "",
        "make_instrumental": True, "user_uploaded_images_b64": None,
        "metadata": {"web_client_pathname": "/create", "create_mode": "custom",
                     "disable_volume_normalization": False, "is_remix": True},
        "override_fields": [], "cover_clip_id": None, "cover_start_s": None, "cover_end_s": None,
        "persona_id": None, "artist_clip_id": None, "artist_start_s": None, "artist_end_s": None,
        "continue_clip_id": song_id, "continued_aligned_prompt": None, "continue_at": None,
        "stem_type_id": 91,
        "stem_type_group_name": "Lead Vocal" if vocal else "Twelve",
        "stem_task": "extract" if vocal else "twelve",
        "transaction_uuid": str(uuid.uuid4()), "token_provider": None,
    }
    if vocal:
        body["stem_name"] = "Lead Vocal"
    return body


def _safe_name(value, fallback: str, limit: int) -> str:
    # ponytail: 字符数上限替代参考实现字节上限（中文文件名场景差异可忽略）
    s = unicodedata.normalize("NFKC", str(value or ""))
    s = " ".join(_NAME_BAD.sub(" ", s).split()).strip(" .")[:limit].strip(" .")
    if not s:
        s = fallback
    if s.split(".")[0].lower() in _RESERVED:
        s = "_" + s
    return s


def _add_clip(candidates: dict, clip, song_id: str, mode: str) -> bool:
    if not isinstance(clip, dict):
        return False
    cid = str(clip.get("id") or "")
    if not ID_RE.fullmatch(cid) or cid == song_id:
        return False
    prev = candidates.get(cid) or {}
    meta = {**(prev.get("metadata") or {}), **(clip.get("metadata") or {})}
    if meta.get("stem_from_id") != song_id or not _matches(meta, mode):
        return False
    candidates[cid] = {**prev, **clip, "metadata": meta}
    return True


# ---------- 网络：feed / 准备状态机 / rights / 下载 ----------

async def _feed(pf, ids: list, cancel) -> list:
    clips = []
    for offset in range(0, len(ids), 50):
        batch = ids[offset:offset + 50]
        cursor, seen = None, set()
        while True:
            body = {"filters": {"ids": {"presence": "True", "clipIds": batch}}, "limit": len(batch)}
            if cursor is not None:
                body["cursor"] = cursor
            res = await pf.request("POST", "/api/feed/v3", body, cancel_event=cancel)
            data = res.get("json") or {}
            if not isinstance(data.get("clips"), list):
                raise AppError("SUNO_HTTP_ERROR", "Suno 返回的分轨状态列表无效。")
            clips.extend(data["clips"])
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            key = json.dumps(cursor, sort_keys=True)
            if cursor is None or key in seen:
                raise AppError("SUNO_HTTP_ERROR", "Suno 分轨状态分页无效。")
            seen.add(key)
    return clips


def _generate_error(e: SunoAPIError) -> AppError:
    """消耗性请求的错误映射：429/5xx/传输失败一律待确认，绝不重试。"""
    if e.status == 401:
        return e  # NOT_LOGGED_IN，未提交
    if e.status == 402:
        return AppError("INSUFFICIENT_CREDITS", "Suno 账号积分或订阅不足，无法生成分轨。")
    if e.status == 403:
        return AppError("SUNO_HTTP_ERROR", "Suno 拒绝了分轨请求，请在网站确认账号权限或完成验证后重试。", 403)
    if e.status == 404:
        return AppError("NOT_FOUND", "歌曲或分轨不存在，或当前 Suno 账号无权访问。")
    if e.status == 429 or e.status >= 500 or e.status == 0:
        return AppError("GENERATION_UNCERTAIN",
                        "生成请求结果未确认，可能已提交并消耗积分。请稍后重试读取已有分轨，勿重复生成。")
    return AppError("SUNO_HTTP_ERROR", f"Suno 拒绝了生成请求（HTTP {e.status}），未提交。", e.status)


async def _prepare(ctx, song_id: str, mode: str, title: str) -> tuple[list, bool, str]:
    """历史复用判定 + 预检 + 单次生成 + 轮询。返回 (detected, generated, decision)。"""
    pf = ctx.pf
    base = f"/api/clip/{song_id}/stems"
    res = await pf.request("GET", base + "/pages", cancel_event=ctx.cancel)
    pages = (res.get("json") or {}).get("pages")
    if not isinstance(pages, int) or isinstance(pages, bool) or not 0 <= pages <= 1000:
        raise AppError("SUNO_HTTP_ERROR", "Suno 返回的分轨页数无效。")
    candidates: dict[str, dict] = {}
    history = {"full": 0, "vocal": 0, "unknown": 0}
    for page in range(pages):
        ctx.check_cancel()
        res = await pf.request("GET", f"{base}?page={page}", cancel_event=ctx.cancel)
        data = res.get("json") or {}
        stems = data.get("stems")
        if not isinstance(stems, list) or ("current_page" in data and data["current_page"] != page):
            raise AppError("SUNO_HTTP_ERROR", "Suno 返回的分轨分页内容无效。")
        for clip in stems:
            history[_classify(clip, song_id)] += 1
            _add_clip(candidates, clip, song_id, mode)
    ctx.log(f"历史分轨：full={history['full']} vocal={history['vocal']} unknown={history['unknown']}，"
            f"本模式候选 {len(candidates)}")

    if mode == "vocal":
        if candidates:
            needs, decision = False, "reuse-vocal"
        elif history["unknown"]:
            raise AppError("GENERATION_UNCERTAIN", "现有分轨类型不明，请先在 Suno 核对已有结果。")
        else:
            needs, decision = True, "generate-vocal-empty"
    elif candidates:
        needs, decision = False, "reuse-existing"
    elif pages == 0:
        needs, decision = True, "generate-full-empty"
    elif history["full"] == 0 and history["vocal"] > 0 and history["unknown"] == 0:
        needs, decision = True, "generate-full-after-vocal"
    else:
        # 类型不明的历史可能是未识别的完整生成：fail-closed，不冒重复消耗积分的风险
        needs, decision = False, "block-full-ambiguous"

    generated = False
    if needs:
        ctx.check_cancel()
        res = await pf.request("POST", "/api/c/check", {"ctype": "generation"}, cancel_event=ctx.cancel)
        required = (res.get("json") or {}).get("required")
        if required is not False:
            # SPEC §6：VERIFICATION_REQUIRED → 弹出 Chrome 窗口让用户完成人机验证
            try:
                page = await ctx.chrome.ensure(visible=True)
                await page.bring_to_front()
            except Exception:
                pass  # 弹窗失败不吞错误码；用户仍可手动打开 Chrome 完成验证
            if required is True:
                raise AppError("VERIFICATION_REQUIRED")
            raise AppError("VERIFICATION_REQUIRED",
                           "无法确认 Suno 生成验证状态，未提交生成。请在 Chrome 的 Suno 网站完成人机验证后重试。")
        ctx.check_cancel()
        ctx.log("预检通过，提交分轨生成（消耗积分，仅提交一次，不自动重试）…")
        ctx.set_result({"generation_pending": True})
        try:
            res = await pf.request("POST", "/api/generate/v2-web/",
                                   generation_body(song_id, title, mode), cancel_event=ctx.cancel)
        except SunoAPIError as e:
            error = _generate_error(e)
            if error.code != "GENERATION_UNCERTAIN":
                ctx.set_result({})
            raise error from None
        data = res.get("json")
        if not isinstance(data, dict):
            raise AppError("GENERATION_UNCERTAIN", "Suno 返回了无法识别的生成结果，请稍后读取已有分轨，勿重复生成。")
        clips = data.get("clips")
        if not isinstance(clips, list) or not clips:
            raise AppError("GENERATION_UNCERTAIN", "Suno 未返回生成分轨 ID，请稍后重试读取已有结果，勿重复生成。")
        for clip in clips:
            if not _add_clip(candidates, clip, song_id, mode):
                raise AppError("GENERATION_UNCERTAIN",
                               "Suno 返回的分轨 ID 无效或不属于原歌曲，结果待确认。请先在 Suno 核对已有分轨，勿重复提交。")
        batch_size = data.get("batch_size")
        if isinstance(batch_size, int) and not isinstance(batch_size, bool) and batch_size > len(candidates):
            raise AppError("GENERATION_UNCERTAIN",
                           f"Suno 返回的分轨 ID 不完整（声明 {batch_size}，收到 {len(candidates)}），"
                           "请稍后重试读取已有结果，勿重复生成。")
        generated = True
        ctx.log(f"生成已提交，收到 {len(candidates)} 个分轨 ID，等待 Suno 完成…")
    if not candidates:
        if decision == "block-full-ambiguous":
            raise AppError("GENERATION_UNCERTAIN",
                           "现有分轨记录无法确认类型，为避免重复消耗积分，未提交完整分轨生成。请先在 Suno 核对已有分轨。")
        raise AppError("GENERATION_FAILED", "现有结果中没有可识别的分轨，未重复提交生成。")

    deadline = time.monotonic() + POLL_TIMEOUT
    while True:
        ctx.check_cancel()
        if time.monotonic() >= deadline:
            msg = ("等待分轨超时。已提交生成，请稍后重试下载以复用已有结果，勿重复提交。" if generated
                   else "等待分轨超时，请稍后重试以读取已有结果。")
            raise AppError("TIMEOUT", msg)
        clips = list(candidates.values())
        detected = _select(clips, mode)
        if any(c.get("status") in FAILED for c in detected):
            raise AppError("GENERATION_FAILED", "部分 Suno 分轨生成失败，已停止下载，未自动重新生成。")
        settled = all(_is_ready(c) or c.get("status") in FAILED for c in clips)
        ready = bool(detected) and all(_is_ready(c) for c in detected)
        if ready and (mode == "vocal" or settled):
            if generated:
                ctx.set_result({})
            return detected, generated, decision
        if not detected and settled:
            # SPEC §4.7 / issue 11：缺对或无有效结果 → 给出失败原因，绝不追加消耗请求
            if any(c.get("status") in FAILED for c in clips):
                raise AppError("GENERATION_FAILED", "部分 Suno 分轨生成失败，已停止下载，未自动重新生成。")
            if mode == "vocal":
                raise AppError("GENERATION_FAILED",
                               "人声/伴奏互补对未形成（缺少 extract 或 remove），未追加生成请求。请稍后在 Suno 核对已有结果。")
            raise AppError("GENERATION_FAILED", "现有结果中没有可下载的有效分轨（全部静音或状态无效），未重复提交生成。")
        ctx.log(f"等待分轨完成：{sum(_is_ready(c) for c in clips)}/{len(clips)} 就绪…")
        try:
            await asyncio.wait_for(ctx.cancel.wait(), timeout=POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass
        else:
            raise AppError("CANCELLED")
        ctx.check_cancel()
        updates = await _feed(pf, list(candidates), ctx.cancel)
        for clip in updates:
            if isinstance(clip, dict) and clip.get("id") in candidates:
                _add_clip(candidates, clip, song_id, mode)


async def _anonymous_rights(ctx, stem_id: str) -> dict:
    """页内无 Authorization 的 rights（SPEC §4.7:170 页内 POST；§7:246 Python 直连仅限 CDN/分享页）。
    auth_token 用响应里的 glt，不是 Clerk；失败由登录态兜底。"""
    try:
        res = await ctx.pf.request("POST", "/api/mango/rights",
                                   {"content_params": {"content_id": stem_id, "content_type": "clip"}},
                                   anonymous=True, cancel_event=ctx.cancel)
    except SunoAPIError as e:
        raise AppError("MANGO_DECRYPT_FAILED", f"匿名授权失败（HTTP {e.status or '网络'}）") from None
    j = res.get("json")
    if not isinstance(j, dict):
        raise AppError("MANGO_DECRYPT_FAILED", "匿名授权响应无效。")
    key, iv, glt = j.get("key"), j.get("iv"), j.get("glt")
    if not all(isinstance(v, str) and v.strip() for v in (key, iv, glt)):
        raise AppError("MANGO_DECRYPT_FAILED", "匿名授权缺少 key/iv/glt 字段。")
    if len(key) > 4096 or len(iv) > 4096 or len(glt) > 8192:
        raise AppError("MANGO_DECRYPT_FAILED", "匿名授权字段超长。")
    return j


async def _decrypt(ctx, stem_id: str, data: bytes) -> tuple[bytes, str]:
    """先嗅探：已知容器直接当明文用；否则匿名解密优先，任何失败改登录态（只解密一次）。"""
    container = sniff_container(data)
    if container != "unknown":
        return data, container
    try:
        rights = await _anonymous_rights(ctx, stem_id)
        key, iv = unwrap_rights(rights, stem_id, rights["glt"])
        clear = decrypt_media(data, key, iv)
        container = sniff_container(clear)
        if container != "unknown":
            ctx.log(f"分轨 {stem_id} 匿名解密完成（{len(clear)} 字节，{container}）")
            return clear, container
        raise AppError("MANGO_DECRYPT_FAILED", "解密结果不是可识别的音频容器。")
    except AppError as e:
        if e.code == "CANCELLED":
            raise
        ctx.debug(f"分轨 {stem_id} 匿名解密不可用（{e.code}），改用登录态授权。")
    ctx.check_cancel()
    token = await ctx.pf.token()
    if len(token) > 8192:
        raise AppError("MANGO_DECRYPT_FAILED", "登录态 token 超长。")
    try:
        res = await ctx.pf.request("POST", "/api/mango/rights",
                                   {"content_params": {"content_id": stem_id, "content_type": "clip"}},
                                   cancel_event=ctx.cancel)
    except SunoAPIError as e:
        if e.status == 403:
            raise AppError("SUNO_HTTP_ERROR", "Suno 拒绝签发该分轨的解密授权（403），请确认账号权限。", 403) from None
        raise
    rights = res.get("json")
    if isinstance(rights, dict) and any(
            isinstance(rights.get(f), str) and len(rights[f]) > 4096 for f in ("key", "iv")):
        raise AppError("MANGO_DECRYPT_FAILED", "rights 字段超长。")
    key, iv = unwrap_rights(rights, stem_id, token)
    clear = decrypt_media(data, key, iv)
    container = sniff_container(clear)
    if container == "unknown":
        raise AppError("MANGO_DECRYPT_FAILED", "解密结果不是可识别的音频容器。")
    ctx.log(f"分轨 {stem_id} 登录态解密完成（{len(clear)} 字节，{container}）")
    return clear, container


async def _download_track(ctx, clip: dict, index: int) -> tuple[bytes, str]:
    stem_id = str(clip["id"])
    cands = merge_candidates(clip)
    if not cands:
        raise AppError("DOWNLOAD_FAILED", f"分轨 {stem_id} 没有可用的音频地址。")
    data = None
    for i, cand in enumerate(cands):
        ctx.check_cancel()
        try:
            p = await ctx.interruptible(download_cdn(cand["url"], ctx.workdir / f"stem_{index}_{i}.bin"))
            data = p.read_bytes()
            break
        except AppError as e:
            if e.code in ("CANCELLED", "NOT_LOGGED_IN"):
                raise
            ctx.debug(f"分轨 {stem_id} 候选 {i + 1} 下载失败：{e.code}")
    if data is None:
        raise AppError("DOWNLOAD_FAILED", f"分轨 {stem_id} 所有候选地址下载失败，请稍后重试。")
    # 解密只走一次：候选循环只覆盖下载，解密失败不落到下一候选（suno-stem-network.cjs）
    return await _decrypt(ctx, stem_id, data)


# ---------- 入口 ----------

async def run(job: dict, ctx) -> None:
    opts = job.get("options") or {}
    mode = str(opts.get("mode") or "stems")
    fmt = str(opts.get("format") or "mp3").lower()
    if mode not in ("stems", "vocal"):
        raise AppError("UNSUPPORTED_FORMAT", f"不支持的分轨模式：{mode}。")
    if fmt not in ("mp3", "wav"):
        raise AppError("UNSUPPORTED_FORMAT", f"输出格式仅支持 mp3/wav，收到 {fmt}。")
    ctx.check_cancel()
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    url = validate_suno_url(job["input"])
    try:
        song_id = extract_song_id(url)
    except AppError:
        song_id = extract_song_id(await ctx.interruptible(fetch_share_html(url)))
    ctx.set_song(song_id)
    ctx.log(f"歌曲 ID：{song_id}，模式：{'全分轨' if mode == 'stems' else '人声/伴奏'}")

    ctx.stage("prepare")
    originals = await _feed(ctx.pf, [song_id], ctx.cancel)
    original = next((c for c in originals if isinstance(c, dict) and c.get("id") == song_id), None)
    if not original:
        raise AppError("NOT_FOUND", "未能读取原歌曲，请确认歌曲链接和当前 Suno 账号。")
    title = str(original.get("title") or "").strip() or "SUNO歌曲"
    detected, generated, decision = await _prepare(ctx, song_id, mode, title)
    if len(detected) > MAX_STEMS:
        raise AppError("DOWNLOAD_FAILED", f"分轨数量超过 {MAX_STEMS} 上限。")
    ctx.log(f"选定 {len(detected)} 个分轨（decision={decision}，generated={generated}）")

    ctx.stage("download")
    song_name = _safe_name(title, "SUNO歌曲", 100)
    entries, tracks, total = [], [], 0
    for i, clip in enumerate(detected):
        ctx.check_cancel()
        pub = _public_stem(clip, mode)
        ctx.log(f"下载分轨 {i + 1}/{len(detected)}：{pub['type']}（{pub['id']}）")
        clear, container = await _download_track(ctx, clip, i)
        total += len(clear)
        if total > MAX_TOTAL_BYTES:
            raise AppError("DOWNLOAD_FAILED", "分轨音频总量超过 2GB 上限。")
        src = ctx.workdir / f"track_{i}.{_EXT[container]}"
        src.write_bytes(clear)
        dst = ctx.workdir / f"track_{i}.{fmt}"
        if _EXT[container] != fmt:
            await ctx.interruptible(ffmedia.transcode(src, dst, fmt))
        else:
            dst = src  # 容器已是目标格式，免转码（ffmpeg 不能原地编辑）
        entry = f"{i + 1:02d}-{song_name}-{_safe_name(pub['type'], '分轨', 80)}.{fmt}"
        entries.append((entry, dst))
        tracks.append({**pub, "entry": entry})
        ctx.log(f"分轨 {i + 1} 转码完成（{fmt}）")

    ctx.stage("pack")
    ctx.check_cancel()
    # ponytail: stdlib zipfile 替代参考实现手写 ZIP（超限时自动 ZIP64）；2GB 音频上限已覆盖场景
    zip_tmp = ctx.workdir / "stems.zip"
    with zipfile.ZipFile(zip_tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for entry, dst in entries:
            z.write(dst, entry)
    out_dir = Path(ctx.settings.get("download_dir") or config.system_download_dir())
    out_dir.mkdir(parents=True, exist_ok=True)
    label = "stems" if mode == "stems" else "vocal_accompaniment"
    out = out_dir / ffmedia.unique_name(f"{label}_{song_id}", job["id"], "zip")
    shutil.move(str(zip_tmp), str(out))
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    ctx.log(f"分轨打包完成：{out.name}（{len(tracks)} 轨，{fmt.upper()}）")
    ctx.finish(str(out), {"song_id": song_id, "mode": mode, "tracks": tracks,
                          "generated": generated, "decision": decision})
