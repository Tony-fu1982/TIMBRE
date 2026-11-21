"""06 Studio 自动还原+导出 / 07 续查 自测（无网络、无真实 Chrome，全 fake）。

覆盖：状态结构（双轨/×4/交叉淡化/warp 注册表）、保存序列（基础→461→1285）、
稳定重读与导出前校验、渲染状态机（submitting/pending/succeeded/failed/unknown）、
360s 时限与至多一次重试、静音判定、清理保护成品、续查只读语义。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import shutil
import subprocess
import time

import pytest

from app import ffmedia
from app.errors import AppError, SunoAPIError
from app.jobs import JobContext
from app.suno import exporter, restorer

# 在 autouse 加速 fixture 改写前记录真实常量，用于合规断言
_ORIG_POLL = exporter.POLL_INTERVAL_S
_ORIG_BUDGET = exporter.RENDER_BUDGET_S
_ORIG_CLEANUP = exporter.CLEANUP_BUDGET_S


def ok(j):
    return {"ok": True, "status": 200, "json": j, "text": "", "retryAfter": None}


class FakePF:
    """按 (method, path 前缀) 路由；handler 可为 dict / callable(path, body) / Exception。"""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def route(self, method, prefix, handler):
        self.routes[(method, prefix)] = handler

    def count(self, method, prefix):
        return sum(1 for m, p, _ in self.calls if m == method and p.startswith(prefix))

    async def request(self, method, path, body=None, *, timeout_ms=20_000, cancel_event=None):
        self.calls.append((method, path, body))
        if cancel_event is not None and cancel_event.is_set():
            raise AppError("CANCELLED")
        for (m, prefix), h in self.routes.items():
            if m == method and path.startswith(prefix):
                if isinstance(h, Exception):
                    raise h
                if callable(h):
                    return h(path, body)
                return h
        return ok({})


class FakePage:
    def __init__(self):
        self.monitor = {"inFlight": 0, "activitySerial": 0, "lastFinishedAt": 0}
        self.installed = False

    async def evaluate(self, script):
        if "XMLHttpRequest" in script:
            self.installed = True
            return True
        return dict(self.monitor)


class FakeChrome:
    def __init__(self):
        self.page = FakePage()

    async def ensure(self):
        return self.page

    async def check_page(self, page):
        pass


class BrokenChrome:
    async def ensure(self):
        raise RuntimeError("no chrome")

    async def check_page(self, page):
        pass


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(restorer, "SAVE_QUIET_S", 0.0)
    monkeypatch.setattr(restorer, "STABLE_INTERVAL_S", 0.0)
    monkeypatch.setattr(restorer, "SINGLE_READ_INTERVAL_S", 0.0)
    monkeypatch.setattr(restorer, "STATE_WAIT_TIMEOUT_S", 0.5)
    monkeypatch.setattr(restorer, "SAVE_WAIT_TIMEOUT_S", 1.0)
    monkeypatch.setattr(exporter, "POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(exporter, "FIRST_POLL_DELAY_S", 0.0)
    monkeypatch.setattr(exporter, "SYNC_TIMEOUT_S", 0.3)


@pytest.fixture
def make_ctx(db, bus, tmp_path):
    def _make(options=None, pf=None, chrome=None):
        job = db.insert_job("upload", "", options or {})
        pf = pf if pf is not None else FakePF()
        chrome = chrome if chrome is not None else FakeChrome()
        workdir = tmp_path / f"job_{job['id']}"
        workdir.mkdir(exist_ok=True)
        return JobContext(job["id"], job, db, asyncio.Event(), workdir,
                          {"debug_logs": "1"}, chrome, pf, bus)
    return _make


CLIP_IDS = [f"c{i}" for i in range(1, 7)]
META = {"sr": 44100, "source_duration": 12.0, "transformed": True,
        "slices": [{"duration": 8.0} for _ in range(6)]}


def studio_routes(pf, *, empty_load=False, break_pitch=False, fail_save=None):
    """模拟 Studio 服务端（2026-11 形态：不套默认 EQ）；保存返回递增版本号。"""
    server = {"state": None, "version": "", "saves": []}

    def create(path, body):
        return ok({"id": "p1"})

    def save(path, body):
        server["saves"].append(copy.deepcopy(body["state"]))
        n = len(server["saves"])
        if fail_save == n:
            raise SunoAPIError("SUNO_HTTP_ERROR", 500, "save failed")
        server["version"] = f"v{n}"
        st = copy.deepcopy(body["state"])
        if break_pitch and n >= 4:
            for c in restorer.iter_clips(st):
                c["transposition"] = 0
        server["state"] = st
        return ok({"studio_project_version_id": server["version"]})

    def load(path, body):
        if empty_load:  # 重读永不返回 state/version → 真·不稳定
            return ok({})
        return ok({"state": copy.deepcopy(server["state"]),
                   "studio_project_version_id": server["version"]})

    pf.route("POST", "/api/studio/create-project", create)
    pf.route("POST", "/api/studio/save-project", save)
    pf.route("GET", "/api/studio/project/p1", load)
    return server


# ================= 纯函数 =================

def test_jsnum_js_string_semantics():
    assert restorer._jsnum(8.0) == "8"
    assert restorer._jsnum(0) == "0"
    assert restorer._jsnum(8.5) == "8.5"


def test_marker_hash_sorted_and_stable():
    expect = "warp_" + hashlib.md5(b"0:0,8:16").hexdigest()
    assert restorer.stable_marker_hash({"8": 16, "0": 0}) == expect
    assert restorer.stable_marker_hash({"0": 0, "8.0": 16.0}) == expect
    assert restorer.stable_marker_hash(None).startswith("warp_")


def test_serialize_moves_markers_to_registry_without_mutation():
    state = {"tracks": [{"clips": [{"clipId": "c1", "uploadId": "u1",
                                    "warp": {"markers": {"0": 0, "8": 16}}}]}]}
    snap = copy.deepcopy(state)
    out = restorer.serialize_project_state(state)
    clip = out["tracks"][0]["clips"][0]
    assert clip["uploadId"] is None
    assert "markers" not in clip["warp"]
    h = clip["warp"]["markersHash"]
    assert out["markersRegistry"][h] == {"0": 0, "8": 16}
    assert state == snap  # 不改原对象


def test_constants_match_spec():
    assert restorer.BPS == 2
    assert restorer.TEMPO_STEPS == (2, 2)
    assert restorer.CROSSFADE_SEC == 0.03
    assert restorer.TRANSPOSE == 24
    assert _ORIG_POLL == 5.0
    assert _ORIG_BUDGET == 360.0
    assert _ORIG_CLEANUP == 10.0


def test_build_project_state_layout_and_crossfade():
    state, layout = restorer.build_project_state(CLIP_IDS, META["slices"], "t")
    t1, t2 = state["tracks"]
    assert [c["clipId"] for c in t1["clips"]] == ["c1", "c3", "c5"]
    assert [c["clipId"] for c in t2["clips"]] == ["c2", "c4", "c6"]
    c0, c1 = t1["clips"][0], t2["clips"][0]
    # ×4 编码：读窗=上传时长×BPS，播放跨度=上传时长/4×BPS
    assert c0["loop"]["endBeats"] == 16.0
    assert c0["endBeats"] - c0["startBeats"] == 4.0
    # 30ms 交叉淡化 = 0.06 beats @BPS2
    assert c1["startBeats"] == pytest.approx((2.0 - 0.03) * 2)
    assert c0["fadeOutBeats"] == pytest.approx(0.06)
    assert c1["fadeInBeats"] == pytest.approx(0.06)
    assert c0["fadeInBeats"] == 0 and c0["fadeOutBeats"] == pytest.approx(0.06)
    assert layout["total_beats"] == pytest.approx((6 * 2.0 - 5 * 0.03) * 2)
    assert state["timing"]["lockBPS"] is True
    assert state["selection"]["anchorBeats"] == 4  # min(end, start+4)
    assert len(restorer.build_downbeats(layout["total_beats"])) == 23


def test_build_project_state_rejects_mismatch():
    with pytest.raises(AppError) as e:
        restorer.build_project_state(["c1"], META["slices"], "t")
    assert e.value.code == "STUDIO_INVALID_CLIPS"


# ================= 还原主流程 =================

async def test_restore_happy_path_saves_and_structure(make_ctx):
    pf = FakePF()
    server = studio_routes(pf)
    ctx = make_ctx(options={"source_name": "test.mp3"}, pf=pf)
    res = await restorer.restore(CLIP_IDS, META, ctx)

    assert res["project_id"] == "p1"
    assert res["version_id"] == "v4"
    assert res["title"] == "test"
    assert pf.calls[0] == ("POST", "/api/studio/create-project?title=test", None)
    assert len(server["saves"]) == 4
    assert ctx.chrome.page.installed  # 首存前已安装保存监视器

    s0, s1, s2, s3 = server["saves"]
    # 首存：原始结构（未移调），warp 标记走注册表
    assert all(c["transposition"] == 0 for c in restorer.iter_clips(s0))
    c0 = s0["tracks"][0]["clips"][0]
    assert c0["uploadId"] is None
    assert "markers" not in c0["warp"]
    assert s0["markersRegistry"][c0["warp"]["markersHash"]] == {"0": 0, "8": 16}
    # 第二次保存：+24 还原已写入，服务端基础 EQ 在轨
    for c in restorer.iter_clips(s1):
        assert c["transposition"] == 24
        assert c["warp"]["enabled"] is False
        assert c["warp"]["awaitingAnalysis"] is False
        assert c["formantCorrection"] == 0
    assert s1["tracks"][0]["eq"] == restorer.EQ_BASE
    # 第三次：band3 461Hz −0.6dB；第四次：band4 1285Hz −0.6dB（顺序不可换）
    assert s2["tracks"][0]["eq"]["band3"]["frequency"] == 461
    assert s2["tracks"][0]["eq"]["band3"]["gain"] == -0.6
    assert s2["tracks"][0]["eq"]["band4"]["frequency"] == 1200
    assert s3["tracks"][0]["eq"]["band3"]["frequency"] == 461
    assert s3["tracks"][0]["eq"]["band4"] == {**restorer.EQ_BASE["band4"],
                                              "frequency": 1285, "gain": -0.6}
    assert s3["tracks"][1]["eq"] == restorer.EQ_FINAL
    # 导出用 state 去掉 lockBPS
    assert "lockBPS" not in res["state"]["timing"]
    assert res["start_beats"] == 0
    assert res["end_beats"] == pytest.approx(23.7)
    assert len(res["downbeats"]) == 23
    assert res["downbeats"][0] == [0.0, 1]
    assert not ctx._finished  # restore/export 不 finish，由 pipeline 统一收尾


async def test_restore_soft_refresh_failure(make_ctx):
    pf = FakePF()
    studio_routes(pf, empty_load=True)  # 重读永不就绪 → 软刷新超时
    ctx = make_ctx(pf=pf)
    with pytest.raises(AppError) as e:
        await restorer.restore(CLIP_IDS, META, ctx)
    assert e.value.code == "STUDIO_SOFT_REFRESH_FAILED"


async def test_restore_pitch_not_saved_aborts(make_ctx):
    pf = FakePF()
    studio_routes(pf, break_pitch=True)  # 终存后服务端把移调改回 0
    ctx = make_ctx(pf=pf)
    with pytest.raises(AppError) as e:
        await restorer.restore(CLIP_IDS, META, ctx)
    assert e.value.code == "STUDIO_PITCH_RESTORE_NOT_SAVED"


async def test_restore_eq_save_failure_code(make_ctx):
    pf = FakePF()
    studio_routes(pf, fail_save=2)  # 基础 EQ 保存失败
    ctx = make_ctx(pf=pf)
    with pytest.raises(AppError) as e:
        await restorer.restore(CLIP_IDS, META, ctx)
    assert e.value.code == "STUDIO_EQ_BASE_FAILED"


async def test_restore_works_without_page_monitor(make_ctx):
    pf = FakePF()
    studio_routes(pf)
    ctx = make_ctx(pf=pf, chrome=BrokenChrome())  # 监视器不可用 → 降级为纯 API 等待
    res = await restorer.restore(CLIP_IDS, META, ctx)
    assert res["version_id"] == "v4"


async def test_save_state_requires_version(make_ctx):
    pf = FakePF()
    pf.route("POST", "/api/studio/save-project", ok({}))
    ctx = make_ctx(pf=pf)
    with pytest.raises(AppError) as e:
        await restorer.save_state(ctx, "p1", {"tracks": []}, "t")
    assert e.value.code == "STUDIO_VERSION_FAILED"


async def test_wait_save_quiet_timeout(make_ctx):
    ctx = make_ctx()
    ctx.chrome.page.monitor["inFlight"] = 1  # 保存一直在途
    with pytest.raises(AppError) as e:
        await restorer.wait_save_quiet(ctx, ctx.chrome.page, time.monotonic() + 0.2)
    assert e.value.code == "STUDIO_SAVE_TIMEOUT"


# ================= 导出 =================

def make_project():
    clips = [{"clipId": cid, "transposition": 24, "warp": {"enabled": False}}
             for cid in ("c1", "c2")]
    state = {"tracks": [{"clips": clips[:1], "eq": restorer.EQ_FINAL},
                        {"clips": clips[1:], "eq": restorer.EQ_FINAL}],
             "timing": {"bps": 2, "lockBPS": True}}
    return {"project_id": "p1", "version_id": "v9", "title": "t",
            "clip_ids": ["c1", "c2"], "state": state,
            "start_beats": 0, "end_beats": 4,
            "downbeats": [[0, 1], [0.5, 2], [1, 3], [1.5, 4]], "total_beats": 4}


def export_routes(pf, project, *, render_ids=("r1",), statuses=None, sync_ok=True):
    seq = iter(render_ids)
    statuses = statuses or {}

    def load(path, body):
        if not sync_ok:
            raise SunoAPIError("NETWORK_ERROR", 0, "down")
        return ok({"state": copy.deepcopy(project["state"]),
                   "studio_project_version_id": "v10"})

    def render(path, body):
        return ok({"id": next(seq)})

    def clip(path, body):
        rid = path.rsplit("/", 1)[1]
        return ok({"id": rid, "status": statuses.get(rid, "complete"),
                   "audio_url": f"https://cdn1.suno.ai/{rid}.mp3"})

    def trash(path, body):
        return ok({"is_trashed": True, "ids": list(body["clip_ids"])})

    pf.route("GET", "/api/studio/project/p1", load)
    pf.route("POST", "/api/studio/render-state", render)
    pf.route("GET", "/api/clip/", clip)
    pf.route("POST", "/api/gen/trash", trash)


def fake_probe(verdict):
    async def _probe(ctx, clip):
        v = verdict(clip) if callable(verdict) else verdict
        return v, f"https://cdn1.suno.ai/{clip.get('id', 'x')}.mp3"
    return _probe


async def test_render_ignores_foreign_id_and_bounds_stalled_poll(make_ctx, monkeypatch):
    clip = {"id": "other", "status": "complete", "audio_url": "https://cdn1.suno.ai/other.mp3"}
    assert exporter._pick_clip({"clips": [clip]}, "target") is None

    async def stall(*args, **kwargs):
        await asyncio.Future()

    monkeypatch.setattr(exporter, "_api", stall)
    result = await asyncio.wait_for(exporter._poll_render(make_ctx(), "target", time.monotonic() + 0.1), 1)
    assert result == ("timeout", "QUEUED_TIMEOUT")


async def test_export_success_state_machine_and_cleanup(make_ctx, db, monkeypatch):
    monkeypatch.setattr(exporter, "_probe_audio", fake_probe("sound"))
    pf = FakePF()
    project = make_project()
    export_routes(pf, project)
    seen = {}

    def render_spy(path, body):
        seen["submit_state"] = db.get_job(ctx.job_id)["render_state"]
        seen["body"] = body
        return ok({"id": "r1"})

    def clip_spy(path, body):
        seen.setdefault("poll_state", db.get_job(ctx.job_id)["render_state"])
        return ok({"id": "r1", "status": "complete",
                   "audio_url": "https://cdn1.suno.ai/r1.mp3"})

    pf.route("POST", "/api/studio/render-state", render_spy)
    pf.route("GET", "/api/clip/", clip_spy)
    ctx = make_ctx(pf=pf)
    out = await exporter.export(project, ctx)

    assert out["song_id"] == "r1"
    assert out["url"] == "https://suno.com/song/r1"
    assert out["version_id"] == "v10"  # 以重读版本为准
    assert out["render_ids"] == ["r1"]
    assert seen["submit_state"] == "submitting"
    assert seen["poll_state"] == "pending"
    assert seen["body"]["export_mode"] == "rendered_context_window"
    assert seen["body"]["from_studio_project_id"] == "p1"
    assert "lockBPS" not in seen["body"]["state"]["timing"]
    job = db.get_job(ctx.job_id)
    assert job["render_state"] == "succeeded" and job["render_id"] == "r1"
    assert job["result"]["clip_ids"] == ["c1", "c2"]
    assert not ctx._finished  # export 不 finish
    assert pf.count("POST", "/api/studio/render-state") == 1
    trash = [b for m, p, b in pf.calls if m == "POST" and p == "/api/gen/trash"]
    assert trash and set(trash[0]["clip_ids"]) == {"c1", "c2"}  # 成品 r1 受保护


async def test_export_timeout_keeps_pending_same_id(make_ctx, db, monkeypatch):
    monkeypatch.setattr(exporter, "RENDER_BUDGET_S", 0.15)
    pf = FakePF()
    export_routes(pf, make_project(), statuses={"r1": "queued"})
    ctx = make_ctx(pf=pf)
    with pytest.raises(AppError) as e:
        await exporter.export(make_project(), ctx)
    assert e.value.code == "QUEUED_TIMEOUT"  # 远端明确排队 → 排队超时（SPEC §6）
    job = db.get_job(ctx.job_id)
    assert job["render_state"] == "pending" and job["render_id"] == "r1"
    assert pf.count("POST", "/api/studio/render-state") == 1  # 绝不重发
    assert pf.count("POST", "/api/gen/trash") == 0


async def test_export_transport_unknown_never_retries(make_ctx, db):
    pf = FakePF()
    export_routes(pf, make_project())
    pf.route("POST", "/api/studio/render-state",
             SunoAPIError("NETWORK_RESULT_UNKNOWN", 0))
    ctx = make_ctx(pf=pf)
    with pytest.raises(AppError) as e:
        await exporter.export(make_project(), ctx)
    assert e.value.code == "NETWORK_RESULT_UNKNOWN"
    job = db.get_job(ctx.job_id)
    assert job["render_state"] == "unknown" and job["render_id"] == ""
    assert pf.count("POST", "/api/studio/render-state") == 1


async def test_export_no_render_id_fails_unknown(make_ctx, db):
    """SPEC §4.6：render-state 已发出但响应没有 render_id → unknown 失败，绝不重发。"""
    pf = FakePF()
    export_routes(pf, make_project())
    pf.route("POST", "/api/studio/render-state", ok({}))
    ctx = make_ctx(pf=pf)
    with pytest.raises(AppError) as e:
        await exporter.export(make_project(), ctx)
    assert e.value.code == "NETWORK_RESULT_UNKNOWN"
    job = db.get_job(ctx.job_id)
    assert job["render_state"] == "unknown" and job["render_id"] == ""
    assert pf.count("POST", "/api/studio/render-state") == 1
    assert pf.count("POST", "/api/gen/trash") == 0


@pytest.mark.parametrize("status,code", [(402, "INSUFFICIENT_CREDITS"), (400, "SUNO_HTTP_ERROR")])
async def test_export_definite_rejection_fails_terminal(make_ctx, db, status, code):
    """确定性 4xx（渲染确定未创建）→ 终态失败并带语义错误码，不是 unknown。"""
    pf = FakePF()
    export_routes(pf, make_project())
    pf.route("POST", "/api/studio/render-state",
             SunoAPIError("SUNO_HTTP_ERROR", status, f"http {status}"))
    ctx = make_ctx(pf=pf)
    with pytest.raises(AppError) as e:
        await exporter.export(make_project(), ctx)
    assert e.value.code == code
    job = db.get_job(ctx.job_id)
    assert job["render_state"] == "failed" and job["render_id"] == ""
    assert pf.count("POST", "/api/studio/render-state") == 1  # 不重发


async def test_export_timeout_no_status_queued_timeout(make_ctx, db, monkeypatch):
    """从未观察到远端状态 → QUEUED_TIMEOUT（保守按排队处理，SPEC §6 显示继续查询）。"""
    monkeypatch.setattr(exporter, "RENDER_BUDGET_S", 0.15)
    pf = FakePF()
    export_routes(pf, make_project(), statuses={"r1": ""})
    ctx = make_ctx(pf=pf)
    with pytest.raises(AppError) as e:
        await exporter.export(make_project(), ctx)
    assert e.value.code == "QUEUED_TIMEOUT"
    assert db.get_job(ctx.job_id)["render_state"] == "pending"


async def test_export_terminal_failure_retries_once(make_ctx, db, monkeypatch):
    monkeypatch.setattr(exporter, "_probe_audio", fake_probe("sound"))
    pf = FakePF()
    export_routes(pf, make_project(), render_ids=("r1", "r2"),
                  statuses={"r1": "error", "r2": "complete"})
    ctx = make_ctx(pf=pf)
    out = await exporter.export(make_project(), ctx)
    assert out["song_id"] == "r2"
    assert out["render_ids"] == ["r1", "r2"]
    assert pf.count("POST", "/api/studio/render-state") == 2
    trash = [b for m, p, b in pf.calls if m == "POST" and p == "/api/gen/trash"]
    assert set(trash[-1]["clip_ids"]) == {"c1", "c2", "r1"}  # 失败渲染 r1 清理，r2 保护
    assert db.get_job(ctx.job_id)["render_state"] == "succeeded"


async def test_export_verified_silence_retries_once(make_ctx, monkeypatch):
    monkeypatch.setattr(exporter, "_probe_audio",
                        fake_probe(lambda c: "silent" if c.get("id") == "r1" else "sound"))
    pf = FakePF()
    export_routes(pf, make_project(), render_ids=("r1", "r2"))
    ctx = make_ctx(pf=pf)
    out = await exporter.export(make_project(), ctx)
    assert out["song_id"] == "r2"
    assert pf.count("POST", "/api/studio/render-state") == 2


async def test_export_both_silent_fails(make_ctx, db, monkeypatch):
    monkeypatch.setattr(exporter, "_probe_audio", fake_probe("silent"))
    pf = FakePF()
    export_routes(pf, make_project(), render_ids=("r1", "r2"))
    ctx = make_ctx(pf=pf)
    with pytest.raises(AppError) as e:
        await exporter.export(make_project(), ctx)
    assert e.value.code == "AUDIO_SILENT"
    assert pf.count("POST", "/api/studio/render-state") == 2
    assert db.get_job(ctx.job_id)["render_state"] == "failed"


async def test_export_no_retry_when_budget_low(make_ctx, db, monkeypatch):
    monkeypatch.setattr(exporter, "RENDER_BUDGET_S", 0.15)
    pf = FakePF()
    export_routes(pf, make_project(), statuses={"r1": "error"})
    ctx = make_ctx(pf=pf)
    with pytest.raises(AppError) as e:
        await exporter.export(make_project(), ctx)
    assert e.value.code == "RENDER_ERROR"
    assert pf.count("POST", "/api/studio/render-state") == 1
    assert db.get_job(ctx.job_id)["render_state"] == "failed"


async def test_export_sync_timeout_no_render(make_ctx, db):
    pf = FakePF()
    export_routes(pf, make_project(), sync_ok=False)
    ctx = make_ctx(pf=pf)
    with pytest.raises(AppError) as e:
        await exporter.export(make_project(), ctx)
    assert e.value.code == "SONG_SYNC_TIMEOUT"
    assert pf.count("POST", "/api/studio/render-state") == 0
    assert db.get_job(ctx.job_id)["render_state"] == ""


async def test_export_sync_pitch_missing_aborts(make_ctx):
    pf = FakePF()
    project = make_project()
    bad = copy.deepcopy(project["state"])
    for c in restorer.iter_clips(bad):
        c["transposition"] = 0
    pf.route("GET", "/api/studio/project/p1",
             ok({"state": bad, "studio_project_version_id": "v10"}))
    pf.route("POST", "/api/studio/render-state", ok({"id": "r1"}))
    ctx = make_ctx(pf=pf)
    with pytest.raises(AppError) as e:
        await exporter.export(project, ctx)
    assert e.value.code == "STUDIO_PITCH_RESTORE_NOT_SAVED"
    assert pf.count("POST", "/api/studio/render-state") == 0


async def test_export_render_status_probe_disabled_after_404(make_ctx, monkeypatch):
    monkeypatch.setattr(exporter, "_probe_audio", fake_probe("sound"))
    pf = FakePF()
    export_routes(pf, make_project())
    pf.route("GET", "/api/studio/render-status", SunoAPIError("SUNO_HTTP_ERROR", 404))
    ctx = make_ctx(pf=pf)
    out = await exporter.export(make_project(), ctx)
    assert out["song_id"] == "r1"
    assert pf.count("GET", "/api/studio/render-status") == 1  # 404 后停用探针


async def test_export_cancel_before_start(make_ctx):
    pf = FakePF()
    ctx = make_ctx(pf=pf)
    ctx.cancel.set()
    with pytest.raises(AppError) as e:
        await exporter.export(make_project(), ctx)
    assert e.value.code == "CANCELLED"
    assert pf.calls == []


# ================= 续查（07） =================

def make_orig(db, *, render_state="pending", render_id="r1"):
    orig = db.insert_job("upload", "x.mp3", {"source_name": "x.mp3"})
    db.update_job(orig["id"], status="failed", render_state=render_state,
                  render_id=render_id, error_code="RENDER_TIMEOUT", error_msg="渲染超时",
                  result={"project_id": "p1", "version_id": "v9",
                          "clip_ids": ["c1", "c2"], "render_ids": [render_id]})
    return orig


async def test_resume_success_updates_original_and_never_resubmits(make_ctx, db, monkeypatch):
    monkeypatch.setattr(exporter, "_probe_audio", fake_probe("sound"))
    orig = make_orig(db)
    pf = FakePF()
    pf.route("GET", "/api/clip/", ok({"id": "r1", "status": "complete",
                                     "audio_url": "https://cdn1.suno.ai/r1.mp3"}))
    pf.route("POST", "/api/gen/trash", lambda p, b: ok({"is_trashed": True, "ids": b["clip_ids"]}))
    ctx = make_ctx(options={"resume_of": orig["id"]}, pf=pf)
    await exporter.resume_run(ctx.job, ctx)

    o = db.get_job(orig["id"])
    assert o["status"] == "success"
    assert o["render_state"] == "succeeded"
    assert o["suno_song_id"] == "r1"
    assert o["result"]["url"] == "https://suno.com/song/r1"
    assert o["error_code"] == ""
    assert ctx._finished
    # 只读：绝不重新提交渲染/保存/建工程
    assert pf.count("POST", "/api/studio/render-state") == 0
    assert pf.count("POST", "/api/studio/save-project") == 0
    assert pf.count("POST", "/api/studio/create-project") == 0
    trash = [b for m, p, b in pf.calls if m == "POST" and p == "/api/gen/trash"]
    assert trash and set(trash[0]["clip_ids"]) == {"c1", "c2"}  # 原任务中间产物，保护成品


async def test_resume_failure_marks_original_failed(make_ctx, db):
    orig = make_orig(db)
    pf = FakePF()
    pf.route("GET", "/api/clip/", ok({"id": "r1", "status": "error"}))
    ctx = make_ctx(options={"resume_of": orig["id"]}, pf=pf)
    with pytest.raises(AppError) as e:
        await exporter.resume_run(ctx.job, ctx)
    assert e.value.code == "RENDER_ERROR"
    o = db.get_job(orig["id"])
    assert o["render_state"] == "failed" and o["status"] == "failed"


async def test_resume_timeout_keeps_original_pending(make_ctx, db, monkeypatch):
    monkeypatch.setattr(exporter, "RENDER_BUDGET_S", 0.15)
    orig = make_orig(db)
    pf = FakePF()
    pf.route("GET", "/api/clip/", ok({"id": "r1", "status": "queued"}))
    ctx = make_ctx(options={"resume_of": orig["id"]}, pf=pf)
    with pytest.raises(AppError) as e:
        await exporter.resume_run(ctx.job, ctx)
    assert e.value.code == "QUEUED_TIMEOUT"  # clip 状态 queued → 排队超时
    assert db.get_job(orig["id"])["render_state"] == "pending"  # 可再次续查
    assert pf.count("POST", "/api/studio/render-state") == 0


async def test_resume_unavailable_when_not_pending(make_ctx, db):
    orig = make_orig(db, render_state="succeeded")
    pf = FakePF()
    ctx = make_ctx(options={"resume_of": orig["id"]}, pf=pf)
    with pytest.raises(AppError) as e:
        await exporter.resume_run(ctx.job, ctx)
    assert e.value.code == "EXPORT_RESUME_UNAVAILABLE"
    assert pf.calls == []


async def test_resume_query_requires_render_id(make_ctx):
    ctx = make_ctx()
    with pytest.raises(AppError) as e:
        await exporter.resume_query("", ctx)
    assert e.value.code == "EXPORT_RESUME_UNAVAILABLE"


# ================= 清理 =================

async def test_cleanup_partial_confirm_retries_once(make_ctx):
    pf = FakePF()
    calls = []

    def trash(path, body):
        calls.append(list(body["clip_ids"]))
        ids = body["clip_ids"][:1] if len(calls) == 1 else body["clip_ids"]
        return ok({"is_trashed": True, "ids": ids})

    pf.route("POST", "/api/gen/trash", trash)
    ctx = make_ctx(pf=pf)
    await exporter._cleanup(ctx, ["c1", "c2"], [], "final")
    assert calls == [["c1", "c2"], ["c2"]]


async def test_cleanup_failure_never_raises(make_ctx):
    pf = FakePF()
    pf.route("POST", "/api/gen/trash", SunoAPIError("SUNO_HTTP_ERROR", 500))
    ctx = make_ctx(pf=pf)
    await exporter._cleanup(ctx, ["c1"], ["r1"], "r1")  # 不抛异常即通过


async def test_cleanup_protects_final_and_dedups(make_ctx):
    pf = FakePF()
    seen = []
    pf.route("POST", "/api/gen/trash",
             lambda p, b: seen.append(b["clip_ids"]) or ok({"is_trashed": True, "ids": b["clip_ids"]}))
    ctx = make_ctx(pf=pf)
    await exporter._cleanup(ctx, ["c1", "r1"], ["r1", "r1"], "r1")
    assert seen == [["c1"]]


# ================= 静音校验（真实 ffmpeg，离线生成 fixture） =================

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要本机 ffmpeg")


def _gen_wav(path, lavfi):
    subprocess.run([ffmedia.FFMPEG, "-y", "-f", "lavfi", "-i", lavfi,
                    "-t", "1", "-ar", "16000", "-ac", "1", str(path)],
                   check=True, capture_output=True)


@requires_ffmpeg
async def test_audio_peak_sine_vs_silence(tmp_path):
    sine, silent = tmp_path / "sine.wav", tmp_path / "silent.wav"
    _gen_wav(sine, "sine=frequency=440:sample_rate=16000")
    _gen_wav(silent, "anullsrc=r=16000:cl=mono")
    assert await exporter._audio_peak(sine, tmp_path) > 0.1
    assert await exporter._audio_peak(silent, tmp_path) == 0.0


@requires_ffmpeg
async def test_probe_audio_sound_silent_mango_error(make_ctx, tmp_path, monkeypatch):
    sine, silent = tmp_path / "sine.wav", tmp_path / "silent.wav"
    _gen_wav(sine, "sine=frequency=440:sample_rate=16000")
    _gen_wav(silent, "anullsrc=r=16000:cl=mono")
    src = {"file": sine}

    async def fake_download(url, dest, **kw):
        shutil.copyfile(src["file"], dest)
        return dest

    monkeypatch.setattr(exporter, "download_cdn", fake_download)
    ctx = make_ctx()
    clip = {"id": "r1", "status": "complete", "audio_url": "https://cdn1.suno.ai/r1.mp3"}
    verdict, url = await exporter._probe_audio(ctx, clip)
    assert verdict == "sound" and url == clip["audio_url"]

    src["file"] = silent
    verdict, _ = await exporter._probe_audio(ctx, clip)
    assert verdict == "silent"

    # 仅加密候选 → 无法验证 ≠ 静音
    mango_clip = {"id": "r2", "media_urls": [
        {"url": "https://cdn1.suno.ai/r2.m4a", "content_type": "audio/mp4",
         "delivery": "mango", "encoding": "mango"}]}
    verdict, _ = await exporter._probe_audio(ctx, mango_clip)
    assert verdict == "unverifiable"

    # 下载失败 → error（继续轮询，不判失败）
    async def bad_download(url, dest, **kw):
        raise AppError("NETWORK_ERROR")

    monkeypatch.setattr(exporter, "download_cdn", bad_download)
    verdict, _ = await exporter._probe_audio(ctx, clip)
    assert verdict == "error"
