"""API 接缝（SPEC §3）：错误形状、登录门禁、设置校验、Key 不回显。不触网。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app


@pytest.fixture
def client(isolate_env, monkeypatch):
    from app.db import Database
    monkeypatch.setattr("app.main.Database", lambda: Database(isolate_env / "data" / "grow.db"))
    with TestClient(app) as c:
        yield c


def test_error_shape_not_found(client):
    r = client.get("/api/jobs/99999")
    assert r.status_code == 404
    d = r.json()["detail"]
    assert d["code"] == "NOT_FOUND" and d["message"]


def test_download_requires_login(client):
    r = client.post("/api/jobs/download", json={"url": "https://suno.com/song/x"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "NOT_LOGGED_IN"


def test_settings_roundtrip_and_invalid_dir(client, tmp_path):
    r = client.get("/api/settings")
    assert r.status_code == 200 and r.json()["download_dir"]
    good = str(tmp_path / "downloads")
    assert client.put("/api/settings", json={"download_dir": good}).json() == {"ok": True}
    assert client.get("/api/settings").json()["download_dir"] == good
    blocked = tmp_path / "afile"
    blocked.write_text("x")
    r = client.put("/api/settings", json={"download_dir": str(blocked)})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "DIR_INVALID"
    assert client.get("/api/settings").json()["download_dir"] == good  # 保留上次有效值


def test_settings_empty_dir_rejected(client, tmp_path):
    good = str(tmp_path / "downloads")
    assert client.put("/api/settings", json={"download_dir": good}).status_code == 200
    for bad in ("", "   "):
        r = client.put("/api/settings", json={"download_dir": bad})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "DIR_INVALID"
    assert client.get("/api/settings").json()["download_dir"] == good


def test_open_folder_registered_dir(client, isolate_env, tmp_path, monkeypatch):
    """SPEC §3：只打开任务登记输出目录内的文件；改下载目录后旧任务仍可打开；缺失/符号链接逃逸拒绝。"""
    from app.db import Database
    import app.routers.jobs as rj
    db = Database(isolate_env / "data" / "grow.db")
    try:
        old_dir = tmp_path / "OldDownloads"
        old_dir.mkdir()
        out = old_dir / "song.mp3"
        out.write_bytes(b"x")
        j = db.insert_job("download", "u", {})
        db.update_job(j["id"], status="success", output_path=str(out))
        opened = []
        monkeypatch.setattr(rj, "_reveal_in_explorer", lambda p: opened.append(str(p)))
        r = client.post(f"/api/jobs/{j['id']}/open-folder")
        assert r.json()["detail"]["code"] == "OUTPUT_UNAVAILABLE"
        assert opened == []
        app.state.queue.output_dirs[j["id"]] = old_dir.resolve()
        # 下载目录改到别处：旧任务仍应打开原登记目录（不以当前设置为根）
        db.set_setting("download_dir", str(tmp_path / "Elsewhere"))
        assert client.post(f"/api/jobs/{j['id']}/open-folder").json() == {"ok": True}
        assert opened and str(out) in opened[0]
        # 产物缺失
        out.unlink()
        r = client.post(f"/api/jobs/{j['id']}/open-folder")
        assert r.json()["detail"]["code"] == "OUTPUT_UNAVAILABLE"
        # 符号链接逃逸：登记路径在目录内，resolve 后指向目录外 → 拒绝
        outside = tmp_path / "outside.mp3"
        outside.write_bytes(b"x")
        link = old_dir / "link.mp3"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("Windows 符号链接权限不可用")
        db.update_job(j["id"], output_path=str(link))
        r = client.post(f"/api/jobs/{j['id']}/open-folder")
        assert r.json()["detail"]["code"] == "OUTPUT_UNAVAILABLE"
    finally:
        db.close()


def test_job_label_roundtrip(client, isolate_env):
    """自定义显示名：备注写入 options.label；空串移除恢复默认；超长截断 60。"""
    from app.db import Database
    db = Database(isolate_env / "data" / "grow.db")
    try:
        j = db.insert_job("dna", "x.mp3", {"mode": "full", "source_name": "荒.mp3"})
        r = client.post(f"/api/jobs/{j['id']}/label", json={"label": "  荒 · 莫小檀  "})
        assert r.status_code == 200
        assert r.json()["options"]["label"] == "荒 · 莫小檀"
        assert r.json()["options"]["source_name"] == "荒.mp3"  # 原文件名保留作兜底
        r = client.post(f"/api/jobs/{j['id']}/label", json={"label": "   "})
        assert "label" not in r.json()["options"]
        r = client.post(f"/api/jobs/{j['id']}/label", json={"label": "长" * 80})
        assert len(r.json()["options"]["label"]) == 60
        assert client.post("/api/jobs/99999/label", json={"label": "x"}).status_code == 404
    finally:
        db.close()


def test_output_dirs_rebuilt_on_queue_init(client, tmp_path):
    """重启场景：新队列从 DB 重建登记簿，旧任务的产物目录仍在册。"""
    from app.jobs import JobQueue
    d = tmp_path / "dl"
    d.mkdir()
    f = d / "a.mp3"
    f.write_bytes(b"x")
    j = app.state.db.insert_job("download", "https://suno.com/s/x", {})
    app.state.db.update_job(j["id"], status="success", output_path=str(f))
    q2 = JobQueue(app.state.db, app.state.bus, None, None)  # 模拟重启后的新队列，不 start worker
    assert q2.output_dirs[j["id"]] == d.resolve()


def test_dna_submit_label(client, tone_wav, monkeypatch):
    """提交即带自定义名：trim+截断 60；空白不写 label 键。fake submit 不入队，worker 不触网。"""
    assert client.put("/api/ai/config", json={"provider": "qwen", "model": "m", "api_key": "k"}).status_code == 200
    captured = []
    monkeypatch.setattr(app.state.queue, "submit",
                        lambda kind, inp, opts: captured.append(opts) or app.state.db.insert_job(kind, inp, opts))
    wav = tone_wav()
    with open(wav, "rb") as f:
        r = client.post("/api/jobs/dna", files={"file": ("荒.mp3", f, "audio/wav")},
                        data={"mode": "full", "label": "  " + "名" * 80 + "  "})
    assert r.status_code == 201
    assert captured[0]["label"] == "名" * 60 and captured[0]["source_name"] == "荒.mp3"
    with open(wav, "rb") as f:
        r = client.post("/api/jobs/dna", files={"file": ("荒.mp3", f, "audio/wav")}, data={"label": "   "})
    assert r.status_code == 201 and "label" not in captured[1]


def test_reveal_in_explorer_com_and_fallback(monkeypatch, tmp_path):
    """Shell API 成功路径 COM 配对、ILFree 走 windll；Shell 失败兜底开文件夹；双失败 OPEN_FOLDER_FAILED。"""
    import ctypes
    from types import SimpleNamespace
    import app.routers.jobs as rj
    from app.errors import AppError
    p = tmp_path / "out" / "song.mp3"
    p.parent.mkdir()
    p.write_bytes(b"x")
    calls = []
    ole = SimpleNamespace(CoInitializeEx=lambda *a: calls.append("init"),
                          CoUninitialize=lambda: calls.append("uninit"))
    shell = SimpleNamespace(SHParseDisplayName=lambda *a: calls.append("parse"),
                            SHOpenFolderAndSelectItems=lambda *a: calls.append("select"))
    monkeypatch.setattr(ctypes, "oledll", SimpleNamespace(ole32=ole, shell32=shell))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(
        shell32=SimpleNamespace(ILFree=lambda pidl: calls.append("ilfree"))))
    rj._reveal_in_explorer(p)
    assert calls == ["init", "parse", "select", "ilfree", "uninit"]
    # COM 初始化失败（如线程已是 MTA）：仍试 Shell API，不 uninit
    calls.clear()
    def boom(*a):
        raise OSError("fail")
    ole.CoInitializeEx = boom
    rj._reveal_in_explorer(p)
    assert calls == ["parse", "select", "ilfree"]
    # Shell 失败 → 兜底开所在文件夹（仍 uninit）
    calls.clear()
    ole.CoInitializeEx = lambda *a: calls.append("init")
    shell.SHParseDisplayName = boom
    opened = []
    monkeypatch.setattr(rj.os, "startfile", lambda s: opened.append(s))
    rj._reveal_in_explorer(p)
    assert opened == [str(p.parent)] and calls == ["init", "uninit"]
    # 兜底也失败 → OPEN_FOLDER_FAILED
    monkeypatch.setattr(rj.os, "startfile", boom)
    with pytest.raises(AppError) as ei:
        rj._reveal_in_explorer(p)
    assert ei.value.code == "OPEN_FOLDER_FAILED"


def test_ai_config_key_never_echoed(client):
    r = client.put("/api/ai/config", json={
        "provider": "deepseek", "model": "deepseek4.1-flash-1017", "api_key": "sk-secret-123"})
    assert r.status_code == 200 and r.json() == {"ok": True}  # SPEC §3：PUT 只回 {ok:true}
    assert "sk-secret-123" not in r.text
    g = client.get("/api/ai/config")
    assert g.json()["key_set"] is True
    assert g.json()["model"] == "deepseek4.1-flash-1017"
    assert "sk-secret-123" not in g.text and "api_key" not in g.json()
    # Key 只落 .env
    assert "sk-secret-123" in config.ENV_PATH.read_text(encoding="utf-8")
    qwen = client.get("/api/ai/config?provider=qwen").json()
    assert qwen["model"] == "qwen3.8-omni-flash" and qwen["key_set"] is False
    assert client.get("/api/ai/config").json()["provider"] == "deepseek"


def test_ai_config_invalid_provider(client):
    r = client.put("/api/ai/config", json={"provider": "gemini", "model": "x"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "INVALID_PROVIDER"


def test_generate_input_validation(client):
    # 无 Key → NO_AI_KEY 先于输入校验
    r = client.post("/api/jobs/generate", json={"mode": "full"})
    assert r.json()["detail"]["code"] == "NO_AI_KEY"
    client.put("/api/ai/config", json={"provider": "deepseek", "model": "m", "api_key": "k"})
    r = client.post("/api/jobs/generate", json={"mode": "full"})
    assert r.json()["detail"]["code"] == "INVALID_GENERATION_INPUT"
    r = client.post("/api/jobs/generate", json={"mode": "full", "dna_result_id": 4242})
    assert r.json()["detail"]["code"] == "INVALID_GENERATION_INPUT"


def test_resume_export_unavailable(client):
    r = client.post("/api/jobs/1/resume-export")
    assert r.status_code in (400, 404)


def test_jobs_list_shape(client):
    r = client.get("/api/jobs")
    assert r.status_code == 200 and r.json() == {"jobs": []}


def test_submit_returns_201_job_id(client, isolate_env):
    """SPEC §3：提交一律 201 {job_id}；完整 Job 走 GET。"""
    src = isolate_env / "data" / "tone.mp3"
    src.write_bytes(b"not really audio")
    with open(src, "rb") as f:
        r = client.post("/api/jobs/convert", files={"file": ("tone.mp3", f, "audio/mpeg")},
                        data={"target": "wav"})
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"job_id"} and isinstance(body["job_id"], int)
    j = client.get(f"/api/jobs/{body['job_id']}").json()
    assert j["kind"] == "convert" and "log_seq" in j


@pytest.mark.parametrize(('kind', 'state', 'code', 'expected'), [
    ('download', '', 'GENERATION_UNCERTAIN', 'GENERATION_UNCERTAIN'),
    ('upload', 'pending', 'RENDER_TIMEOUT', 'EXPORT_RESUME_UNAVAILABLE'),
    ('upload', 'unknown', 'INTERNAL_ERROR', 'NETWORK_RESULT_UNKNOWN'),
])
def test_retry_blocks_unconfirmed_remote_work(client, kind, state, code, expected):
    db = app.state.db
    j = db.insert_job(kind, 'fixture', {})
    db.update_job(j['id'], status='failed', error_code=code, render_state=state)
    response = client.post(f"/api/jobs/{j['id']}/retry")
    assert response.json()['detail']['code'] == expected
    assert [job['id'] for job in db.list_jobs()] == [j['id']]


def test_cancel_returns_ok(client, isolate_env):
    src = isolate_env / "data" / "tone.mp3"
    src.write_bytes(b"x")
    with open(src, "rb") as f:
        jid = client.post("/api/jobs/convert", files={"file": ("tone.mp3", f, "audio/mpeg")}).json()["job_id"]
    assert client.post(f"/api/jobs/{jid}/cancel").json() == {"ok": True}
    assert client.post(f"/api/jobs/{jid}/cancel").json() == {"ok": True}  # 终态重复取消=成功
    r = client.post("/api/jobs/99999/cancel")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "NOT_FOUND"


def test_resume_export_cancelled_accepted(client, isolate_env, monkeypatch):
    """SPEC §3：已结束（含 cancelled）+ render pending 可继续查询；201 {job_id}。"""
    async def fake_resume(job, ctx):
        ctx.finish(result={"song_id": "x"})
    monkeypatch.setattr("app.suno.exporter.resume_run", fake_resume)
    from app.db import Database
    db = Database(isolate_env / "data" / "grow.db")
    try:
        j = db.insert_job("upload", "in.wav", {})
        db.update_job(j["id"], status="cancelled", render_id="rid-1", render_state="pending")
    finally:
        db.close()
    client.app.state.login.state = "logged_in"
    r = client.post(f"/api/jobs/{j['id']}/resume-export")
    assert r.status_code == 201 and set(r.json()) == {"job_id"}


def test_validation_error_unified_shape(client):
    """SPEC §3：参数校验失败也走 {"detail":{"code","message"}}，不泄露请求体。"""
    r = client.post("/api/jobs/download", json={"url": "https://suno.com/song/x", "format": "flac"})
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "INVALID_INPUT" and d["message"]
    r = client.post("/api/jobs/generate")  # 缺 body
    assert r.status_code == 400 and r.json()["detail"]["code"] == "INVALID_INPUT"


def test_retry_only_terminal_and_file_exists(client, isolate_env):
    from app.db import Database
    db = Database(isolate_env / "data" / "grow.db")
    try:
        # 排队中不可重试
        j = db.insert_job("convert", "x.ncm", {"target": "mp3"})
        r = client.post(f"/api/jobs/{j['id']}/retry")
        assert r.json()["detail"]["code"] == "BUSY"
        # 失败但原文件不存在 → BAD_AUDIO 提示重选
        db.update_job(j["id"], status="failed", error_code="DECRYPT_FAILED", error_msg="x")
        r = client.post(f"/api/jobs/{j['id']}/retry")
        assert r.json()["detail"]["code"] == "BAD_AUDIO"
        # 失败且文件在 → 重新入队（同 kind/options）
        src = isolate_env / "data" / "song.ncm"
        src.write_bytes(b"fake")
        j2 = db.insert_job("convert", str(src), {"target": "wav"})
        db.update_job(j2["id"], status="failed", error_code="DECRYPT_FAILED", error_msg="x")
        r = client.post(f"/api/jobs/{j2['id']}/retry")
        assert r.status_code == 200
        nj = r.json()
        assert nj["kind"] == "convert" and nj["status"] == "queued" and nj["options"]["target"] == "wav"
        assert nj["id"] != j2["id"]
    finally:
        db.close()
