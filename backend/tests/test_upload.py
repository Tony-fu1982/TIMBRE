"""issue 05 自测：transformer 用真 ffmpeg（conftest tone_wav）；uploader 用假 PageFetch/Chrome。
不触网、不开真浏览器。覆盖：44.1k/48k 变形≈4×、六片、六步顺序、S3 重试上限 4、
副作用 POST 未知不重放、轮询兜底、取消（含页内 abort）、白名单、未登录。"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from app import ffmedia
from app.errors import AppError, SunoAPIError
from app.jobs import JobContext
from app.suno import transformer, uploader


# ---------- 假件 ----------

class FakePage:
    def __init__(self):
        self.calls = []  # (script, arg)
        self.s3_handler = None  # callable(arg) -> dict | coroutine

    async def evaluate(self, script, arg=None):
        self.calls.append((script, arg))
        if isinstance(arg, dict) and "base64" in arg:  # S3 直传脚本
            if self.s3_handler is not None:
                res = self.s3_handler(arg)
                if asyncio.iscoroutine(res):
                    res = await res
                return res
            return {"ok": True, "status": 204}
        return None  # 取消 watcher 等


class FakeChrome:
    def __init__(self, page):
        self.page = page
        self.checks = 0
        self.off_site = False

    async def ensure(self):
        return self.page

    async def check_page(self, page):
        self.checks += 1
        if self.off_site:
            raise AppError("PAGE_OFF_SITE")


class FakePF:
    def __init__(self, handler):
        self.calls = []  # (method, path, body)
        self.handler = handler

    async def request(self, method, path, body=None, *, timeout_ms=20000, cancel_event=None):
        self.calls.append((method, path, body))
        if cancel_event is not None and cancel_event.is_set():
            raise AppError("CANCELLED")
        return self.handler(method, path, body)


def happy_handler(state):
    """六步全绿：poll 回 complete（不带 clip），clipId 由 initialize-clip 给出。"""
    def h(method, path, body):
        if method == "GET" and path == "/api/user/user_config/":
            return {"ok": True, "status": 200, "json": {"id": "user"}}
        if method == "POST" and path == "/api/uploads/audio/":
            state["init"] = state.get("init", 0) + 1
            n = state["init"]
            return {"ok": True, "status": 200,
                    "json": {"id": f"up{n}", "url": "https://s3.example/presigned",
                             "fields": {"key": f"k{n}", "policy": "p"}}}
        if path.endswith("/upload-finish/"):
            assert body["upload_type"] == "file_upload" and body["upload_filename"]
            return {"ok": True, "status": 200, "json": {}}
        if method == "GET" and path.startswith("/api/uploads/audio/"):
            return {"ok": True, "status": 200, "json": {"status": "complete"}}
        if path.endswith("/initialize-clip/"):
            up = path.split("/")[4]
            return {"ok": True, "status": 200, "json": {"clip_id": f"clip-{up}"}}
        raise AssertionError(f"意外请求 {method} {path}")
    return h


def make_ctx(tmp_path, db, bus, pf=None, chrome=None):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    job = db.insert_job("upload", "", {})
    return JobContext(job["id"], job, db, asyncio.Event(), work, {}, chrome, pf, bus)


def make_slices(tmp_path, n=6):
    out = []
    for i in range(n):
        p = tmp_path / f"part_{i}.mp3"
        p.write_bytes(b"\xff\xfb-fake-mp3-" + bytes([i]) * 64)
        out.append({"path": str(p), "duration": 10.0 + i})
    return out


def s3_args(page):
    return [arg for _, arg in page.calls if isinstance(arg, dict) and "base64" in arg]


def count(pf, method, path):
    return sum(1 for m, p, _ in pf.calls if m == method and p == path)


@pytest.fixture
def fast(monkeypatch):
    for name in ("POLL_SLEEP_S", "POLL_JITTER_S", "INIT_RETRY_SLEEP_S",
                 "S3_RETRY_BASE_S", "S3_RETRY_JITTER_S"):
        monkeypatch.setattr(uploader, name, 0.0)


def upload_env(tmp_path, db, bus, handler=None, state=None):
    state = state if state is not None else {}
    pf = FakePF(handler or happy_handler(state))
    page = FakePage()
    chrome = FakeChrome(page)
    ctx = make_ctx(tmp_path, db, bus, pf, chrome)
    return ctx, pf, page, chrome, state


# ---------- transformer（真 ffmpeg） ----------

@pytest.mark.parametrize("sr", [44100, 48000])
async def test_transform_four_times_and_six_slices(sr, tone_wav, tmp_path, db, bus):
    src = tone_wav(name=f"tone_{sr}.wav", sr=sr, seconds=3.0)
    ctx = make_ctx(tmp_path, db, bus)
    meta = await transformer.transform(src, ctx)
    assert meta["transformed"] is True
    assert meta["sr"] == sr
    assert meta["source_duration"] == pytest.approx(3.0, abs=0.05)
    assert len(meta["slices"]) == 6
    total = 0.0
    for sl in meta["slices"]:
        p = Path(sl["path"])
        assert p.is_file() and p.parent == ctx.workdir
        info = await ffmedia.probe(p)
        assert info["sr"] == sr  # aresample 回源采样率
        assert sl["duration"] == pytest.approx(info["duration"], abs=0.01)
        total += info["duration"]
    assert total == pytest.approx(3.0 * 4, rel=0.02)  # ≈4×（±2%）
    for sl in meta["slices"]:
        assert sl["duration"] == pytest.approx(total / 6, abs=0.2)  # 等分


async def test_transform_bad_audio(tmp_path, db, bus):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"this is not audio")
    ctx = make_ctx(tmp_path, db, bus)
    with pytest.raises(AppError) as ei:
        await transformer.transform(bad, ctx)
    assert ei.value.code == "BAD_AUDIO"


async def test_transform_too_short(tone_wav, tmp_path, db, bus):
    src = tone_wav(name="short.wav", seconds=0.4)
    ctx = make_ctx(tmp_path, db, bus)
    with pytest.raises(AppError) as ei:
        await transformer.transform(src, ctx)
    assert ei.value.code == "BAD_AUDIO" and "过短" in ei.value.message


async def test_transform_size_cap(tone_wav, tmp_path, db, bus, monkeypatch):
    monkeypatch.setattr(transformer, "MAX_INPUT_BYTES", 10)
    ctx = make_ctx(tmp_path, db, bus)
    with pytest.raises(AppError) as ei:
        await transformer.transform(tone_wav(), ctx)
    assert ei.value.code == "BAD_AUDIO" and "50MB" in ei.value.message


async def test_transform_cancelled(tone_wav, tmp_path, db, bus):
    ctx = make_ctx(tmp_path, db, bus)
    ctx.cancel.set()
    with pytest.raises(AppError) as ei:
        await transformer.transform(tone_wav(), ctx)
    assert ei.value.code == "CANCELLED"
    assert not (ctx.workdir / "slow.mp3").exists()


# ---------- uploader（假 PageFetch/Chrome） ----------

@pytest.fixture(autouse=True)
def _upload_seq(monkeypatch):
    # 失败语义用例（绝不重放/停在第一片/轮询上限）按串行副作用计数断言；
    # 并发路径由 happy 用例单独覆盖（其内 monkeypatch 回 3）。
    monkeypatch.setattr(uploader, "UPLOAD_CONCURRENCY", 1)


async def test_upload_six_steps_order_and_ids(tmp_path, db, bus, fast, monkeypatch):
    monkeypatch.setattr(uploader, "UPLOAD_CONCURRENCY", 3)
    ctx, pf, page, chrome, _ = upload_env(tmp_path, db, bus)
    slices = make_slices(tmp_path)
    ids = await uploader.upload_slices(slices, ctx)
    assert ids == [f"clip-up{i}" for i in range(1, 7)]  # gather 保序
    assert count(pf, "GET", "/api/user/user_config/") == 6
    assert count(pf, "POST", "/api/uploads/audio/") == 6
    for i in range(1, 7):  # 并发下全局顺序不定，按片断言六步链内顺序
        chain = [(m, p) for m, p, _ in pf.calls if f"/up{i}/" in p]
        assert chain == [("POST", f"/api/uploads/audio/up{i}/upload-finish/"),
                         ("GET", f"/api/uploads/audio/up{i}/"),
                         ("POST", f"/api/uploads/audio/up{i}/initialize-clip/")]
    args = sorted(s3_args(page), key=lambda a: a["fileName"])
    assert len(args) == 6
    for i, a in enumerate(args):
        assert base64.b64decode(a["base64"]) == Path(slices[i]["path"]).read_bytes()
        assert a["url"] == "https://s3.example/presigned"
        assert a["fields"] == {"key": f"k{i + 1}", "policy": "p"}
        assert a["fileName"] == f"part_{i}.mp3"
        assert a["timeoutMs"] == 120_000
    assert chrome.checks == 6  # 每次 S3 页内请求前 check_page
    lines = [r["message"] for r in db.logs_after(ctx.job_id, 0)["lines"]]
    for i in range(1, 7):
        assert f"上传 {i}/6" in lines
        assert f"片段 {i} 上传完成：clipId=clip-up{i}" in lines


async def test_s3_retry_cap_4_then_fail(tmp_path, db, bus, fast):
    ctx, pf, page, _, _ = upload_env(tmp_path, db, bus)
    page.s3_handler = lambda a: {"ok": False, "status": 403, "text": "AccessDenied"}
    with pytest.raises(AppError) as ei:
        await uploader.upload_slices(make_slices(tmp_path), ctx)
    assert ei.value.code == "NETWORK_ERROR"
    assert len(s3_args(page)) == 4  # 重试上限 4，且停在第一片
    assert count(pf, "GET", "/api/user/user_config/") == 1
    assert not any(p.endswith("/upload-finish/") for _, p, _ in pf.calls)


async def test_init_transport_unknown_no_replay(tmp_path, db, bus, fast):
    base = happy_handler({})

    def h(m, p, b):
        if m == "POST" and p == "/api/uploads/audio/":
            raise SunoAPIError("NETWORK_RESULT_UNKNOWN", 0, "TIMEOUT_OR_ABORTED")
        return base(m, p, b)

    ctx, pf, page, _, _ = upload_env(tmp_path, db, bus, h)
    with pytest.raises(AppError) as ei:
        await uploader.upload_slices(make_slices(tmp_path), ctx)
    assert ei.value.code == "NETWORK_RESULT_UNKNOWN"
    assert count(pf, "POST", "/api/uploads/audio/") == 1  # 绝不重放
    assert s3_args(page) == []


async def test_init_5xx_unknown_no_replay(tmp_path, db, bus, fast):
    base = happy_handler({})

    def h(m, p, b):
        if m == "POST" and p == "/api/uploads/audio/":
            raise SunoAPIError("SUNO_HTTP_ERROR", 502, "bad gateway")
        return base(m, p, b)

    ctx, pf, _, _, _ = upload_env(tmp_path, db, bus, h)
    with pytest.raises(AppError) as ei:
        await uploader.upload_slices(make_slices(tmp_path), ctx)
    assert ei.value.code == "NETWORK_RESULT_UNKNOWN"
    assert count(pf, "POST", "/api/uploads/audio/") == 1


async def test_init_malformed_json_unknown_no_replay(tmp_path, db, bus, fast):
    base = happy_handler({})

    def h(m, p, b):
        if m == "POST" and p == "/api/uploads/audio/":
            return {"ok": True, "status": 200, "json": "not-an-object"}  # 畸形响应不崩、按未知处理
        return base(m, p, b)

    ctx, pf, page, _, _ = upload_env(tmp_path, db, bus, h)
    with pytest.raises(AppError) as ei:
        await uploader.upload_slices(make_slices(tmp_path), ctx)
    assert ei.value.code == "NETWORK_RESULT_UNKNOWN"
    assert count(pf, "POST", "/api/uploads/audio/") == 1
    assert s3_args(page) == []


async def test_initialize_clip_unknown_no_replay_poll_fallback(tmp_path, db, bus, fast):
    base = happy_handler({})

    def h(m, p, b):
        if p.endswith("/initialize-clip/"):
            raise SunoAPIError("NETWORK_RESULT_UNKNOWN", 0, "TIMEOUT_OR_ABORTED")
        return base(m, p, b)

    ctx, pf, _, _, _ = upload_env(tmp_path, db, bus, h)
    with pytest.raises(AppError) as ei:
        await uploader.upload_slices(make_slices(tmp_path), ctx)
    assert ei.value.code == "NETWORK_RESULT_UNKNOWN"
    assert count(pf, "POST", "/api/uploads/audio/up1/initialize-clip/") == 1  # 只发一次
    # 步5 首次 poll(complete) + 40 次兜底轮询
    assert count(pf, "GET", "/api/uploads/audio/up1/") == 1 + uploader.CLIP_POLL_MAX


async def test_initialize_ok_without_clip_recovers_via_poll(tmp_path, db, bus, fast):
    state = {"polls": 0}
    base = happy_handler(state)

    def h(m, p, b):
        if p.endswith("/initialize-clip/") and "/up1/" in p:
            return {"ok": True, "status": 200, "json": {}}  # 200 但无 clipId
        if m == "GET" and p == "/api/uploads/audio/up1/":
            state["polls"] += 1
            if state["polls"] >= 3:
                return {"ok": True, "status": 200, "json": {"status": "complete", "clip_id": "clip-late"}}
            return {"ok": True, "status": 200, "json": {"status": "complete"}}
        return base(m, p, b)

    ctx, pf, _, _, _ = upload_env(tmp_path, db, bus, h, state)
    ids = await uploader.upload_slices(make_slices(tmp_path), ctx)
    assert ids == ["clip-late"] + [f"clip-up{i}" for i in range(2, 7)]
    assert count(pf, "POST", "/api/uploads/audio/up1/initialize-clip/") == 1


async def test_finish_5xx_recovered_by_poll_with_clip(tmp_path, db, bus, fast):
    base = happy_handler({})

    def h(m, p, b):
        if p.endswith("/upload-finish/"):
            raise SunoAPIError("SUNO_HTTP_ERROR", 502, "bad gateway")
        if m == "GET" and p.startswith("/api/uploads/audio/up"):
            up = p.split("/")[4]
            return {"ok": True, "status": 200, "json": {"status": "complete", "clip_id": f"clip-{up}"}}
        return base(m, p, b)

    ctx, pf, _, _, _ = upload_env(tmp_path, db, bus, h)
    ids = await uploader.upload_slices(make_slices(tmp_path), ctx)
    assert ids == [f"clip-up{i}" for i in range(1, 7)]
    assert not any(p.endswith("/initialize-clip/") for _, p, _ in pf.calls)  # poll 已带 clip


async def test_finish_unknown_never_complete(tmp_path, db, bus, fast):
    base = happy_handler({})

    def h(m, p, b):
        if p.endswith("/upload-finish/"):
            raise SunoAPIError("NETWORK_RESULT_UNKNOWN", 0, "TIMEOUT_OR_ABORTED")
        if m == "GET" and p.startswith("/api/uploads/audio/up"):
            return {"ok": True, "status": 200, "json": {"status": "processing"}}
        return base(m, p, b)

    ctx, pf, _, _, _ = upload_env(tmp_path, db, bus, h)
    with pytest.raises(AppError) as ei:
        await uploader.upload_slices(make_slices(tmp_path), ctx)
    assert ei.value.code == "NETWORK_RESULT_UNKNOWN"
    assert count(pf, "GET", "/api/uploads/audio/up1/") == uploader.POLL_MAX  # 轮询上限 120
    assert not any(p.endswith("/initialize-clip/") for _, p, _ in pf.calls)


@pytest.mark.parametrize("bad_status", ["error", "failed"])
async def test_poll_processing_error(bad_status, tmp_path, db, bus, fast):
    base = happy_handler({})

    def h(m, p, b):
        if m == "GET" and p.startswith("/api/uploads/audio/up"):
            return {"ok": True, "status": 200, "json": {"status": bad_status}}
        return base(m, p, b)

    ctx, pf, _, _, _ = upload_env(tmp_path, db, bus, h)
    with pytest.raises(AppError) as ei:
        await uploader.upload_slices(make_slices(tmp_path), ctx)
    assert ei.value.code == "SUNO_HTTP_ERROR" and bad_status in ei.value.message
    assert not any(p.endswith("/initialize-clip/") for _, p, _ in pf.calls)


async def test_not_logged_in_stops_before_init(tmp_path, db, bus, fast):
    def h(m, p, b):
        if p == "/api/user/user_config/":
            raise SunoAPIError("NOT_LOGGED_IN", 401)
        raise AssertionError("未登录后不应再有请求")

    ctx, pf, _, _, _ = upload_env(tmp_path, db, bus, h)
    with pytest.raises(AppError) as ei:
        await uploader.upload_slices(make_slices(tmp_path), ctx)
    assert ei.value.code == "NOT_LOGGED_IN"
    assert len(pf.calls) == 1


async def test_cancel_before_start(tmp_path, db, bus, fast):
    ctx, pf, page, _, _ = upload_env(tmp_path, db, bus)
    ctx.cancel.set()
    with pytest.raises(AppError) as ei:
        await uploader.upload_slices(make_slices(tmp_path), ctx)
    assert ei.value.code == "CANCELLED"
    assert pf.calls == [] and page.calls == []


async def test_cancel_during_s3_no_finish(tmp_path, db, bus, fast):
    ctx, pf, page, _, _ = upload_env(tmp_path, db, bus)

    def s3(a):
        ctx.cancel.set()
        return {"ok": True, "status": 204}

    page.s3_handler = s3
    with pytest.raises(AppError) as ei:
        await uploader.upload_slices(make_slices(tmp_path), ctx)
    assert ei.value.code == "CANCELLED"
    assert not any(p.endswith("/upload-finish/") for _, p, _ in pf.calls)


async def test_cancel_aborts_inflight_s3(tmp_path, db, bus, fast):
    ctx, pf, page, _, _ = upload_env(tmp_path, db, bus)

    async def slow_s3(a):
        await asyncio.sleep(0.05)  # 期间取消 → watcher 页内 abort
        return {"ok": False, "status": 0, "cancelled": True, "text": "AbortError"}

    page.s3_handler = slow_s3

    async def canceller():
        await asyncio.sleep(0.01)
        ctx.cancel.set()

    task = asyncio.create_task(canceller())
    with pytest.raises(AppError) as ei:
        await uploader.upload_slices(make_slices(tmp_path), ctx)
    await task
    assert ei.value.code == "CANCELLED"
    assert any("__gjStop" in script for script, _ in page.calls)  # 页内 abort 已下发
    assert not any(p.endswith("/upload-finish/") for _, p, _ in pf.calls)


async def test_page_off_site_blocks_s3(tmp_path, db, bus, fast):
    ctx, pf, page, chrome, _ = upload_env(tmp_path, db, bus)
    chrome.off_site = True
    with pytest.raises(AppError) as ei:
        await uploader.upload_slices(make_slices(tmp_path), ctx)
    assert ei.value.code == "PAGE_OFF_SITE"
    assert s3_args(page) == []  # 白名单不过 → 不进页内直传
    assert count(pf, "POST", "/api/uploads/audio/") == 1


async def test_slice_count_guard(tmp_path, db, bus, fast):
    ctx, pf, _, _, _ = upload_env(tmp_path, db, bus)
    with pytest.raises(AppError) as ei:
        await uploader.upload_slices(make_slices(tmp_path, 5), ctx)
    assert ei.value.code == "INTERNAL_ERROR"
    assert pf.calls == []
