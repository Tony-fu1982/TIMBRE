"""分轨/人声状态机测试（issue 10/11，SPEC §4.7）：假 PageFetch + 假 CDN，真 ffmpeg、真 mango。
覆盖复用判定、c/check fail-closed、消耗性请求永不重试、互补对缺对失败、ZIP 内容、匿名/登录态解密。
不触网、不开 Chrome。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import uuid
import zipfile
from pathlib import Path

import pytest
from Crypto.Cipher import AES

from app import ffmedia
from app.errors import AppError, SunoAPIError
from app.jobs import JobContext
from app.suno import stems
from app.suno.common import sniff_container

SONG_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SHARE_URL = f"https://suno.com/song/{SONG_ID}"
TOKEN = "fake-clerk-token-SECRET"
ENC_URL = "https://cdn1.suno.ai/enc.m4a"
ORIGINAL = {"id": SONG_ID, "title": "测试歌曲", "status": "complete",
            "audio_url": f"https://cdn1.suno.ai/{SONG_ID}.mp3"}


# ---------- 夹具与假件 ----------

@pytest.fixture(autouse=True)
def no_real_anon(monkeypatch):
    """默认禁止匿名 rights 触网；加密测试自行覆盖。"""
    async def boom(ctx, sid):
        raise AssertionError("测试不应触网：_anonymous_rights 被调用")
    monkeypatch.setattr(stems, "_anonymous_rights", boom)


@pytest.fixture
async def mp3_bytes(tone_wav, tmp_path):
    dst = tmp_path / "tone.mp3"
    await ffmedia.transcode(tone_wav(), dst, "mp3")
    return dst.read_bytes()


def stem(sid, *, task="twelve", group="Twelve", status="complete", batch=0, complement=None,
         silent=False, created="2026-01-01T00:00:00Z", url=None, stem_from=SONG_ID, type_id=91,
         media_urls=None):
    meta = {"stem_from_id": stem_from, "stem_type_group_name": group, "stem_task": task,
            "stem_type_id": type_id, "batch_index": batch}
    if complement is not None:
        meta["complement_of_id"] = complement
    if silent:
        meta["is_loudness_under_threshold"] = True
    clip = {"id": sid, "title": sid, "status": status, "batch_index": batch,
            "audio_url": url or f"https://cdn1.suno.ai/{sid}.mp3", "created_at": created,
            "metadata": meta}
    if media_urls is not None:
        clip["media_urls"] = media_urls
    return clip


def vocal_pair(prefix="v", *, batch=0, created="2026-01-01T00:00:00Z", status="complete"):
    a = stem(f"{prefix}-extract-01", task="extract", group="Lead Vocal", batch=batch,
             complement=f"{prefix}-remove-01", created=created, status=status)
    b = stem(f"{prefix}-remove-01", task="remove", group="Lead Vocal", batch=batch,
             complement=f"{prefix}-extract-01", created=created, status=status)
    return a, b


ENC_MEDIA = [{"url": ENC_URL, "content_type": "audio/m4a-opus", "encoding": "mango",
              "delivery": "progressive"}]


def complete(*clips):
    return {"clips": [dict(c, status="complete") for c in clips]}


def audio_map(clips, data):
    return {c["audio_url"]: data for c in clips}


class FakePF:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.state = {"generate": 0, "feed": 0, "poll": 0}

    async def token(self, force=False):
        return TOKEN

    async def request(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        out = self.handler(method, path, body, self.state)
        if isinstance(out, Exception):
            raise out
        return {"ok": True, "status": 200, "json": out, "text": "", "retryAfter": None}


def std_pf(*, history=(), pages=None, original=True, check=False, generate=None, poll=None,
           rights=None, errors=None, pages_json=None, stems_json=None):
    errors = errors or {}
    if pages is None:
        pages = 1 if history else 0

    def handler(method, path, body, state):
        if path == "/api/generate/v2-web/":
            state["generate"] += 1
        if path in errors:
            return errors[path]
        if path.endswith("/stems/pages"):
            return pages_json if pages_json is not None else {"pages": pages}
        if "/stems?" in path:
            if stems_json is not None:
                return stems_json
            page = int(path.split("page=")[1])
            return {"stems": list(history) if page == 0 else [], "current_page": page}
        if path == "/api/feed/v3":
            state["feed"] += 1
            ids = body["filters"]["ids"]["clipIds"]
            if ids == [SONG_ID]:
                return {"clips": [ORIGINAL] if original else []}
            state["poll"] += 1
            if callable(poll):
                return poll(body, state["poll"])
            return poll if poll is not None else {"clips": []}
        if path == "/api/c/check":
            return check if isinstance(check, dict) else {"required": check}
        if path == "/api/generate/v2-web/":
            return generate if generate is not None else {"clips": []}
        if path == "/api/mango/rights":
            return rights if rights is not None else {}
        raise AssertionError(f"意外请求：{method} {path}")

    return FakePF(handler)


@pytest.fixture
def rig(db, bus, tmp_path, monkeypatch):
    monkeypatch.setattr(stems, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(stems, "POLL_TIMEOUT", 5.0)

    def setup(pf, *, mode="stems", fmt="mp3", url=SHARE_URL, mapping=None, dl=None):
        mapping = mapping or {}

        async def default_dl(u, dest, timeout=120, on_progress=None):
            if u not in mapping:
                raise AppError("NETWORK_ERROR", "CDN 404")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(mapping[u])
            return dest

        monkeypatch.setattr(stems, "download_cdn", dl or default_dl)
        job = db.insert_job("download", url, {"format": fmt, "mode": mode})
        settings = db.settings()
        settings["download_dir"] = str(tmp_path / "out")
        ctx = JobContext(job["id"], job, db, asyncio.Event(), tmp_path / "work",
                         settings, None, pf, bus)
        return ctx, job

    return setup


def finished(db, job):
    j = db.get_job(job["id"])
    assert j["status"] == "success", j
    return j


def logs(db, job):
    return [l["message"] for l in db.logs_after(job["id"], 0, limit=500)["lines"]]


def wrap_fixture(media: bytes, auth_token: str, content_id: str):
    """真 mango 夹具：GCM 包裹 key/iv（AAD=content_id）+ CTR 加密媒体。"""
    key, iv = os.urandom(16), os.urandom(16)
    uk = hashlib.sha256(auth_token.encode()).digest()

    def wrap(pt):
        nonce = os.urandom(12)
        c = AES.new(uk, AES.MODE_GCM, nonce=nonce)
        c.update(content_id.encode())
        ct, tag = c.encrypt_and_digest(pt)
        return base64.b64encode(nonce + ct + tag).decode()

    cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=int.from_bytes(iv, "big"))
    return cipher.encrypt(media), {"key": wrap(key), "iv": wrap(iv)}


def unwrap_spy(monkeypatch):
    calls, real = [], stems.unwrap_rights

    def spy(rights, content_id, auth_token):
        calls.append((content_id, auth_token))
        return real(rights, content_id, auth_token)

    monkeypatch.setattr(stems, "unwrap_rights", spy)
    return calls


# ---------- 复用判定：不提交生成 ----------

async def test_reuse_full_no_submit(rig, db, mp3_bytes):
    clips = [stem("stem-drums-01", group="Drums"), stem("stem-bass-01", group="Bass"),
             stem("stem-other-01", group="Other")]
    pf = std_pf(history=clips)
    ctx, job = rig(pf, mapping=audio_map(clips, mp3_bytes))
    await stems.run(ctx.job, ctx)
    j = finished(db, job)
    assert j["result"]["decision"] == "reuse-existing" and j["result"]["generated"] is False
    assert j["result"]["song_id"] == SONG_ID and j["result"]["mode"] == "stems"
    assert not any(p in ("/api/c/check", "/api/generate/v2-web/") for _, p, _ in pf.calls)
    assert not any(p == "/api/mango/rights" for _, p, _ in pf.calls)  # 明文不走授权
    out = Path(j["output_path"])
    assert out.parent == Path(ctx.settings["download_dir"])
    assert out.name.startswith(f"stems_{SONG_ID}_{job['id']}_") and out.suffix == ".zip"
    with zipfile.ZipFile(out) as z:
        assert z.namelist() == ["01-测试歌曲-Drums.mp3", "02-测试歌曲-Bass.mp3", "03-测试歌曲-Other.mp3"]
    assert [t["type"] for t in j["result"]["tracks"]] == ["Drums", "Bass", "Other"]


async def test_reuse_vocal_pair(rig, db, mp3_bytes):
    a, b = vocal_pair()
    pf = std_pf(history=[b, a])  # 乱序输入，输出必须 extract 在前
    ctx, job = rig(pf, mode="vocal", mapping=audio_map([a, b], mp3_bytes))
    await stems.run(ctx.job, ctx)
    j = finished(db, job)
    assert j["result"]["decision"] == "reuse-vocal" and j["result"]["generated"] is False
    assert [t["type"] for t in j["result"]["tracks"]] == ["人声", "伴奏"]
    assert not any(p == "/api/generate/v2-web/" for _, p, _ in pf.calls)
    with zipfile.ZipFile(j["output_path"]) as z:
        assert z.namelist() == ["01-测试歌曲-人声.mp3", "02-测试歌曲-伴奏.mp3"]
    assert Path(j["output_path"]).name.startswith(f"vocal_accompaniment_{SONG_ID}_")


async def test_silent_group_skipped(rig, db, mp3_bytes):
    clips = [stem("stem-drums-01", group="Drums"), stem("stem-quiet-01", group="Quiet", silent=True)]
    pf = std_pf(history=clips)
    ctx, job = rig(pf, mapping=audio_map(clips, mp3_bytes))
    await stems.run(ctx.job, ctx)
    j = finished(db, job)
    assert [t["type"] for t in j["result"]["tracks"]] == ["Drums"]


async def test_batch_index_picks_latest(rig, db, mp3_bytes):
    old = stem("stem-drums-old", group="Drums", batch=0)
    new = stem("stem-drums-new", group="Drums", batch=1)
    pf = std_pf(history=[new, old])
    ctx, job = rig(pf, mapping=audio_map([old, new], mp3_bytes))
    await stems.run(ctx.job, ctx)
    j = finished(db, job)
    assert [t["id"] for t in j["result"]["tracks"]] == ["stem-drums-new"]


# ---------- c/check fail-closed ----------

class PopupChrome:
    """记录 VERIFICATION_REQUIRED 时是否真的弹了 Chrome 窗口（SPEC §6）。"""
    def __init__(self):
        self.calls = []

    async def ensure(self, visible=False):
        self.calls.append(("ensure", visible))
        return self

    async def bring_to_front(self):
        self.calls.append(("front", None))


async def test_check_required_true_pops_chrome(rig, db):
    pf = std_pf(check=True)
    ctx, job = rig(pf)
    chrome = PopupChrome()
    ctx.chrome = chrome
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "VERIFICATION_REQUIRED"
    assert chrome.calls == [("ensure", True), ("front", None)]
    assert pf.state["generate"] == 0


async def test_check_required_true_blocks(rig, db):
    pf = std_pf(check=True)
    ctx, job = rig(pf)
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "VERIFICATION_REQUIRED"
    assert pf.state["generate"] == 0


@pytest.mark.parametrize("check", [{}, {"required": None}, {"required": "false"}])
async def test_check_ambiguous_blocks(rig, db, check):
    pf = std_pf(check=check)
    ctx, job = rig(pf)
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "VERIFICATION_REQUIRED"
    assert pf.state["generate"] == 0


# ---------- 生成：单次提交、错误映射 ----------

async def test_generate_full_body_and_success(rig, db, mp3_bytes):
    clips = [stem("stem-drums-01", group="Drums"), stem("stem-bass-01", group="Bass")]
    submitted = [dict(c, status="submitted") for c in clips]
    pf = std_pf(check=False, generate={"clips": submitted}, poll=complete(*clips))
    ctx, job = rig(pf, mapping=audio_map(clips, mp3_bytes))
    await stems.run(ctx.job, ctx)
    j = finished(db, job)
    assert j["result"]["decision"] == "generate-full-empty" and j["result"]["generated"] is True
    assert pf.state["generate"] == 1
    body = next(b for _, p, b in pf.calls if p == "/api/generate/v2-web/")
    assert body["task"] == "gen_stem" and body["generation_type"] == "TEXT"
    assert body["title"] == "测试歌曲" and body["continue_clip_id"] == SONG_ID
    assert body["mv"] == "chirp-v3-5-b" and body["make_instrumental"] is True
    assert body["metadata"] == {"web_client_pathname": "/create", "create_mode": "custom",
                                "disable_volume_normalization": False, "is_remix": True}
    assert body["stem_type_id"] == 91 and body["stem_task"] == "twelve"
    assert body["stem_type_group_name"] == "Twelve" and "stem_name" not in body
    assert body["token"] is None and body["token_provider"] is None
    uuid.UUID(body["transaction_uuid"])
    check_body = next(b for _, p, b in pf.calls if p == "/api/c/check")
    assert check_body == {"ctype": "generation"}


@pytest.mark.parametrize("err", [SunoAPIError("RATE_LIMITED", 429),
                                 SunoAPIError("SUNO_HTTP_ERROR", 503),
                                 SunoAPIError("NETWORK_RESULT_UNKNOWN", 0)])
async def test_generate_uncertain_never_retries(rig, db, err):
    pf = std_pf(check=False, errors={"/api/generate/v2-web/": err})
    ctx, job = rig(pf)
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "GENERATION_UNCERTAIN"
    assert pf.state["generate"] == 1  # 消耗性请求绝不重试


async def test_generate_401_not_logged_in(rig, db):
    pf = std_pf(check=False, errors={"/api/generate/v2-web/": SunoAPIError("NOT_LOGGED_IN", 401)})
    ctx, job = rig(pf)
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "NOT_LOGGED_IN" and pf.state["generate"] == 1


async def test_generate_402_insufficient_credits(rig, db):
    pf = std_pf(check=False, errors={"/api/generate/v2-web/": SunoAPIError("SUNO_HTTP_ERROR", 402)})
    ctx, job = rig(pf)
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "INSUFFICIENT_CREDITS" and pf.state["generate"] == 1


async def test_generate_batch_size_mismatch(rig, db):
    c = stem("stem-drums-01", group="Drums")
    pf = std_pf(check=False, generate={"clips": [c], "batch_size": 12})
    ctx, job = rig(pf)
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "GENERATION_UNCERTAIN" and pf.state["generate"] == 1


@pytest.mark.parametrize("gen", [{"clips": []}, {"clips": [{"id": "bad", "metadata": {}}]}, None])
async def test_generate_invalid_clips_uncertain(rig, db, gen):
    pf = std_pf(check=False, generate=gen)
    ctx, job = rig(pf)
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "GENERATION_UNCERTAIN" and pf.state["generate"] == 1


# ---------- 历史类型不明 / 人声后补全 ----------

async def test_block_full_ambiguous(rig, db):
    pf = std_pf(history=[stem("stem-mystery-1", task="mystery")])
    ctx, job = rig(pf)
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "GENERATION_UNCERTAIN"
    assert "无法确认类型" in e.value.message
    assert not any(p in ("/api/c/check", "/api/generate/v2-web/") for _, p, _ in pf.calls)


async def test_generate_full_after_vocal(rig, db, mp3_bytes):
    a, b = vocal_pair()
    full = [stem("stem-drums-01", group="Drums")]
    pf = std_pf(history=[a, b], check=False,
                generate={"clips": [dict(full[0], status="submitted")]}, poll=complete(*full))
    ctx, job = rig(pf, mapping=audio_map(full, mp3_bytes))
    await stems.run(ctx.job, ctx)
    j = finished(db, job)
    assert j["result"]["decision"] == "generate-full-after-vocal" and pf.state["generate"] == 1


# ---------- 人声互补对（issue 11） ----------

async def test_vocal_missing_pair_after_generate_fails(rig, db):
    lone = stem("stem-extract-1", task="extract", group="Lead Vocal")
    pf = std_pf(check=False, generate={"clips": [lone]}, poll=complete(lone))
    ctx, job = rig(pf, mode="vocal")
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "GENERATION_FAILED"
    assert "互补对未形成" in e.value.message
    assert pf.state["generate"] == 1  # 缺对绝不追加消耗请求


async def test_vocal_empty_history_generates_pair(rig, db, mp3_bytes):
    a, b = vocal_pair(prefix="gen")
    pf = std_pf(check=False,
                generate={"clips": [dict(c, status="submitted") for c in (a, b)]},
                poll=complete(a, b))
    ctx, job = rig(pf, mode="vocal", mapping=audio_map([a, b], mp3_bytes))
    await stems.run(ctx.job, ctx)
    j = finished(db, job)
    assert j["result"]["decision"] == "generate-vocal-empty"
    assert [t["type"] for t in j["result"]["tracks"]] == ["人声", "伴奏"]
    body = next(b for _, p, b in pf.calls if p == "/api/generate/v2-web/")
    assert body["stem_task"] == "extract" and body["stem_name"] == "Lead Vocal"
    assert body["stem_type_group_name"] == "Lead Vocal" and body["stem_type_id"] == 91


async def test_vocal_existing_lone_or_unknown_never_generates(rig):
    for clip, expected in (
        (stem("stem-extract-1", task="extract", group="Lead Vocal"), "GENERATION_FAILED"),
        ({"id": "unknown"}, "GENERATION_UNCERTAIN"),
    ):
        pf = std_pf(history=[clip])
        ctx, _ = rig(pf, mode="vocal")
        with pytest.raises(AppError) as exc:
            await stems.run(ctx.job, ctx)
        assert exc.value.code == expected
        assert pf.state["generate"] == 0


def test_select_vocal_requires_mutual():
    a = stem("stem-extract-1", task="extract", group="Lead Vocal", complement="stem-remove-1")
    b = stem("stem-remove-1", task="remove", group="Lead Vocal")  # 单向 complement → 不配对
    assert stems.select_vocal([a, b]) == []


def test_select_vocal_picks_latest_pair():
    old_a, old_b = vocal_pair(prefix="old", created="2026-01-01T00:00:00Z")
    new_a, new_b = vocal_pair(prefix="new", created="2026-02-01T00:00:00Z")
    picked = stems.select_vocal([old_a, old_b, new_a, new_b])
    assert [c["id"] for c in picked] == ["new-extract-01", "new-remove-01"]


# ---------- 轮询 ----------

async def test_poll_updates_until_complete(rig, db, mp3_bytes):
    c = stem("stem-drums-01", group="Drums")
    submitted = dict(c, status="submitted")

    def poll(body, n):
        return complete(c) if n >= 2 else {"clips": [dict(c, status="streaming")]}

    pf = std_pf(check=False, generate={"clips": [submitted]}, poll=poll)
    ctx, job = rig(pf, mapping=audio_map([c], mp3_bytes))
    await stems.run(ctx.job, ctx)
    finished(db, job)
    assert pf.state["poll"] >= 2


async def test_poll_timeout(rig, db, monkeypatch):
    monkeypatch.setattr(stems, "POLL_TIMEOUT", 0.05)
    c = stem("stem-drums-01", group="Drums", status="streaming")
    pf = std_pf(history=[c], poll={"clips": [c]})
    ctx, job = rig(pf)
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "TIMEOUT"
    assert pf.state["generate"] == 0


async def test_detected_failed_status(rig, db):
    pf = std_pf(history=[stem("stem-drums-01", group="Drums", status="error")])
    ctx, job = rig(pf)
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "GENERATION_FAILED" and pf.state["generate"] == 0


# ---------- ZIP 内容（真 ffmpeg） ----------

async def test_zip_wav_contents_playable(rig, db, mp3_bytes, tmp_path):
    clips = [stem("stem-drums-01", group="Drums"), stem("stem-bass-01", group="Bass"),
             stem("stem-other-01", group="Other")]
    pf = std_pf(history=clips)
    ctx, job = rig(pf, fmt="wav", mapping=audio_map(clips, mp3_bytes))
    await stems.run(ctx.job, ctx)
    j = finished(db, job)
    names = ["01-测试歌曲-Drums.wav", "02-测试歌曲-Bass.wav", "03-测试歌曲-Other.wav"]
    with zipfile.ZipFile(j["output_path"]) as z:
        assert z.namelist() == names
        for i, name in enumerate(names):
            p = tmp_path / f"check_{i}.wav"
            p.write_bytes(z.read(name))
            info = await ffmedia.probe(p)
            assert info["codec"] == "pcm_s16le" and abs(info["duration"] - 2.0) < 0.3
    assert [t["entry"] for t in j["result"]["tracks"]] == names


# ---------- 解密：匿名 glt 优先，登录态兜底 ----------

async def test_encrypted_anonymous_glt(rig, db, mp3_bytes, monkeypatch):
    s = stem("stem-enc-001", media_urls=ENC_MEDIA)
    glt = "guest-license-SECRET"
    enc, rights = wrap_fixture(mp3_bytes, glt, s["id"])
    rights = {**rights, "glt": glt}
    anon_calls = []

    async def anon(ctx, sid):
        anon_calls.append(sid)
        return rights

    monkeypatch.setattr(stems, "_anonymous_rights", anon)
    unwrap_calls = unwrap_spy(monkeypatch)
    pf = std_pf(history=[s])
    ctx, job = rig(pf, mapping={ENC_URL: enc})
    await stems.run(ctx.job, ctx)
    j = finished(db, job)
    assert anon_calls == [s["id"]]
    assert unwrap_calls == [(s["id"], glt)]  # 匿名用 glt，不是 Clerk token
    assert not any(p == "/api/mango/rights" for _, p, _ in pf.calls)
    assert all(glt not in m and TOKEN not in m for m in logs(db, job))
    with zipfile.ZipFile(j["output_path"]) as z:
        assert sniff_container(z.read(z.namelist()[0])) == "mp3"


async def test_encrypted_logged_in_fallback(rig, db, mp3_bytes, monkeypatch):
    s = stem("stem-enc-002", media_urls=ENC_MEDIA)
    enc, rights = wrap_fixture(mp3_bytes, TOKEN, s["id"])

    async def anon(ctx, sid):
        raise AppError("MANGO_DECRYPT_FAILED", "匿名授权 HTTP 401")

    monkeypatch.setattr(stems, "_anonymous_rights", anon)
    unwrap_calls = unwrap_spy(monkeypatch)
    pf = std_pf(history=[s], rights=rights)
    ctx, job = rig(pf, mapping={ENC_URL: enc})
    await stems.run(ctx.job, ctx)
    j = finished(db, job)
    assert unwrap_calls == [(s["id"], TOKEN)]  # 兜底用登录态 token
    assert [(m, p, b) for m, p, b in pf.calls if p == "/api/mango/rights"] == [
        ("POST", "/api/mango/rights", {"content_params": {"content_id": s["id"], "content_type": "clip"}})]
    assert all(TOKEN not in m for m in logs(db, job))
    assert TOKEN not in str(j["result"])
    with zipfile.ZipFile(j["output_path"]) as z:
        assert sniff_container(z.read(z.namelist()[0])) == "mp3"


# ---------- 输入/协议校验 ----------

async def test_cancel_before_start(rig, db):
    pf = std_pf()
    ctx, job = rig(pf)
    ctx.cancel.set()
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "CANCELLED" and pf.calls == []


@pytest.mark.parametrize("opts", [{"fmt": "flac"}, {"mode": "karaoke"}])
async def test_unsupported_options(rig, db, opts):
    pf = std_pf()
    ctx, job = rig(pf, **opts)
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "UNSUPPORTED_FORMAT" and pf.calls == []


async def test_short_link_html(rig, db, mp3_bytes, monkeypatch):
    html = f'<link rel="canonical" href="{SHARE_URL}">'
    monkeypatch.setattr(stems, "fetch_share_html",
                        lambda u, timeout=20: asyncio.sleep(0, result=html))
    c = stem("stem-drums-01", group="Drums")
    pf = std_pf(history=[c])
    ctx, job = rig(pf, url="https://suno.com/s/short123", mapping=audio_map([c], mp3_bytes))
    await stems.run(ctx.job, ctx)
    j = finished(db, job)
    assert j["suno_song_id"] == SONG_ID


async def test_original_missing_not_found(rig, db):
    pf = std_pf(original=False)
    ctx, job = rig(pf)
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "NOT_FOUND"


@pytest.mark.parametrize("pages_json", [{"pages": "x"}, {"pages": -1}, {"pages": 1001},
                                        {"pages": True}, {}])
async def test_invalid_pages(rig, db, pages_json):
    pf = std_pf(pages_json=pages_json)
    ctx, job = rig(pf)
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "SUNO_HTTP_ERROR" and "页数无效" in e.value.message


@pytest.mark.parametrize("stems_json", [{"stems": [], "current_page": 5}, {"stems": "no"}])
async def test_invalid_page_content(rig, db, stems_json):
    pf = std_pf(history=[stem("stem-drums-01")], stems_json=stems_json)
    ctx, job = rig(pf)
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "SUNO_HTTP_ERROR" and "分页内容无效" in e.value.message


@pytest.mark.parametrize("payload,frag", [
    ({"clips": None}, "列表无效"),
    ({"clips": [ORIGINAL], "has_more": True, "next_cursor": None}, "分页无效"),
    ({"clips": [ORIGINAL], "has_more": True, "next_cursor": "same"}, "分页无效"),
])
async def test_feed_invalid_payloads(rig, db, payload, frag):
    def handler(m, p, b, s):
        assert p == "/api/feed/v3", p
        return payload

    pf = FakePF(handler)
    ctx, job = rig(pf)
    with pytest.raises(AppError) as e:
        await stems.run(ctx.job, ctx)
    assert e.value.code == "SUNO_HTTP_ERROR" and frag in e.value.message


def test_safe_name():
    assert stems._safe_name('Drums/\\:*?"<>|', "分轨", 80) == "Drums"
    assert stems._safe_name("CON", "分轨", 80) == "_CON"
    assert stems._safe_name("  .. ", "分轨", 80) == "分轨"
    assert stems._safe_name(None, "SUNO歌曲", 100) == "SUNO歌曲"
