"""KGM/KGMA/VPR 解密（issue 18）。施工：convert 代理。
逐行移植 extracted/electron/encrypted-audio-codec.cjs decodeKugou（只读参考）：
版本 1/2/4 嵌套 16 进制位掩码；版本 3 槽位 1 用 kugouMd5 槽变换（嗅探失败回退旧掩码）；
版本 5 查用户本地 kgmusicv3.db 的 ekey → deriveKey(allowRaw) → qmcTransform。
其余版本 → UNSUPPORTED_FORMAT。仅 v5 分支需要密钥库（SPEC §4.9）。"""
from __future__ import annotations

import hashlib
import re
import struct

from ..errors import AppError
from . import qmc
from .tables import KGM1, KGM2, KGM_MEND, VPR

KGM_MAGIC = bytes.fromhex("7cd532eb86027f4ba8afa68e0fff9914")
VPR_MAGIC = bytes.fromhex("0528bc96e9e45a4391aabdd07af53631")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _kugou_md5(data: bytes) -> bytes:
    """md5 摘要按对倒序重排（参考 kugouMd5）。"""
    d = hashlib.md5(data).digest()
    out = bytearray(16)
    for i in range(0, 16, 2):
        out[i] = d[14 - i]
        out[i + 1] = d[15 - i]
    return bytes(out)


def _legacy_mask(audio: bytearray, data: bytes) -> None:
    """旧版嵌套掩码：own=data[28:44]+0；位掩码按 16 进制位递归查 KGM1/KGM2。
    递归用 DP 展开：dm[p]=KGM1[p%272]^KGM2[(p>>4)%272]^dm[p>>8]（p≥17）。"""
    n = len(audio)
    own = data[28:44] + b"\x00"
    vpr = data[:16] == VPR_MAGIC
    top = n >> 4
    dm = bytearray(top + 1)
    for p in range(17, top + 1):
        dm[p] = KGM1[p % 272] ^ KGM2[(p >> 4) % 272] ^ dm[p >> 8]
    for i in range(n):
        mask = dm[i >> 4] ^ KGM_MEND[i % 272]
        value = audio[i] ^ own[i % 17]
        extra = VPR[i % 17] if vpr else 0
        audio[i] = value ^ ((value & 15) << 4) ^ mask ^ ((mask & 15) << 4) ^ extra


def decrypt(data: bytes, filename: str, keydb) -> tuple[bytes, str]:
    fmt = qmc.sniff_audio(data)
    if fmt:  # 客户端偶尔给普通音频保留加密扩展名
        return data, fmt
    if len(data) < 68 or data[:16] not in (KGM_MAGIC, VPR_MAGIC):
        raise AppError("UNSUPPORTED_FORMAT", "不是有效的酷狗加密音频文件。")
    offset, version = struct.unpack_from("<II", data, 16)
    if offset < 68 or offset >= len(data):
        raise AppError("DECRYPT_FAILED", "酷狗文件的音频偏移无效。")
    audio = bytearray(data[offset:])
    if version == 5:
        if offset < 76:
            raise AppError("DECRYPT_FAILED", "酷狗文件密钥标识不完整。")
        length = struct.unpack_from("<I", data, 68)[0]
        if not length or length > 512 or 72 + length > offset:
            raise AppError("DECRYPT_FAILED", "酷狗文件密钥标识无效。")
        try:
            key_id = data[72:72 + length].decode("utf-8").rstrip("\x00")
        except UnicodeDecodeError:
            raise AppError("DECRYPT_FAILED", "酷狗文件密钥标识无效。")
        if not _KEY_ID_RE.match(key_id):
            raise AppError("DECRYPT_FAILED", "酷狗文件密钥标识无效。")
        if keydb is None:
            raise AppError("KEY_DB_UNAVAILABLE")  # 默认文案：先酷狗下载并完全关闭后重试
        ekey = keydb.lookup(key_id)
        if ekey is None:
            raise AppError("KEY_DB_UNAVAILABLE",
                           "酷狗本地密钥库里没有这首歌曲的密钥，请使用本机酷狗客户端重新下载该歌曲后重试。")
        key = qmc.derive_key(ekey, allow_raw=True)
        audio = bytearray(qmc.qmc_transform(bytes(audio), key))
    else:
        if version == 3:
            if struct.unpack_from("<I", data, 24)[0] != 1:
                raise AppError("UNSUPPORTED_FORMAT", "暂不支持此酷狗加密槽位。")
            slot = _kugou_md5(b"\x6c\x2c\x2f\x27")
            file_key = _kugou_md5(data[44:60]) + b"\x6b"
            for i in range(len(audio)):
                value = audio[i] ^ file_key[i % 17]
                value ^= (value << 4) & 255
                audio[i] = (value ^ slot[i % 16] ^ (i & 255) ^ ((i >> 8) & 255)
                            ^ ((i >> 16) & 255) ^ ((i >> 24) & 255))
            fmt = qmc.sniff_audio(bytes(audio))
            if fmt:
                return bytes(audio), fmt
            audio = bytearray(data[offset:])  # 部分客户端版本 3 仍用旧掩码
        if version not in (1, 2, 3, 4):
            raise AppError("UNSUPPORTED_FORMAT", f"暂不支持酷狗加密版本 {version}。")
        _legacy_mask(audio, data)
    fmt = qmc.sniff_audio(bytes(audio))
    if not fmt:
        raise AppError("DECRYPT_FAILED", "还原后无法识别音频，文件可能损坏、密钥不匹配或加密版本尚未支持。")
    return bytes(audio), fmt