"""QMC 解密（issue 17）。施工：convert 代理。
静态 mask / TEA 密钥派生逐行移植 extracted/electron/encrypted-audio-codec.cjs（只读参考，MIT KGG-Decryptor 血统）；
QTag/STag/尾部内嵌密钥布局参考 libtakiyasha（https://github.com/nukemiko/libtakiyasha）。
注意：原客户端渲染层拒绝独立 QMC 文件，本模块按 SPEC §4.9 / issue 17 要求实现（偏差见报告）。"""
from __future__ import annotations

import base64
import math
import re
import struct

from ..errors import AppError
from . import ncm
from .tables import QMC, xor_mask

ENCV2_PREFIX = b"QQMusic EncV2,Key:"
GARBLE_1 = b"386ZJY!@#*$%^&)("
GARBLE_2 = b"**#!(#$%&^a1cZ,T"
_B64_RE = re.compile(rb"^[A-Za-z0-9+/]+={0,2}$")
_MASK = 0xFFFFFFFF


def _bad(msg: str) -> AppError:
    return AppError("DECRYPT_FAILED", msg)


def sniff_audio(data: bytes) -> str | None:
    """flac/mp3/ogg/wav/m4a 容器嗅探（对应参考 sniffAudio）。"""
    fmt = ncm.audio_format(data)
    if fmt:
        return fmt
    if len(data) >= 27 and data[:4] == b"OggS" and data[4] == 0:
        return "ogg"
    if len(data) >= 44 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if len(data) >= 16 and data[4:8] == b"ftyp":
        return "m4a"
    return None


def _b64(value) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8", "ignore")
    text = value.rstrip(b"\x00").strip()
    if not text or len(text) > 65536 or not _B64_RE.match(text) or len(text) % 4 == 1:
        raise _bad("歌曲密钥格式无效。")
    return base64.b64decode(text)


def _tea_block(block: bytes, words: list[int]) -> bytes:
    """QQ TEA 16 轮解密单块（参考 teaBlock，全 >>> 无符号移位）。"""
    left, right = struct.unpack(">II", block)
    total = (0x9E3779B9 * 16) & _MASK
    w0, w1, w2, w3 = words
    for _ in range(16):
        right = (right - (((left << 4) + w2) ^ (left + total) ^ ((left >> 5) + w3))) & _MASK
        left = (left - (((right << 4) + w0) ^ (right + total) ^ ((right >> 5) + w1))) & _MASK
        total = (total - 0x9E3779B9) & _MASK
    return struct.pack(">II", left, right)


def tencent_tea(data: bytes, key: bytes) -> bytes:
    """TEA-CBC 变体（密文反馈+明文反馈双重 XOR）+ 随机填充/零尾校验。"""
    if len(data) < 16 or len(data) % 8 or len(key) != 16:
        raise _bad("歌曲密钥解码失败。")
    words = list(struct.unpack(">4I", key))
    plain = bytearray(len(data))
    prev_cipher = bytes(8)
    prev_decoded = bytes(8)
    for offset in range(0, len(data), 8):
        cipher = data[offset:offset + 8]
        block = bytes(a ^ b for a, b in zip(cipher, prev_decoded))
        decoded = _tea_block(block, words)
        plain[offset:offset + 8] = bytes(a ^ b for a, b in zip(decoded, prev_cipher))
        prev_cipher, prev_decoded = cipher, decoded
    start = 1 + (plain[0] & 7) + 2
    end = len(plain) - 7
    if start > end or any(plain[end:]):
        raise _bad("歌曲密钥校验失败。")
    return bytes(plain[start:end])


def derive_key(ekey, allow_raw: bool = False) -> bytes:
    """ekey(base64) → 实际解密 key；EncV2 双层 TEA；tan 盐 TEA 解 raw[8:]。"""
    raw = _b64(ekey)
    if raw.startswith(ENCV2_PREFIX):
        inner = tencent_tea(raw[len(ENCV2_PREFIX):], GARBLE_1)
        # Node toString('ascii') 逐字节 &0x7f
        raw = _b64(bytes(b & 0x7F for b in tencent_tea(inner, GARBLE_2)))
    if len(raw) < 16 or len(raw) > 16384:
        raise _bad("歌曲密钥长度无效。")
    tea_key = bytearray(16)
    for i in range(8):
        tea_key[i * 2] = math.floor(abs(math.tan(106 + i * 0.1)) * 100) & 255
        tea_key[i * 2 + 1] = raw[i]
    try:
        return raw[:8] + tencent_tea(raw[8:], bytes(tea_key))
    except AppError:
        if allow_raw:
            return raw
        raise


def _ramp_cycle(small: bytes, n: int) -> bytes:
    """mask[i]=small[at_i % len]，at_i=i(i≤0x7fff) 否则 i%0x7fff；
    前 32768B 直线段 + 周期 32767 循环段，整段拼接供一次 XOR。"""
    size = len(small)
    ramp = (small * (32768 // size + 1))[:min(n, 32768)]
    if n <= 32768:
        return ramp
    cycle = bytes(small[(j % 32767) % size] for j in range(1, 32768))
    rest = n - 32768
    return ramp + (cycle * (rest // 32767 + 1))[:rest]


def _static_mask(n: int) -> bytes:
    small = bytes(QMC[(j * j + 27) & 255] for j in range(256))
    return _ramp_cycle(small, n)


def _map_mask(n: int, key: bytes) -> bytes:
    size = len(key)
    small = bytearray(size)
    for j in range(size):
        index = (j * j + 71214) % size
        rotate = (index + 4) & 7
        v = key[index]
        small[j] = ((v << rotate) | (v >> rotate)) & 255  # 参考原式（OR，非标准循环移位）
    return _ramp_cycle(bytes(small), n)


def _rc4(data: bytes, key: bytes) -> bytes:
    """RC4 变体：hash 种子 + KSA 初始盒 + 分段 skip 流（逐字节，参考同序）。"""
    size = len(key)
    out = bytearray(data)
    hash_ = 1
    for i in range(size):
        if key[i] and hash_ != 0:
            nxt = (hash_ * key[i]) & _MASK
            if nxt and nxt > hash_:
                hash_ = nxt
            else:
                break
    initial = bytearray(i & 255 for i in range(size))
    swap = 0
    for i in range(size):
        swap = (swap + initial[i] + key[i]) % size
        initial[i], initial[swap] = initial[swap], initial[i]

    def segment_value(seg_id: int, seed: int) -> int:
        return math.floor(hash_ / ((seg_id + 1) * seed) * 100) if seed else 0

    n = len(out)
    for i in range(min(128, n)):
        out[i] ^= key[segment_value(i, key[i % size]) % size]
    offset = 128
    while offset < n:
        count = min(5120 - offset % 5120, n - offset)
        seg_id = offset // 5120
        skip = offset % 5120 + (segment_value(seg_id, key[seg_id & 511]) & 511)
        box = initial[:]
        j = k = 0
        for i in range(-skip, count):
            j = (j + 1) % size
            k = (k + box[j]) % size
            box[j], box[k] = box[k], box[j]
            if i >= 0:
                out[offset + i] ^= box[(box[j] + box[k]) % size]
        offset += count
    return bytes(out)


def qmc_transform(data: bytes, key: bytes | None) -> bytes:
    """无 key=静态 mask；≤300=map；≥512=RC4；(300,512) 未支持。"""
    if not data:
        return data
    if key is None:
        return xor_mask(data, _static_mask(len(data)))
    size = len(key)
    if size <= 300:
        return xor_mask(data, _map_mask(len(data), key))
    if size < 512:
        raise AppError("UNSUPPORTED_FORMAT", "此分段音频密钥长度尚未支持。")
    return _rc4(data, key)


def _split(data: bytes) -> tuple[bytes, bytes | None]:
    """切出音频区与密钥：QTag 内嵌 ekey / STag 不支持 / 尾部 TEA-b64 或裸 key / 无尾=静态。"""
    n = len(data)
    if n >= 8:
        tail = data[-4:]
        if tail in (b"QTag", b"STag"):
            tag_len = struct.unpack(">I", data[-8:-4])[0]
            if tag_len == 0 or tag_len > n - 8:
                raise _bad("QMC 尾部标签长度无效。")
            if tail == b"STag":
                raise AppError("UNSUPPORTED_FORMAT", "此 QMC 文件（STag）不含内嵌密钥，暂不支持离线解密。")
            tag = data[n - 8 - tag_len:n - 8]
            parts = tag.split(b",")
            if len(parts) != 3 or not parts[0]:
                raise _bad("QMC 尾部标签格式无效。")
            return data[:n - 8 - tag_len], derive_key(parts[0])
        size = struct.unpack("<I", data[-4:])[0]
        if 0 < size < 0x300 and size <= n - 4:
            key_data = data[n - 4 - size:n - 4]
            audio = data[:n - 4 - size]
            try:
                return audio, derive_key(key_data, allow_raw=True)
            except AppError:
                return audio, key_data  # 二进制裸 key 变体
    return data, None


def decrypt(data: bytes, filename: str) -> tuple[bytes, str]:
    if not data:
        raise _bad("音频数据为空或损坏。")
    fmt = sniff_audio(data)
    if fmt:  # 客户端偶尔给普通音频保留加密扩展名
        return data, fmt
    audio, key = _split(data)
    out = qmc_transform(audio, key)
    fmt = sniff_audio(out)
    if not fmt:
        raise _bad("解密后无法识别音频，文件可能损坏、密钥不匹配或加密版本尚未支持。")
    return out, fmt