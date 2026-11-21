"""队列接缝（SPEC §1）：忙判断、ID 顺序、取消语义、退出/重启恢复。"""
from __future__ import annotations

import asyncio

import pytest

from app import jobs as jobs_mod
from app.errors import AppError
from app.jobs import JobQueue


@pytest.fixture
def queue(db, bus, monkeypatch):
    monkeypatch.setattr(jobs_mod.config, "TMP_DIR", db.path.parent / "tmp")
    q = JobQueue(db, bus, chrome=None, pf=None)
    return q


def test_download_enqueues_while_busy(queue, db):
    a = queue.submit("download", "https://suno.com/song/x", {"mode": "song"})
    db.update_job(a["id"], status="running")
    b = queue.submit("download", "https://suno.com/song/y", {"mode": "song"})
    assert b["status"] == "queued"


def test_other_kinds_busy(queue, db):
    a = queue.submit("download", "u", {})
    db.update_job(a["id"], status="running")
    with pytest.raises(AppError) as e:
        queue.submit("upload", "f", {})
    assert e.value.code == "BUSY"
    with pytest.raises(AppError):
        queue.submit("dna", "f", {})


def test_cancel_queued_no_request(queue, db):
    a = queue.submit("download", "u", {})
    queue.cancel(a["id"])
    j = db.get_job(a["id"])
    assert j["status"] == "cancelled"
    assert "未发出任何请求" in j["error_msg"]


def test_repeat_cancel_terminal_is_success(queue, db):
    a = queue.submit("download", "u", {})
    db.update_job(a["id"], status="success")
    queue.cancel(a["id"])
    assert db.get_job(a["id"])["status"] == "success"


async def test_running_cancel_waits_handler_exit(queue, db, monkeypatch):
    started = asyncio.Event()

    async def handler(job, ctx):
        started.set()
        while not ctx.cancel.is_set():
            await asyncio.sleep(0.01)
        ctx.check_cancel()

    monkeypatch.setattr(jobs_mod, "_handler_for", lambda job: handler)
    queue.start()
    a = queue.submit("download", "u", {})
    await asyncio.wait_for(started.wait(), 2)
    queue.cancel(a["id"])
    for _ in range(200):
        if db.get_job(a["id"])["status"] == "cancelled":
            break
        await asyncio.sleep(0.01)
    assert db.get_job(a["id"])["status"] == "cancelled"
    await queue.stop()


async def test_id_order_single_execution(queue, db, monkeypatch):
    order = []

    async def handler(job, ctx):
        order.append(job["id"])
        await asyncio.sleep(0.02)
        ctx.finish()

    monkeypatch.setattr(jobs_mod, "_handler_for", lambda job: handler)
    ids = [queue.submit("download", f"u{i}", {})["id"] for i in range(3)]
    queue.start()
    for _ in range(300):
        if all(db.get_job(i)["status"] == "success" for i in ids):
            break
        await asyncio.sleep(0.01)
    await queue.stop()
    assert order == ids


async def test_stop_cancels_pending(queue, db):
    a = queue.submit("download", "u", {})
    queue.start()
    await queue.stop()
    assert db.get_job(a["id"])["status"] in ("cancelled", "success")


def test_recover_leftovers(db):
    a = db.insert_job("download", "u", {})
    b = db.insert_job("upload", "f", {})
    db.update_job(b["id"], status="running", render_state="submitting", render_id="r1")
    c = db.insert_job("download", "v", {})
    db.update_job(c["id"], status="success", suno_song_id="s1")
    db.recover()
    assert db.get_job(a["id"])["status"] == "cancelled"
    jb = db.get_job(b["id"])
    assert jb["status"] == "failed" and jb["render_state"] == "unknown" and jb["render_id"] == "r1"
    jc = db.get_job(c["id"])
    assert jc["status"] == "success" and jc["suno_song_id"] == "s1"  # 历史与远端 ID 保留


@pytest.mark.parametrize("debug", ["0", "1"])
async def test_internal_error_text_gated(queue, db, monkeypatch, debug):
    """未预期异常原文（可能含 token）只在调试日志开启时落库（CONTRACTS 日志红线）。"""
    from app.errors import DEFAULT_MESSAGES

    async def handler(job, ctx):
        raise RuntimeError("secret-token-xyz")

    monkeypatch.setattr(jobs_mod, "_handler_for", lambda job: handler)
    db.set_setting("debug_logs", debug)
    queue.start()
    a = queue.submit("download", "u", {})
    for _ in range(300):
        if db.get_job(a["id"])["status"] == "failed":
            break
        await asyncio.sleep(0.01)
    await queue.stop()
    j = db.get_job(a["id"])
    assert j["error_code"] == "INTERNAL_ERROR"
    assert j["error_msg"] == DEFAULT_MESSAGES["INTERNAL_ERROR"]
    logs = " ".join(l["message"] for l in db.logs_after(a["id"], 0, 100)["lines"])
    assert "secret-token-xyz" not in logs
    assert ("RuntimeError" in logs) == (debug == "1")
