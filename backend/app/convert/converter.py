"""格式转换入口（issue 15–19）。施工：convert 代理。
输入 MP3/WAV/FLAC/M4A/OGG/NCM/QMC/KGM/KGMA/KGG/VPR/KWM；输出仅 MP3/WAV；头部验证；唯一名 0600。
SPEC §4.9：只有真正需要密钥的分支（酷狗 v5）才查密钥库，其余版本绝不触碰密钥库。"""
from __future__ import annotations

import struct
from pathlib import Path

from .. import config, ffmedia
from ..errors import AppError
from ..jobs import JobContext
from ..suno.common import sniff_container
from . import keydb, kgm, kgg, kwm, ncm, qmc

MAX_OUTPUT_BYTES = 250 * 1024 * 1024
_ORDINARY_EXTS = {"mp3", "wav", "flac", "m4a", "ogg"}


def _sniff_head(head: bytes) -> str | None:
    if head.startswith(b"fLaC"):
        return "flac"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    kind = sniff_container(head)
    if kind == "mp4":
        return "m4a"
    return kind if kind in ("mp3", "ogg") else None


def detect_format(path) -> str:
    p = Path(path)
    ext = p.suffix.lower().lstrip(".")
    with p.open("rb") as f:
        head = f.read(4096)
        try:
            f.seek(-8, 2)
            tail = f.read(8)
        except OSError:
            tail = b""
    if head.startswith(ncm.MAGIC):
        return "ncm"
    if head[:16] == kgm.KGM_MAGIC:
        return {"kgg": "kgg", "kgma": "kgma"}.get(ext, "kgm")
    if head[:16] == kgm.VPR_MAGIC:
        return "vpr"
    fmt = _sniff_head(head)
    if fmt:  # 普通音频即使带加密扩展名也直接放行
        return fmt
    if tail[-4:] in (b"QTag", b"STag") or ext.startswith(("qmc", "mflac", "mgg", "mmp4")) or ext == "tkm":
        return "qmc"
    if ext in ("ncm", "kgm", "kgma", "kgg", "vpr", "kwm"):
        return ext
    if ext in _ORDINARY_EXTS:
        return ext
    raise AppError("UNSUPPORTED_FORMAT")


def _decrypt_kugou(fmt: str, data: bytes, name: str, src: Path, ctx: JobContext) -> tuple[bytes, str]:
    def call(kdb):
        return kgg.decrypt(data, kdb) if fmt == "kgg" else kgm.decrypt(data, name, kdb)

    try:
        return call(None)  # v1–4 不需要密钥库；v5 会抛 KEY_DB_UNAVAILABLE
    except AppError as e:
        if e.code != "KEY_DB_UNAVAILABLE":
            raise
    ctx.log("该文件需要酷狗本地密钥库，正在查找 kgmusicv3.db…")
    candidates = keydb.find_key_dbs(str(src))
    if not candidates:
        raise AppError("KEY_DB_UNAVAILABLE",
                       "未找到酷狗本地密钥库，请先在本机酷狗客户端下载并播放该歌曲后重试。")
    last: AppError | None = None
    for path in candidates:
        ctx.check_cancel()
        try:
            with keydb.KeyDB(path) as kdb:
                return call(kdb)
        except AppError as e:
            if e.code != "KEY_DB_UNAVAILABLE":
                raise
            last = e
    raise last or AppError("KEY_DB_UNAVAILABLE")


def _validate_output(data: bytes, target: str) -> None:
    if target == "mp3":
        if ncm.audio_format(data) is None:
            raise AppError("BAD_AUDIO", "转换输出不是有效的 MP3 音频。")
    elif not (len(data) >= 44 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"
              and struct.unpack_from("<I", data, 4)[0] == len(data) - 8):
        raise AppError("BAD_AUDIO", "转换输出不是有效的 WAV 音频。")


async def run(job: dict, ctx: JobContext) -> None:
    ctx.check_cancel()
    src = Path(job["input"])
    if not src.is_file():
        raise AppError("NOT_FOUND", "输入文件不存在，请重新上传。")
    opts = job.get("options") or {}
    target = str(opts.get("target") or "mp3").lower()
    if target not in ("mp3", "wav"):
        raise AppError("UNSUPPORTED_FORMAT", f"输出格式仅支持 mp3/wav，收到 {target}")
    name = str(opts.get("source_name") or src.name)
    ctx.stage("detect")
    fmt = detect_format(src)
    ctx.log(f"识别输入格式：{fmt}")
    data = src.read_bytes()
    ctx.check_cancel()
    if fmt == "ncm":
        audio, ext = ncm.decrypt(data)
    elif fmt == "qmc":
        audio, ext = qmc.decrypt(data, name)
    elif fmt in ("kgm", "kgma", "kgg", "vpr"):
        audio, ext = _decrypt_kugou(fmt, data, name, src, ctx)
    elif fmt == "kwm":
        audio, ext = kwm.decrypt(data, name)
    else:
        audio, ext = data, fmt
    ctx.check_cancel()
    ctx.stage("transcode")
    mid = ctx.workdir / f"decoded.{ext}"
    mid.write_bytes(audio)
    dst = ctx.workdir / f"converted.{target}"
    await ffmedia.transcode(mid, dst, target)
    ctx.check_cancel()
    if dst.stat().st_size > MAX_OUTPUT_BYTES:
        raise AppError("BAD_AUDIO", "转换输出超过 250MB 上限，请改用 MP3 输出或更小的源文件。")
    out = dst.read_bytes()
    _validate_output(out, target)
    out_dir = ctx.settings.get("download_dir") or config.system_download_dir()
    final = ffmedia.save_output(out, out_dir, Path(name).stem or "audio", ctx.job_id, target)
    ctx.log(f"转换完成：{final.name}")
    ctx.finish(str(final))