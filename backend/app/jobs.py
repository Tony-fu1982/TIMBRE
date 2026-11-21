"""任务队列（SPEC §1）：后端唯一真相；单循环按 id 顺序；download 忙时入队、其他 BUSY；取消/恢复语义。"""
from __future__ import annotations

import asyncio
import shutil
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .db import Database, TERMINAL
from .errors import AppError, DEFAULT_MESSAGES
from .events import EventBus


@dataclass
class JobContext:
    job_id: int
    job: dict
    db: Database
    cancel: asyncio.Event
    workdir: Path
    settings: dict
    chrome: object  # ChromeSession（惰性 ensure）
    pf: object  # PageFetch
    bus: EventBus
    _finished: bool = field(default=False, repr=False)

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise AppError("CANCELLED")

    async def interruptible(self, operation):
        task = asyncio.create_task(operation)
        watch = asyncio.create_task(self.cancel.wait())
        try:
            await asyncio.wait((task, watch), return_when=asyncio.FIRST_COMPLETED)
            self.check_cancel()
            return await task
        finally:
            for item in (task, watch):
                if not item.done():
                    item.cancel()
            await asyncio.gather(task, watch, return_exceptions=True)

    def _publish(self) -> None:
        self.bus.publish("job", self.db.snapshot(self.job_id))

    def log(self, msg: str) -> None:
        self.db.append_log(self.job_id, msg)
        self._publish()

    def debug(self, msg: str) -> None:
        if self.settings.get("debug_logs") == "1":
            self.log(msg)

    def stage(self, name: str) -> None:
        self.db.update_job(self.job_id, stage=name)
        self._publish()

    def set_result(self, result: dict) -> None:
        self.db.update_job(self.job_id, result=result)

    def set_render(self, render_id: str, state: str) -> None:
        self.db.update_job(self.job_id, render_id=render_id, render_state=state)

    def set_song(self, song_id: str) -> None:
        self.db.update_job(self.job_id, suno_song_id=song_id)

    def finish(self, output_path: str = "", result: dict | None = None) -> None:
        field_ = {"status": "success", "output_path": output_path, "error_code": "", "error_msg": ""}
        if result is not None:
            field_["result"] = result
        self.db.update_job(self.job_id, **field_)
        self._finished = True
        self._publish()


def _handler_for(job: dict):
    # 延迟 import：模块施工期 stub 也能启动
    from .suno import downloader, pipeline, stems
    from .ai import dna, generate
    from .convert import converter
    from .suno import exporter

    if job["kind"] == "upload" and job["options"].get("resume_of"):
        return exporter.resume_run
    return {
        "download": downloader.run if job["options"].get("mode", "song") == "song" else stems.run,
        "upload": pipeline.run,
        "dna": dna.run,
        "generate": generate.run,
        "convert": converter.run,
    }[job["kind"]]


class JobQueue:
    def __init__(self, db: Database, bus: EventBus, chrome, pf, login=None):
        self.db, self.bus, self.chrome, self.pf = db, bus, chrome, pf
        self.login = login  # LoginFlow：任务遇 NOT_LOGGED_IN 时触发会话复检（SPEC §4.1）
        self._wake = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task | None = None
        self._cancels: dict[int, asyncio.Event] = {}
        self.output_dirs: dict[int, Path] = {}
        # 重启会丢内存登记簿：从 DB 重建，否则重启前完成的任务点「打开文件夹」全误报 OUTPUT_UNAVAILABLE
        for row in self.db.q("SELECT id, output_path FROM jobs WHERE status='success' AND output_path != ''"):
            self.output_dirs[row["id"]] = Path(row["output_path"]).parent.resolve()

    # ---- 提交 ----
    def submit(self, kind: str, input_: str, options: dict) -> dict:
        # SPEC §1：忙判断+入队同一事务语义（锁内）
        with self.db.lock:
            if kind != "download" and self.db.has_active():
                raise AppError("BUSY")
            job = self.db.insert_job(kind, input_, options)
        self.bus.publish("job", self.db.snapshot(job["id"]))
        self._wake.set()
        return job

    # ---- 取消 ----
    def cancel(self, job_id: int) -> dict:
        job = self.db.get_job(job_id)
        if not job:
            raise AppError("NOT_FOUND")
        if job["status"] == "queued":
            self.db.update_job(job_id, status="cancelled", error_msg="已取消（未发出任何请求）")
            self.bus.publish("job", self.db.snapshot(job_id))
        elif job["status"] == "running":
            ev = self._cancels.get(job_id)
            if ev:
                ev.set()  # worker 等 handler 退出后落 cancelled
        # 终态重复取消 = 成功，保持原状态（SPEC §1）
        return self.db.get_job(job_id)

    # ---- worker ----
    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        for row in self.db.q("SELECT id FROM jobs WHERE status='queued'"):
            self.db.update_job(row["id"], status="cancelled", error_msg="应用退出后，等待任务将取消")
        for ev in self._cancels.values():
            ev.set()
        self._wake.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _loop(self) -> None:
        while not self._stopping:
            job = self.db.next_queued()
            if not job:
                self._wake.clear()
                await self._wake.wait()
                continue
            await self._run_one(job)

    async def _run_one(self, job: dict) -> None:
        jid = job["id"]
        cancel = asyncio.Event()
        self._cancels[jid] = cancel
        workdir = config.TMP_DIR / f"job_{jid}"
        workdir.mkdir(parents=True, exist_ok=True)
        self.db.update_job(jid, status="running", stage="start", error_code="", error_msg="")
        self.bus.publish("job", self.db.snapshot(jid))
        ctx = JobContext(jid, job, self.db, cancel, workdir, self.db.settings(), self.chrome, self.pf, self.bus)
        try:
            await _handler_for(job)(job, ctx)
            if not ctx._finished:  # 兜底：handler 未显式 finish 视为成功
                ctx.finish()
        except AppError as e:
            if e.code == "CANCELLED" or cancel.is_set():
                self.db.update_job(jid, status="cancelled", error_code=e.code, error_msg=e.message)
            else:
                if e.code == "NOT_LOGGED_IN" and self.login:
                    self.login.mark_expired()  # 其他 API 401 → 复检确认后才降级（SPEC §4.1）
                self.db.update_job(jid, status="failed", error_code=e.code, error_msg=e.message)
        except asyncio.CancelledError:
            self.db.update_job(jid, status="cancelled", error_msg="应用退出，任务中止")
            raise
        except Exception as e:
            self.db.append_log(jid, DEFAULT_MESSAGES["INTERNAL_ERROR"])
            if self.db.settings().get("debug_logs") == "1":
                frames = traceback.extract_tb(e.__traceback__)
                locations = " → ".join(f"{Path(f.filename).name}:{f.lineno}" for f in frames[-6:])
                # 红线：异常原文可能带 token，绝不入日志；只记已知安全的浏览器级原因短语。
                cause = next((p for p in ("Execution context was destroyed", "has been closed",
                                          "Page crashed") if p in str(e)), "")
                self.db.append_log(jid, f"{type(e).__name__}：{cause + '｜' if cause else ''}{locations}")
            self.db.update_job(jid, status="failed", error_code="INTERNAL_ERROR",
                               error_msg=DEFAULT_MESSAGES["INTERNAL_ERROR"])
        finally:
            self._cancels.pop(jid, None)
            final = self.db.get_job(jid)
            if final and final["status"] == "success":
                if final["output_path"]:
                    self.output_dirs[jid] = Path(final["output_path"]).parent.resolve()
                shutil.rmtree(workdir, ignore_errors=True)  # 失败保留现场供排查
            self.bus.publish("job", self.db.snapshot(jid))
