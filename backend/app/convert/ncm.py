"""NCM 解密（issue 16）。施工：convert 代理。
逐行移植 extracted/electron/ncm-decoder.cjs（只读参考）：
AES-128-ECB(CORE_KEY) 解出 keybox 密钥 → RC4 风格置换 mask 还原音频 → 容器校验。"""
from __future__ import annotations

import struct

from Crypto.Cipher import AES

from ..errors import AppError
from .tables import xor_mask

MAGIC = b"CTENFDAM"
CORE_KEY = bytes.fromhex("687a4852416d736f356b496e62617857")
KEY_PREFIX = b"neteasecloudmusic"
MAX_INPUT_BYTES = 100 * 1024 * 1024


def _bad(msg: str) -> AppError:
    return AppError("DECRYPT_FAILED", msg)


def is_mp3_frame(data: bytes, offset: int) -> bool:
    """校验 offset 处是否为完整 MPEG 音频帧（含帧长边界）。"""
    if offset + 4 > len(data):
        return False
    b1, b2 = data[offset + 1], data[offset + 2]
    if data[offset] != 0xFF or (b1 & 0xE0) != 0xE0:
        return False
    version = (b1 >> 3) & 3
    if version == 1 or ((b1 >> 1) & 3) != 1:
        return False
    rate_index = b2 >> 4
    if rate_index in (0, 15):
        return False
    sample_index = (b2 >> 2) & 3
    if sample_index == 3:
        return False
    rates = ([0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
             if version == 3 else
             [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160])
    freq = [44100, 48000, 32000][sample_index] / (1 if version == 3 else 2 if version == 2 else 4)
    frame_len = int((144000 if version == 3 else 72000) * rates[rate_index] / freq) + ((b2 >> 1) & 1)
    return offset + frame_len <= len(data)


def audio_format(data: bytes) -> str | None:
    """识别 flac / mp3（含 ID3 前置标签）；其余返回 None。"""
    if len(data) >= 42 and data[:4] == b"fLaC" and (data[4] & 0x7F) == 0 \
            and int.from_bytes(data[5:8], "big") == 34:
        return "flac"
    if is_mp3_frame(data, 0):
        return "mp3"
    if len(data) >= 10 and data[:3] == b"ID3":
        ver = data[3]
        if 2 <= ver <= 4 and data[4] != 0xFF and all(b <= 0x7F for b in data[6:10]):
            tag_len = data[6] * 0x200000 + data[7] * 0x4000 + data[8] * 0x80 + data[9]
            footer = 10 if ver == 4 and data[5] & 0x10 else 0
            if is_mp3_frame(data, 10 + tag_len + footer):
                return "mp3"
    return None


def _unpad(data: bytes) -> bytes:
    """PKCS7 去填充（参考用 Node 自动去填充，此处手工校验）。"""
    if not data:
        raise _bad("NCM 密钥损坏，或此加密版本暂不支持。")
    pad = data[-1]
    if not 1 <= pad <= 16 or data[-pad:] != bytes([pad]) * pad:
        raise _bad("NCM 密钥损坏，或此加密版本暂不支持。")
    return data[:-pad]


def decrypt(data: bytes) -> tuple[bytes, str]:
    if len(data) > MAX_INPUT_BYTES:
        raise _bad("NCM 文件过大。")
    pos = 0

    def take(n: int) -> bytes:
        nonlocal pos
        if n < 0 or pos + n > len(data):
            raise _bad("NCM 文件不完整，或包含无效的数据长度。")
        chunk = data[pos:pos + n]
        pos += n
        return chunk

    def read_len() -> int:
        return struct.unpack("<I", take(4))[0]

    if take(8) != MAGIC:
        raise AppError("UNSUPPORTED_FORMAT", "文件不是受支持的 NCM 格式。")
    take(2)
    key_len = read_len()
    if key_len == 0 or key_len > 65536 or key_len % 16:
        raise _bad("NCM 密钥区长度无效。")
    key_cipher = bytes(b ^ 0x64 for b in take(key_len))
    key_data = _unpad(AES.new(CORE_KEY, AES.MODE_ECB).decrypt(key_cipher))
    if len(key_data) <= 17 or not key_data.startswith(KEY_PREFIX):
        raise AppError("UNSUPPORTED_FORMAT", "NCM 密钥标识无效，或此加密版本暂不支持。")
    key = key_data[17:]
    take(read_len())          # meta 区
    take(5)
    cover_frame_len = read_len()
    cover_len = read_len()
    if cover_len > cover_frame_len:
        raise _bad("NCM 封面区长度无效。")
    take(cover_frame_len)
    if pos == len(data):
        raise _bad("NCM 文件没有音频数据。")
    # keybox：RC4 风格 KSA 后按 (perm[n]+perm[(perm[n]+n)&255]) 取 mask
    perm = list(range(256))
    j = 0
    for i in range(256):
        j = (j + perm[i] + key[i % len(key)]) & 255
        perm[i], perm[j] = perm[j], perm[i]
    mask = bytearray(256)
    for i in range(256):
        n = (i + 1) & 255
        mask[i] = perm[(perm[n] + perm[(perm[n] + n) & 255]) & 255]
    payload = data[pos:]
    full_mask = (bytes(mask) * (len(payload) // 256 + 1))
    audio = xor_mask(payload, full_mask)
    fmt = audio_format(audio)
    if not fmt:
        raise _bad("NCM 音频数据损坏，或其中的音频格式暂不支持。")
    return audio, fmt