"""4× 降速变形 + 6 等分切片（issue 05，SPEC §4.4 / 文档08 §4.1）。施工：upload 代理。
ffmpeg asetrate=sr/4,aresample=sr（sr=ffprobe 实测源采样率，禁用固定 44100）→ 时长 4× / −24 半音；
segment muxer 等分 6 片 MP3；返回 {"sr","source_duration","transformed","slices":[{path,duration}×6]}。"""
from __future__ import annotations

from pathlib import Path

from ..errors import AppError
from ..ffmedia import probe, run_ff
from ..jobs import JobContext

SLICE_COUNT = 6
SLOW_FACTOR = 4
MAX_INPUT_BYTES = 50 * 1024 * 1024  # issue 05 验收：≤50MB（上传页同限，后端兜底）
# ponytail: ±2% 时长校验容差为自定值（SPEC 只说"约为四倍"）；MP3 编码 padding 在短音频上占比更大，
# 上限放宽到 5% 也安全。升级路径：真实曲目批量验证后收紧。
DURATION_TOL = 0.02
# ponytail: 源音频最短 1s 为自定 fail-closed 下限（原作未暴露阈值）；升级路径：按 Suno 实际受理下限校准（HITL）。
MIN_SOURCE_SEC = 1.0


async def transform(src: Path, ctx: JobContext) -> dict:
    ctx.check_cancel()
    src = Path(src)
    if src.stat().st_size > MAX_INPUT_BYTES:
        raise AppError("BAD_AUDIO", "音频文件超过 50MB 上限，请压缩或剪短后重试。")
    info = await probe(src)  # 坏音频 → BAD_AUDIO
    sr, source_duration = info["sr"], info["duration"]
    if source_duration < MIN_SOURCE_SEC:
        raise AppError("BAD_AUDIO", f"音频过短（不足 {MIN_SOURCE_SEC:g} 秒），请换一段更长的音频。")

    work = ctx.workdir
    slow = work / "slow.mp3"
    # SPEC §4.4：sr/4（文档08 "等效于 asetrate=4*sr" 的注释方向有误，以 SPEC 为准）
    await run_ff(
        ["-y", "-i", str(src), "-af", f"asetrate={sr}/{SLOW_FACTOR},aresample={sr}",
         "-b:a", "128k", str(slow)],
        timeout=600,  # 50MB 长音频 4× 后编码耗时放宽；segment 拷贝很快用默认 300s
    )
    ctx.check_cancel()
    total = (await probe(slow))["duration"]
    expected = source_duration * SLOW_FACTOR
    if abs(total - expected) > expected * DURATION_TOL:
        raise AppError("BAD_AUDIO", f"变形时长校验失败：期望≈{expected:.1f}s，实际 {total:.1f}s。")

    times = ",".join(f"{total / SLICE_COUNT * i:.6f}" for i in range(1, SLICE_COUNT))
    await run_ff(["-y", "-i", str(slow), "-f", "segment", "-segment_times", times,
                  "-c", "copy", str(work / "part_%d.mp3")])
    slices = []
    for i in range(SLICE_COUNT):
        p = work / f"part_{i}.mp3"
        if not p.is_file():
            raise AppError("BAD_AUDIO", f"切片失败：缺少第 {i + 1}/6 片。")
        slices.append({"path": str(p), "duration": (await probe(p))["duration"]})
    ctx.check_cancel()
    ctx.log(f"变形完成：{source_duration:.1f}s → {total:.1f}s（{sr}Hz），已切 6 片")
    return {"sr": sr, "source_duration": source_duration, "transformed": True, "slices": slices}
