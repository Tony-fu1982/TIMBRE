"""Qwen 参考音频预处理（SPEC §4.8 / 02 §8.1）：MP3 码率阶梯压缩，实测 base64 ≤10MB。

原文件不动；中间产物写 ctx.workdir（成功后自动清理）。全档超限 → AUDIO_TOO_LARGE（提示缩短音频）。
"""
from __future__ import annotations

import base64
from pathlib import Path

from .. import ffmedia
from ..errors import AppError

MAX_B64_BYTES = 10_000_000  # mN=1e7（02 §8.1）


def bitrate_ladder(duration: float) -> list[int]:
    # ponytail: 阶梯为项目自定常量（原作 R2 未逐字取证；02 §8.1 只给时长候选思路，MP3-only）
    if duration <= 120:
        return [192, 128, 96, 64, 48, 32]
    if duration <= 300:
        return [128, 96, 64, 48, 32]
    if duration <= 600:
        return [96, 64, 48, 32]
    return [64, 48, 32]


async def prepare_qwen_audio(src: Path, ctx) -> tuple[str, str]:
    info = await ffmedia.probe(src)
    for k in bitrate_ladder(info["duration"]):
        ctx.check_cancel()
        dst = Path(ctx.workdir) / f"qwen_{k}k.mp3"
        await ffmedia.run_ff(
            ["-y", "-i", str(src), "-b:a", f"{k}k", "-ac", "2", "-codec:a", "libmp3lame", str(dst)]
        )
        b64 = base64.b64encode(dst.read_bytes()).decode()
        ctx.debug(f"音频压缩 {k}kbps：base64 {len(b64)} 字节")
        if len(b64) <= MAX_B64_BYTES:
            ctx.log(f"参考音频已压缩至 {k}kbps（base64 {len(b64)} 字节）")
            return b64, "mp3"
    raise AppError("AUDIO_TOO_LARGE")
