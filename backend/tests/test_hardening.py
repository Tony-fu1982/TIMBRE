"""评审加固回归：登录复检抛错不卡 verifying；ffprobe 超时必杀子进程。"""
from __future__ import annotations

import asyncio

import pytest

from app.errors import AppError


async def test_verify_exception_not_stuck(db, bus):
    from app.login import LoginFlow

    class FakeSession:
        async def ensure(self, visible=False):
            raise AssertionError("loggedIn 分支不应走兜底复检")

        async def minimize(self):
            pass

    class FakePage:
        url = "https://suno.com/login"

        async def evaluate(self, expr, arg=None):
            return {"ready": True, "loggedIn": True, "account": "a@b.c"}

    flow = LoginFlow(FakeSession(), db, bus)
    flow.state = "waiting_manual"

    async def boom(page):
        raise RuntimeError("clerk exploded")

    flow._verify = boom
    await flow._poll(FakePage(), interval=0.01, deadline_s=2)
    assert flow.state == "not_logged_in"
    assert db.session()["state"] == "not_logged_in"


async def test_probe_timeout_kills(monkeypatch, tmp_path):
    from app import ffmedia

    class HangProc:
        returncode = None

        def __init__(self):
            self.killed = False

        async def communicate(self):
            if self.killed:
                return b"", b""
            await asyncio.sleep(60)

        def kill(self):
            self.killed = True

    proc = HangProc()

    async def fake_exec(*a, **k):
        return proc

    real_wait_for = asyncio.wait_for

    async def fast_wait_for(coro, timeout):
        return await real_wait_for(coro, 0.05)

    monkeypatch.setattr(ffmedia.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ffmedia.asyncio, "wait_for", fast_wait_for)
    with pytest.raises(AppError) as ei:
        await ffmedia.probe(tmp_path / "x.wav")
    assert ei.value.code == "TIMEOUT"
    assert proc.killed


# ---------- SPEC §4.1：401 分级降级 + 队列接线 ----------

def _login_flow(db, bus, probe_result):
    from app.login import LoginFlow

    class FakePage:
        url = "https://suno.com/"

        async def evaluate(self, expr, arg=None):
            return probe_result

    class FakeSession:
        async def ensure(self, visible=False):
            return FakePage()

        async def check_page(self, page):
            pass

        async def minimize(self):
            pass

    flow = LoginFlow(FakeSession(), db, bus)
    flow.state = "logged_in"
    flow.account = "a@b.c"
    return flow


async def test_mark_expired_recheck_downgrades_on_401(db, bus):
    """其他 Suno API 401 → mark_expired 只排复检；探针 401（ready+not loggedIn）才降级。"""
    flow = _login_flow(db, bus, {"ready": True, "loggedIn": False, "account": ""})
    flow.mark_expired()
    assert flow._recheck is not None
    await flow._recheck
    assert flow.state == "not_logged_in"
    assert db.session()["state"] == "not_logged_in"


async def test_recheck_500_keeps_logged_in(db, bus):
    """探针 500（ready:false）不是权威答复，不降级已确认的登录（SPEC §4.1）。"""
    flow = _login_flow(db, bus, {"ready": False, "loggedIn": False, "error": "http 500"})
    flow._save()  # 先落库基线
    out = await flow.recheck()
    assert out["state"] == "logged_in"
    assert db.session()["state"] == "logged_in"


async def test_queue_not_logged_in_triggers_recheck(db, bus):
    """任务失败 NOT_LOGGED_IN → 队列调用 mark_expired（复检确认后才降级）。"""
    from app.jobs import JobQueue

    class FakeLogin:
        def __init__(self):
            self.calls = 0

        def mark_expired(self):
            self.calls += 1

    async def boom(job, ctx):
        raise AppError("NOT_LOGGED_IN")

    import app.jobs as jobs_mod
    orig = jobs_mod._handler_for
    jobs_mod._handler_for = lambda job: boom
    try:
        login = FakeLogin()
        q = JobQueue(db, bus, None, None, login=login)
        j = db.insert_job("download", "https://suno.com/song/x", {})
        db.update_job(j["id"], status="running")
        await q._run_one(db.get_job(j["id"]))
        assert login.calls == 1
        assert db.get_job(j["id"])["error_code"] == "NOT_LOGGED_IN"
    finally:
        jobs_mod._handler_for = orig
