"""Studio 自动还原（SPEC §4.5；main.cjs 7180–7782 忠实移植，非 UI 驱动）。

流程：创建工程 → 双轨 ×4 时间轴排布（读窗=上传时长、播放窗=上传时长/4）→
序列化 warp markers → 安装保存监视器 → 首次保存 → 软刷新（=API 重读，非页面刷新）
等待保存队列安静 + 版本稳定 → 全部片段 Transpose +24 / warp 关闭 →
三段 EQ 依次保存（基础 → band3 461Hz −0.6dB → band4 1285Hz −0.6dB）→
导出前重读校验，任何一步失败即中止，绝不带病导出。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import time
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from ..errors import AppError, SunoAPIError
from ..jobs import JobContext

# ---- 常量（main.cjs 6151–6167 / SPEC §4.5）----
BPS = 2                      # beats per second
TEMPO_STEPS = (2, 2)         # 本地两次 ×2，累计 ×4（体现在读窗/播放窗比例，无独立 tempo 字段）
CROSSFADE_SEC = 0.03         # 相邻片段 30ms 交叉淡化
SAVE_QUIET_S = 3.0           # 保存队列安静窗口
SAVE_WAIT_TIMEOUT_S = 60.0   # 等待保存队列超时
STABLE_READS = 2             # 连续稳定重读次数
STABLE_INTERVAL_S = 1.5      # 稳定重读间隔
SINGLE_READ_INTERVAL_S = 1.0  # 单次重读模式间隔
STATE_WAIT_TIMEOUT_S = 60.0  # 工程状态等待上限
TRANSPOSE = 24               # +24 半音还原（×4 减速的音高补偿）


def _eq_band(btype: str, enabled: bool, freq: float, gain: float = 0.0) -> dict:
    return {"type": btype, "enabled": enabled, "frequency": freq, "gain": gain, "q": 0.707}


# EQ 预设（main.cjs 6790–6804 逐字；字段名 frequency）
EQ_BASE = {
    "enabled": True,
    "band1": _eq_band("highpass", False, 60),
    "band2": _eq_band("lowshelf", True, 160),
    "band3": _eq_band("peaking", True, 450),
    "band4": _eq_band("peaking", True, 1200),
    "band5": _eq_band("highshelf", True, 3200),
    "band6": _eq_band("lowpass", False, 8800),
}
EQ_BAND3 = copy.deepcopy(EQ_BASE)
EQ_BAND3["band3"].update({"frequency": 461, "gain": -0.6})
EQ_FINAL = copy.deepcopy(EQ_BAND3)
EQ_FINAL["band4"].update({"frequency": 1285, "gain": -0.6})


# ---- 工程 JSON 工具（main.cjs 6759–6921 移植）----

def _jsnum(x) -> str:
    """JS String(number) 语义：整数不带小数点（warp 哈希源串必须与前端一致）。"""
    f = float(x)
    return str(int(f)) if f.is_integer() else str(f)


def iter_clips(state: dict):
    for track in state.get("tracks") or []:
        yield from track.get("clips") or []
        for lane in track.get("takeLanes") or []:
            yield from lane.get("clips") or []


def stable_marker_hash(markers: dict) -> str:
    entries = sorted(
        ((float(k), float(v)) for k, v in (markers or {}).items()
         if math.isfinite(float(k)) and math.isfinite(float(v))),
        key=lambda e: e[0],
    )
    source = ",".join(f"{_jsnum(sec)}:{_jsnum(beats)}" for sec, beats in entries)
    return "warp_" + hashlib.md5(source.encode("utf-8")).hexdigest()


def serialize_project_state(state: dict) -> dict:
    """uploadId 置空；warp.markers 移入 markersRegistry（按稳定哈希去重）。"""
    s = copy.deepcopy(state)
    registry = dict(s.get("markersRegistry") or {})
    for clip in iter_clips(s):
        clip["uploadId"] = None
        warp = clip.get("warp")
        if isinstance(warp, dict) and isinstance(warp.get("markers"), dict):
            h = stable_marker_hash(warp["markers"])
            registry.setdefault(h, warp["markers"])
            del warp["markers"]
            warp["markersHash"] = h
    s["markersRegistry"] = registry
    return s


def hydrate_autosave_state(state: dict) -> dict:
    """补齐 Studio 自动保存期望的默认字段（main.cjs 6805–6851）。"""
    s = copy.deepcopy(state)
    s.setdefault("recording", {"countInBars": 0, "preRollBars": 0})
    s.setdefault("majorVersion", 1)
    s.setdefault("routing", {})
    s.setdefault("masterRoutingMode", "linear")
    s.setdefault("midiCcMappings", {})
    s.setdefault("masterSignalChain", [])
    if s.get("midiNotePreviewEnabled") is not False:
        s["midiNotePreviewEnabled"] = True
    s.setdefault("contextualWindowConfig", {"mainPanel": None, "upperPanel": None})
    s.setdefault("libraryPanelState", {
        "isOpen": False, "activeTab": "my-uploads", "sourceType": "uploads",
        "selectedCollectionId": None, "searchQuery": "", "sortBy": "created_at",
        "selectedClipIds": [],
    })
    s.setdefault("layout", {"libraryWidth": 320, "contextualWindowHeight": 320,
                            "propertiesWidth": 320, "transportHeight": 80})
    s.setdefault("selection", {"anchorBeats": 0, "focusBeats": 0, "trackIds": [],
                               "focusedTrackId": None, "focusedArea": None})
    default_eq = {
        "enabled": False,
        "band1": _eq_band("highpass", False, 60),
        "band2": _eq_band("lowshelf", False, 160),
        "band3": _eq_band("peaking", False, 450),
        "band4": _eq_band("peaking", False, 1200),
        "band5": _eq_band("highshelf", False, 3200),
        "band6": _eq_band("lowpass", False, 8800),
    }
    for track in s.get("tracks") or []:
        track.setdefault("faderAutomation", {})
        track.setdefault("eq", copy.deepcopy(default_eq))
        track.setdefault("signalChain", [])
        for clip in track.get("clips") or []:
            clip.setdefault("formantCorrection", 0)
        for lane in track.get("takeLanes") or []:
            for clip in lane.get("clips") or []:
                clip.setdefault("formantCorrection", 0)
    return s


def apply_pitch_restore(state: dict, clip_ids: set) -> int:
    """对全部变形片段设置 Transpose +24、formantCorrection 0、warp 关闭；返回命中数。"""
    n = 0
    for clip in iter_clips(state):
        if clip.get("clipId") in clip_ids:
            clip["transposition"] = TRANSPOSE
            clip["formantCorrection"] = 0
            warp = dict(clip.get("warp") or {})
            warp["enabled"] = False
            warp["awaitingAnalysis"] = False
            clip["warp"] = warp
            n += 1
    return n


def has_pitch_restore(state: dict, clip_ids: set) -> bool:
    seen = set()
    for clip in iter_clips(state):
        cid = clip.get("clipId")
        if cid not in clip_ids:
            continue
        seen.add(cid)
        if float(clip.get("transposition") or 0) != TRANSPOSE:
            return False
        if (clip.get("warp") or {}).get("enabled") is not False:
            return False
    return seen == set(clip_ids)


def eq_signature(eq) -> str:
    eq = eq or {}
    bands = []
    for i in range(1, 7):
        b = eq.get(f"band{i}") or {}
        bands.append([b.get("type"), bool(b.get("enabled")),
                      float(b.get("frequency") or 0), float(b.get("gain") or 0),
                      float(b.get("q") or 0)])
    return json.dumps([bool(eq.get("enabled")), *bands], ensure_ascii=False)


def apply_track_eq(state: dict, track_index: int, preset: dict) -> None:
    tracks = state.get("tracks") or []
    if track_index < len(tracks):
        tracks[track_index]["eq"] = copy.deepcopy(preset)


def build_render_state(state: dict) -> dict:
    """渲染提交用状态：去掉 lockBPS，uploadId 全部置空（main.cjs 6892–6909）。"""
    s = copy.deepcopy(state)
    (s.get("timing") or {}).pop("lockBPS", None)
    for clip in iter_clips(s):
        clip["uploadId"] = None
    return s


def pick_version_id(j: dict) -> str:
    j = j or {}
    data = j.get("data") or {}
    for src in (j, data):
        for key in ("studio_project_version_id", "version_id", "latest_version_id"):
            v = src.get(key)
            if v:
                return str(v)
        v = src.get("version")
        if isinstance(v, dict) and v.get("id"):
            return str(v["id"])
    versions = j.get("versions") or data.get("versions")
    if isinstance(versions, list) and versions:
        first = versions[0]
        if isinstance(first, dict):
            return str(first.get("id") or first.get("studio_project_version_id") or "")
        return str(first)
    return ""


# ---- API / 保存监视 ----

async def _api(ctx: JobContext, method: str, path: str, body=None, *, timeout_ms: int = 20_000) -> dict:
    return await ctx.pf.request(method, path, body, timeout_ms=timeout_ms, cancel_event=ctx.cancel)


# ponytail: 非 UI 驱动时监视器只防 Studio 页面自身的自动保存抢写；安装失败降级为
# “无在途请求”判断（我们的 POST 均同步等待响应）。升级路径：真实页面驱动时必装。
_MONITOR_JS = """() => {
  if (window.__growSaveMonitor) return true;
  const m = { inFlight: 0, activitySerial: 0, lastFinishedAt: 0 };
  // 2026-11 漂移：页面自身 fetch 已迁 /a/api/*，两种前缀都认。
  const isSave = (u) => { try { const p = new URL(String(u), location.href).pathname; return p === '/api/studio/save-project' || p === '/a/api/studio/save-project'; } catch (e) { return false; } };
  const start = () => { m.inFlight += 1; m.activitySerial += 1; };
  const end = () => { m.inFlight = Math.max(0, m.inFlight - 1); m.lastFinishedAt = Date.now(); m.activitySerial += 1; };
  const of = window.fetch;
  if (of) window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    const save = method === 'POST' && isSave(url);
    if (save) start();
    const p = of.apply(this, arguments);
    if (save) p.then(end, end);
    return p;
  };
  const XO = XMLHttpRequest.prototype.open, XS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__growSave = String(method || '').toUpperCase() === 'POST' && isSave(url);
    return XO.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    if (this.__growSave) { start(); this.addEventListener('loadend', end, { once: true }); }
    return XS.apply(this, arguments);
  };
  window.__growSaveMonitor = m;
  return true;
}"""
_MONITOR_READ_JS = ("() => window.__growSaveMonitor ? { inFlight: window.__growSaveMonitor.inFlight,"
                    " activitySerial: window.__growSaveMonitor.activitySerial,"
                    " lastFinishedAt: window.__growSaveMonitor.lastFinishedAt } : null")


async def install_save_monitor(ctx: JobContext):
    """在首次保存前安装 fetch/XHR 保存监视器；返回 page（失败返回 None，降级继续）。"""
    try:
        page = await ctx.chrome.ensure()
        await ctx.chrome.check_page(page)
        await page.evaluate(_MONITOR_JS)
        return page
    except Exception as e:  # noqa: BLE001 - 监视器是保护项，不是关键路径
        ctx.debug(f"保存监视器安装失败（{type(e).__name__}），按无在途保存处理。")
        return None


async def _monitor_read(ctx: JobContext, page) -> dict:
    idle = {"inFlight": 0, "activitySerial": 0, "lastFinishedAt": 0}
    if page is None:
        return idle
    try:
        m = await page.evaluate(_MONITOR_READ_JS)
        return m if isinstance(m, dict) else idle
    except Exception:  # noqa: BLE001
        return idle


async def wait_save_quiet(ctx: JobContext, page, deadline: float) -> None:
    """等待保存队列安静 ≥SAVE_QUIET_S（main.cjs 7519–7554）。"""
    quiet_since = time.time()
    while True:
        ctx.check_cancel()
        if time.monotonic() >= deadline:
            raise AppError("STUDIO_SAVE_TIMEOUT", "等待 Studio 保存队列超时，已中止以避免覆盖未保存状态。")
        m = await _monitor_read(ctx, page)
        now = time.time()
        if m["inFlight"] > 0:
            quiet_since = now
        else:
            quiet_from = max(quiet_since, (m.get("lastFinishedAt") or 0) / 1000.0)
            if now - quiet_from >= SAVE_QUIET_S:
                return
        await asyncio.sleep(0.25)


async def load_project(ctx: JobContext, pid: str) -> tuple[dict, str]:
    """重读工程（软刷新）。

    ponytail: SPEC 措辞为 “load-project”，实测端点为 GET /api/studio/project/{id}
    （main.cjs waitForStudioProjectState 同源）；响应 shape 以 state/data.state 双兼容。
    """
    r = await _api(ctx, "GET", f"/api/studio/project/{pid}")
    j = r.get("json") or {}
    state = j.get("state") or (j.get("data") or {}).get("state") or {}
    version = pick_version_id(j) or pick_version_id(state)
    return state, version


async def wait_stable(ctx: JobContext, pid: str, *, page=None, clip_count: int = 0,
                      expected_eqs: list | None = None, stable_reads: int = STABLE_READS,
                      wait_quiet: bool = False,
                      timeout_s: float = STATE_WAIT_TIMEOUT_S) -> tuple[dict, str]:
    """重读直到 版本+片段数+EQ 就绪且连续 stable_reads 次版本不变（main.cjs 7555–7629）。"""
    deadline = time.monotonic() + timeout_s
    if wait_quiet:
        await wait_save_quiet(ctx, page, deadline)
    interval = STABLE_INTERVAL_S if stable_reads > 1 else SINGLE_READ_INTERVAL_S
    stable_version, count = "", 0
    while time.monotonic() < deadline:
        ctx.check_cancel()
        state, version = {}, ""
        try:
            state, version = await load_project(ctx, pid)
        except AppError as e:
            if e.code in ("CANCELLED", "NOT_LOGGED_IN"):
                raise
            ctx.debug(f"重读工程暂不可用（{e.code}），继续等待。")
        if state and version:
            clips_ready = sum(1 for _ in iter_clips(state)) >= clip_count
            tracks = state.get("tracks") or []
            eq_ready = expected_eqs is None or all(
                i < len(tracks) and eq_signature(tracks[i].get("eq")) == eq_signature(expected_eqs[i])
                for i in range(len(expected_eqs)))
            m = await _monitor_read(ctx, page)
            if clips_ready and eq_ready and m["inFlight"] == 0:
                count = count + 1 if version == stable_version else 1
                stable_version = version
                if count >= stable_reads:
                    return state, version
            else:
                count, stable_version = 0, ""
        await asyncio.sleep(interval)
    raise AppError("STUDIO_STATE_NOT_STABLE", "工程状态未在时限内稳定，已中止。")


async def save_state(ctx: JobContext, pid: str, state: dict, title: str, *,
                     confirm: bool = False, expected_eqs: list | None = None,
                     page=None) -> tuple[dict, str]:
    """POST save-project；confirm=True 时再稳定重读确认（main.cjs 7631–7672）。"""
    try:
        r = await _api(ctx, "POST", "/api/studio/save-project",
                       {"project_id": pid, "state": state, "title": title})
    except SunoAPIError as e:
        if e.code in ("CANCELLED", "NOT_LOGGED_IN"):
            raise
        raise AppError("STUDIO_SAVE_FAILED", f"保存工程失败：{e.message}") from e
    version = pick_version_id(r.get("json") or {})
    if not version:
        raise AppError("STUDIO_VERSION_FAILED", "保存工程未返回 version_id。")
    if not confirm:
        return hydrate_autosave_state(state), version
    confirmed, _ = await wait_stable(ctx, pid, page=page, expected_eqs=expected_eqs,
                                     wait_quiet=True)
    return hydrate_autosave_state(confirmed), version


# ---- 时间轴排布（main.cjs 7226–7334 逐字移植）----

def _track(tid: str, name: str, color: str, clips: list) -> dict:
    return {"type": "audio", "id": tid, "input": None, "color": color, "icon": "Audio",
            "name": name, "clips": clips, "clipCreationIntents": [], "height": 160,
            "solo": False, "mute": False, "arm": False, "amplitude": 1, "balance": 0,
            "instrument": {"type": "song"}, "soloTakeLaneId": None,
            "takeLanesExpanded": False, "takeLanes": [], "routingMode": "linear"}


def build_project_state(clip_ids: list, slices: list, title: str) -> tuple[dict, dict]:
    """返回 (首存状态, 布局信息)。读窗=上传时长×BPS，播放窗=上传时长/4×BPS（×2 ×2）。"""
    if not clip_ids or len(clip_ids) != len(slices):
        raise AppError("STUDIO_INVALID_CLIPS", "上传片段与时长信息不一致，无法排布工程。")
    t1, t2 = str(uuid4()), str(uuid4())
    clips1, clips2 = [], []
    last, cum = None, 0.0
    export_start, export_end = 0.0, 0.0
    crossfade_beats = CROSSFADE_SEC * BPS
    for i, (cid, sl) in enumerate(zip(clip_ids, slices)):
        upload_dur = float(sl.get("duration") or 0)
        if upload_dur <= 0:
            raise AppError("STUDIO_INVALID_CLIPS", f"片段 {i + 1} 时长无效，无法排布工程。")
        timeline_dur = upload_dur
        for step in TEMPO_STEPS:      # 两次 ×2，累计 ×4
            timeline_dur /= step
        clip_beats = timeline_dur * BPS
        read_end = upload_dur * BPS
        if i > 0:
            cum = max(0.0, cum - CROSSFADE_SEC)
            if last is not None:
                last["fadeOutBeats"] = crossfade_beats
        start = cum * BPS
        end = start + clip_beats
        on_track2 = i % 2 == 1
        clip = {
            "type": "audio", "streaming": False, "id": str(uuid4()),
            "asset": {"type": "clip", "id": cid}, "clipId": cid, "uploadId": None,
            "mute": False, "reversed": False, "name": f"Part {i + 1}",
            "color": "#7251F7" if on_track2 else "#FF6A00",
            "startBeats": start, "endBeats": end, "readStartBeats": 0,
            "fadeInBeats": crossfade_beats if i > 0 else 0, "fadeOutBeats": 0,
            "fadeInCurve": 1, "fadeOutCurve": 1,
            "transposition": 0, "formantCorrection": 0, "amplitude": 1,
            "loop": {"enabled": False, "startBeats": 0, "endBeats": read_end},
            "warp": {"enabled": True, "awaitingAnalysis": False,
                     "markers": {"0": 0, _jsnum(upload_dur): read_end}},
        }
        (clips2 if on_track2 else clips1).append(clip)
        last = clip
        if i == 0:
            export_start = start
        export_end = end
        cum += timeline_dur
    total_beats = cum * BPS
    state = {
        "metadata": {"usedDelete": True}, "amplitude": 1, "sections": {},
        "lyricsCorrectionsByClipId": {}, "metronome": {"enabled": False, "amplitude": 1},
        "timing": {"type": "manual", "bps": BPS, "bpsAutomation": [],
                   "lockBPS": True, "firstBeatSeconds": 0},
        "timeSignatureChanges": [{"startBeats": 0, "subdivisionsPerBar": 4,
                                  "beatsPerSubdivision": 1}],
        "tracks": [_track(t1, "Track 1", "#208BFF", clips1),
                   _track(t2, "Track 2", "#7251F7", clips2)],
        "selection": {"anchorBeats": export_start, "focusBeats": export_start,
                      "trackIds": [t1, t2], "focusedTrackId": t1, "focusedArea": "timeline"},
        "loop": {"enabled": False, "startBeats": 0, "endBeats": 32},
        "songFadeInBeats": 0, "songFadeOutBeats": 0,
        "editorPointerModes": {"audio": "select", "midi": "select"},
        "title": title, "markersRegistry": {},
    }
    state = hydrate_autosave_state(serialize_project_state(state))
    focus = min(export_end, export_start + 4)
    state["selection"]["anchorBeats"] = focus
    state["selection"]["focusBeats"] = focus
    layout = {"start_beats": export_start, "end_beats": export_end, "total_beats": total_beats}
    return state, layout


def build_downbeats(total_beats: float) -> list:
    beat_count = max(1, math.floor(total_beats))
    return [[beat / BPS, (beat % 4) + 1] for beat in range(beat_count)]


# ---- 主流程 ----

async def restore(clip_ids: list, meta: dict, ctx: JobContext) -> dict:
    ctx.check_cancel()
    slices = meta.get("slices") or []
    title = Path(ctx.job.get("options", {}).get("source_name") or "").stem or "TIMBRE 还原"

    # 1) 创建工程
    try:
        r = await _api(ctx, "POST", f"/api/studio/create-project?title={quote(title)}")
    except SunoAPIError as e:
        if e.code in ("CANCELLED", "NOT_LOGGED_IN"):
            raise
        raise AppError("STUDIO_CREATE_PROJECT_FAILED", f"创建 Studio 工程失败：{e.message}") from e
    pid = str((r.get("json") or {}).get("id") or "")
    if not pid:
        raise AppError("STUDIO_CREATE_PROJECT_FAILED", "创建 Studio 工程未返回 project_id。")
    ctx.log(f"已创建 Studio 工程：{pid}")

    # 2) 双轨 ×4 排布 + 首次保存（监视器必须先装）
    state, layout = build_project_state(clip_ids, slices, title)
    page = await install_save_monitor(ctx)
    ctx.check_cancel()
    try:
        r = await _api(ctx, "POST", "/api/studio/save-project",
                       {"project_id": pid, "state": state, "title": title})
    except SunoAPIError as e:
        if e.code in ("CANCELLED", "NOT_LOGGED_IN"):
            raise
        raise AppError("STUDIO_SAVE_FAILED", f"首次保存工程失败：{e.message}") from e
    if not pick_version_id(r.get("json") or {}):
        ctx.debug("首次保存未返回 version_id，改由软刷新重读获取。")
    ctx.log("首次保存已提交，等待服务端状态稳定（软刷新）。")

    # 3) 软刷新：等片段数+版本连续稳定。2026-11 漂移：服务端不再给新工程套默认基础 EQ
    #    （回读全禁用 EQ），故不以 EQ_BASE 作就绪信号；EQ 由第 5 步我方写入、第 6 步逐轨校验 EQ_FINAL。
    try:
        state, version = await wait_stable(ctx, pid, page=page, clip_count=len(clip_ids),
                                           wait_quiet=True)
    except AppError as e:
        if e.code in ("CANCELLED", "NOT_LOGGED_IN"):
            raise
        raise AppError("STUDIO_SOFT_REFRESH_FAILED", f"软刷新后工程状态未就绪：{e.message}") from e
    state = hydrate_autosave_state(state)

    # 4) 全部变形片段 +24 / warp 关闭
    if apply_pitch_restore(state, set(clip_ids)) != len(clip_ids):
        raise AppError("STUDIO_PITCH_RESTORE_FAILED", "未能在工程中找到全部变形片段，已中止还原。")

    # 5) 三段 EQ 依次保存（每段保存后立即以本地状态继续，最后统一稳定确认）
    for code, label, preset in (("BASE", "基础 EQ", EQ_BASE),
                                ("BAND3", "band3 461Hz −0.6dB", EQ_BAND3),
                                ("FINAL", "band4 1285Hz −0.6dB", EQ_FINAL)):
        ctx.check_cancel()
        apply_track_eq(state, 0, preset)
        apply_track_eq(state, 1, preset)
        try:
            state, version = await save_state(ctx, pid, state, title, page=page)
        except AppError as e:
            if e.code in ("CANCELLED", "NOT_LOGGED_IN"):
                raise
            raise AppError(f"STUDIO_EQ_{code}_FAILED", f"保存 {label} 失败：{e.message}") from e
        ctx.log(f"已保存 {label}（version={version}）。")

    # 6) 导出前最终确认：版本稳定 + EQ 到位 + 音高还原确实落库
    try:
        state, version = await wait_stable(ctx, pid, page=page, clip_count=len(clip_ids),
                                           expected_eqs=[EQ_FINAL, EQ_FINAL], wait_quiet=True)
    except AppError as e:
        if e.code in ("CANCELLED", "NOT_LOGGED_IN"):
            raise
        raise AppError("STUDIO_EXPORT_READY_FAILED", f"导出前工程状态未就绪：{e.message}") from e
    state = hydrate_autosave_state(state)
    if not has_pitch_restore(state, set(clip_ids)):
        raise AppError("STUDIO_PITCH_RESTORE_NOT_SAVED",
                       "导出前校验发现 +24 音高还原未保存成功，已中止导出。")
    ctx.log("导出前校验通过：全部片段 Transpose +24、warp 关闭、EQ 到位。")

    return {
        "project_id": pid,
        "version_id": version,
        "title": title,
        "clip_ids": list(clip_ids),
        "state": build_render_state(state),
        "start_beats": layout["start_beats"],
        "end_beats": layout["end_beats"],
        "downbeats": build_downbeats(layout["total_beats"]),
        "total_beats": layout["total_beats"],
    }
