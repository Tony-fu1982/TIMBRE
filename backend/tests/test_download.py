"""单曲下载集成测试（issue 02/03）：假 PageFetch + 假 CDN，真 ffmpeg。
覆盖候选顺序、明文嗅探优先、mango 授权解密、回退链、取消与错误码。不触网、不开 Chrome。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from pathlib import Path

import httpx
import pytest
from Crypto.Cipher import AES

from app import ffmedia
from app.errors import AppError, SunoAPIError
from app.jobs import JobContext
from app.suno import downloader

SONG_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SHARE_URL = f"https://suno.com/song/{SONG_ID}"
TOKEN = "fake-clerk-token-do-not-log"
MP3_URL = "https://cdn1.suno.ai/plain.mp3"
ENC_URL = "https://cdn1.suno.ai/enc.m4a"
FALLBACK_URLS = [t.format(id=SONG_ID) for t in downloader.common.CDN_FALLBACK]


class FakePF:
    def __init__(self, clip_payload=None, rights=None, rights_error=None, clip_error=None):
        self.clip_payload, self.rights = clip_payload, rights
        self.rights_error, self.clip_error = rights_error, clip_error
        self.calls = []

    async def token(self, force=False):
        return TOKEN

    async def request(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        if path.startswith("/api/clip/"):
            if self.clip_error:
                raise self.clip_error
            return {"ok": True, "status": 200, "json": self.clip_payload, "text": "", "retryAfter": None}
        if path == "/api/mango/rights":
            if self.rights_error:
                raise self.rights_error
            return {"ok": True, "status": 200, "json": self.rights, "text": "", "retryAfter": None}
        raise AssertionError(f"意外请求：{method} {path}")


def encrypt_fixture(media: bytes, token=TOKEN, content_id=SONG_ID):
    """自制 mango 夹具：GCM 包裹 key/iv + CTR 加密媒体。返回 (密文, rights 响应)。"""
    key, iv = os.urandom(16), os.urandom(16)
    uk = hashlib.sha256(token.encode()).digest()

    def wrap(pt):
        nonce = os.urandom(12)
        c = AES.new(uk, AES.MODE_GCM, nonce=nonce)
        c.update(content_id.encode())
        ct, tag = c.encrypt_and_digest(pt)
        return base64.b64encode(nonce + ct + tag).decode()

    cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=int.from_bytes(iv, "big"))
    return cipher.encrypt(media), {"key": wrap(key), "iv": wrap(iv)}


@pytest.fixture
async def mp3_bytes(tone_wav, tmp_path):
    dst = tmp_path / "tone.mp3"
    await ffmedia.transcode(tone_wav(), dst, "mp3")
    return dst.read_bytes()


@pytest.fixture
def rig(db, bus, tmp_path, monkeypatch):
    """返回 setup(clip, mapping, pf, fmt, url, fail, on_call) → (ctx, job, 下载 URL 记录)。"""
    monkeypatch.setattr(downloader.common, "fetch_share_html",
                        lambda url, timeout=20: asyncio.sleep(0, result=f'<link rel="canonical" href="{SHARE_URL}">'))

    def setup(clip=None, mapping=None, pf=None, fmt="mp3", url=SHARE_URL, fail=(), on_call=None, html=None):
        if html is not None:
            monkeypatch.setattr(downloader.common, "fetch_share_html",
                                lambda u, timeout=20: asyncio.sleep(0, result=html))
        seen = []

        async def dl(u, dest, timeout=120, on_progress=None):
            seen.append(u)
            if on_call:
                await on_call(u)
            if u in fail or u not in (mapping or {}):
                raise AppError("NETWORK_ERROR", "CDN 返回 404")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(mapping[u])
            return dest

        monkeypatch.setattr(downloader.common, "download_cdn", dl)
        pf = pf if pf is not None else FakePF({"clip": clip or {"id": SONG_ID}})
        job = db.insert_job("download", url, {"format": fmt, "mode": "song"})
        ctx = JobContext(job["id"], job, db, asyncio.Event(), tmp_path / "work",
                         {"download_dir": str(tmp_path / "out")}, None, pf, bus)
        return ctx, job, seen, pf

    return setup


def _finished(db, job):
    j = db.get_job(job["id"])
    assert j["status"] == "success", j
    return j


async def test_plaintext_mp3_success(rig, db, mp3_bytes):
    ctx, job, seen, pf = rig(clip={"id": SONG_ID, "title": "测试歌曲", "audio_url": MP3_URL},
                             mapping={MP3_URL: mp3_bytes})
    await downloader.run(ctx.job, ctx)
    j = _finished(db, job)
    assert j["suno_song_id"] == SONG_ID
    assert j["result"]["container"] == "mp3" and j["result"]["encrypted"] is False
    assert j["result"]["title"] == "测试歌曲" and j["result"]["format"] == "mp3"
    out = Path(j["output_path"])
    assert out.suffix == ".mp3" and out.exists()
    assert abs((await ffmedia.probe(out))["duration"] - 2.0) < 0.3
    assert seen == [MP3_URL]
    assert not any(p == "/api/mango/rights" for _, p, _ in pf.calls)  # 明文不走授权


async def test_broken_audio_candidate_falls_back_after_transcode(rig, db, mp3_bytes):
    fallback = downloader.common.CDN_FALLBACK[1].format(id=SONG_ID)
    ctx, job, seen, _ = rig(clip={"id": SONG_ID, "audio_url": MP3_URL},
                            mapping={MP3_URL: b"ID3" + bytes(600), fallback: mp3_bytes})
    await downloader.run(ctx.job, ctx)
    assert seen[0] == MP3_URL
    assert _finished(db, job)["result"]["source_url"] == fallback


async def test_cancel_interrupts_stalled_cdn_read(rig):
    started, stopped = asyncio.Event(), asyncio.Event()

    async def stall(_):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    ctx, _, _, _ = rig(clip={"id": SONG_ID, "audio_url": MP3_URL}, on_call=stall)
    task = asyncio.create_task(downloader.run(ctx.job, ctx))
    await asyncio.wait_for(started.wait(), 1)
    ctx.cancel.set()
    with pytest.raises(AppError) as exc:
        await asyncio.wait_for(task, 1)
    assert exc.value.code == "CANCELLED" and stopped.is_set()


async def test_wav_output(rig, db, mp3_bytes):
    ctx, job, _, _ = rig(clip={"id": SONG_ID, "audio_url": MP3_URL},
                         mapping={MP3_URL: mp3_bytes}, fmt="wav")
    await downloader.run(ctx.job, ctx)
    j = _finished(db, job)
    out = Path(j["output_path"])
    assert out.suffix == ".wav"
    assert (await ffmedia.probe(out))["codec"] == "pcm_s16le"


async def test_encrypted_success_and_token_never_logged(rig, db, mp3_bytes):
    enc, rights = encrypt_fixture(mp3_bytes)
    clip = {"id": SONG_ID, "title": "加密歌", "media_urls": [
        {"url": ENC_URL, "content_type": "audio/m4a-opus", "encoding": "mango", "delivery": "progressive"}]}
    ctx, job, seen, pf = rig(clip=clip, mapping={ENC_URL: enc}, pf=FakePF({"clip": clip}, rights=rights))
    await downloader.run(ctx.job, ctx)
    j = _finished(db, job)
    assert j["result"]["encrypted"] is True and j["result"]["container"] == "mp3"
    assert abs((await ffmedia.probe(Path(j["output_path"])))["duration"] - 2.0) < 0.3
    rights_calls = [(m, p, b) for m, p, b in pf.calls if p == "/api/mango/rights"]
    assert rights_calls == [("POST", "/api/mango/rights",
                             {"content_params": {"content_id": SONG_ID, "content_type": "clip"}})]
    logs = db.logs_after(job["id"], 0, limit=500)["lines"]
    assert all(TOKEN not in line["message"] for line in logs)
    assert TOKEN not in (j["result"].get("source_url", "") + str(j["result"]))


async def test_candidate_order_encrypted_first(rig, db, mp3_bytes):
    clip = {"id": SONG_ID, "audio_url": MP3_URL, "media_urls": [
        {"url": ENC_URL, "content_type": "audio/m4a-opus", "encoding": "mango", "delivery": "progressive"}]}
    ctx, job, seen, _ = rig(clip=clip, mapping={MP3_URL: mp3_bytes})  # 加密候选 404 → 落到 mp3
    await downloader.run(ctx.job, ctx)
    _finished(db, job)
    assert seen == [ENC_URL, MP3_URL]


async def test_forbidden_url_dropped(rig, db, mp3_bytes):
    clip = {"id": SONG_ID, "audio_url": "https://cdn1.suno.ai/api/forbidden",
            "stream_audio_url": MP3_URL}
    ctx, job, seen, _ = rig(clip=clip, mapping={MP3_URL: mp3_bytes})
    await downloader.run(ctx.job, ctx)
    _finished(db, job)
    assert seen == [MP3_URL]


async def test_fallback_chain(rig, db, mp3_bytes):
    ctx, job, seen, _ = rig(clip={"id": SONG_ID}, mapping={FALLBACK_URLS[2]: mp3_bytes})
    await downloader.run(ctx.job, ctx)
    j = _finished(db, job)
    assert seen == FALLBACK_URLS  # 三条回退按序尝试
    assert j["result"]["source_url"] == FALLBACK_URLS[2]


async def test_all_fail_download_failed(rig, db, mp3_bytes):
    ctx, job, seen, _ = rig(clip={"id": SONG_ID, "audio_url": MP3_URL}, mapping={})
    with pytest.raises(AppError) as e:
        await downloader.run(ctx.job, ctx)
    assert e.value.code == "DOWNLOAD_FAILED"
    assert seen == [MP3_URL] + FALLBACK_URLS


async def test_all_mango_fail_surfaces_mango_code(rig, db):
    # 三条回退 URL 同时是加密候选（去重后不再追加明文回退）→ 全部死于解密 → 报 MANGO_*
    clip = {"id": SONG_ID, "media_urls": [
        {"url": u, "encoding": "mango", "delivery": "progressive"} for u in FALLBACK_URLS]}
    ctx, job, _, _ = rig(clip=clip, mapping={u: os.urandom(1024) for u in FALLBACK_URLS},
                         pf=FakePF({"clip": clip}, rights={}))  # rights 缺 key/iv
    with pytest.raises(AppError) as e:
        await downloader.run(ctx.job, ctx)
    assert e.value.code == "MANGO_RIGHTS_MISSING"


async def test_decrypted_unknown_container_tries_next(rig, db, mp3_bytes):
    enc, rights = encrypt_fixture(os.urandom(1024))  # 解密结果不是任何已知容器
    clip = {"id": SONG_ID, "audio_url": MP3_URL, "media_urls": [
        {"url": ENC_URL, "content_type": "audio/m4a-opus", "encoding": "mango", "delivery": "progressive"}]}
    ctx, job, seen, _ = rig(clip=clip, mapping={ENC_URL: enc, MP3_URL: mp3_bytes},
                            pf=FakePF({"clip": clip}, rights=rights))
    await downloader.run(ctx.job, ctx)
    j = _finished(db, job)
    assert seen == [ENC_URL, MP3_URL]
    assert j["result"]["encrypted"] is False


async def test_encrypted_flag_but_plaintext_skips_rights(rig, db, mp3_bytes):
    clip = {"id": SONG_ID, "media_urls": [
        {"url": ENC_URL, "content_type": "audio/m4a-opus", "encoding": "mango", "delivery": "progressive"}]}
    ctx, job, _, pf = rig(clip=clip, mapping={ENC_URL: mp3_bytes})  # 标记加密但实为明文（漂移）
    await downloader.run(ctx.job, ctx)
    j = _finished(db, job)
    assert j["result"]["encrypted"] is False
    assert not any(p == "/api/mango/rights" for _, p, _ in pf.calls)


async def test_cancel_before_start(rig, db):
    ctx, job, seen, _ = rig()
    ctx.cancel.set()
    with pytest.raises(AppError) as e:
        await downloader.run(ctx.job, ctx)
    assert e.value.code == "CANCELLED"
    assert seen == []


async def test_cancel_mid_download(rig, db):
    ctx, job, seen, _ = rig(clip={"id": SONG_ID, "audio_url": MP3_URL}, mapping={})

    async def on_call(u):
        ctx.cancel.set()

    ctx, job, seen, _ = rig(clip={"id": SONG_ID, "audio_url": MP3_URL}, mapping={}, on_call=on_call)
    with pytest.raises(AppError) as e:
        await downloader.run(ctx.job, ctx)
    assert e.value.code == "CANCELLED"


async def test_not_logged_in_propagates(rig, db):
    pf = FakePF(clip_error=SunoAPIError("NOT_LOGGED_IN", 401))
    ctx, job, seen, _ = rig(pf=pf)
    with pytest.raises(AppError) as e:
        await downloader.run(ctx.job, ctx)
    assert e.value.code == "NOT_LOGGED_IN"
    assert seen == []


async def test_rate_limited_propagates(rig, db):
    clip = {"id": SONG_ID, "media_urls": [
        {"url": ENC_URL, "content_type": "audio/m4a-opus", "encoding": "mango", "delivery": "progressive"}]}
    pf = FakePF({"clip": clip}, rights_error=SunoAPIError("RATE_LIMITED", 429))
    ctx, job, seen, _ = rig(clip=clip, mapping={ENC_URL: os.urandom(1024)}, pf=pf)
    with pytest.raises(AppError) as e:
        await downloader.run(ctx.job, ctx)
    assert e.value.code == "RATE_LIMITED"
    assert seen == [ENC_URL]  # 不再消耗后续候选


async def test_invalid_url(rig, db):
    ctx, job, seen, _ = rig(url="https://evil.example/song/x")
    with pytest.raises(AppError) as e:
        await downloader.run(ctx.job, ctx)
    assert e.value.code == "INVALID_URL"
    assert seen == []


async def test_unsupported_format(rig, db):
    ctx, job, _, _ = rig(fmt="flac")
    with pytest.raises(AppError) as e:
        await downloader.run(ctx.job, ctx)
    assert e.value.code == "UNSUPPORTED_FORMAT"


async def test_no_song_id_anywhere(rig, db):
    ctx, job, _, _ = rig(url="https://suno.com/s/short", html="<html>没有 ID</html>")
    with pytest.raises(AppError) as e:
        await downloader.run(ctx.job, ctx)
    assert e.value.code == "INVALID_URL"


async def test_clip_404(rig, db):
    pf = FakePF(clip_error=SunoAPIError("SUNO_HTTP_ERROR", 404))
    ctx, job, _, _ = rig(pf=pf)
    with pytest.raises(AppError) as e:
        await downloader.run(ctx.job, ctx)
    assert e.value.code == "INVALID_URL"
    assert "不存在" in e.value.message


@pytest.mark.parametrize("shape", ["data.clip", "data", "clips"])
async def test_clip_payload_shapes(rig, db, mp3_bytes, shape):
    clip = {"id": SONG_ID, "audio_url": MP3_URL}
    payload = {"data": {"clip": clip}} if shape == "data.clip" else (
        {"data": clip} if shape == "data" else {"clips": [clip]})
    ctx, job, _, _ = rig(mapping={MP3_URL: mp3_bytes}, pf=FakePF(payload))
    await downloader.run(ctx.job, ctx)
    _finished(db, job)


async def test_httpx_error_tries_next(rig, db, mp3_bytes, monkeypatch):
    clip = {"id": SONG_ID, "audio_url": MP3_URL, "media_urls": [
        {"url": ENC_URL, "content_type": "audio/m4a-opus", "encoding": "mango", "delivery": "progressive"}]}
    ctx, job, seen, _ = rig(clip=clip, mapping={MP3_URL: mp3_bytes})
    orig = downloader.common.download_cdn
    tried = []

    async def flaky(u, dest, timeout=120, on_progress=None):
        tried.append(u)
        if u == ENC_URL:
            raise httpx.ConnectError("模拟断网")
        return await orig(u, dest, timeout)

    monkeypatch.setattr(downloader.common, "download_cdn", flaky)
    await downloader.run(ctx.job, ctx)
    _finished(db, job)
    assert tried == [ENC_URL, MP3_URL]


# ---------- 字节进度（issue 02：节流写库，不每块都写） ----------

def _progress_spy(db, monkeypatch):
    updates = []
    orig = db.update_job

    def spy(job_id, **fields):
        if "progress" in fields:
            updates.append(fields["progress"])
        return orig(job_id, **fields)

    monkeypatch.setattr(db, "update_job", spy)
    return updates


def _chunked_dl(orig, chunks, total):
    """假 CDN：按 64KB 块同步回调 on_progress，再走真（假）下载写盘。"""
    async def dl(u, dest, timeout=120, on_progress=None):
        if on_progress:
            for i in range(1, chunks + 1):
                on_progress(i * 65536, total)
        return await orig(u, dest, timeout)
    return dl


@pytest.mark.parametrize("total", [250 * 1024 * 1024, 0])
async def test_progress_throttled_by_bytes(rig, db, mp3_bytes, monkeypatch, total):
    # 160 个 64KB 块、total 很大或未知 → 百分比条件不触发，只按 ≥256KB 节流：
    # 恰好 40 次中间上报 + 1 次收尾兜底，远少于 160 块
    ctx, job, _, _ = rig(clip={"id": SONG_ID, "audio_url": MP3_URL}, mapping={MP3_URL: mp3_bytes})
    updates = _progress_spy(db, monkeypatch)
    monkeypatch.setattr(downloader.common, "download_cdn",
                        _chunked_dl(downloader.common.download_cdn, 160, total))
    await downloader.run(ctx.job, ctx)
    j = _finished(db, job)
    assert len(updates) == 41
    assert updates[:-1] == [f"{k * 262144}/{total}" for k in range(1, 41)]
    # 收尾兜底写的是真实字节数；progress 字段终态会被 append_log 覆盖为最新日志行（db 既有语义）
    assert updates[-1] == f"{len(mp3_bytes)}/{len(mp3_bytes)}"
    logs = db.logs_after(job["id"], 0)["lines"]
    assert sum("下载中" in l["message"] for l in logs) == 0  # 未到 25% 不打里程碑日志


async def test_progress_small_file_pct_and_milestone_logs(rig, db, mp3_bytes, monkeypatch):
    # total=1MB、16 块：每块百分比变化 ≥1% → 逐块上报也仅 16+1 次；
    # 里程碑日志只在 25/50/75% 各一条（100% 由完成日志覆盖）
    ctx, job, _, _ = rig(clip={"id": SONG_ID, "audio_url": MP3_URL}, mapping={MP3_URL: mp3_bytes})
    updates = _progress_spy(db, monkeypatch)
    monkeypatch.setattr(downloader.common, "download_cdn",
                        _chunked_dl(downloader.common.download_cdn, 16, 1048576))
    await downloader.run(ctx.job, ctx)
    j = _finished(db, job)
    assert len(updates) == 17
    assert updates[-1] == f"{len(mp3_bytes)}/{len(mp3_bytes)}"
    logs = [l["message"] for l in db.logs_after(job["id"], 0)["lines"] if "下载中" in l["message"]]
    assert len(logs) == 3
    assert "25%" in logs[0] and "50%" in logs[1] and "75%" in logs[2]
