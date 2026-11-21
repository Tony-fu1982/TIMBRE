"""上传还原总编排（SPEC §4.4–4.6）：transform → upload → restore → export。
各阶段实现见 transformer/uploader/restorer/exporter（模块施工分工见 CONTRACTS.md）。"""
from __future__ import annotations

from pathlib import Path

from ..errors import AppError
from ..jobs import JobContext
from . import exporter, restorer, transformer, uploader


async def run(job: dict, ctx: JobContext) -> None:
    src = Path(job["input"])
    if not src.is_file():
        raise AppError("BAD_AUDIO", "上传的源文件不存在。")

    ctx.stage("transform")
    ctx.log("开始 4× 降速变形（-24 半音）并切 6 片…")
    meta = await transformer.transform(src, ctx)
    ctx.check_cancel()

    ctx.stage("upload")
    ctx.log(f"变形完成：源采样率 {meta['sr']}Hz，时长 {meta['source_duration']:.1f}s → 6 片上传")
    clip_ids = await uploader.upload_slices(meta["slices"], ctx)
    ctx.check_cancel()

    ctx.stage("restore")
    ctx.log("进入 Studio：双轨重建 + 升调还原 + EQ…")
    project = await restorer.restore(clip_ids, meta, ctx)
    ctx.check_cancel()

    ctx.stage("export")
    ctx.log("提交渲染导出…")
    out = await exporter.export(project, ctx)
    ctx.set_song(out["song_id"])
    ctx.finish(result={"song_id": out["song_id"], "url": out["url"],
                       "project_id": project.get("project_id", ""),
                       "transformed": meta.get("transformed", True)})
    ctx.log(f"导出成功：https://suno.com/song/{out['song_id']}")