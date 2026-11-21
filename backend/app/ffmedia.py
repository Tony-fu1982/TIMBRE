"""ffmpeg/ffprobe 助手（SPEC：本机 ffmpeg 8.x）。"""
from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from contextlib import suppress
from pathlib import Path

from .errors import AppError

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


async def _communicate(proc, timeout: float, name: str):
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        with suppress(ProcessLookupError):
            proc.kill()
        await proc.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise AppError("TIMEOUT", f"{name} 处理超时") from exc


async def run_ff(args: list[str], timeout: float = 300) -> str:
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, err = await _communicate(proc, timeout, "ffmpeg")
    if proc.returncode != 0:
        raise AppError("BAD_AUDIO", f"ffmpeg 处理失败：{err.decode(errors='replace')[-300:]}")
    return err.decode(errors="replace")


async def probe(path: Path | str) -> dict:
    """返回 {sr, duration, codec}；失败抛 BAD_AUDIO。"""
    proc = await asyncio.create_subprocess_exec(
        FFPROBE, "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate,codec_name", "-show_entries", "format=duration",
        "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await _communicate(proc, 30, "ffprobe")
    try:
        j = json.loads(out)
        st = j["streams"][0]
        return {"sr": int(st["sample_rate"]), "duration": float(j["format"]["duration"]), "codec": st.get("codec_name", "")}
    except Exception:
        raise AppError("BAD_AUDIO")


async def transcode(src: Path | str, dst: Path | str, fmt: str, sr: int | None = None) -> None:
    args = ["-y", "-i", str(src)]
    if sr:
        args += ["-ar", str(sr)]
    if fmt == "mp3":
        args += ["-codec:a", "libmp3lame", "-q:a", "2"]
    elif fmt == "wav":
        args += ["-codec:a", "pcm_s16le"]
    else:
        raise AppError("UNSUPPORTED_FORMAT", f"输出格式仅支持 mp3/wav，收到 {fmt}")
    args.append(str(dst))
    await run_ff(args)


def unique_name(stem: str, job_id: int, ext: str) -> str:
    safe = "".join(c for c in stem if c not in '\\/:*?"<>|').strip() or "audio"
    return f"{safe}_{job_id}_{uuid.uuid4().hex[:8]}.{ext}"


def save_output(data: bytes, directory: str | Path, stem: str, job_id: int, ext: str) -> Path:
    """唯一名 + 0600 落盘（SPEC §4.7）。"""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    p = d / unique_name(stem, job_id, ext)
    p.write_bytes(data)
    try:
        import os
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p
